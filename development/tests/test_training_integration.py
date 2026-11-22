"""Exercise the trainer-to-client handoff without loading a model in tests."""
import hashlib
import json
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from capability_manager import PackageError, PackageManager
from client_app import ClientApp
from development.tests.test_training_publish import _bind as bind_fixture_run, fixture_run
from local_runtime import LocalRuntime, MissingCapability
from trainer_app import TrainerApp, load_result, result_text, training_runs
from training_publish import BASE_MODEL_ID, publish_run


class TrainingIntegrationTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='training-integration-', dir=Path(__file__).resolve().parents[1])
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.models = self.root / 'models'
        self.client_manager = PackageManager(self.models)
        self.trainer_manager = PackageManager(self.models)
        self.run = fixture_run(self.root / 'runs' / 'qwen-lora-fixture')

    def install_fixture(self, identity):
        """Stage a verified-receipt fixture; only the adapter is a real GGUF."""
        spec = self.client_manager.catalog[identity]
        entry = spec['entrypoint']
        directory = self.models / 'installed' / identity
        directory.mkdir(parents=True)
        payload = (identity + ' fixture').encode('utf-8')
        path = directory / entry
        path.write_bytes(payload)
        receipt = {'entrypoint': entry, 'fingerprint': self.client_manager._fingerprint(identity),
                   'files': {entry: {'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}}}
        (directory / 'receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
        return path

    def runtime(self):
        runtime = LocalRuntime(self.client_manager, compute_root=self.root)
        self.addCleanup(runtime.shutdown)
        return runtime

    def test_published_result_appears_in_existing_client_and_loads_with_base(self):
        self.install_fixture('engine-cpu')
        base = self.install_fixture(BASE_MODEL_ID)
        menu = Mock()
        app = ClientApp.__new__(ClientApp)
        app.root, app.model_button, app.api_settings = Mock(), Mock(), Mock()
        app.sessions, app.status, app.refresh_capabilities, app.select_page = Mock(), Mock(), Mock(), Mock()
        app.manager, app.busy, app.backend, app.selected_model = self.client_manager, False, 'local', None
        app.model_button.winfo_rootx.return_value = 10
        app.model_button.winfo_rooty.return_value = 20
        app.api_settings.public.return_value = {'model': 'fixture'}

        trainer = TrainerApp.__new__(TrainerApp)
        trainer.manager, trainer.result, trainer.busy = self.trainer_manager, load_result(self.run), False
        trainer.events, trainer.cancel_event = queue.Queue(), threading.Event()
        trainer.status, trainer.log, trainer.write = Mock(), Mock(), Mock()
        trainer.status.get.return_value = '发布完成'
        trainer.submit = Mock(side_effect=lambda title, work, done, **kwargs: done(work()))
        trainer.publish_result()
        identity, = [key for key in self.trainer_manager.catalog if key.startswith('trained-')]
        title = self.trainer_manager.catalog[identity]['title']
        self.assertIn(title, trainer.status.set.call_args.args[0])
        self.assertNotIn(identity, self.client_manager.catalog)
        with patch('client_app.tk.Menu', return_value=menu):
            app.model_menu()
        command, = [call.kwargs['command'] for call in menu.add_command.call_args_list
                    if call.kwargs['label'] == title]
        with patch('qwen_chat.shutdown'):
            command()
        self.assertEqual(app.selected_model, identity)
        app.sessions.set.assert_any_call('client_model', identity)

        runtime = self.runtime()
        process = Mock()
        process.poll.return_value = None
        with (patch('local_runtime.WindowsJob') as windows_job,
              patch('local_runtime.subprocess.Popen') as popen,
              patch.object(runtime, '_json', return_value={'status': 'ok'}),
              patch.object(runtime, '_available_port', return_value=7543)):
            windows_job.return_value.spawn.return_value = process
            popen.return_value = process
            address = runtime.ensure_server(model_id=identity)
            command, = (spawn.call_args.args[0] for spawn in (windows_job.return_value.spawn, popen)
                        if spawn.called)
            self.assertEqual(address, 'http://127.0.0.1:7543')
            self.assertEqual(command[command.index('--model') + 1], str(base))
            self.assertEqual(command[command.index('--lora') + 1], str(self.client_manager.asset_path(identity)))
            self.assertEqual(command[command.index('--alias') + 1], identity)
            self.assertEqual(runtime._in_use, {'engine-cpu', BASE_MODEL_ID, identity})
            for package_id in (BASE_MODEL_ID, identity):
                with self.subTest(package=package_id), self.assertRaisesRegex(PackageError, '正在使用'):
                    self.client_manager.uninstall(package_id)
            self.assertEqual(runtime.ensure_server(model_id=identity), address)
            self.assertEqual(windows_job.return_value.spawn.call_count + popen.call_count, 1)

        runtime.shutdown()
        self.client_manager.uninstall(identity)
        self.assertTrue(base.is_file())
        self.assertTrue((self.run / 'adapter' / 'adapter_model.safetensors').is_file())

    def test_catalog_refresh_handles_externally_removed_selection(self):
        identity = publish_run(self.trainer_manager, self.run)
        self.client_manager.refresh_trained_models()
        self.trainer_manager.uninstall(identity)

        app = ClientApp.__new__(ClientApp)
        app.manager, app.backend, app.selected_model = self.client_manager, 'local', identity
        app.cap_section, app.cap_tabs = 'hf-models', {}
        app.cap_cards, app.hf_model_panel, app.cap_area, app.cap_host = Mock(), Mock(), Mock(), Mock()
        app.setup_button, app.space, app.model_state, app.model_button = Mock(), Mock(), Mock(), Mock()
        app.layout_chat = Mock()
        app.cap_cards.winfo_children.return_value = []

        app.refresh_capabilities()

        self.assertNotIn(identity, app.package_rows)
        self.assertEqual(app.selected_model, identity)
        self.assertIn('所选模型记录已移除或损坏', app.model_state.set.call_args.args[0])
        app.setup_button.pack.assert_called_once()

    def test_missing_or_damaged_adapter_cannot_start_base_alone(self):
        self.install_fixture('engine-cpu')
        identity = publish_run(self.trainer_manager, self.run)
        runtime = self.runtime()
        with patch.object(runtime, '_start') as start:
            with self.assertRaises(MissingCapability):
                runtime.ensure_server(model_id=identity)
            self.install_fixture(BASE_MODEL_ID)
            self.assertTrue(self.client_manager.ready(identity))
            self.client_manager.asset_path(identity).write_bytes(b'broken test artifact')
            with self.assertRaises(MissingCapability):
                runtime.ensure_server(model_id=identity)
        start.assert_not_called()

    def test_cuda_start_failure_retries_same_adapter_on_cpu(self):
        self.install_fixture('engine-cuda')
        self.install_fixture('engine-cpu')
        base = self.install_fixture(BASE_MODEL_ID)
        identity = publish_run(self.trainer_manager, self.run)
        runtime = self.runtime()

        process = Mock()
        process.poll.return_value = 1

        with (patch('local_runtime.WindowsJob') as windows_job,
              patch('local_runtime.subprocess.Popen', return_value=process) as popen,
              patch.object(runtime, '_available_port', return_value=7543)):
            windows_job.return_value.spawn.return_value = process
            with self.assertRaisesRegex(RuntimeError, '未能启动'):
                runtime.ensure_server(model_id=identity)
            spawn = windows_job.return_value.spawn if windows_job.return_value.spawn.called else popen
            commands = [call.args[0] for call in spawn.call_args_list]

        self.assertEqual(len(commands), 2)
        self.assertEqual([cmd[cmd.index('--n-gpu-layers') + 1] for cmd in commands], ['99', '0'])
        for command in commands:
            self.assertEqual(command[command.index('--model') + 1], str(base))
            self.assertEqual(command[command.index('--lora') + 1], str(self.client_manager.asset_path(identity)))
        self.assertFalse(runtime._in_use)
        self.client_manager.uninstall(identity)

    def test_trainer_gates_publication_on_recomputed_acceptance(self):
        app = SimpleNamespace(busy=False, result=load_result(self.run), publish_button=Mock(),
                              status=Mock(), submit=Mock())
        self.assertEqual(app.result['acceptance']['status'], 'passed')
        TrainerApp._refresh_publish_button(app)
        app.publish_button.configure.assert_called_with(state='normal')

        comparison_path = self.run / 'comparison.json'
        rows = json.loads(comparison_path.read_text(encoding='utf-8'))
        for row in rows:
            row['after']['text'] = 'incorrect'
        comparison_path.write_text(json.dumps(rows), encoding='utf-8')
        (self.run / 'acceptance.json').write_text('{"status":"passed"}', encoding='utf-8')
        for status, label in (('needs_review', '需要核实'), ('failed', '验收未通过')):
            with self.subTest(status=status):
                if status == 'failed':
                    bind_fixture_run(self.run)
                app.result = load_result(self.run)
                record, = training_runs(self.run.parent)
                self.assertTrue(record['completed'])
                self.assertEqual(record['acceptance']['status'], status)
                self.assertIn(label, result_text(app.result))
                TrainerApp._refresh_publish_button(app)
                app.publish_button.configure.assert_called_with(state='disabled')
                TrainerApp.publish_result(app)
        app.submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
