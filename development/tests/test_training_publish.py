"""Publication tests use synthetic LoRA matrices and scored fixture answers."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from capability_manager import PackageManager
from training_gguf import _tensor_layout
import training_publish as publication


def _json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def _bind(run):
    summary = json.loads((run / 'summary.json').read_text(encoding='utf-8'))
    summary['publication_inputs'] = {
        name: hashlib.sha256((run / name).read_bytes()).hexdigest() for name in publication.SOURCE_INPUTS
    }
    _json(run / 'summary.json', summary)


def fixture_run(run):
    run = Path(run)
    (run / 'adapter').mkdir(parents=True)
    _json(run / 'run_config.json', {
        'base_model': publication.BASE_MODEL, 'revision': publication.TRAINING_REVISION,
        'base_model_sha256': publication.TRAINING_WEIGHT_SHA256,
        'base_snapshot': 'D:/fixture/model',
    })
    _json(run / 'adapter/adapter_config.json', {
        'peft_type': 'LORA', 'task_type': 'CAUSAL_LM', 'bias': 'none',
        'target_modules': ['v_proj', 'q_proj'], 'r': 8, 'lora_alpha': 16,
        'base_model_name_or_path': 'D:\\fixture\\model', 'revision': None,
    })
    header = {'__metadata__': {'format': 'pt'}}
    raw = bytearray()
    for name, _, (rows, columns) in _tensor_layout(8):
        size = rows * columns * 4
        header[name] = {'dtype': 'F32', 'shape': [rows, columns], 'data_offsets': [len(raw), len(raw) + size]}
        raw.extend(struct.pack('<f', 0.01))
        raw.extend(bytes(size - 4))
    header = json.dumps(header).encode('utf-8')
    header += b' ' * (-len(header) % 8)
    (run / 'adapter/adapter_model.safetensors').write_bytes(struct.pack('<Q', len(header)) + header + raw)

    rows = []
    for group in ['domain', 'general']:
        for index in range(5):
            identity = f'{group}-{index}'
            rows.append({
                'id': identity, 'prompt': f'Fixture question {identity}', 'group': group,
                'response': 'A', 'evaluation': {'type': 'exact'},
                'before': {'text': 'B', 'new_tokens': 1, 'hit_token_limit': False},
                'after': {'text': 'A', 'new_tokens': 1, 'hit_token_limit': False},
            })
    _json(run / 'comparison.json', rows)
    _json(run / 'summary.json', {'completed': True, 'test_count': len(rows), 'best_epoch': 1,
                                 'baseline_validation_loss': 1.5, 'best_validation_loss': 1.0,
                                 'acceptance_status': 'passed'})
    _bind(run)
    return run


class TrainingPublicationTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='freeai-training-publish-', dir=Path(__file__).resolve().parents[1])
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.run_dir = fixture_run(self.root / 'fixture')
        self.manager = PackageManager(self.root / 'models')

    def test_publish_reopen_refresh_and_remove(self):
        inspection = publication.inspect_run(self.manager, self.run_dir)
        self.assertEqual(inspection['acceptance']['status'], 'passed')
        watcher = PackageManager(self.manager.root)
        identity = publication.publish_run(self.manager, self.run_dir, name='本地发布测试')
        self.assertEqual(identity, inspection['identity'])
        spec = self.manager.catalog[identity]
        self.assertEqual(spec['dependencies'], [publication.BASE_MODEL_ID])
        self.assertEqual(spec['title'], '本地发布测试')
        self.assertEqual(spec['assets'], [])
        self.assertTrue(publication.ready(self.manager, identity, full=True))
        self.assertFalse(self.manager.ready(identity), 'stock GGUF and engine are absent in this scratch directory')
        self.assertNotIn(identity, watcher.catalog)
        watcher.refresh_trained_models()
        watcher.refresh_trained_models()
        self.assertEqual(watcher.catalog[identity], spec)

        output = publication.asset_path(self.manager, identity)
        with output.open('rb') as stream:
            self.assertEqual(struct.unpack('<4sIQQ', stream.read(24)), (b'GGUF', 3, 112, 4))
        record = json.loads(output.with_name('publication.json').read_text(encoding='utf-8'))
        self.assertEqual(record['source_sha256'], inspection['source_sha256'])
        self.assertEqual(record['artifact']['sha256'], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(publication.publish_run(watcher, self.run_dir), identity)
        self.assertEqual(watcher.catalog[identity]['title'], '本地发布测试')
        self.assertEqual(PackageManager(self.manager.root).catalog[identity], spec)

        watcher.uninstall(identity)
        self.assertFalse(output.exists())
        self.assertTrue((self.run_dir / 'adapter/adapter_model.safetensors').is_file())
        self.manager.refresh_trained_models()
        self.assertNotIn(identity, self.manager.catalog)

    def test_rechecks_acceptance_instead_of_trusting_saved_status(self):
        comparison = self.run_dir / 'comparison.json'
        rows = json.loads(comparison.read_text(encoding='utf-8'))
        for row in rows:
            row['after']['text'] = 'incorrect'
        _json(comparison, rows)
        _json(self.run_dir / 'acceptance.json', {'status': 'passed'})
        _bind(self.run_dir)
        self.assertEqual(publication.inspect_run(self.manager, self.run_dir)['acceptance']['status'], 'failed')
        with self.assertRaisesRegex(publication.PublishError, '尚未通过'):
            publication.publish_run(self.manager, self.run_dir)
        self.assertFalse((self.manager.root / 'trained').exists())

    def test_rejects_changed_source_and_incompatible_base(self):
        config = self.run_dir / 'run_config.json'
        data = json.loads(config.read_text(encoding='utf-8'))
        data['revision'] = '0' * 40
        _json(config, data)
        with self.assertRaisesRegex(publication.PublishError, '文件已改变'):
            publication.publish_run(self.manager, self.run_dir)
        _bind(self.run_dir)
        with self.assertRaisesRegex(publication.PublishError, '基座不匹配'):
            publication.publish_run(self.manager, self.run_dir)

    def test_rejects_invalid_tensor_data_even_if_source_hashes_match(self):
        weight = self.run_dir / 'adapter/adapter_model.safetensors'
        raw = bytearray(weight.read_bytes())
        raw[-4:] = struct.pack('<f', float('nan'))
        weight.write_bytes(raw)
        _bind(self.run_dir)
        with self.assertRaisesRegex(publication.PublishError, '非有限数值'):
            publication.publish_run(self.manager, self.run_dir)

    def test_interrupted_publication_leaves_no_artifact(self):
        cancel = threading.Event()

        def progress(event):
            if event['phase'] == 'converting':
                cancel.set()

        with self.assertRaisesRegex(publication.PublishError, '已停止发布'):
            publication.publish_run(self.manager, self.run_dir, progress=progress, cancel_event=cancel)
        with patch.object(publication, 'write_adapter', side_effect=OSError('fault injection')):
            with self.assertRaisesRegex(publication.PublishError, '无法写入'):
                publication.publish_run(self.manager, self.run_dir)
        self.assertFalse(list((self.manager.root / 'staging').iterdir()))
        self.assertEqual(publication.load_specs(self.manager), {})

    def test_corrupted_adapter_is_not_ready_and_can_be_republished(self):
        identity = publication.publish_run(self.manager, self.run_dir)
        output = publication.asset_path(self.manager, identity)
        with output.open('r+b') as stream:
            stream.seek(-4, 2)
            stream.write(b'BAD!')
        self.assertFalse(publication.ready(self.manager, identity, full=True))
        self.assertEqual(publication.publish_run(self.manager, self.run_dir), identity)
        self.assertTrue(publication.ready(self.manager, identity, full=True))


if __name__ == '__main__':
    unittest.main()
