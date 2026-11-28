"""Exercise Hub metadata and verified downloads without fetching real weights."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from capability_manager import PackageError, PackageManager
import hf_models
from local_runtime import LocalRuntime
from model_catalog import is_chat_model


REPO = 'example/Text-GGUF'
REVISION = '1' * 40
DATA = b'GGUF file fixture'
DIGEST = hashlib.sha256(DATA).hexdigest()
REMOTE_FILE = 'nested/模型 Q4.gguf'
ENCODED_FILE = 'nested/%E6%A8%A1%E5%9E%8B%20Q4.gguf'
ENGINE = b'engine fixture'


def _file(name, *, size=len(DATA), sha256=DIGEST):
    return {'rfilename': name, 'size': size, 'lfs': {'size': size, 'sha256': sha256}}


def _repo(*files, **overrides):
    return {'id': REPO, 'sha': REVISION, 'pipeline_tag': 'text-generation',
            'tags': ['gguf', 'license:apache-2.0'], 'siblings': list(files), **overrides}


def _candidate(**overrides):
    value = {'repo_id': REPO, 'revision': REVISION, 'filename': REMOTE_FILE,
             'size': len(DATA), 'sha256': DIGEST, 'license': 'apache-2.0', 'available': True}
    return {**value, **overrides}


class HuggingFaceModelTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='hf-model-test-', dir=Path(__file__).resolve().parents[1])
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def test_bundled_recommendations_are_available_as_text_choices_without_downloading(self):
        expected = (
            ('text-qwen3-1-7b', 'Qwen3-1.7B-Q8_0.gguf', 1834426016, 4, 'disabled'),
            ('text-qwen3-4b-instruct-2507', 'Qwen3-4B-Instruct-2507-Q4_K_M.gguf', 2497281120, 6, 'none'),
            ('text-code-7b', 'qwen2.5-coder-7b-instruct-q4_k_m.gguf', 4683073536, 8, 'none'),
        )
        with patch.object(hf_models, '_get_json', side_effect=AssertionError('No startup network allowed')):
            manager = PackageManager(self.root)
        runtime = LocalRuntime(manager)
        for model_id, filename, byte_size, vram, reasoning in expected:
            with self.subTest(model_id=model_id):
                self.assertTrue(is_chat_model(manager.catalog, model_id))
                self.assertFalse(manager.ready(model_id))
                spec = runtime._selection(model_id, 4096)
                self.assertEqual(spec['entrypoint'], filename)
                self.assertEqual(spec['dependencies'], ['engine-cpu'])
                self.assertEqual(spec['recommended_context'], 4096)
                self.assertEqual(spec['recommended_vram_gb'], vram)
                self.assertEqual(spec['reasoning_style'], reasoning)
                self.assertEqual(spec['chat_template_kwargs'],
                                 {'enable_thinking': False} if reasoning == 'disabled' else {})
                self.assertRegex(spec['version'], r'^[a-f0-9]{40}$')
                asset, = spec['assets']
                self.assertEqual(asset['filename'], filename)
                self.assertEqual(asset['size'], byte_size)
                self.assertEqual(asset['url'], f"{spec['quantized_source']}/resolve/{spec['version']}/{filename}")
                self.assertRegex(asset['sha256'], r'^[a-f0-9]{64}$')
                with self.assertRaisesRegex(ValueError, '只支持文字'):
                    runtime._selection(model_id, 4096, image_mode=True)

    def test_search_limits_and_encodes_query_with_list_metadata_only(self):
        requests = []
        rows = [{'id': REPO, 'downloads': 123, 'gated': 'manual'}, {'id': 'unsafe/../reponame'},
                {'id': REPO.upper()}, {'id': 'private/Model', 'private': True}]
        self._server(requests, rows=rows)
        query = '中文 + q4&sort=likes'
        self.assertEqual(hf_models.search_models(query), [
            {'repo_id': REPO, 'url': f'{hf_models._HUB}/{REPO}', 'downloads': 123,
             'likes': 0, 'pipeline_tag': '', 'gated': True},
        ])
        self.assertEqual(len(requests), 1)
        path = urlsplit(requests[0])
        self.assertEqual(path.path, '/api/models')
        self.assertEqual(parse_qs(path.query)['search'], [query])
        self.assertEqual(parse_qs(path.query)['filter'], ['gguf'])
        self.assertEqual(parse_qs(path.query)['limit'], ['20'])
        for query, limit in [('x' * 201, 20), ('model', 51), ('model', True)]:
            with self.assertRaises(PackageError):
                hf_models.search_models(query, limit)
        self.assertEqual(len(requests), 1)

    def test_files_describe_ineligible_model_artifacts(self):
        repo = _repo(_file(REMOTE_FILE), _file('mmproj-F16.gguf'), _file('q4-00001-of-00002.gguf'),
                     _file('bad.gguf', size=None, sha256='wrong'), _file('../outside.gguf'),
                     _file('escape%2fescape.gguf'), _file('README.md'))
        with patch.object(hf_models, '_get_json', return_value=repo):
            files = hf_models.model_files(REPO)
        self.assertEqual(files[0]['filename'], REMOTE_FILE)
        self.assertTrue(files[0]['available'])
        self.assertEqual(files[0]['revision'], REVISION)
        self.assertEqual(files[0]['size'], len(DATA))
        by_name = {row['filename']: row for row in files}
        self.assertEqual(len(by_name), 6)
        self.assertTrue(all(row['reason'] for row in files[1:]))
        self.assertIn('投影', by_name['mmproj-F16.gguf']['reason'])
        self.assertIn('分片', by_name['q4-00001-of-00002.gguf']['reason'])
        self.assertIsNone(by_name['bad.gguf']['size'])
        self.assertIn('SHA-256', by_name['bad.gguf']['reason'])
        self.assertIn('路径', by_name['../outside.gguf']['reason'])
        for override, reason in [({'gated': 'manual'}, '登录'), ({'pipeline_tag': 'feature-extraction'}, '任务类型')]:
            with patch.object(hf_models, '_get_json', return_value={**repo, **override}):
                result = hf_models.model_files(REPO)
                self.assertTrue(all(not item['available'] and reason in item['reason'] for item in result))

    def test_invalid_pin_or_repo_gives_a_user_facing_error(self):
        with patch.object(hf_models, '_get_json') as request:
            with self.assertRaisesRegex(PackageError, '仓库名称无效'):
                hf_models.model_files('example/../Text-GGUF')
            request.assert_not_called()
            request.return_value = {**_repo(_file(REMOTE_FILE)), 'sha': 'main'}
            with self.assertRaisesRegex(PackageError, '固定版本'):
                hf_models.model_files(REPO)

    def test_registration_is_idempotent_and_restores_without_network(self):
        manager = PackageManager(self.root)
        model_id = hf_models.add_model(manager, _candidate(context_length=2048))
        self.assertEqual(hf_models.add_model(manager, _candidate()), model_id)
        self.assertFalse(manager.ready(model_id))
        self.assertEqual(manager.catalog[model_id]['assets'][0]['url'],
                         f'https://huggingface.co/{REPO}/resolve/{REVISION}/{ENCODED_FILE}')
        self.assertEqual(manager.catalog[model_id]['dependencies'], ['engine-cpu'])
        with self.assertRaisesRegex(PackageError, '不一致'):
            hf_models.add_model(manager, _candidate(sha256='2' * 64))
        with self.assertRaises(PackageError):
            hf_models.add_model(manager, _candidate(filename='q4-00001-of-00002.gguf'))
        with patch.object(hf_models, '_get_json', side_effect=AssertionError('No startup network allowed')):
            restored = PackageManager(self.root)
        self.assertEqual(restored.catalog[model_id], manager.catalog[model_id])
        runtime = LocalRuntime(restored)
        self.assertEqual(runtime._selection(model_id, 2048)['id'], model_id)
        with self.assertRaisesRegex(ValueError, '2K'):
            runtime._selection(model_id, 4096)

    def test_malformed_descriptors_are_ignored_at_startup(self):
        manager = PackageManager(self.root)
        model_id = hf_models.add_model(manager, _candidate())
        path = self.root / 'huggingface-models' / f'{model_id}.json'
        path.write_text('{broken', encoding='utf-8')
        self.assertNotIn(model_id, PackageManager(self.root).catalog)

    def test_download_install_restore_and_chat_selection(self):
        requests = []
        self._server(requests, repo=_repo(_file(REMOTE_FILE)))
        engine = {'id': 'engine-cpu', 'title': 'Fixture engine', 'version': 'fixture',
                  'kind': 'runtime', 'entrypoint': 'engine.exe',
                  'assets': [{'filename': 'engine.exe', 'url': f'{hf_models._HUB}/fixture-engine',
                              'size': len(ENGINE), 'sha256': hashlib.sha256(ENGINE).hexdigest(), 'format': 'file'}]}
        manager = PackageManager(self.root)
        manager.catalog['engine-cpu'] = engine
        model_id = hf_models.add_model(manager, hf_models.model_files(REPO)[0])
        self.assertEqual(requests, [f'/api/models/{REPO}?blobs=true'])
        model_path = manager.install(model_id)
        self.assertEqual(model_path.read_bytes(), DATA)
        self.assertEqual(requests[1:], ['/fixture-engine', f'/{REPO}/resolve/{REVISION}/{ENCODED_FILE}'])
        restored = PackageManager(self.root)
        self.assertTrue(is_chat_model(restored.catalog, model_id))
        restored.catalog['engine-cpu'] = engine
        self.assertTrue(restored.verify(model_id))
        runtime = LocalRuntime(restored)
        command = runtime._command(restored.asset_path('engine-cpu'), model_path, 4096, False, 1025, False, model_id)
        self.assertIn(str(model_path), command)
        self.assertIn('--offline', command)
        self.assertNotIn('--mmproj', command)
        self.assertEqual(command[command.index('--alias') + 1], model_id)
        with self.assertRaisesRegex(ValueError, '只支持文字'):
            runtime._selection(model_id, 4096, True)
        restored.uninstall(model_id)
        reopened = PackageManager(self.root)
        reopened.catalog['engine-cpu'] = engine
        self.assertTrue(is_chat_model(reopened.catalog, model_id))
        self.assertFalse(reopened.ready(model_id))
        self.assertEqual(reopened.install(model_id).read_bytes(), DATA)
        self.assertEqual(requests[3:], [f'/{REPO}/resolve/{REVISION}/{ENCODED_FILE}'])

    def _server(self, requests, *, rows=None, repo=None):
        responses = {'/api/models': json.dumps(rows).encode(),
                     f'/api/models/{REPO}': json.dumps(repo).encode(),
                     f'/{REPO}/resolve/{REVISION}/{ENCODED_FILE}': DATA, '/fixture-engine': ENGINE}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append(self.path)
                payload = responses.get(urlsplit(self.path).path)
                if payload is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        thread.start()
        host = patch.object(hf_models, '_HUB', f'http://127.0.0.1:{server.server_port}')
        host.start()
        self.addCleanup(host.stop)


if __name__ == '__main__':
    unittest.main()
