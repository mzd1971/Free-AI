"""Opt-in Windows experiment using Free AI, an official Harness executable, and a local model fixture."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from acp_transport import ACPProcess
from harness_agent import HarnessAgent, load_harness_run
import harness_settings
from remote_api import APIClient

KEY = 'freeai-harness-experiment-fixture'
MODEL = 'harness-experiment-model'
TASKS = {
    'baseline': 'Run the fixture tests before repairing the calculation.',
    'repair': 'Repair the multiplication in the fixture source.',
    'verify': 'Rerun the fixture tests.',
    'decline': 'Ask for confirmation before creating the marker.',
    'cancel': 'Wait for the model fixture so the client can cancel.',
    'after_cancel': 'Confirm that the original conversation was recovered.',
}
BROKEN = 'def total(price, quantity):\n    return price + quantity\n'
FIXED = 'def total(price, quantity):\n    return price * quantity\n'
TEST = '''
import unittest
from invoice import total


class InvoiceTest(unittest.TestCase):
    def test_total(self):
        self.assertEqual(total(7, 3), 21)
'''
TASK_PREFIX = 'HARNESS_EXPERIMENT:'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


class ModelFixture(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), ModelRequests)
        self.calls = []
        self.errors = []
        self.pending = Event()
        self.release = Event()
        interpreter = str(Path(sys.executable).resolve()).replace("'", "''")
        command = f"& '{interpreter}' -B -m unittest -v test_invoice"
        self.tools = {
            'baseline': ('pwsh', {'description': 'Run fixture tests before repair', 'command': command}),
            'repair': ('write', {'file_path': 'invoice.py', 'content': FIXED}),
            'verify': ('pwsh', {'description': 'Run fixture tests after repair', 'command': command}),
            'decline': ('pwsh', {
                'description': 'Create the denial marker',
                'command': "Set-Content -LiteralPath 'declined.txt' -Value 'ran'",
            }),
        }


class ModelRequests(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        try:
            self.complete()
        except (OSError, ValueError, AssertionError, KeyError) as error:
            self.server.errors.append(f'{type(error).__name__}: {error}')
            self.close_connection = True

    def complete(self):
        require(self.path == '/v1/chat/completions', 'Unexpected fixture endpoint.')
        require(self.headers.get('Authorization') == f'Bearer {KEY}', 'Fixture authorization mismatch.')
        size = int(self.headers['Content-Length'])
        require(0 < size < 2_000_000, 'Unexpected fixture request size.')
        body = json.loads(self.rfile.read(size))
        require(body['model'] == MODEL and body['stream'] is True, 'Wrong model or stream mode.')
        tasks = []
        for row in body['messages']:
            content = row.get('content')
            if row.get('role') == 'user' and isinstance(content, str) and content.startswith(TASK_PREFIX):
                tasks.append(content.splitlines()[0].removeprefix(TASK_PREFIX))
        require(bool(tasks), 'Missing scenario in the latest messages.')
        stage = tasks[-1]
        require(stage in TASKS, 'Unrecognized fixture scenario.')
        call_id = f'call-harness-experiment-{stage}'
        results = [row['content'] for row in body['messages']
                   if row.get('role') == 'tool' and row.get('tool_call_id') == call_id]
        self.server.calls.append({'stage': stage, 'previous_user_tasks': tasks[:-1], 'tool_results': results})
        if stage == 'cancel':
            self.server.pending.set()
            self.server.release.wait(30)
            return

        if stage in self.server.tools and not results:
            name, arguments = self.server.tools[stage]
            tools = {tool['function']['name'] for tool in body['tools']}
            require(name in tools, f'Real runtime did not offer {name}.')
            call = {'index': 0, 'id': call_id, 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(arguments)}}
            delta, stop = {'role': 'assistant', 'tool_calls': [call]}, 'tool_calls'
        else:
            delta, stop = {'role': 'assistant', 'content': f'fixture reply for {stage}'}, 'stop'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        frame = {'id': 'chatcmpl-harness-experiment', 'model': MODEL,
                 'object': 'chat.completion.chunk', 'created': 1}
        for item, reason in ((delta, None), ({}, stop)):
            row = {**frame, 'choices': [{'index': 0, 'delta': item, 'finish_reason': reason}]}
            self.wfile.write(('data: ' + json.dumps(row) + '\n\n').encode('utf-8'))
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()


class Experiment:
    def __init__(self, output, executable, fixture):
        self.output, self.executable, self.fixture = output, executable, fixture
        self.workspace = output / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'invoice.py').write_text(BROKEN, encoding='utf-8')
        (self.workspace / 'test_invoice.py').write_text(TEST, encoding='utf-8')
        self.runs = output / 'runs'
        self.model = APIClient(f'http://127.0.0.1:{fixture.server_port}/v1', MODEL, KEY)
        self.report = {'scope': 'Official Windows runtime with scripted local SSE. No real model or API credentials.',
                       'probe': None, 'turns': {}, 'wire': [], 'verified': False}

    def run(self, stage, *, parent=None, approval=True, stop=None):
        operations = []
        sent = self.report['wire']

        class RecordingTransport(ACPProcess):
            def _send(self, payload, deadline, cancel_event=None):
                super()._send(payload, deadline, cancel_event)
                frame = json.loads(payload)
                sent.append({'stage': stage, 'method': frame.get('method'), 'id': frame.get('id'),
                             'sessionId': frame.get('params', {}).get('sessionId')})

            def request(self, method, params, *, timeout=30, **kwargs):
                return super().request(method, params, timeout=min(40, timeout), **kwargs)

        def approve(request):
            operations.append(json.loads(request['operation']))
            return approval

        user_task = f'{TASK_PREFIX}{stage}\n{TASKS[stage]}'
        runner = HarnessAgent(self.workspace, self.model, executable=self.executable, runs_root=self.runs,
                              approve_command=approve, cancel_event=stop, transport_factory=RecordingTransport)
        result = runner.run(user_task, resume_from=parent['run_dir'] if parent else None)
        note = self.report['turns'][stage] = {'result': result, 'operations': operations}
        metadata = load_harness_run(self.runs, result['run_dir'], self.workspace)
        note['session'] = metadata
        require(result['status'] != 'completed' and result['task_state']['status'] != 'completed',
                f'{stage} was incorrectly marked complete.')
        require(result['continuation_available'], f'{stage} lost the continuation: {result["answer"]}')
        if parent:
            previous = load_harness_run(self.runs, parent['run_dir'], self.workspace)
            require(metadata['session_id'] == previous['session_id'] and metadata['home_id'] == previous['home_id'],
                    f'{stage} changed the session identity.')
            task, previous_task = result['task_state'], parent['task_state']
            require(task['parent_run'] == Path(parent['run_dir']).name and task['task'] == previous_task['task'],
                    f'{stage} did not inherit the parent task.')
        if stage in self.fixture.tools:
            expected, arguments = self.fixture.tools[stage]
            require(len(operations) == 1 and operations[0].get('title') == expected
                    and operations[0].get('rawInput') == arguments, f'{stage} approval omitted the operation.')
        print(f'{stage}: {result["status"]}', flush=True)
        return result

    def tool_result(self, stage):
        return '\n'.join(result for row in self.fixture.calls if row['stage'] == stage
                         for result in row['tool_results'])

    def execute(self):
        with patch.object(harness_settings, 'ARTIFACTS', self.output / 'probe'):
            self.report['probe'] = harness_settings.probe(self.executable)
        require(self.report['probe']['resume'], 'The real component does not support resume.')
        print('ACP probe: new/close/resume passed', flush=True)

        baseline = self.run('baseline')
        require('FAILED (failures=1)' in self.tool_result('baseline'), 'Baseline unittest did not fail.')
        require((self.workspace / 'invoice.py').read_text(encoding='utf-8') == BROKEN, 'Baseline altered the source.')

        repair = self.run('repair', parent=baseline)
        require((self.workspace / 'invoice.py').read_text(encoding='utf-8') == FIXED, 'The repair did not reach disk.')

        verified = self.run('verify', parent=repair)
        require('\nOK' in self.tool_result('verify').replace('\r\n', '\n'),
                'Unittest did not pass after the write.')
        require(verified['status'] == 'review', 'Passing fixture must still require review.')
        require((self.workspace / 'test_invoice.py').read_text(encoding='utf-8') == TEST, 'Fixture tests changed.')

        declined = self.run('decline', parent=verified, approval=False)
        require(declined['status'] == 'unfinished' and not (self.workspace / 'declined.txt').exists(),
                'A rejected command settlement or side effect was incorrect.')
        require('rejected' in self.tool_result('decline'), 'Harness did not report the permission rejection.')

        cancellation = Event()

        def cancel_after_model_request():
            if self.fixture.pending.wait(30):
                cancellation.set()

        stopper = Thread(target=cancel_after_model_request, daemon=True)
        stopper.start()
        try:
            stopped = self.run('cancel', parent=declined, stop=cancellation)
            require(stopped['status'] == 'stopped', 'The cancelled prompt did not settle as stopped.')
            wire = [item for item in self.report['wire'] if item['stage'] == 'cancel']
            require(any(item['method'] == 'session/cancel' and item['id'] is None
                        and item['sessionId'] == self.report['turns']['cancel']['session']['session_id']
                        for item in wire), 'No real session/cancel notification was sent.')
            require(any(item['method'] == 'session/close' for item in wire),
                    'The cancelled session was not explicitly closed.')
        finally:
            self.fixture.release.set()
        recovered = self.run('after_cancel', parent=stopped)
        require(recovered['status'] == 'review', 'The original ACP conversation did not resume after cancel.')
        self.check_history()
        self.scan_credentials()
        require(not self.fixture.errors, f'Model fixture errors: {self.fixture.errors}')
        self.report['verified'] = True

    def scan_credentials(self):
        home_id = self.report['turns']['baseline']['session']['home_id']
        home = self.output / 'harness' / 'sessions' / home_id
        suffixes = {'.json', '.jsonl', '.log', '.md', '.mjs', '.txt', '.yaml', '.yml'}
        checked, skipped = [], []
        for directory in (home, self.runs):
            for path in sorted(directory.rglob('*')):
                if not path.is_file() or path.suffix.lower() not in suffixes:
                    continue
                relative = str(path.relative_to(self.output))
                if path.stat().st_size > 2_000_000:
                    skipped.append(relative)
                    continue
                try:
                    contents = path.read_text(encoding='utf-8')
                except UnicodeDecodeError:
                    skipped.append(relative)
                    continue
                require(KEY not in contents, f'Fixture key was persisted in {relative}.')
                checked.append(relative)
        require(str((home / 'freeai.patch.yml').relative_to(self.output)) in checked,
                'The Harness patch was not covered by the fixture credential scan.')
        self.report['credential_scan'] = {'checked_text_files': checked, 'skipped_nontext_or_large': skipped}

    def check_history(self):
        for stage, prior in [('repair', ['baseline']), ('verify', ['baseline', 'repair']),
                             ('after_cancel', ['baseline', 'repair', 'verify', 'decline'])]:
            record = next((row for row in self.fixture.calls if row['stage'] == stage), None)
            require(record is not None and all(item in record['previous_user_tasks'] for item in prior),
                    f'The model request after {stage} lost the earlier conversation.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path, help='Path to the official Windows executable.')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('This experiment exercises the Windows PowerShell runtime.')
    executable = harness_settings.checked_executable(args.runtime.resolve())
    reports = ROOT / 'development' / 'reports'
    reports.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='harness-experiment-', dir=reports))
    print(f'Artifacts: {output}', flush=True)
    with ModelFixture() as fixture:
        server = Thread(target=fixture.serve_forever, daemon=True)
        server.start()
        experiment = Experiment(output, executable, fixture)
        try:
            experiment.execute()
        except Exception as error:
            experiment.report['failure'] = f'{type(error).__name__}: {error}'
            raise
        finally:
            fixture.release.set()
            fixture.shutdown()
            experiment.report['fixture_calls'] = fixture.calls
            experiment.report['fixture_errors'] = fixture.errors
            write_json(output / 'report.json', experiment.report)
    print('PASS: official ACP runtime + local model fixture', flush=True)


if __name__ == '__main__':
    main()
