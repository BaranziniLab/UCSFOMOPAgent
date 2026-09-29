"""Durable, bounded-memory query jobs. Kept identical in the three UCSF connectors."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import uuid
from filelock import FileLock, Timeout

ACTIVE = ('queued', 'running')
TERMINAL = ('completed', 'failed', 'cancelled', 'timed_out', 'interrupted')
INSTRUCTIONS = """
For schema discovery and small previews use the existing synchronous tools.
For exports, joins/scans with uncertain cost, or a query that timed out, use
submit_query_job. It starts an independent local CLI worker and returns immediately.
Credentials stay inside the connector and worker, never request them in chat.
Resolve schema/concepts first; select only needed columns and use indexed filters.
Use mode='explain' to inspect a plan without executing the query (SQL Server needs
SHOWPLAN permission). Do not run an expensive COUNT(*) solely to estimate export size.
Poll query_job_status at its recommended_poll_seconds until a terminal state.
Report phase, elapsed time, rows and bytes. An unknown ETA is not a stuck query;
execution before the first row often has no measurable percentage. Estimates from
prior identical queries or expected_rows are advisory, never guarantees.
Only completed means the result file is complete; partial files are incomplete.
Use cancel_query_job to stop unwanted work. Do not resubmit while a job is active.
Do not repeatedly retry failures: inspect schema/plan and narrow the query first.
Jobs survive chat/MCP disconnects, but not host reboot; interrupted queries require
an explicit new submission. There is no unsafe automatic OFFSET resume or retry.
Process downloaded data locally and return summaries instead of full rows to chat.
"""


def validate_query(query: str, dialect: str) -> str:
    if not query.strip() or len(query.encode()) > 1_000_000:
        raise ValueError('Supply a nonempty query smaller than 1 MB.')
    # Remove literals and identifiers before checking operations, while preserving
    # their boundaries; nested comments are rejected rather than ambiguously parsed.
    if dialect == 'sql':
        pattern = r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\[(?:\]\]|[^\]])*\]|--[^\n]*|/\*.*?\*/"
    else:
        pattern = r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`|//[^\n]*|/\*.*?\*/"
    def strip(m):
        token = m.group()
        if token.startswith('/*') and '/*' in token[2:]:
            raise ValueError('Nested comments are not supported.')
        return ' '
    body = re.sub(pattern, strip, query, flags=re.S)
    if any(c in body for c in ("'", '"', '`')) or '/*' in body or '*/' in body:
        raise ValueError('Unterminated literal or comment.')
    body = body.strip().removesuffix(';').strip()
    if ';' in body:
        raise ValueError('Only one read-only statement is allowed.')
    forbidden = r'\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|DENY|DISABLE|ENABLE|KILL|WAITFOR|SHUTDOWN|CHECKPOINT|BULK|RECONFIGURE|EXEC|EXECUTE|INTO|SET|REMOVE|LOAD|FOREACH|CALL|USE|DBCC|BACKUP|RESTORE|OPENROWSET|OPENQUERY|OPENDATASOURCE|NEXT\s+VALUE)\b'
    if re.search(forbidden, body, re.I):
        raise ValueError('Only read-only SELECT/WITH or MATCH/RETURN queries are supported; procedures and external sources are refused.')
    first = re.match(r'\w+', body)
    allowed = {'SELECT', 'WITH'} if dialect == 'sql' else {'MATCH', 'OPTIONAL', 'WITH', 'RETURN', 'UNWIND'}
    if not first or first.group().upper() not in allowed:
        raise ValueError('Unsupported read-only query statement.')
    if dialect == 'sql':
        import sqlglot
        from sqlglot import exp
        try:
            parse_query = re.sub(r'%\(([A-Za-z_]\w*)\)s', r'@\1', query)
            trees = sqlglot.parse(parse_query, read='tsql', error_level=sqlglot.ErrorLevel.RAISE)
        except sqlglot.errors.SqlglotError:
            raise ValueError('Query must parse as a single read-only T-SQL SELECT.') from None
        if len(trees) != 1 or not isinstance(trees[0], (exp.Select, exp.Union, exp.Except, exp.Intersect)):
            raise ValueError('Exactly one read-only T-SQL SELECT statement is supported.')
    return query.strip().removesuffix(';')


def private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError('Job directories must not be symbolic links.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


class JobStore:
    def __init__(self, package: str, root: str | Path | None = None):
        self.package = package
        self.root = private_dir(Path(root or os.environ.get('UCSF_AGENT_JOB_ROOT', Path.home() / '.local/share/ucsf-agents')) / package)
        self.dbpath = self.root / 'jobs.sqlite3'
        if self.dbpath.is_symlink():
            raise ValueError('Job database must not be a symbolic link.')
        fd = os.open(self.dbpath, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, state TEXT, updated REAL, fingerprint TEXT, data TEXT)')
        self.dbpath.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.dbpath, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def directory(self, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise ValueError('Invalid job ID.')
        return self.root / job_id

    def lease(self, job_id):
        return FileLock(self.directory(job_id) / 'worker.lock', mode=0o600)

    def update(self, data):
        data['updated_at'] = time.time()
        with self.connect() as db:
            cursor = db.execute("UPDATE jobs SET state=?, updated=?, data=? WHERE id=? AND state IN ('queued','running')",
                       (data['state'], data['updated_at'], json.dumps(data), data['job_id']))
            return cursor.rowcount == 1

    def status(self, job_id):
        self.directory(job_id)
        with self.connect() as db:
            row = db.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('Unknown job ID.')
        data = json.loads(row[0])
        now = time.time()
        data['heartbeat_stale'] = data['state'] in ACTIVE and now - data['updated_at'] > 60
        if data['heartbeat_stale']:
            try:
                with self.lease(job_id).acquire(timeout=0):
                    previous_update = data['updated_at']
                    data = dict(data, state='interrupted', phase='interrupted', error='Worker exited without completing. Inspect before explicitly submitting a new job.', finished_at=now)
                    with self.connect() as db:
                        cursor = db.execute("UPDATE jobs SET state=?, data=? WHERE id=? AND updated=? AND state IN ('queued','running')",
                                            ('interrupted', json.dumps(data), job_id, previous_update))
                    if not cursor.rowcount:
                        return self.status(job_id)
            except Timeout:
                data['heartbeat_warning'] = 'Worker still holds its process lease but heartbeat is stale; do not resubmit.'
        data['elapsed_seconds'] = round((data.get('finished_at') or now) - data['created_at'], 2)
        data['heartbeat_age_seconds'] = round(now - data['updated_at'], 2)
        data['recommended_poll_seconds'] = 0 if data['state'] in TERMINAL else min(60, max(5, int(data['elapsed_seconds'] / 5)))
        data['eta_seconds'] = None
        data['estimate_basis'] = 'unknown'
        if data['state'] in ACTIVE:
            if data.get('stream_started_at') and data.get('expected_rows') and data['rows'] > 0:
                duration = now - data['stream_started_at']
                if duration > 0 and data['rows'] < data['expected_rows']:
                    data['eta_seconds'] = round((data['expected_rows'] - data['rows']) * duration / data['rows'], 1)
                    data['estimate_basis'] = 'caller-supplied row estimate and observed streaming rate; excludes future stalls'
            elif data.get('historical_seconds') and data['elapsed_seconds'] < data['historical_seconds']:
                data['eta_seconds'] = round(data['historical_seconds'] - data['elapsed_seconds'], 1)
                data['estimate_basis'] = 'median of prior successful identical queries; load and data may differ'
        return data

    def list_jobs(self):
        with self.connect() as db:
            ids = db.execute('SELECT id FROM jobs ORDER BY updated DESC LIMIT 50').fetchall()
        return [self.status(row[0]) for row in ids]

    def purge(self, job_id):
        import shutil
        data = self.status(job_id)
        if data['state'] in ACTIVE:
            raise ValueError('Cancel and wait for a terminal state before purging.')
        directory = self.directory(job_id)
        if directory.is_symlink():
            raise ValueError('Job directory must not be a symbolic link.')
        try:
            with self.lease(job_id).acquire(timeout=0):
                with self.connect() as db:
                    db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
        except Timeout:
            raise ValueError('Worker is still exiting; retry purge after it releases its lease.') from None
        shutil.rmtree(directory)
        return {'job_id': job_id, 'purged': True}

    def cancel(self, job_id):
        data = self.status(job_id)
        if data['state'] in ACTIVE:
            (self.directory(job_id) / 'cancel').touch(mode=0o600)
            data['cancel_requested'] = True
        return data

    def submit(self, query, parameters, credentials, *, mode='query', format='jsonl', timeout_seconds=3600, expected_rows=None, max_bytes=10_000_000_000):
        from importlib import import_module
        backend = import_module(f'{self.package}.query_backend')
        query = validate_query(query, backend.DIALECT)
        if mode not in ('query', 'explain') or format not in ('csv', 'jsonl'):
            raise ValueError('Invalid mode or format.')
        if not 1 <= timeout_seconds <= 86400 or not 1024 <= max_bytes <= 100_000_000_000:
            raise ValueError('timeout_seconds must be 1..86400; max_bytes must be 1024..100000000000.')
        if expected_rows is not None and expected_rows <= 0:
            raise ValueError('expected_rows must be positive or omitted.')
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise ValueError('parameters must be a JSON object.')
        backend.check_credentials(credentials)
        request = dict(query=query, parameters=parameters, mode=mode, format=format, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
        endpoint = {k: v for k, v in credentials.items() if k in ('server', 'database', 'uri')}
        fingerprint = hashlib.sha256(json.dumps([request, endpoint], sort_keys=True).encode()).hexdigest()
        now = time.time()
        job_id = uuid.uuid4().hex
        data = dict(job_id=job_id, state='queued', phase='queued', created_at=now, updated_at=now, rows=0, bytes=0, expected_rows=expected_rows, result_path=None, format=format, mode=mode, timeout_seconds=timeout_seconds)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Stale workers retain their slot until the operator checks their status.
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= 2:
                raise ValueError('Two jobs are already active. Monitor or cancel them before submitting another.')
            history = db.execute("SELECT data FROM jobs WHERE fingerprint=? AND state='completed' ORDER BY updated DESC LIMIT 5", (fingerprint,)).fetchall()
            if history:
                data['historical_seconds'] = statistics.median(json.loads(r[0])['finished_at'] - json.loads(r[0])['created_at'] for r in history)
            private_dir(self.directory(job_id))
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)', (job_id, 'queued', now, fingerprint, json.dumps(data)))
        # Requests contain potentially sensitive query parameters, so only the status
        # goes to disk. Credentials and query text reach the worker over a private pipe.
        env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'WINDIR', 'TMPDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL', 'PYTHONPATH', 'SSL_CERT_FILE', 'SSL_CERT_DIR')}
        env['UCSF_AGENT_JOB_ROOT'] = str(self.root.parent)
        args = [sys.executable, '-m', f'{self.package}.job_worker', job_id]
        options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
        try:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, **options)
            process.stdin.write(json.dumps(dict(request=request, credentials=credentials)).encode())
            process.stdin.close()
            threading.Thread(target=process.wait, daemon=True).start()
        except Exception:
            data.update(state='failed', phase='failed', error='Could not start the local worker.', finished_at=time.time())
            self.update(data)
            raise RuntimeError('Could not start the local worker.') from None
        return self.status(job_id)


def register_job_tools(mcp, package, credentials, prefix=''):
    from mcp.types import ToolAnnotations
    store = JobStore(package)
    @mcp.tool(name=prefix + 'submit_query_job', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def submit_query_job(query: str, parameters: dict | None = None, mode: str = 'query', format: str = 'jsonl', timeout_seconds: int = 3600, expected_rows: int | None = None, max_bytes: int = 10_000_000_000) -> dict:
        """Start a detached read-only SQL/Cypher export, or mode=explain for a plan. Returns a job ID immediately; poll query_job_status. No result-row cap; max_bytes is a fail-visible disk budget. Credentials come from the connector, never tool arguments."""
        return store.submit(query, parameters, credentials, mode=mode, format=format, timeout_seconds=timeout_seconds, expected_rows=expected_rows, max_bytes=max_bytes)

    @mcp.tool(name=prefix + 'query_job_status', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def query_job_status(job_id: str) -> dict:
        """Read phase, heartbeat, rows, bytes, elapsed time, advisory ETA and completed file path. Poll at recommended_poll_seconds. No credentials needed; never reads result rows into chat."""
        return store.status(job_id)

    @mcp.tool(name=prefix + 'cancel_query_job', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def cancel_query_job(job_id: str) -> dict:
        """Request cancellation. Poll until cancelled/completed; closing the worker connection releases the query. Partial files are not complete results."""
        return store.cancel(job_id)
