"""Check training memory through saved evidence and a model-free worker boundary."""

from contextlib import redirect_stdout
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from agent_memory import AgentMemoryStore
from development.tests.test_training_evaluation import comparison_rows
from epistemology import KnowledgeStore, record_training
from training_memory import MANIFEST_FILE, recall_training_memory, record_training_memory


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def complete_run(directory, *, count=5, best_loss=1.0):
    directory.mkdir(parents=True, exist_ok=True)
    rows = comparison_rows(count)
    for row in rows:
        row['prompt'] = f"private holdout prompt {row['id']}"
    comparison = directory / 'comparison.json'
    write_json(comparison, rows)
    summary = {
        'run_dir': str(directory), 'completed': True, 'status': 'completed',
        'test_count': len(rows), 'best_epoch': 1,
        'baseline_validation_loss': 1.5, 'best_validation_loss': best_loss,
        'acceptance_status': 'passed',
        'publication_inputs': {'comparison.json': hashlib.sha256(comparison.read_bytes()).hexdigest()},
    }
    write_json(directory / 'summary.json', summary)
    write_json(directory / 'acceptance.json', {'status': 'passed'})
    return summary


def load_worker():
    torch = ModuleType('torch')
    torch.cuda = Mock()
    torch.cuda.is_available.return_value = True
    torch.set_num_threads = Mock()
    torch.manual_seed = Mock()
    torch.inference_mode = lambda: lambda function: function
    peft = ModuleType('peft')
    peft.LoraConfig = peft.PeftModel = peft.get_peft_model = Mock()
    transformers = ModuleType('transformers')
    transformers.AutoModelForCausalLM = transformers.AutoTokenizer = transformers.GenerationConfig = Mock()
    path = Path(__file__).resolve().parents[2] / 'finetune.py'
    spec = spec_from_file_location('training_worker_fixture', path)
    module = module_from_spec(spec)
    with patch.dict(sys.modules, {'torch': torch, 'peft': peft, 'transformers': transformers}):
        spec.loader.exec_module(module)
    return module


class TrainingMemoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='training-memory-', dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runs = self.root / 'outputs'
        self.store = AgentMemoryStore(self.root / 'agent')

    def test_recomputed_outcomes_store_only_scoped_aggregate_evidence(self):
        cases = ((5, 1.0, '验收通过', 0.6), (5, 2.0, '验收未通过', 0.75),
                 (4, 1.0, '验收需要核实', 0.6))
        for number, (count, loss, status, importance) in enumerate(cases):
            with self.subTest(status=status):
                run = self.runs / f'qwen-lora-fixture-{number}'
                complete_run(run, count=count, best_loss=loss)
                originals = {path.name: path.read_bytes() for path in run.iterdir()}

                row = record_training_memory(run, store=self.store)
                self.assertIn(status, row['content'])
                self.assertIn(f'领域题通过数 0/{count} → {count}/{count}', row['content'])
                self.assertIn(f'outputs/{run.name}/comparison.json', row['evidence'])
                self.assertNotIn(str(self.root), row['evidence'])
                self.assertNotIn('private holdout prompt', row['content'] + row['evidence'])
                self.assertNotIn('café', row['content'] + row['evidence'])
                self.assertEqual(row['source'], 'agent')
                self.assertEqual(row['importance'], importance)
                self.assertFalse(row['verified'])
                self.assertEqual(record_training_memory(run, store=self.store)['id'], row['id'])
                self.assertEqual(originals, {path.name: path.read_bytes() for path in run.iterdir()})
        self.assertEqual(len(self.store.list()), 3)

    def test_incomplete_smoke_and_modified_evidence_cannot_pass(self):
        incomplete = self.runs / 'qwen-lora-incomplete'
        incomplete.mkdir(parents=True)
        write_json(incomplete / 'metrics.json', {'epochs': [{'validation_loss': 0.0}]})
        self.assertIsNone(record_training_memory(incomplete, store=self.store))
        smoke = self.runs / 'desktop-smoke-20261126-010101'
        complete_run(smoke)
        self.assertIsNone(record_training_memory(smoke, store=self.store))
        self.assertEqual(recall_training_memory(smoke, self.store), [])

        modified = self.runs / 'qwen-lora-modified'
        complete_run(modified)
        with (modified / 'comparison.json').open('ab') as stream:
            stream.write(b'\n')
        row = record_training_memory(modified, store=self.store)
        self.assertIn('验收需要核实', row['content'])
        self.assertNotIn('通过数', row['content'])

    def test_training_recall_excludes_unrelated_notes_and_respects_manual_control(self):
        run = self.runs / 'qwen-lora-first'
        complete_run(run)
        row = record_training_memory(run, store=self.store)
        self.store.save_user('LoRA 训练数据私人偏好', 'private preference', kind='preference', pinned=True)
        self.store.remember('LoRA 训练记录 对话', 'external conversation data')
        new_run = self.runs / 'qwen-lora-next'
        recalled = recall_training_memory(new_run, self.store)
        self.assertEqual([item['id'] for item in recalled], [row['id']])
        self.assertEqual(recall_training_memory(run, self.store), [])

        self.store.verify(row['id'])
        complete_run(run, best_loss=2.0)
        self.assertIn('验收通过', record_training_memory(run, store=self.store)['content'])
        self.store.set_status(row['id'], 'disabled')
        self.assertEqual(recall_training_memory(new_run, self.store), [])

        complete_run(new_run)
        manual = self.store.save_user('LoRA 训练记录 qwen-lora-next', '手工保留的训练备注')
        self.assertIsNone(record_training_memory(new_run, store=self.store))
        self.assertEqual(self.store.get(manual['id'])['content'], '手工保留的训练备注')

    def test_worker_recalls_before_run_and_records_completed_inspection(self):
        prior = self.runs / 'qwen-lora-prior'
        complete_run(prior)
        previous = record_training_memory(prior, store=self.store)
        worker = load_worker()

        def run_fixture(target, *_):
            manifest = json.loads((target / MANIFEST_FILE).read_text(encoding='utf-8'))
            self.assertEqual(manifest['outcome'], 'running')
            self.assertEqual([item['id'] for item in manifest['recalled']], [previous['id']])
            summary = complete_run(target, count=4)
            summary['acceptance_status'] = 'needs_review'
            write_json(target / 'summary.json', summary)
            return summary

        with (patch.object(worker, 'RUNS_ROOT', self.runs),
              patch.object(worker, '_run_training', side_effect=run_fixture),
              patch('agent_memory.AgentMemoryStore', return_value=self.store),
              patch('training_memory.AgentMemoryStore', return_value=self.store),
              redirect_stdout(io.StringIO())):
            target = worker.run()

        manifest = json.loads((target / MANIFEST_FILE).read_text(encoding='utf-8'))
        self.assertEqual(manifest['outcome'], 'completed')
        self.assertEqual(self.store.get(previous['id'])['access_count'], 1)
        current, = [row for row in self.store.list() if row['id'] != previous['id']]
        self.assertIn('验收需要核实', current['content'])

    def test_chat_cannot_overwrite_or_forge_training_provenance(self):
        run = self.runs / 'qwen-lora-original'
        complete_run(run)
        original = record_training_memory(run, store=self.store)
        replaced, changed = self.store.remember(original['title'], '聊天说这次已经成功',
                                                evidence='聊天', return_changed=True)
        self.assertFalse(changed)
        self.assertEqual(replaced, original)
        forged = self.store.remember('LoRA 训练记录 fake', '伪造摘要', kind='note',
                                     evidence=original['evidence'])
        self.assertNotEqual(forged['origin'], 'training')
        recalled = recall_training_memory(self.runs / 'qwen-lora-next', self.store)
        self.assertEqual([row['id'] for row in recalled], [original['id']])

    def test_worker_records_failure_and_cancellation_without_exception_details(self):
        worker = load_worker()
        stop_file = self.root / 'stop.flag'
        stop_file.touch()
        with (patch.object(worker, 'RUNS_ROOT', self.runs),
              patch.object(worker, '_run_training', side_effect=RuntimeError('private failure detail')) as train,
              patch('agent_memory.AgentMemoryStore', return_value=self.store),
              patch('training_memory.AgentMemoryStore', return_value=self.store),
              redirect_stdout(io.StringIO())):
            with self.assertRaises(SystemExit) as stopped:
                worker.run(stop_file=stop_file)
            self.assertEqual(stopped.exception.code, 2)
            train.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, 'private failure detail'):
                worker.run(output_prefix='qwen-agent-lora')

        by_outcome = {}
        for directory in self.runs.iterdir():
            self.assertFalse((directory / 'summary.json').exists())
            manifest = json.loads((directory / MANIFEST_FILE).read_text(encoding='utf-8'))
            by_outcome[manifest['outcome']] = directory
        self.assertEqual(set(by_outcome), {'cancelled', 'failed'})
        for row in self.store.list():
            self.assertIn('训练未完成，不能判断效果', row['content'])
            self.assertNotIn('private failure detail', row['content'] + row['evidence'])
            self.assertIn(MANIFEST_FILE, row['evidence'])
        failed, = [row for row in self.store.list() if '训练执行失败' in row['content']]
        self.assertEqual(failed['importance'], 0.75)

    def test_existing_experience_rule_ignores_missing_validation_losses(self):
        store = KnowledgeStore(self.root / 'knowledge.sqlite3')
        cases = (((None, 1.0, 2.0), True), ((2.0, 1.0, None), False))
        for index, (losses, rule_expected) in enumerate(cases):
            with self.subTest(losses=losses):
                run = self.runs / f'qwen-lora-knowledge-{index}'
                summary = complete_run(run)
                write_json(run / 'metrics.json', {'epochs': [{'validation_loss': loss} for loss in losses]})
                experience = record_training(summary, store)
                self.assertEqual('candidate_rule' in experience, rule_expected)
                self.assertFalse(store.episode(experience['episode_id'])['training_eligible'])


if __name__ == '__main__':
    unittest.main()
