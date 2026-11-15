"""Validate the recovered packaging boundary without reading user configuration."""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

DEVELOPMENT = Path(__file__).resolve().parents[1]


def latest_stage(test):
    report = DEVELOPMENT / 'reports/latest-build.json'
    if not report.exists():
        test.skipTest('Run build_lite.ps1 -PrepareOnly first.')
    build = json.loads(report.read_text(encoding='utf-8-sig'))
    if build['version'] != '2.8.0':
        test.skipTest('Prepare the 2.8.0 payload first.')
    return build, Path(build['stage'])


class PackagingTests(unittest.TestCase):
    def test_installer_identity_and_model_location_contract(self):
        source = (DEVELOPMENT / 'installer/free-ai-lite.iss').read_text(encoding='utf-8')
        self.assertIn('AppId={{3EAACF9D-4709-48F0-AF84-0275969D1600}', source)
        self.assertNotIn('[InstallDelete]', source)
        self.assertNotIn('[UninstallDelete]', source)
        self.assertIn('PrivilegesRequired=lowest', source)
        self.assertIn('#define AppVersion "2.8.0"', source)
        self.assertIn('SaveStringsToUTF8File', source)
        self.assertIn('LoadStringsFromFile', source)
        self.assertIn('{param:MODELDIR|}', source)
        self.assertIn('model-location.txt', source)
        self.assertIn("'D:\\FreeAI'", source)
        self.assertNotIn('storage.json', source)

    def test_payload_has_code_but_no_models_settings_or_development(self):
        report, stage = latest_stage(self)
        self.assertTrue(report['excludes_model_location'])
        for name in ('Free AI.exe', 'Free AI Trainer.exe', 'api_panel.py', 'api_settings.py',
                     'remote_api.py', 'app_skills.py', 'model_catalog.py', 'paths.py',
                     'client_settings.py', 'coding_tools.py', 'ui_preferences.py', 'hf_models.py', 'hf_model_panel.py', 'hf_datasets.py', 'hf_dataset_panel.py', 'hf_dataset_catalog.py',
                     'project_agent.py', 'project_chat.py', 'local_project_model.py',
                     'agent_workspace.py', 'agent_task_state.py', 'agent_review.py',
                     'agent_memory.py', 'memory_learning.py', 'memory_panel.py',
                     'assets/free-ai.ico', 'assets/free-ai-ui.png',
                     'runtime/python/Lib/venv/__init__.py', 'runtime/python/Lib/ensurepip/__init__.py'):
            self.assertTrue((stage / name).is_file(), name)
        forbidden = {'models', 'portable_data', 'development', 'site-packages', '__pycache__',
                     'capabilities', 'datasets', 'outputs', 'training-tools'}
        private = re.compile(r'(^|[-_.])(api[-_]?connection|api[-_]?settings|settings|secrets?|credentials?|api[-_]?keys?|tokens?)([-_.]|$)|^\.env($|\.)', re.I)
        for path in stage.rglob('*'):
            self.assertFalse(forbidden.intersection(path.relative_to(stage).parts), str(path))
            if path.is_file():
                self.assertNotIn(path.name, {'storage.json', 'model-location.txt', 'tokenizer.json', 'config.json'})
                self.assertNotIn(path.suffix.lower(), {'.gguf','.safetensors','.pt','.pth','.onnx','.db','.sqlite3','.key','.dpapi'})
                if path.suffix not in ('.py', '.pyi'):
                    self.assertIsNone(private.search(path.name), str(path))
        self.assertLess(report['stage_bytes'], 60_000_000)

    def test_both_apps_start_offline_and_use_isolated_model_storage(self):
        _, stage = latest_stage(self)
        environment = {name: os.environ[name] for name in ('SystemRoot','WINDIR') if name in os.environ}
        environment.update(TEMP=str(stage), TMP=str(stage))
        code = '''from unittest.mock import patch
import free_ai_bootstrap
with patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected request')) as request, patch('socket.create_connection', side_effect=AssertionError('Unexpected connection')) as connection:
    free_ai_bootstrap.self_test(trainer=TRAINER)
    request.assert_not_called()
    connection.assert_not_called()
'''
        for trainer in (False, True):
            with self.subTest(trainer=trainer):
                result = subprocess.run([str(stage/'runtime/python/python.exe'), '-E','-s','-B','-X','utf8','-c',code.replace('TRAINER',repr(trainer))], cwd=stage, env=environment, capture_output=True, text=True, encoding='utf-8', timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads((stage / ('trainer-self-test.json' if trainer else 'launcher-self-test.json')).read_text(encoding='utf-8'))
                self.assertTrue(report['model_storage_isolated'])
                self.assertTrue(report['starts_without_selected_model'])
                if not trainer:
                    self.assertTrue(report['api_startup_offline'])
                    self.assertTrue(report['api_default_unselected'])
                    self.assertEqual(report['skill_options'], 10)


if __name__ == '__main__':
    unittest.main()
