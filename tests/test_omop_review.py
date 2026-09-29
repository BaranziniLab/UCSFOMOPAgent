import asyncio
import json
import unittest
from unittest.mock import patch

from fastmcp import Client
from ucsfomopagent.server import UCSFOMOPConfig, create_ucsf_omop_server


class Cursor:
    description = [('column',)]

    def __init__(self, connection):
        self.connection = connection
        self.sql = ''

    def execute(self, sql):
        if self.connection.fail_next:
            self.connection.fail_next = False
            raise RuntimeError("secret-driver-payload")
        self.sql = sql
        self.connection.statements.append(sql)

    def fetchall(self):
        if 'CROSS APPLY' in self.sql:
            return [(3004410, 8554, 100, 4.0, 8.0), (3004410, 0000, 1, 9.0, 9.0)]
        if 'INFORMATION_SCHEMA' in self.sql:
            return [('person_id', 'bigint', None, 'NO')]
        return []

    def fetchmany(self, size):
        if 'FROM concept' in self.sql:
            return [(3004410, 'Hemoglobin A1c', 'LOINC', 'S')]
        return []

    def close(self):
        pass


class Connection:
    def __init__(self):
        self.statements = []
        self.fail_next = False
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def close(self):
        self.closed = True


class OMOPReviewTests(unittest.TestCase):
    def run_client(self, callback):
        connection = Connection()
        async def run():
            mcp = create_ucsf_omop_server(UCSFOMOPConfig(username='test', password='test'))
            with patch('ucsfomopagent.server.pymssql.connect', return_value=connection) as connect:
                async with Client(mcp) as client:
                    await callback(client, connection)
                if connect.called:
                    self.assertTrue(connect.call_args.kwargs['autocommit'])
        asyncio.run(run())

    def test_lab_discovery_samples_with_units_without_exact_scans(self):
        async def check(client, connection):
            response = await client.call_tool('find_measurement', {'name': 'a1c'})
            result = json.loads(response.content[0].text)
            self.assertEqual(result['recommended_concept_ids'], [3004410])
            self.assertEqual(len(result['measurements'][0]['numeric_samples_by_unit']), 2)
            self.assertIn('nonrandom', result['hint'])
            self.assertIn('unit_concept_id', result['full_profile_query'])
            queries = '\n'.join(connection.statements)
            self.assertIn('TOP 100', queries)
            self.assertIn('CROSS APPLY', queries)
            self.assertNotIn('COUNT_BIG(DISTINCT', queries)
            self.assertNotIn('AVG(', queries)
        self.run_client(check)

    def test_empty_searches_do_not_touch_database(self):
        async def check(client, connection):
            for tool, args in [('find_measurement', {'name': ' '}), ('search_concepts', {'query': ' '})]:
                result = await client.call_tool(tool, args, raise_on_error=False)
                self.assertTrue(result.is_error)
            self.assertEqual(connection.statements, [])
        self.run_client(check)

    def test_schema_scope_is_preserved(self):
        async def check(client, connection):
            await client.call_tool('get_omop_schema', {'table': 'person'})
            self.assertIn("TABLE_SCHEMA = 'omop'", '\n'.join(connection.statements))
            result = await client.call_tool('get_omop_schema', {'table': 'other.person'}, raise_on_error=False)
            self.assertTrue(result.is_error)
        self.run_client(check)

    def test_failed_query_discards_connection_and_redacts_driver_details(self):
        async def check(client, connection):
            connection.fail_next = True
            result = await client.call_tool('query_ucsf_omop', {'sql_query': 'SELECT 1'}, raise_on_error=False)
            self.assertTrue(result.is_error)
            self.assertTrue(connection.closed)
            self.assertNotIn('secret-driver-payload', result.content[0].text)
            self.assertIn('omop-submit_query_job', result.content[0].text)
        self.run_client(check)
