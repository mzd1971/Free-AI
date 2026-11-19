"""Windows checkout smoke test. Run directly using a working Python test driver."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'runtime' / 'python'


@unittest.skipUnless(os.name == 'nt', 'the shipped launchers and runtime target Windows')
class CheckoutRuntimeTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='checkout-runtime-', dir=ROOT / 'development')
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)

    def run_bundled(self, executable, *args):
        self.assertTrue(executable.is_file(), f'Missing checkout executable: {executable}')
        windows = os.environ['SystemRoot']
        # Do not let a developer's Python, Tcl library or DLL search path mask missing files.
        scratch = str(self.scratch)
        env = {'SystemRoot': windows, 'WINDIR': windows, 'PATH': str(Path(windows) / 'System32'),
               'TEMP': scratch, 'TMP': scratch, 'APPDATA': scratch, 'LOCALAPPDATA': scratch,
               'USERPROFILE': scratch, 'AI_TRAINING_DATA': scratch}
        return subprocess.run([str(executable), *args], cwd=self.scratch, env=env,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_checkout_native_modules_and_tk(self):
        code = """
import ctypes, sqlite3, ssl, sys, tkinter
import _ctypes, _sqlite3, _ssl, _tkinter
from pathlib import Path
runtime = Path(sys.argv[1]).resolve()
for module in (_ctypes, _sqlite3, _ssl, _tkinter):
    assert Path(module.__file__).resolve().is_relative_to(runtime), module.__file__
assert ctypes.sizeof(ctypes.c_uint32) == 4
ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
with sqlite3.connect(':memory:') as db:
    assert db.execute('SELECT 1').fetchone() == (1,)
window = tkinter.Tk()
try:
    window.withdraw()
    window.update_idletasks()
    for variable in ('tcl_library', 'tk_library'):
        assert Path(str(window.tk.getvar(variable))).resolve().is_relative_to(runtime), variable
finally:
    window.destroy()
"""
        result = self.run_bundled(RUNTIME / 'python.exe', '-I', '-B', '-X', 'utf8', '-c', code, str(RUNTIME))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def check_launcher(self, name, mode, report_name, log_names):
        report = ROOT / report_name
        # Native self-tests write beside the exe. Preserve existing user troubleshooting files.
        outputs = [report, *(ROOT / filename for filename in log_names)]
        previous = {path: path.read_bytes() if path.is_file() else None for path in outputs}
        previous_report_time = report.stat().st_mtime_ns if previous[report] is not None else None
        try:
            result = self.run_bundled(ROOT / name, '--self-test')
            self.assertEqual(result.returncode, 0, f'{name} --self-test exited {result.returncode}. '
                             f'{result.stdout}{result.stderr}')
            self.assertTrue(report.is_file(), f'{name} did not write a new self-test report')
            self.assertNotEqual(report.stat().st_mtime_ns, previous_report_time, 'Self-test report was not refreshed')
            details = json.loads(report.read_text(encoding='utf-8'))
            self.assertTrue(details['passed'])
            self.assertEqual(details['mode'], mode)
            self.assertEqual(Path(details['python']).resolve(), (RUNTIME / 'pythonw.exe').resolve())
            self.assertEqual(Path(details['app']).resolve(), ROOT)
            self.assertTrue(details['model_storage_isolated'])
            self.assertTrue(details['starts_without_selected_model'])
            if mode == 'client':
                self.assertTrue(details['api_startup_offline'])
        finally:
            for path, original in previous.items():
                if original is None:
                    path.unlink(missing_ok=True)
                elif not path.is_file() or path.read_bytes() != original:
                    path.write_bytes(original)

    def test_checkout_client_launcher(self):
        self.check_launcher('Free AI.exe', 'client', 'launcher-self-test.json', ('launcher-error.log', 'startup-error.log'))

    def test_checkout_trainer_launcher(self):
        self.check_launcher('Free AI Trainer.exe', 'trainer', 'trainer-self-test.json',
                            ('trainer-launcher-error.log', 'trainer-startup-error.log'))


if __name__ == '__main__':
    unittest.main()
