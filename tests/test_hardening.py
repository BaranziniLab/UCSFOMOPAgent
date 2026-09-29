import importlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

PACKAGE='ucsfomopagent'
jobs=importlib.import_module(PACKAGE+'.jobs')
worker=importlib.import_module(PACKAGE+'.job_worker')
backend=importlib.import_module(PACKAGE+'.query_backend')


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=jobs.JobStore(PACKAGE,self.temp.name)

    def queued(self, **options):
        query='SELECT 1 AS value' if backend.DIALECT=='sql' else 'RETURN 1 AS value'
        with patch.object(backend,'check_credentials'),patch.object(jobs.subprocess,'Popen') as process:
            data=self.store.submit(query,{}, {},**options)
            wire=json.loads(process.return_value.stdin.write.call_args.args[0])
        return data,wire

    def test_tsql_quote_and_admin_bypasses(self):
        bad=["SELECT '\\'; DELETE FROM demo -- '","SELECT '\\' DELETE FROM demo --'",'SELECT 1 SELECT 2','SELECT 1\nDENY SELECT ON OBJECT::dbo.demo TO public','SELECT 1\nDISABLE TRIGGER ALL ON dbo.demo','SELECT 1\nKILL 52',"SELECT 1 WAITFOR DELAY '00:00:05'"]
        for query in bad:
            with self.subTest(query=query),self.assertRaises(ValueError):jobs.validate_query(query,'sql')
        self.assertTrue(jobs.validate_query("SELECT 'O''Brien' AS value",'sql'))
        self.assertTrue(jobs.validate_query('SELECT %(value)s AS value','sql'))
        self.assertTrue(jobs.validate_query('SELECT 1 AS n UNION ALL SELECT 2','sql'))

    def test_csv_header_obeys_budget(self):
        data,wire=self.queued(format='csv',max_bytes=1024)
        done=worker.execute(self.store,data['job_id'],wire,lambda *args:iter([(['x'*2048],[])]))
        self.assertEqual(done['state'],'failed')
        self.assertLessEqual((self.store.directory(data['job_id'])/'result.csv.partial').stat().st_size,1024)

    def test_cancel_before_start_does_not_connect(self):
        data,wire=self.queued()
        self.store.cancel(data['job_id'])
        stream=Mock()
        done=worker.execute(self.store,data['job_id'],wire,stream)
        stream.assert_not_called()
        self.assertEqual(done['state'],'cancelled')

    def test_live_lease_prevents_interruption_and_purge(self):
        data,wire=self.queued()
        with self.store.connect() as db:
            data['updated_at']-=120
            db.execute('UPDATE jobs SET updated=?, data=? WHERE id=?',(data['updated_at'],json.dumps(data),data['job_id']))
        with self.store.lease(data['job_id']):
            status=self.store.status(data['job_id'])
            self.assertEqual(status['state'],'queued')
            self.assertTrue(status['heartbeat_stale'])
            with self.assertRaises(ValueError):self.store.purge(data['job_id'])
        status=self.store.status(data['job_id'])
        self.assertEqual(status['state'],'interrupted')
        data['state']='running'
        self.assertFalse(self.store.update(data))
        self.assertEqual(self.store.status(data['job_id'])['state'],'interrupted')
        self.store.purge(data['job_id'])
        self.assertFalse(self.store.directory(data['job_id']).exists())

    @unittest.skipIf(backend.DIALECT!='sql','SQL adapter')
    def test_sql_binding_streaming_and_multiple_results(self):
        import pymssql
        cursor=Mock()
        cursor.description=[('value',)]
        cursor.fetchmany.side_effect=[[(1,)],[]]
        cursor.nextset.return_value=True
        cursor.__enter__=Mock(return_value=cursor);cursor.__exit__=Mock(return_value=None)
        conn=Mock();conn.cursor.return_value=cursor
        conn.__enter__=Mock(return_value=conn);conn.__exit__=Mock(return_value=None)
        request={'query':'SELECT %(value)s AS value','parameters':{'value':"x'; DELETE FROM demo --"},'timeout_seconds':120,'mode':'query'}
        with patch.object(pymssql,'connect',return_value=conn):
            with self.assertRaises(ValueError):list(backend.stream(request,dict(server='localhost',database='test',username='user',password='private'),{}))
        cursor.execute.assert_called_once_with(request['query'],request['parameters'])
        cursor.fetchall.assert_not_called()

    @unittest.skipIf(backend.DIALECT!='cypher','Neo4j adapter')
    def test_graph_export_keeps_identity_and_labels(self):
        from neo4j.graph import Graph, Node
        node=Node(Graph(),'element-1',1,['Gene'],{'name':'ABC'})
        encoded=backend.encode_value(node)
        self.assertEqual(encoded['element_id'],'element-1')
        self.assertEqual(encoded['labels'],['Gene'])
        self.assertEqual(encoded['properties'],{'name':'ABC'})

    def test_explicit_env_credentials_do_not_read_keyring(self):
        cli=importlib.import_module(PACKAGE+'.query_cli')
        with patch.dict(os.environ,{backend.SECRET_KEYS[0]:'supplied'},clear=True),patch.object(backend,'from_environment',return_value={}) as resolve,patch('keyring.get_password') as keyring:
            cli.credentials(backend)
            keyring.assert_not_called()
            resolve.assert_called_once()
