"""Streaming SQL Server adapter; each background job owns its connection."""
import os

DIALECT = 'sql'
SECRET_KEYS = ('CLINICAL_RECORDS_USERNAME', 'CLINICAL_RECORDS_PASSWORD')
ENV_KEYS = ('CLINICAL_RECORDS_USERNAME', 'CLINICAL_RECORDS_PASSWORD', 'CLINICAL_RECORDS_SERVER', 'CLINICAL_RECORDS_DATABASE')
DEFAULT_DATABASE = 'OMOP_DEID'


def from_environment(env=None):
    env = os.environ if env is None else env
    return dict(server=env.get('CLINICAL_RECORDS_SERVER', 'QCDIDDWDB001.ucsfmedicalcenter.org'), database=env.get('CLINICAL_RECORDS_DATABASE', DEFAULT_DATABASE), username=env.get('CLINICAL_RECORDS_USERNAME'), password=env.get('CLINICAL_RECORDS_PASSWORD'))


def check_credentials(values):
    if not values.get('username') or not values.get('password'):
        raise ValueError('Configure CLINICAL_RECORDS_USERNAME and CLINICAL_RECORDS_PASSWORD through BioRouter or the interactive auth command.')


def stream(request, credentials, progress):
    import pymssql
    check_credentials(credentials)
    with pymssql.connect(server=credentials['server'], database=credentials['database'], user=credentials['username'], password=credentials['password'], timeout=request['timeout_seconds'], login_timeout=20, autocommit=True, appname=__package__ + '-query-job') as conn:
        with conn.cursor() as cursor:
            if request['mode'] == 'explain':
                cursor.execute('SET SHOWPLAN_XML ON')
            progress['phase'] = 'planning' if request['mode'] == 'explain' else 'executing'
            cursor.execute(request['query'], request['parameters'] or None)
            columns = [c[0] for c in cursor.description] if cursor.description else []
            if len(columns) != len(set(columns)):
                raise ValueError('Duplicate column names: alias each projection.')
            while True:
                rows = cursor.fetchmany(1000)
                yield columns, rows
                if not rows:
                    break
            if cursor.nextset():
                raise ValueError("Multiple result sets are not supported; submit one SELECT per job.")
