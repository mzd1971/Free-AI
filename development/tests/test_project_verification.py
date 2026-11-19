"""The manual verification path uses a real child and scoped project fingerprints."""

import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from project_agent import OUTPUT_LIMIT
from project_verification import format_verification_context, parse_command, run_verification, workspace_fingerprint


DEVELOPMENT = Path(__file__).resolve().parents[1]


def python(code):
    return json.dumps([sys.executable, '-B', '-c', code])


class ProjectVerificationTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(scratch.cleanup)
        self.workspace = Path(scratch.name)
        self.source = self.workspace / 'source.py'
        self.source.write_text('value = 1\n', encoding='utf-8')

    def test_parses_simple_and_quoted_windows_arguments(self):
        self.assertEqual(parse_command('python -m unittest discover'), ['python', '-m', 'unittest', 'discover'])
        argv = [r'C:\Path With Spaces\python.exe', '', '-c', "print('literal ; > |')"]
        self.assertEqual(parse_command(json.dumps(argv)), argv)
        for command in ('', '[]', '[1]', '["python",', 'python -m unittest; echo unexpected',
                        'python > output.txt', 'python\nnot_a_second_argument', '["python","\\u0000"]',
                        '["python","--api-key","test-sensitive-token"]'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                parse_command(command)

    def test_runs_real_child_and_reports_exit_code_and_streams(self):
        passed = run_verification(self.workspace, python("print('check ran')"))
        self.assertEqual((passed['status'], passed['returncode']), ('passed', 0), passed)
        self.assertEqual(passed['stdout'], 'check ran')
        self.assertIn('check ran', passed['output'])
        self.assertEqual(passed['fingerprint_before'], passed['fingerprint_after'])
        self.assertTrue(passed['current'])
        self.assertIn('这条检查', passed['message'])

        failed = run_verification(self.workspace, python("import sys; print('assertion failed', file=sys.stderr); sys.exit(1)"))
        self.assertEqual((failed['status'], failed['returncode']), ('failed', 1), failed)
        self.assertEqual(failed['stderr'], 'assertion failed')
        self.assertIn('assertion failed', format_verification_context(failed))
        summary = format_verification_context(passed)
        self.assertIn('check ran', summary)
        self.assertIn('退出码：0', summary)
        self.assertIn('不自动重放', summary)
        self.assertIn('已标为过期', format_verification_context({**passed, 'stale': True}))

    def test_readonly_prevents_execution_and_missing_executable_fails(self):
        result = run_verification(self.workspace, python("from pathlib import Path; Path('marker.txt').touch()"),
                                  permission_mode='readonly')
        self.assertEqual(result['status'], 'error')
        self.assertFalse((self.workspace / 'marker.txt').exists())

        result = run_verification(self.workspace, 'this-freeai-check-program-does-not-exist')
        self.assertEqual(result['status'], 'error')
        self.assertIsNone(result['returncode'])

        with patch.dict(os.environ, {'PATH': str(self.workspace)}):
            result = run_verification(self.workspace, 'python -m unittest discover')
        self.assertEqual(result['status'], 'error')
        self.assertIn('可执行文件', result['message'])

    def test_cancellation_stops_a_running_child(self):
        cancel = Event()
        marker = self.workspace / 'started.txt'

        def wait_for_child():
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            cancel.set()

        stopper = Thread(target=wait_for_child)
        stopper.start()
        try:
            result = run_verification(self.workspace, python(
                "from pathlib import Path; import time; Path('started.txt').touch(); "
                "time.sleep(15); Path('finished.txt').touch()"), cancel_event=cancel, timeout=12)
        finally:
            stopper.join()
        self.assertTrue(marker.exists(), result)
        self.assertEqual(result['status'], 'stopped', result)
        self.assertFalse((self.workspace / 'finished.txt').exists())

    def test_timeout_stops_the_process(self):
        result = run_verification(self.workspace, python(
            "from pathlib import Path; import time; time.sleep(15); Path('finished.txt').touch()"), timeout=1)
        self.assertEqual(result['status'], 'timeout', result)
        self.assertFalse((self.workspace / 'finished.txt').exists())
        self.assertLess(result['duration'], 10)

    def test_mutated_or_incomplete_fingerprints_cannot_pass(self):
        result = run_verification(self.workspace, python(
            "from pathlib import Path; Path('source.py').write_text('value = 2')"))
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['returncode'], 0)
        self.assertFalse(result['current'])
        self.assertNotEqual(result['fingerprint_before'], result['fingerprint_after'])

        with patch('project_verification.FINGERPRINT_BYTES', 1):
            result = run_verification(self.workspace, python("print('directly observed')"))
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['returncode'], 0)
        self.assertFalse(result['current'])
        self.assertIsNone(result['fingerprint_before'])
        self.assertIn('directly observed', result['output'])

    def test_fingerprint_excludes_private_and_cache_paths(self):
        initial = workspace_fingerprint(self.workspace)
        self.assertTrue(initial['complete'])
        (self.workspace / '.env').write_text('fixture env', encoding='utf-8')
        cache = self.workspace / '__pycache__'
        cache.mkdir()
        (cache / 'cached.pyc').touch()
        models = self.workspace / 'models'
        models.mkdir()
        (models / 'local.gguf').write_bytes(b'fixture data')
        self.assertEqual(workspace_fingerprint(self.workspace), initial)
        self.source.write_text('value = 3\n', encoding='utf-8')
        self.assertNotEqual(workspace_fingerprint(self.workspace)['digest'], initial['digest'])

    def test_output_is_bounded_and_credentials_are_redacted(self):
        secret = 'very-private-fixture-password'
        (self.workspace / 'check.py').write_text(
            f'import os, sys\nprint("password={secret}")\n'
            'print(os.getenv("OPENAI_API_KEY", "absent"))\n'
            f'print("x" * {OUTPUT_LIMIT * 2})\nsys.exit(1)\n', encoding='utf-8')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'unrelated-fixture-key'}):
            result = run_verification(self.workspace, json.dumps([sys.executable, '-B', 'check.py']))
        self.assertEqual(result['status'], 'failed', result)
        self.assertTrue(result['output_truncated'])
        self.assertLessEqual(len(result['output'].encode('utf-8')), OUTPUT_LIMIT)
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn('unrelated-fixture-key', json.dumps(result))
        self.assertIn('absent', result['output'])
        self.assertNotIn(secret, format_verification_context(result))


if __name__ == '__main__':
    unittest.main()
