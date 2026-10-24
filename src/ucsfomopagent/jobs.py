"""Durable, bounded-memory query jobs shared by the OMOP and CDW connectors."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, suppress
import asyncio
import codecs
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import statistics
import subprocess
import sys
import threading
import time
import uuid
from filelock import FileLock, Timeout
from mcp.types import CallToolResult

from .private_storage import PrivateStorageError, private_directory, private_storage_call

ACTIVE = ('queued', 'running')
TERMINAL = ('completed', 'failed', 'cancelled', 'timed_out', 'interrupted')
MAX_ACTIVE_JOBS = 2
MAX_WAIT_SECONDS = 30
WAIT_LOOKUP_SECONDS = 0.05
MAX_RESULT_BYTES = 65536
OPERATION_META_KEY = 'io.biorouter/operation'


class JobError(ValueError):
    def __init__(self, code, kind, message, phase, retryable=False, **context):
        super().__init__(message)
        self.details = dict(code=code, kind=kind, message=message, phase=phase, retryable=retryable)
        self.context = context


def failure_details(error, phase):
    if isinstance(error, JobError):
        return error.details
    if isinstance(error, PrivateStorageError):
        return dict(code=error.code, kind=error.kind, message=str(error), phase=phase, retryable=False)
    code, kind, message, retryable = 'query_failed', 'tool_failure', 'Database execution or result processing failed. Check the query plan, schema and selected output format.', False
    if isinstance(error, OverflowError):
        code, message = 'output_limit_exceeded', 'Output byte budget exceeded; narrow the export or explicitly raise max_bytes.'
    elif isinstance(error, TimeoutError):
        code, kind, message = 'database_timeout', 'timeout', 'Database timeout. Inspect the plan and filters before explicitly resubmitting.'
    elif isinstance(error, OSError):
        if error.errno == errno.ENOSPC:
            code, message = 'disk_full', 'Local result storage is full. Free space before explicitly resubmitting.'
        elif error.errno in (errno.EACCES, errno.EPERM):
            code, kind, message = 'storage_permission_denied', 'permission_denied', 'Local result storage access was denied.'
        elif isinstance(error, ConnectionError):
            code, kind, message, retryable = 'connection_interrupted', 'transient', 'The database connection was interrupted.', True
        else:
            code, message = 'output_io_failed', 'Local result file processing failed. Check available space and file permissions.'
    elif type(error).__module__.split('.')[0] == 'pymssql':
        number = error.args[0] if error.args and type(error.args[0]) is int else None
        if number == 20003:
            code, kind, message = 'database_timeout', 'timeout', 'Database timeout. Inspect the plan and filters before explicitly resubmitting.'
        elif number == 18456:
            code, kind, message = 'authentication_failed', 'permission_denied', 'Database authentication failed. Reconfigure credentials through the trusted UI or CLI.'
        elif number == 229:
            code, kind, message = 'database_permission_denied', 'permission_denied', 'The database denied this operation. Check read or SHOWPLAN permissions.'
        elif number in (1205, 1222):
            code, kind, message, retryable = 'database_busy', 'transient', 'The database could not obtain the required locks.', True
        elif type(error).__name__ == 'ProgrammingError':
            code, kind, message = 'query_rejected', 'invalid_args', 'The database rejected the query. Check schema, projections and parameters.'
        elif phase == 'connecting':
            code, message = 'connection_failed', 'Database connection failed. Check connectivity and the configured authentication route.'
    return dict(code=code, kind=kind, message=message, phase=phase, retryable=retryable)


def record_failure(data, details, state='failed'):
    data.update(state=state, phase=state, error=details['message'], error_details=details, finished_at=time.time())


def operation_metadata(package, data):
    state = data['state']
    status = 'failed' if state in ('failed', 'timed_out', 'interrupted') else state
    operation = dict(version=1, id=package + ':' + data['job_id'], status=status)
    if status == 'failed':
        operation['error'] = data.get('error_details') or dict(code='job_' + state, kind='tool_failure', message='The job did not complete. Inspect its status before explicitly submitting a replacement.', phase=state, retryable=False)
    return operation


def tool_result(data, package=None):
    from mcp.types import TextContent
    metadata = {OPERATION_META_KEY: operation_metadata(package, data)} if package else None
    return CallToolResult(content=[TextContent(type='text', text=json.dumps(data))], structuredContent=data, isError=False, _meta=metadata)


def tool_failure(error, phase='request'):
    from mcp.types import TextContent
    details = failure_details(error, phase)
    data = dict(error=details, **(error.context if isinstance(error, JobError) else {}))
    return CallToolResult(content=[TextContent(type='text', text=details['message'])], structuredContent=data, isError=True)


INSTRUCTIONS = """
For schema discovery and small previews use the existing synchronous tools.
For exports, joins/scans with uncertain cost, or a query that timed out, use
submit_query_job. It starts an independent local CLI worker and returns immediately.
Credentials stay inside the connector and worker, never request them in chat.
Resolve schema/concepts first; select only needed columns and use indexed filters.
Use mode='explain' to inspect a plan without executing the query (SQL Server needs
SHOWPLAN permission). Do not run an expensive COUNT(*) solely to estimate export size.
Check query_job_capacity before submission. Use wait_for_query_job for bounded native
waiting (up to 30 seconds per call); query_job_status remains an immediate lookup.
Report phase, elapsed time, rows and bytes. An unknown ETA is not a stuck query;
execution before the first row often has no measurable percentage. Estimates from
prior identical queries or expected_rows are advisory, never guarantees.
Only completed means the result file is complete; partial files are incomplete.
Use cancel_query_job to stop unwanted work. Do not resubmit while a job is active.
Inspect error_details.code, phase and retryable before explicitly resubmitting.
Fix credentials, connectivity, permissions or disk conditions rather than blindly
rewriting SQL. Revise the query only when its schema, syntax or plan is the cause.
Jobs survive chat/MCP disconnects, but not host reboot; interrupted queries require
an explicit new submission. There is no unsafe automatic OFFSET resume or retry.
Use read_query_job_result with the completed job ID for bounded UTF-8 chunks;
continue with next_offset until eof. Do not pass result_path to filesystem tools.
For large analyses process the complete local file and return summaries to chat.
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
    with private_directory(path) as directory:
        return directory.path


