import asyncio
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = 'ucsfomopagent'


class MCPTests(unittest.TestCase):
    def test_tool_inventory_and_job_contract(self):
        async def run():
            from fastmcp import Client
            server = importlib.import_module(PACKAGE + '.server')
            if PACKAGE == 'ucsfomopagent':
                mcp = server.create_ucsf_omop_server(server.UCSFOMOPConfig(username='test',password='test'))
                prefix, count = 'omop-', 8
            elif PACKAGE == 'cdwagent':
                from cdwagent.config import CDWConfig, ClinicalDBConfig
                mcp = server.create_cdw_server(CDWConfig(clinical_db=ClinicalDBConfig(username='test',password='test')))
                prefix, count = 'CDW-', 25
            else:
                mcp = server.create_spoke_server(server.SPOKEConfig(uri='bolt://localhost:7687',username='test',password='test',database='neo4j'))
                prefix, count = 'spoke-', 8
            async with Client(mcp) as client:
                tools = await client.list_tools()
                names = {t.name for t in tools}
                self.assertEqual(len(names), count, names)
                self.assertTrue({prefix+n for n in ['submit_query_job','query_job_status','cancel_query_job']} <= names)
                submit = next(t for t in tools if t.name == prefix+'submit_query_job')
                self.assertNotIn('credentials', submit.input_schema['properties'])
                query = 'SELECT 1 AS value' if PACKAGE != 'spokeagent' else 'RETURN 1 AS value'
                jobs = importlib.import_module(PACKAGE + '.jobs')
                with patch.object(jobs.subprocess, 'Popen'):
                    result = await client.call_tool(prefix+'submit_query_job', {'query':query})
                data = json.loads(result.content[0].text)
                status = await client.call_tool(prefix+'query_job_status', {'job_id':data['job_id']})
                self.assertIn('queued', status.content[0].text)
                await client.call_tool(prefix+'cancel_query_job', {'job_id':data['job_id']})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'UCSF_AGENT_JOB_ROOT': tmp}):
            asyncio.run(run())
