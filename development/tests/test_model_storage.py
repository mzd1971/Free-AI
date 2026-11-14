"""All path/download fixtures are temporary; no real model/cache is modified."""
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import paths
from capability_manager import PackageManager


class ModelStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='model-path-test-',
            dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temp.name)
        self.app = self.root / 'FreeAI'
        self.app.mkdir()
        self.artifacts = self.app / 'portable_data'
        self.environment = patch.dict(os.environ, {'AI_TRAINING_DATA': ''})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def config(self, value, encoding='utf-8'):
        (self.app / 'model-location.txt').write_text(value, encoding=encoding)

    def resolve(self, **kwargs):
        return paths.models_root(root=self.app, artifacts=self.artifacts, **kwargs)

    def legacy(self):
        directory = self.artifacts / 'capabilities'
        directory.mkdir(parents=True)
        (directory / 'existing.gguf').write_bytes(b'existing model fixture')
        return directory

    def test_new_install_defaults_inside_application_without_creating_anything(self):
        self.assertEqual(self.resolve(), self.app / 'models')
        self.assertEqual(list(self.app.iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows drive placement')
    def test_c_drive_without_explicit_data_location_does_not_silently_scatter_files(self):
        with patch.object(Path, 'exists', return_value=False):
            with self.assertRaisesRegex(ValueError, '请将 FreeAI 安装到非 C 盘'):
                paths.storage_root(Path('C:/FreeAI'), override='')

    @unittest.skipUnless(os.name == 'nt', 'Windows drive placement')
    def test_c_drive_keeps_explicit_data_location_and_self_test_override_supported(self):
        scratch = self.root / 'self-test'
        self.assertEqual(paths.storage_root(Path('C:/FreeAI'), override=str(scratch)), scratch)
        configured = self.root / 'explicit data'
        with patch.object(Path, 'exists', return_value=True):
            with patch.object(Path, 'read_text', return_value=json.dumps({'data_root': str(configured)})):
                self.assertEqual(paths.storage_root(Path('C:/FreeAI'), override=''), configured)

    def test_chat_and_training_data_default_stay_with_application(self):
        self.assertEqual(paths.storage_root(self.app, override=''), self.app / 'portable_data')

    def test_empty_legacy_cache_does_not_override_new_default(self):
        (self.artifacts / 'capabilities').mkdir(parents=True)
        self.assertEqual(self.resolve(), self.app / 'models')

    def test_explicit_default_accepts_utf8_bom_and_one_trailing_newline(self):
        self.config('models\n', encoding='utf-8-sig')
        self.assertEqual(self.resolve(), self.app / 'models')

    def test_absolute_custom_directory_with_chinese_and_spaces(self):
        custom = self.root / '我的 模型' / '聊天能力'
        self.config(str(custom))
        self.assertEqual(self.resolve(), custom)
        self.assertFalse(custom.exists())

    def test_relative_directory_follows_application_when_moved(self):
        self.config('models')
        self.assertEqual(self.resolve(), self.app / 'models')
        relocated = self.root / '新的 FreeAI'
        self.app.rename(relocated)
        self.assertEqual(paths.models_root(root=relocated, artifacts=relocated / 'portable_data'),
                         relocated / 'models')

    def test_existing_nonempty_legacy_cache_is_reused_only_without_config(self):
        legacy = self.legacy()
        self.assertEqual(self.resolve(), legacy)
        self.assertEqual((legacy / 'existing.gguf').read_bytes(), b'existing model fixture')

    def test_explicit_default_or_external_choice_wins_over_old_cache(self):
        legacy = self.legacy()
        self.config('models')
        self.assertEqual(self.resolve(), self.app / 'models')
        external = self.root / 'external models'
        self.config(str(external))
        self.assertEqual(self.resolve(), external)
        self.assertTrue((legacy / 'existing.gguf').exists())

    def test_invalid_configuration_reports_error_without_legacy_fallback_or_overwrite(self):
        legacy = self.legacy()
        for value in ('', '   ', 'models\nsecond-directory', 'models\n\n',
                      '../outside', 'nested/../../outside', '..\\outside',
                      'D:relative-models', '\\rooted-models', 'models\x00bad',
                      'models*bad', 'models|bad', 'models:stream'):
            with self.subTest(value=value):
                self.config(value)
                with self.assertRaisesRegex(ValueError, '目录|位置|model-location'):
                    self.resolve()
                self.assertEqual((self.app / 'model-location.txt').read_text(encoding='utf-8'), value)
                self.assertTrue((legacy / 'existing.gguf').exists())

    def test_invalid_encoding_and_file_destination_fail_clearly(self):
        location = self.app / 'model-location.txt'
        location.write_bytes(b'\xff\xff')
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            self.resolve()
        destination = self.root / 'not-a-directory'
        destination.write_bytes(b'keep this')
        self.config(str(destination))
        with self.assertRaisesRegex(ValueError, '文件夹'):
            self.resolve()
        self.assertEqual(destination.read_bytes(), b'keep this')

    def test_environment_override_ignores_even_invalid_real_location(self):
        self.config('../invalid-real-setting')
        self.legacy()
        scratch = self.root / 'isolated check'
        with patch.dict(os.environ, {'AI_TRAINING_DATA': str(scratch)}):
            with patch.object(Path, 'read_text', side_effect=AssertionError('Must not read application location')):
                self.assertEqual(self.resolve(), scratch / 'models')
        self.assertFalse(scratch.exists())

    def test_explicit_override_isolated_and_wins_over_environment(self):
        scratch = self.root / 'explicit scratch'
        with patch.dict(os.environ, {'AI_TRAINING_DATA': str(self.root / 'other scratch')}):
            self.assertEqual(self.resolve(override=scratch), scratch / 'models')
        for value in ('', 'relative scratch'):
            with self.assertRaises(ValueError):
                self.resolve(override=value)

    def test_package_manager_downloads_small_verified_fixture_into_selected_model_directory(self):
        payload = b'Local model storage fixture; not real weights.'

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        target = self.root / '自选 模型文件夹'
        self.config(str(target))
        catalog = {'fixture-model': {'id': 'fixture-model', 'title': 'Fixture',
            'description': 'Local test', 'kind': 'model', 'license': 'test', 'source': 'fixture',
            'version': '1', 'entrypoint': 'fixture.gguf', 'dependencies': [],
            'assets': [{'filename': 'fixture.gguf', 'format': 'file', 'size': len(payload),
                        'sha256': hashlib.sha256(payload).hexdigest(),
                        'url': f'http://127.0.0.1:{server.server_port}/fixture.gguf'}]}}
        try:
            with patch.object(paths, 'MODELS_ROOT', self.resolve()):
                manager = PackageManager(catalog=catalog)
                installed = manager.install('fixture-model')
                self.assertEqual(manager.root, target)
                self.assertEqual(installed, target / 'installed' / 'fixture-model' / 'fixture.gguf')
                self.assertEqual(installed.read_bytes(), payload)
                self.assertTrue(manager.verify('fixture-model'))
            self.assertFalse((self.artifacts / 'capabilities').exists())
            self.assertFalse((self.app / 'models').exists())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