class JobStore:
    def __init__(self, package: str, root: str | Path | None = None):
        self.package = package
        self._storage = private_directory(Path(root or os.environ.get('UCSF_AGENT_JOB_ROOT', Path.home() / '.local/share/ucsf-agents')) / package)
        self.root = self._storage.path
        self._root_identity = self._storage.identity
        self.dbpath = self.root / 'jobs.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, state TEXT, updated REAL, fingerprint TEXT, data TEXT)')

    @contextmanager
    def connect(self, timeout=10):
        with private_storage_call(lambda: self._storage.open_file('jobs.sqlite3')):
            pass
        db = sqlite3.connect(self.dbpath, timeout=timeout)
        try:
            with db:
                yield db
        finally:
            db.close()

    def directory(self, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise JobError('invalid_job_id', 'invalid_args', 'Invalid job ID.', 'lookup')
        return self.root / job_id

    def lease(self, job_id):
        return FileLock(self.directory(job_id) / 'worker.lock', mode=0o600)

    def update(self, data):
        data['updated_at'] = time.time()
        with self.connect() as db:
            cursor = db.execute("UPDATE jobs SET state=?, updated=?, data=? WHERE id=? AND state IN ('queued','running')",
                       (data['state'], data['updated_at'], json.dumps(data), data['job_id']))
            return cursor.rowcount == 1

    def status(self, job_id, *, database_timeout=10):
        deadline = time.monotonic() + database_timeout
        try:
            return self._status(job_id, deadline)
        except sqlite3.OperationalError as error:
            if (getattr(error, 'sqlite_errorcode', 0) & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            raise JobError('status_lookup_busy', 'transient', 'Job status storage is busy. Retry the lookup after existing work releases it.', 'status_lookup', retryable=True) from None

    def _status(self, job_id, deadline):
        self.directory(job_id)
        with self.connect(timeout=max(0, deadline - time.monotonic())) as db:
            row = db.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise JobError('job_not_found', 'not_found', 'Unknown job ID.', 'lookup')
        data = json.loads(row[0])
        now = time.time()
        data['heartbeat_stale'] = data['state'] in ACTIVE and now - data['updated_at'] > 60
        if data['heartbeat_stale']:
            try:
                with self.lease(job_id).acquire(timeout=0):
                    previous_update = data['updated_at']
                    record_failure(data, dict(code='worker_interrupted', kind='internal', message='Worker exited without completing. Inspect before explicitly submitting a new job.', phase=data.get('phase', 'unknown'), retryable=False), state='interrupted')
                    with self.connect(timeout=max(0, deadline - time.monotonic())) as db:
                        cursor = db.execute("UPDATE jobs SET state=?, data=? WHERE id=? AND updated=? AND state IN ('queued','running')",
                                            ('interrupted', json.dumps(data), job_id, previous_update))
                    if not cursor.rowcount:
                        return self._status(job_id, deadline)
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
        data['capacity'] = self.capacity(database_timeout=max(0, deadline - time.monotonic()))
        return data

    def capacity(self, *, database_timeout=10):
        with self.connect(timeout=database_timeout) as db:
            active = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
        return dict(active_jobs=active, max_active_jobs=MAX_ACTIVE_JOBS, available_slots=max(0, MAX_ACTIVE_JOBS - active), advisory=True)

    async def wait(self, job_id, timeout_seconds=30):
        if type(timeout_seconds) is not int or not 0 <= timeout_seconds <= MAX_WAIT_SECONDS:
            raise JobError('invalid_wait', 'invalid_args', 'timeout_seconds must be an integer from 0 to 30.', 'validation')
        deadline = time.monotonic() + timeout_seconds
        data = None
        while True:
            remaining = max(0, deadline - time.monotonic())
            try:
                data = await asyncio.to_thread(self.status, job_id, database_timeout=min(WAIT_LOOKUP_SECONDS, remaining))
            except JobError as error:
                if error.details['code'] != 'status_lookup_busy':
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if data is None:
                        raise
                    data.update(wait_timed_out=True, wait_status_stale=True, wait_warning='The wait ended with status storage busy; this is the last successfully observed active state.')
                    return data
                await asyncio.sleep(min(0.25, remaining))
                continue
            remaining = deadline - time.monotonic()
            if data['state'] in TERMINAL or remaining <= 0:
                data['wait_timed_out'] = data['state'] not in TERMINAL
                return data
            await asyncio.sleep(min(0.25, remaining))

    def read_result(self, job_id, offset=0, max_bytes=8192):
        if type(offset) is not int or offset < 0 or type(max_bytes) is not int or not 4 <= max_bytes <= MAX_RESULT_BYTES:
            raise JobError('invalid_result_chunk', 'invalid_args', 'offset must be a nonnegative byte offset; max_bytes must be 4..65536.', 'result_read')
        try:
            self._storage.verify()
        except (OSError, ValueError):
            raise JobError('result_scope_changed', 'permission_denied', 'The managed job directory changed or cannot be secured; result access was refused.', 'result_read') from None
        data = self.status(job_id)
        if data['state'] != 'completed':
            raise JobError('result_not_complete', 'invalid_args', 'Only completed job results can be read.', 'result_read')
        if data.get('format') not in ('csv', 'jsonl'):
            raise JobError('invalid_result_format', 'internal', 'The saved result format is invalid.', 'result_read')
        if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY') or os.open not in os.supports_dir_fd:
            raise JobError('secure_reader_unavailable', 'internal', 'This platform does not support the secure result reader; use the local CLI result file.', 'result_read')
        try:
            with ExitStack() as stack:
                root_fd = self._storage.duplicate_fd()
                stack.callback(os.close, root_fd)
                root_stat = os.fstat(root_fd)
                if (root_stat.st_dev, root_stat.st_ino) != self._root_identity:
                    raise JobError('result_scope_changed', 'permission_denied', 'The managed job directory changed; result access was refused.', 'result_read')
                directory_fd = os.open(job_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                stack.callback(os.close, directory_fd)
                result_fd = os.open('result.' + data['format'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                stack.callback(os.close, result_fd)
                result_stat = os.fstat(result_fd)
                if not stat.S_ISREG(result_stat.st_mode) or result_stat.st_nlink != 1:
                    raise JobError('invalid_result_file', 'permission_denied', 'The result must be a regular managed file without links.', 'result_read')
                size = result_stat.st_size
                if size != data.get('bytes'):
                    raise JobError('result_size_changed', 'internal', 'The completed result size changed; access was refused.', 'result_read')
                if offset > size:
                    raise JobError('invalid_result_offset', 'invalid_args', 'The byte offset exceeds the completed result size.', 'result_read')
                os.lseek(result_fd, offset, os.SEEK_SET)
                raw = os.read(result_fd, min(max_bytes, size - offset))
                if len(raw) != min(max_bytes, size - offset):
                    raise JobError('result_size_changed', 'internal', 'The completed result changed while it was being read.', 'result_read')
                decoder = codecs.getincrementaldecoder('utf-8')()
                text = decoder.decode(raw, final=offset + len(raw) == size)
                consumed = len(raw) - len(decoder.getstate()[0])
        except UnicodeDecodeError:
            raise JobError('invalid_result_encoding', 'invalid_args', 'The result or byte offset is not valid UTF-8.', 'result_read') from None
        except OSError:
            raise JobError('result_unavailable', 'permission_denied', 'The managed result file is missing, linked or inaccessible.', 'result_read') from None
        next_offset = offset + consumed
        return dict(job_id=job_id, format=data['format'], text=text, offset=offset, bytes_read=consumed, total_bytes=size, eof=next_offset == size, next_offset=None if next_offset == size else next_offset)

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
            with private_storage_call(lambda: self._storage.child(job_id)) as directory:
                with private_storage_call(lambda: directory.open_file('cancel')):
                    pass
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
            active = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
            if active >= MAX_ACTIVE_JOBS:
                raise JobError('capacity_exceeded', 'transient', 'Two jobs are already active. Wait for completion or cancel unwanted work before submitting another.', 'admission', retryable=True, capacity=dict(active_jobs=active, max_active_jobs=MAX_ACTIVE_JOBS, available_slots=0, advisory=True))
            history = db.execute("SELECT data FROM jobs WHERE fingerprint=? AND state='completed' ORDER BY updated DESC LIMIT 5", (fingerprint,)).fetchall()
            if history:
                data['historical_seconds'] = statistics.median(json.loads(r[0])['finished_at'] - json.loads(r[0])['created_at'] for r in history)
            with private_storage_call(lambda: self._storage.child(job_id)):
                pass
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)', (job_id, 'queued', now, fingerprint, json.dumps(data)))
        try:
            self._start_worker(job_id, request, credentials)
        except Exception:
            details = dict(code='worker_start_failed', kind='internal', message='Could not start the local worker.', phase='worker_start', retryable=False)
            record_failure(data, details)
            self.update(data)
        return self.status(job_id)

    def _start_worker(self, job_id, request, credentials):
        # Keep query text and credentials off disk and out of child arguments.
        env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'WINDIR', 'TMPDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL', 'PYTHONPATH', 'SSL_CERT_FILE', 'SSL_CERT_DIR')}
        env['UCSF_AGENT_JOB_ROOT'] = str(self.root.parent)
        args = [sys.executable, '-m', f'{self.package}.job_worker', job_id]
        options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
        wire = json.dumps(dict(request=request, credentials=credentials)).encode()
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, **options)
        try:
            process.stdin.write(wire)
            process.stdin.close()
        except Exception:
            with suppress(OSError):
                if process.stdin is not None:
                    process.stdin.close()
            raise
        finally:
            with suppress(RuntimeError):
                threading.Thread(target=process.wait, daemon=True).start()


def register_job_tools(mcp, package, credentials, prefix='', *, store=None):
    from mcp.types import ToolAnnotations
    storage_error = None
    if store is None:
        try:
            store = JobStore(package)
        except PrivateStorageError as error:
            storage_error = error

    def call(action, operation=False):
        try:
            if storage_error is not None:
                raise storage_error
            data = action()
            return tool_result(data, package if operation else None)
        except ValueError as error:
            if isinstance(error, PrivateStorageError):
                return tool_failure(error, 'storage')
            if not isinstance(error, JobError):
                error = JobError('invalid_request', 'invalid_args', 'Invalid query or job arguments. Check the read-only query policy and declared argument bounds.', 'validation')
            return tool_failure(error)
        except Exception as error:
            return tool_failure(error)

    @mcp.tool(name=prefix + 'submit_query_job', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def submit_query_job(query: str, parameters: dict | None = None, mode: str = 'query', format: str = 'jsonl', timeout_seconds: int = 3600, expected_rows: int | None = None, max_bytes: int = 10_000_000_000) -> CallToolResult:
        """Start a detached read-only export or mode=explain plan. Check query_job_capacity first, then wait_for_query_job. Credentials come from the connector. No automatic retry or result-row cap."""
        return call(lambda: store.submit(query, parameters, credentials, mode=mode, format=format, timeout_seconds=timeout_seconds, expected_rows=expected_rows, max_bytes=max_bytes), operation=True)

    @mcp.tool(name=prefix + 'query_job_capacity', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def query_job_capacity() -> CallToolResult:
        """Read active_jobs, max_active_jobs and available_slots before submission. Capacity is advisory; admission is atomically checked again when submitting."""
        return call(lambda: store.capacity())

    @mcp.tool(name=prefix + 'query_job_status', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def query_job_status(job_id: str) -> CallToolResult:
        """Immediately read phase, heartbeat, rows, bytes, capacity and job outcome. Failed jobs include sanitized error_details. Use wait_for_query_job instead of rapid polling."""
        return call(lambda: store.status(job_id), operation=True)

    @mcp.tool(name=prefix + 'wait_for_query_job', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    async def wait_for_query_job(job_id: str, timeout_seconds: int = 30) -> CallToolResult:
        """Wait natively for a terminal job state, for 0..30 seconds. wait_timed_out means this wait ended while the job remains active; it does not cancel or time out the job. Cancellation stops waiting only."""
        try:
            if storage_error is not None:
                raise storage_error
            return tool_result(await store.wait(job_id, timeout_seconds), package)
        except Exception as error:
            return tool_failure(error)

    @mcp.tool(name=prefix + 'read_query_job_result', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def read_query_job_result(job_id: str, offset: int = 0, max_bytes: int = 8192) -> CallToolResult:
        """Read 4..65536 bytes of a completed managed result as UTF-8 text. Continue at next_offset until eof. Offsets are bytes, not rows; CSV rows may span chunks. Accepts job IDs, never arbitrary file paths."""
        return call(lambda: store.read_result(job_id, offset, max_bytes))

    @mcp.tool(name=prefix + 'cancel_query_job', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def cancel_query_job(job_id: str) -> CallToolResult:
        """Request cancellation, then wait_for_query_job until terminal. Closing the worker releases its query connection; partial files remain incomplete."""
        return call(lambda: store.cancel(job_id), operation=True)
