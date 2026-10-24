import importlib
import json
import unittest

import pymssql

PACKAGE = 'ucsfomopagent'
jobs = importlib.import_module(PACKAGE + '.jobs')


class DriverTimeoutTests(unittest.TestCase):
    def test_freetds_timeout_keeps_its_kind_and_phase_without_driver_text(self):
        for phase in ('connecting', 'executing', 'streaming'):
            with self.subTest(phase=phase):
                error = pymssql.OperationalError(20003, b'private_marker query=private_marker')
                details = jobs.failure_details(error, phase)
                self.assertEqual(details['code'], 'database_timeout')
                self.assertEqual(details['kind'], 'timeout')
                self.assertEqual(details['phase'], phase)
                self.assertFalse(details['retryable'])
                self.assertNotIn('private_marker', json.dumps(details))
                failed_job = {'job_id': 'a' * 32, 'state': 'running'}
                jobs.record_failure(failed_job, details)
                operation = jobs.operation_metadata(PACKAGE, failed_job)
                self.assertEqual(operation['status'], 'failed')
                self.assertEqual(operation['error'], details)
                self.assertNotIn('private_marker', json.dumps(operation))


if __name__ == '__main__':
    unittest.main()
