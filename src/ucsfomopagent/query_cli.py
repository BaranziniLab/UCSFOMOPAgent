"""CLI front end for the same jobs exposed over MCP."""
import argparse
import getpass
import importlib
import json
import os
from pathlib import Path
import sys
import time

from .jobs import JobStore, TERMINAL


def credentials(backend):
    import keyring
    if any(os.environ.get(key) for key in backend.SECRET_KEYS):
        return backend.from_environment(os.environ)
    values = {}
    for key in backend.ENV_KEYS:
        value = os.environ.get(key)
        if value is None:
            value = keyring.get_password('ucsf-agent:' + __package__, key)
        if value:
            values[key] = value
    return backend.from_environment(values)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only background queries. No arguments starts the MCP server.')
    sub = parser.add_subparsers(dest='command', required=True)
    auth = sub.add_parser('auth', help='Interactively configure standalone CLI credentials in the OS keyring; never pass secrets as arguments.')
    auth.add_argument('--clear', action='store_true', help='Delete the standalone CLI credential profile before switching authentication routes.')
    sub.add_parser('list', help='List the most recent 50 jobs and reconcile lost workers.')
    submit = sub.add_parser('submit', help='Submit SQL/Cypher from a local file; returns immediately.')
    submit.add_argument('--query-file', required=True, type=Path)
    submit.add_argument('--parameters-file', type=Path)
    submit.add_argument('--mode', choices=['query', 'explain'], default='query')
    submit.add_argument('--format', choices=['csv', 'jsonl'], default='jsonl')
    submit.add_argument('--timeout-seconds', type=int, default=3600)
    submit.add_argument('--expected-rows', type=int)
    submit.add_argument('--max-bytes', type=int, default=10_000_000_000)
    for command in ['status', 'watch', 'cancel', 'purge']:
        sub.add_parser(command).add_argument('job_id')
    args = parser.parse_args(argv)
    try:
        backend = importlib.import_module(__package__ + '.query_backend')
        if args.command == 'auth':
            import keyring
            if args.clear:
                for key in backend.ENV_KEYS:
                    try:
                        keyring.delete_password('ucsf-agent:' + __package__, key)
                    except keyring.errors.PasswordDeleteError:
                        pass
                print(json.dumps({'cleared': True}))
                return 0
            if not sys.stdin.isatty():
                raise ValueError('Use auth from an interactive terminal, or supply environment variables through your secret manager.')
            values = {}
            for key in backend.ENV_KEYS:
                value = getpass.getpass(key + ' (Enter to keep existing/default): ')
                if value:
                    keyring.set_password('ucsf-agent:' + __package__, key, value)
                    values[key] = True
            print(json.dumps({'configured_keys': list(values)}))
            return 0
        store = JobStore(__package__)
        if args.command == 'list':
            print(json.dumps(store.list_jobs()))
            return 0
        if args.command == 'purge':
            print(json.dumps(store.purge(args.job_id)))
            return 0
        if args.command == 'submit':
            params = json.loads(args.parameters_file.read_text()) if args.parameters_file else {}
            data = store.submit(args.query_file.read_text(), params, credentials(backend), mode=args.mode, format=args.format, timeout_seconds=args.timeout_seconds, expected_rows=args.expected_rows, max_bytes=args.max_bytes)
        elif args.command == 'cancel':
            data = store.cancel(args.job_id)
        else:
            while True:
                data = store.status(args.job_id)
                if args.command != 'watch' or data['state'] in TERMINAL:
                    break
                print(json.dumps(data), flush=True)
                time.sleep(data['recommended_poll_seconds'])
        print(json.dumps(data), flush=True)
        return 1 if data['state'] in ('failed', 'timed_out', 'cancelled', 'interrupted') else 0
    except (ValueError, RuntimeError) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 2
    except Exception as error:
        print(json.dumps({'error': 'Local operation failed (' + type(error).__name__ + '); check file permissions, JSON inputs and OS keyring availability.'}), file=sys.stderr)
        return 2
