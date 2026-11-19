"""Install and probe checks with a tiny in-memory wheel and an isolated data root."""
import hashlib
import io
import os
from pathlib import Path
import platform
import stat
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
import zipfile

from acp_transport import ACPCancelled, ACPError
from app_state import Sessions
import harness_settings as settings


DEVELOPMENT = Path(__file__).resolve().parents[1]
LICENSE = 'fixture-1.dist-info/licenses/LICENSE'
NATIVE = b'native fixture, never executed'
WINDOWS_X64 = os.name == 'nt' and platform.machine().lower() in ('amd64', 'x86_64')


class SettingsFixture(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.enterContext(patch.object(settings, 'ARTIFACTS', self.root))
        self.download = self.enterContext(patch('harness_settings.urllib.request.urlopen',
                                                side_effect=AssertionError('unexpected download')))
        self.target = settings.default_executable()


class PreferenceAndProbeTests(SettingsFixture):
    def test_selection_is_persisted_and_invalid_paths_leave_preferences_intact(self):
        store = Sessions(self.root)
        self.assertEqual(settings.load_preferences(store), {'engine': 'builtin', 'executable': ''})
        selected = {'engine': 'harness', 'executable': str(self.target)}
        settings.save_preferences(store, selected)
        with self.assertRaises(ValueError):
            settings.save_preferences(store, {**selected, 'executable': 'relative/dsh.exe'})
        self.assertEqual(settings.load_preferences(Sessions(self.root)), selected)
        self.download.assert_not_called()

    def test_initialize_success_without_new_session_fails_probe(self):
        executable = self.root / 'dsh.exe'
        executable.touch()
        with patch('acp_transport.ACPProcess') as process:
            request = process.return_value.__enter__.return_value.request
            request.side_effect = [{'protocolVersion': 1}, ACPError('fixture session unavailable')]
            with self.assertRaises(ValueError):
                settings.probe(executable)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['initialize', 'session/new'])
        self.assertFalse(any((self.root / 'harness' / 'probes').iterdir()))
        self.download.assert_not_called()


@unittest.skipUnless(WINDOWS_X64, 'the optional installer targets Windows x64')
class InstallComponentTests(SettingsFixture):
    def setUp(self):
        super().setUp()
        self.component = self.root / 'harness' / 'component'
        self.destination = self.component / settings.RUNTIME_VERSION
        self.disk_usage = self.enterContext(patch('harness_settings.shutil.disk_usage'))
        self.disk_usage.return_value.free = 1024**3

    def wheel(self, extra_member=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as wheel:
            wheel.writestr(self.target.relative_to(self.destination).as_posix(), NATIVE)
            wheel.writestr(LICENSE, b'fixture license text')
            if extra_member is not None:
                wheel.writestr(extra_member, b'')
        content = stream.getvalue()
        self.enterContext(patch.object(settings, 'WHEEL_BYTES', len(content)))
        self.enterContext(patch.object(settings, 'WHEEL_SHA256', hashlib.sha256(content).hexdigest()))
        self.download.side_effect = lambda *args, **kwargs: io.BytesIO(content)

    def assert_unpublished(self):
        self.assertFalse(self.destination.exists())
        if self.component.exists():
            self.assertEqual(list(self.component.iterdir()), [])

    def test_hash_mismatch_rejects_before_probing(self):
        self.wheel()
        with patch.object(settings, 'WHEEL_SHA256', '0' * 64), patch.object(settings, 'probe') as probe:
            with self.assertRaises(ValueError):
                settings.install_component()
        probe.assert_not_called()
        self.assert_unpublished()

    def test_invalid_archive_entries_are_rejected_without_publishing(self):
        symlink = zipfile.ZipInfo('invalid-link')
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        for member in ('unused/../blocked.txt', symlink):
            with self.subTest(member=member), patch.object(settings, 'probe') as probe:
                self.wheel(member)
                with self.assertRaises(ValueError):
                    settings.install_component()
                probe.assert_not_called()
                self.assert_unpublished()

    def test_cancellation_before_download_and_after_probe(self):
        cancel = Event()
        cancel.set()
        with self.assertRaises(ACPCancelled):
            settings.install_component(cancel)
        self.download.assert_not_called()
        self.assert_unpublished()
        cancel.clear()
        self.wheel()

        def stop_during_probe(executable, event):
            self.assertEqual(Path(executable).read_bytes(), NATIVE)
            self.assertIs(event, cancel)
            event.set()

        with patch.object(settings, 'probe', side_effect=stop_during_probe):
            with self.assertRaises(ACPCancelled):
                settings.install_component(cancel)
        self.assert_unpublished()

    def test_session_failure_during_staged_probe_prevents_publication(self):
        self.wheel()
        for failure in ('session/new', 'session/resume'):
            with self.subTest(failure=failure), patch('acp_transport.ACPProcess') as process:
                request = process.return_value.__enter__.return_value.request
                def reply(method, params, **kwargs):
                    if method == failure:
                        raise ACPError('fixture operation unavailable')
                    if method == 'initialize':
                        return {'protocolVersion': 1, 'agentCapabilities': {'sessionCapabilities': {'resume': {}}}}
                    return {'sessionId': 'test-session'} if method == 'session/new' else {}
                request.side_effect = reply
                with self.assertRaises(ValueError):
                    settings.install_component()
                self.assertEqual(request.call_args.args[0], failure)
                self.assert_unpublished()

    def test_installs_probed_payload_and_keeps_license(self):
        self.wheel()
        with patch('acp_transport.ACPProcess') as process:
            request = process.return_value.__enter__.return_value.request
            request.side_effect = [
                {'protocolVersion': 1, 'agentCapabilities': {'sessionCapabilities': {'resume': {}}}},
                {'sessionId': 'fixture'}, {}, {}, {},
            ]
            installed = settings.install_component()
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         ['initialize', 'session/new', 'session/close', 'session/resume', 'session/close'])
        self.assertEqual(installed, settings.default_executable())
        self.assertEqual(installed.read_bytes(), NATIVE)
        self.assertEqual((self.destination / LICENSE).read_bytes(), b'fixture license text')
        self.assertEqual(list(self.component.iterdir()), [self.destination])

    def test_unhealthy_existing_installation_is_preserved(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b'already installed')
        license_file = self.destination / 'LICENSE'
        license_file.write_bytes(b'existing license')
        with patch.object(settings, 'probe', side_effect=ValueError('existing component failed probe')) as probe:
            with self.assertRaises(ValueError):
                settings.install_component()
        probe.assert_called_once_with(self.target, None)
        self.download.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'already installed')
        self.assertEqual(license_file.read_bytes(), b'existing license')
