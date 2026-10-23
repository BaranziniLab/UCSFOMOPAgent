"""Credential-free MCP job fixture; only the three documented queries are accepted."""
from __future__ import annotations

import argparse
import importlib
import json
import math
from pathlib import Path
import sys
import threading
import time

REPOSITORY = Path(__file__).resolve().parents[1]
PACKAGE = json.loads((REPOSITORY / 'manifest.json').read_text())['name']
PREFIXES = {'ucsfomopagent': 'omop-', 'cdwagent': 'CDW-'}
if PACKAGE not in PREFIXES:
    raise RuntimeError('This fixture supports only the OMOP and CDW connector packages.')
PREFIX = PREFIXES[PACKAGE]
sys.path.insert(0, str(REPOSITORY / 'src'))

jobs = importlib.import_module(PACKAGE + '.jobs')
worker = importlib.import_module(PACKAGE + '.job_worker')

EXAMPLE_QUERY = 'SELECT 1 AS example'
FAILURE_QUERY = 'SELECT 0 AS simulate_failure'
SLOW_QUERY = 'SELECT 2 AS simulate_slow'
ALLOWED_QUERIES = frozenset((EXAMPLE_QUERY, FAILURE_QUERY, SLOW_QUERY))
EXAMPLE_ROWS = ((1, 'café'), (2, '漢字 😀'), (3, 'a, "quote"\nsecond line'))
SYNTHETIC_CREDENTIALS = dict(
    server='synthetic.invalid', database='synthetic',
    username='synthetic-only', password='synthetic-only',
)


def bounded_delay(value):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        raise ValueError('delay_seconds must be a number from 0 to 30.') from None
    if isinstance(value, bool) or not math.isfinite(delay) or not 0 <= delay <= 30:
        raise ValueError('delay_seconds must be a number from 0 to 30.')
    return delay


class SyntheticJobStore(jobs.JobStore):
    def __init__(self, root, delay_seconds=2):
        self.delay_seconds = bounded_delay(delay_seconds)
        root = Path(root)
        if not root.is_absolute() or root.is_symlink():
            raise ValueError('Supply an absolute, fresh temporary directory as root.')
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise ValueError('The synthetic job root must be empty; use a fresh temporary directory.')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        super().__init__(PACKAGE, root)
        self._threads = []
        self._threads_lock = threading.Lock()
        self._slow_release = threading.Event()
        self._closed = False

    def submit(self, query, parameters, credentials, **options):
        if (not isinstance(query, str) or query.strip() not in ALLOWED_QUERIES
                or parameters not in (None, {}) or options.get('mode', 'query') != 'query'):
            raise jobs.JobError(
                'synthetic_query_rejected', 'invalid_args',
                'This fixture accepts only its three documented queries without parameters.',
                'validation',
            )
        if self._closed:
            raise jobs.JobError('synthetic_server_closed', 'internal',
                                'The synthetic job server is closed.', 'admission')
        return super().submit(query.strip(), {}, dict(SYNTHETIC_CREDENTIALS), **options)

    def _stream(self, request, credentials, progress):
        if request['query'] == FAILURE_QUERY:
            raise jobs.JobError('synthetic_failure', 'tool_failure',
                                'Synthetic job failed as requested.', 'executing')
        if request['query'] == SLOW_QUERY:
            progress['phase'] = 'executing'
            self._slow_release.wait(self.delay_seconds)
        yield ['example', 'label'], EXAMPLE_ROWS

    def _start_worker(self, job_id, request, credentials):
        def run():
            try:
                with self.lease(job_id).acquire(timeout=0):
                    worker.execute(self, job_id, dict(request=request, credentials={}), self._stream)
            except Exception:
                data = self.status(job_id)
                jobs.record_failure(data, dict(
                    code='synthetic_worker_failed', kind='internal',
                    message='The synthetic worker could not complete.',
                    phase='synthetic_worker', retryable=False,
                ))
                self.update(data)

        thread = threading.Thread(target=run, name='synthetic-query-job', daemon=True)
        with self._threads_lock:
            self._threads.append(thread)
            thread.start()

    def release_slow_jobs(self):
        self._slow_release.set()

    def close(self):
        self._closed = True
        self.release_slow_jobs()
        deadline = time.monotonic() + 5
        with self._threads_lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in threads)


def create_server(root, delay_seconds=2):
    from fastmcp import FastMCP
    store = SyntheticJobStore(root, delay_seconds)
    server = FastMCP(
        PACKAGE + ' synthetic jobs',
        instructions=(
            'This is a synthetic fixture with no database access or real records. '
            'Allowed queries are exactly SELECT 1 AS example, SELECT 0 AS simulate_failure, '
            'and SELECT 2 AS simulate_slow. Use query_job_capacity, submit_query_job, '
            'wait_for_query_job and read_query_job_result to exercise the job contract.'
        ),
    )
    jobs.register_job_tools(server, PACKAGE, dict(SYNTHETIC_CREDENTIALS), PREFIX, store=store)
    return server, store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True,
                        help='Absolute path to a fresh, empty temporary directory.')
    parser.add_argument('--delay-seconds', type=float, default=2,
                        help='Synthetic slow-job delay, from 0 to 30 seconds (default: 2).')
    arguments = parser.parse_args(argv)
    try:
        server, store = create_server(arguments.root, arguments.delay_seconds)
    except ValueError as error:
        parser.error(str(error))
    try:
        server.run(transport='stdio')
    finally:
        store.close()


if __name__ == '__main__':
    main()
