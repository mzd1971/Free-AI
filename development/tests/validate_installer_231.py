"""Isolated installs exercise real directory selection; no models or accounts."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / 'development/reports'
CHECKS = ROOT / 'development/install-checks'
INSTALLER = Path('D:/FreeAI-Releases/FreeAI-Setup-2.3.1.exe')
environment = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
temporary = ROOT / 'development/tmp'
temporary.mkdir(parents=True, exist_ok=True)
environment.update(TEMP=str(temporary), TMP=str(temporary))
events = []


def run(command, timeout=90):
    completed = subprocess.run([str(value) for value in command], cwd=ROOT, env=environment,
        capture_output=True, text=True, encoding='utf-8', timeout=timeout,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if completed.returncode:
        raise AssertionError(f'Exit {completed.returncode}: {command[0]}\n{completed.stdout}\n{completed.stderr}')
    return completed.stdout


def install(directory, label, model=None, live=False):
    args = [INSTALLER, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
            '/NOCLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS', '/DIR=' + str(directory),
            '/TASKS=' + ('desktopicon,trainerdesktopicon' if live else ''),
            '/LOG=' + str(REPORTS / (label + '.log'))]
    if model is not None:
        args.append('/MODELDIR=' + str(model))
    run(args)
    events.append({'action': label, 'directory': str(directory), 'exit_code': 0})


def inspect(directory):
    command = ('import json; from paths import ROOT,ARTIFACTS,MODELS_ROOT; '
               'from capability_manager import PackageManager; '
               'print(json.dumps(dict(root=str(ROOT),data=str(ARTIFACTS),'
               'models=str(MODELS_ROOT),manager=str(PackageManager().root))))')
    completed = subprocess.run([str(directory / 'runtime/python/python.exe'), '-E', '-s', '-B', '-X', 'utf8', '-c', command],
        cwd=directory, env=environment, capture_output=True, text=True, encoding='utf-8', timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def self_checks(directory):
    for entry, name in (('Free AI.exe', 'launcher-self-test.json'), ('Free AI Trainer.exe', 'trainer-self-test.json')):
        run([directory / entry, '--self-test'], timeout=40)
        outcome = json.loads((directory / name).read_text(encoding='utf-8'))
        assert outcome['passed'] and outcome['model_storage_isolated'], outcome
        events.append({'action': 'self_test', 'entry': str(directory / entry), 'passed': True})


try:
    default = CHECKS / '全新 默认 2.3.1'
    custom = CHECKS / '自选模型 2.3.1'
    external = CHECKS / '模型仓库 中文 空格'
    assert not default.exists() and not custom.exists() and not external.exists(), 'Check folders must be new.'
    install(default, '231-fresh-default')
    found = inspect(default)
    assert Path(found['models']) == default / 'models', found
    assert Path(found['data']) == default / 'portable_data', found
    assert found['manager'] == found['models']
    assert (default / 'model-location.txt').read_text(encoding='utf-8-sig').strip() == 'models'
    self_checks(default)

    install(custom, '231-fresh-custom', external)
    found = inspect(custom)
    assert Path(found['models']) == external and found['models'] == found['manager'], found
    assert Path(found['data']) == custom / 'portable_data', found
    marker = external / 'download-retention-fixture.txt'
    marker.write_text('keep this downloaded file', encoding='utf-8')
    saved = (custom / 'model-location.txt').read_bytes()
    install(custom, '231-upgrade-preserves-choice')
    assert (custom / 'model-location.txt').read_bytes() == saved
    assert Path(inspect(custom)['models']) == external
    assert marker.read_text(encoding='utf-8') == 'keep this downloaded file'
    install(custom, '231-return-to-default', 'models')
    assert Path(inspect(custom)['models']) == custom / 'models'
    assert marker.is_file(), 'Changing the model location must not delete prior downloads.'
    data_marker = custom / 'portable_data/retention-fixture.txt'
    data_marker.parent.mkdir(exist_ok=True)
    data_marker.write_text('keep personal data', encoding='utf-8')
    model_marker = custom / 'models/retention-fixture.txt'
    model_marker.write_text('keep model data', encoding='utf-8')
    uninstaller = next(custom.glob('unins*.exe'))
    run([uninstaller, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
         '/LOG=' + str(REPORTS / '231-test-uninstall.log')])
    assert not (custom / 'Free AI.exe').exists()
    assert data_marker.is_file() and model_marker.is_file() and marker.is_file()
    events.append({'action': 'isolated_uninstall', 'models_and_data_preserved': True})

    # Restore the real installation registration only after isolated experiments.
    install(ROOT, '231-live-upgrade', 'models', live=True)
    found = inspect(ROOT)
    assert Path(found['models']) == ROOT / 'models', found
    assert Path(found['data']) == ROOT / 'portable_data', found
    self_checks(ROOT)
    before = json.loads((REPORTS / 'before-consolidation.json').read_text(encoding='utf-8-sig'))
    checked = 0
    for row in before:
        if not (row['path'].startswith('portable_data\\') or row['path'] == 'storage.json'):
            continue
        path = ROOT / row['path']
        assert path.is_file(), row['path']
        with path.open('rb') as stream:
            current = hashlib.file_digest(stream, 'sha256').hexdigest()
        assert current.upper() == row['sha256'].upper(), 'Data changed: ' + row['path']
        checked += 1
    result = {'passed': True, 'events': events, 'preserved_personal_files': checked,
              'live_paths': found, 'physical_new_computer_tested': False,
              'model_weights_downloaded': False, 'real_api_used': False}
except Exception as exc:
    result = {'passed': False, 'events': events, 'error': str(exc)}
    raise
finally:
    (REPORTS / '231-install-validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
