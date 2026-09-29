"""Opt-in live checks. Inject credentials through the environment, never arguments."""
import asyncio
import importlib
import json
import logging
import os
from pathlib import Path
import time

PACKAGE = 'ucsfomopagent'
PREFIX = 'omop-'


async def main():
    from fastmcp import Client
    backend = importlib.import_module(PACKAGE + '.query_backend')
    server = importlib.import_module(PACKAGE + '.server')
    credentials = backend.from_environment()
    if PACKAGE == 'ucsfomopagent':
        mcp = server.create_ucsf_omop_server(server.UCSFOMOPConfig(**credentials))
        tool, args = 'query_ucsf_omop', {'sql_query': 'SELECT TOP 3 1 AS row_present FROM person'}
        export = 'SELECT TOP 10000 1 AS row_present FROM person'
    elif PACKAGE == 'cdwagent':
        from cdwagent.config import CDWConfig, ClinicalDBConfig
        mcp = server.create_cdw_server(CDWConfig(clinical_db=ClinicalDBConfig(**credentials)))
        tool, args = 'CDW-query', {'sql_query': 'SELECT TOP 3 1 AS row_present FROM deid_uf.PatientDim', 'row_limit': 3}
        export = 'SELECT TOP 10000 1 AS row_present FROM deid_uf.PatientDim'
    else:
        mcp = server.create_spoke_server(server.SPOKEConfig(**credentials))
        tool, args = 'query_spoke', {'cypher_query': 'MATCH (g:Gene) RETURN 1 AS row_present LIMIT 3'}
        export = 'MATCH (g:Gene) RETURN 1 AS row_present LIMIT 10000'
    async with Client(mcp) as client:
        tools = await client.list_tools()
        result = await client.call_tool(tool, args)
        assert not result.is_error and 'row_present' in result.content[0].text
        result = await client.call_tool(PREFIX + 'submit_query_job', {'query': export, 'timeout_seconds': 120, 'expected_rows': 10000})
        job = json.loads(result.content[0].text)
        deadline = time.monotonic() + 140
        while time.monotonic() < deadline:
            result = await client.call_tool(PREFIX + 'query_job_status', {'job_id': job['job_id']})
            status = json.loads(result.content[0].text)
            if status['state'] not in ('queued', 'running'):
                break
            await asyncio.sleep(status['recommended_poll_seconds'])
        assert status['state'] == 'completed' and 0 < status['rows'] <= 10000
        with Path(status['result_path']).open() as output:
            assert sum(1 for _ in output) == status['rows']
        print(json.dumps({'agent': PACKAGE, 'tools': len(tools), 'small_query': 'passed', 'export_state': status['state'], 'export_rows': status['rows'], 'seconds': status['elapsed_seconds']}))


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(main())
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__}))
        raise SystemExit(1) from None
