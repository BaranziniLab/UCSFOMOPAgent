"""Detached worker: the supervisor stays responsive even when the DB driver blocks."""
import csv
import importlib
import io
import json
import os
import sys
import threading
import time

from .jobs import JobStore, ACTIVE


def execute(store, job_id, payload, stream):
    data = store.status(job_id)
    if data['state'] not in ACTIVE:
        return data
    request = payload['request']
    directory = store.directory(job_id)
    if (directory / 'cancel').exists():
        data.update(state='cancelled', phase='cancelled', finished_at=time.time())
        store.update(data)
        return data
    partial = directory / ('result.' + request['format'] + '.partial')
    final = directory / ('result.' + request['format'])
    progress = {'phase': 'connecting', 'rows': 0, 'bytes': 0}
    done = threading.Event()
    failure = []

    def run():
        try:
            with partial.open('xb') as out:
                os.chmod(partial, 0o600)
                csv_buffer = io.StringIO(newline='')
                writer = csv.writer(csv_buffer) if request['format'] == 'csv' else None
                header = False
                def write_row(row):
                    if writer:
                        csv_buffer.seek(0); csv_buffer.truncate(0)
                        writer.writerow([json.dumps(v, default=str, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in row])
                        encoded = csv_buffer.getvalue().encode('utf-8')
                    else:
                        encoded = (json.dumps(row, default=str, ensure_ascii=False) + '\n').encode('utf-8')
                    if progress['bytes'] + len(encoded) > request['max_bytes']:
                        raise OverflowError('output budget')
                    out.write(encoded)
                    progress['bytes'] += len(encoded)
                for columns, batch in stream(request, payload['credentials'], progress):
                    progress.setdefault('stream_started_at', time.time())
                    progress['phase'] = 'streaming'
                    if writer and not header:
                        write_row(columns)
                        header = True
                    for row in batch:
                        write_row(row if writer else dict(zip(columns, row)))
                        progress['rows'] += 1
                    out.flush()
                out.flush()
                os.fsync(out.fileno())
        except Exception as error:
            name = type(error).__name__
            if isinstance(error, OverflowError):
                failure.append('Output byte budget exceeded; narrow the export or explicitly raise max_bytes.')
            elif 'Auth' in name:
                failure.append('Database authentication failed. Reconfigure credentials through the trusted UI or CLI.')
            elif 'Timeout' in name:
                failure.append('Database timeout. Inspect the plan and filters before explicitly resubmitting.')
            else:
                failure.append('Database or output operation failed (' + name + '). Check credentials, connectivity, schema, permissions and disk space; raw errors are withheld to protect data.')
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    started = time.monotonic()
    while True:
        data.update(progress, state='running')
        if (directory / 'cancel').exists():
            data.update(state='cancelled', phase='cancelled')
        elif time.monotonic() - started >= request['timeout_seconds']:
            data.update(state='timed_out', phase='timed_out', error='Job wall-clock budget expired. Inspect the plan before explicitly resubmitting with a larger budget.')
        elif done.is_set():
            data.update(progress)
            if failure:
                data.update(state='failed', phase='failed', error=failure[0])
            else:
                partial.replace(final)
                data.update(state='completed', phase='completed', result_path=str(final), bytes=final.stat().st_size)
        if data['state'] != 'running':
            data['finished_at'] = time.time()
        if not store.update(data):
            return store.status(job_id)
        if data['state'] != 'running':
            return data
        done.wait(1)


def main():
    os.umask(0o077)
    package = __package__
    job_id = sys.argv[1]
    store = JobStore(package)
    lease = store.lease(job_id)
    lease.acquire(timeout=0)
    try:
        payload = json.load(sys.stdin)
        backend = importlib.import_module(package + '.query_backend')
        execute(store, job_id, payload, backend.stream)
    except Exception:
        data = store.status(job_id)
        data.update(state='failed', phase='failed', error='Worker initialization failed.', finished_at=time.time())
        store.update(data)
    # Keep the OS lease until exit kills the driver thread too. Releasing it while
    # a blocked thread can still write would let purge race against an active file.
    os._exit(0)


if __name__ == '__main__':
    main()
