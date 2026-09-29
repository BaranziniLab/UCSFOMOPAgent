import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PACKAGE = 'ucsfomopagent'
jobs = importlib.import_module(PACKAGE + '.jobs')
worker = importlib.import_module(PACKAGE + '.job_worker')
backend = importlib.import_module(PACKAGE + '.query_backend')


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = jobs.JobStore(PACKAGE, self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def queued(self, **options):
        query = 'SELECT 1 AS value' if backend.DIALECT == 'sql' else 'RETURN 1 AS value'
        with patch.object(backend, 'check_credentials'), patch.object(jobs.subprocess, 'Popen') as popen:
            data = self.store.submit(query, {}, {'password': 'secret-must-not-appear'}, **options)
            wire = json.loads(popen.return_value.stdin.write.call_args.args[0])
            self.assertNotIn('secret-must-not-appear', repr(popen.call_args))
        return data, wire

    def test_streaming_csv_and_completion(self):
        data, wire = self.queued(format='csv')
        def stream(req, creds, progress):
            yield ['id','note'], [[1, 'a, "quote"\nsecond line'], [2, None]]
        done = worker.execute(self.store, data['job_id'], wire, stream)
        self.assertEqual(done['state'], 'completed')
        import csv
        with open(done['result_path']) as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows, [['id','note'],['1','a, "quote"\nsecond line'],['2','']])
        self.assertEqual(done['rows'], 2)
        self.assertFalse(list(self.store.directory(data['job_id']).glob('*.partial')))
        self.assertNotIn('secret-must-not-appear', self.store.dbpath.read_bytes().decode(errors='ignore'))
        self.assertEqual(os.stat(done['result_path']).st_mode & 0o777, 0o600)

    def test_no_silent_row_cap(self):
        data, wire = self.queued()
        def stream(req, creds, progress):
            for start in range(0, 100000, 1000):
                yield ['id'], [[n] for n in range(start,start+1000)]
        done = worker.execute(self.store, data['job_id'], wire, stream)
        self.assertEqual(done['state'], 'completed')
        self.assertEqual(done['rows'], 100000)
        with open(done['result_path']) as f:
            self.assertEqual(sum(1 for _ in f), 100000)

    def test_failure_redacts_raw_driver_errors(self):
        data, wire = self.queued()
        def stream(req, creds, progress):
            yield ['value'], [[1]]
            raise RuntimeError('password=secret-must-not-appear patient=private')
        done = worker.execute(self.store, data['job_id'], wire, stream)
        self.assertEqual(done['state'], 'failed')
        self.assertIsNone(done['result_path'])
        self.assertNotIn('private', json.dumps(done))
        self.assertNotIn('secret-must-not-appear', json.dumps(done))

    def test_cancel_while_driver_is_blocked(self):
        data, wire = self.queued()
        self.store.cancel(data['job_id'])
        def stream(req, creds, progress):
            time.sleep(1)
            return
            yield
        done = worker.execute(self.store, data['job_id'], wire, stream)
        self.assertEqual(done['state'], 'cancelled')
        self.assertIsNone(done['result_path'])
        time.sleep(1.1)

    def test_timeout_while_driver_is_blocked(self):
        data, wire = self.queued(timeout_seconds=1)
        def stream(req, creds, progress):
            time.sleep(1.5)
            return
            yield
        done = worker.execute(self.store, data['job_id'], wire, stream)
        self.assertEqual(done['state'], 'timed_out')
        time.sleep(.6)

    def test_disk_budget_is_failure_not_truncation(self):
        data, wire = self.queued(max_bytes=1024)
        done = worker.execute(self.store, data['job_id'], wire, lambda *args: iter([(['note'], [['x'*2048]])]))
        self.assertEqual(done['state'], 'failed')
        self.assertIsNone(done['result_path'])

    def test_admission_limit_and_id_validation(self):
        self.queued(); self.queued()
        with self.assertRaises(ValueError): self.queued()
        with self.assertRaises(ValueError): self.store.status('../jobs.sqlite3')

    def test_unknown_eta_and_stale_worker(self):
        data, wire = self.queued()
        self.assertIsNone(self.store.status(data['job_id'])['eta_seconds'])
        with self.store.connect() as db:
            data['updated_at'] -= 120
            db.execute('UPDATE jobs SET updated=?, data=? WHERE id=?', (data['updated_at'], json.dumps(data), data['job_id']))
        self.assertEqual(self.store.status(data['job_id'])['state'], 'interrupted')

    def test_read_only_guard(self):
        for query in ['SELECT * INTO stolen FROM patient', 'SELECT 1; DELETE FROM patient', 'SELECT * FROM OPENROWSET(foo)', 'WITH x AS (SELECT 1 AS a) UPDATE patient SET x=1', 'CALL apoc.cypher.run($q,{})', 'MATCH (n) DETACH DELETE n', 'LOAD CSV FROM $url AS x RETURN x', "RETURN 'unterminated", 'SELECT 1 /* outer /* nested */ DELETE x */']:
            with self.subTest(query=query), self.assertRaises(ValueError):
                jobs.validate_query(query, backend.DIALECT)
        dialect_queries = [('sql', "-- comment\n SELECT 'DELETE; O''Brien' AS note"), ('sql', 'WITH c AS (SELECT 1 AS id) SELECT id FROM c;'), ('cypher', "MATCH (n) WHERE n.name=$name RETURN n.name AS name"), ('cypher', "RETURN 'CREATE; DELETE' AS note")]
        for dialect, query in dialect_queries:
            self.assertTrue(jobs.validate_query(query, dialect))

    def test_help_without_credentials(self):
        env = {k:v for k,v in os.environ.items() if not k.startswith(('CLINICAL_RECORDS_', 'KNOWLEDGE_GRAPH_', 'SPOKEAGENT_'))}
        result = subprocess.run([sys.executable, '-m', PACKAGE, '--help'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('submit', result.stdout)


if __name__ == '__main__':
    unittest.main()
