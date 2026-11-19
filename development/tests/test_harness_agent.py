"""Run the Harness adapter against an ACP peer with durable fixture sessions."""

import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
from threading import Event, Lock
import unittest
from unittest.mock import patch

from acp_transport import ACPProcess
from harness_agent import HarnessAgent, MAX_TASK_BYTES, load_harness_run, resolve_resume, validate_task
from local_project_model import LocalProjectModel
from remote_api import APIClient


DEVELOPMENT = Path(__file__).resolve().parents[1]
PEER = r"""
import json
import os
from pathlib import Path
import subprocess
import sys


def read():
    line = sys.stdin.buffer.readline()
    if not line:
        raise SystemExit(0)
    return json.loads(line)


def send(**contents):
    message = {"jsonrpc": "2.0", **contents}
    sys.stdout.buffer.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def reply(request, **result):
    send(id=request["id"], result=result)


def update(session, **fields):
    send(method="session/update", params={"sessionId": session, "update": fields})


action, name = sys.argv[1:]
data_file = Path(os.environ["DSH_HOME"]) / "fixture-session.json"
identity = "fixture-session-123"
initialize = read()
assert initialize["method"] == "initialize"
reply(initialize, protocolVersion=1, agentCapabilities={"sessionCapabilities": {"resume": {}}})
session = read()
params = session["params"]
if session["method"] == "session/new":
    assert not data_file.exists()
    state = {"sessionId": identity, "cwd": params["cwd"], "turns": [], "changes": []}
    reply(session, sessionId=identity)
elif session["method"] == "session/resume":
    if not data_file.exists():
        send(id=session["id"], error={"code": -32602, "message": "Session unavailable"})
        raise SystemExit(0)
    state = json.loads(data_file.read_text(encoding="utf-8"))
    assert params["sessionId"] == state["sessionId"] == identity
    assert params["cwd"] == state["cwd"]
    reply(session)
else:
    raise AssertionError(session["method"])

prompt = read()
assert prompt["method"] == "session/prompt"
assert prompt["params"]["sessionId"] == identity
assert prompt["params"]["prompt"][0]["type"] == "text"
if action == "check_environment":
    assert "AWS_SECRET_ACCESS_KEY" not in os.environ
    assert "GITHUB_TOKEN" not in os.environ
    assert "NODE_OPTIONS" not in os.environ
    assert os.environ["DSH_TELEMETRY_MODE"] == "DISABLED"
if action == "check_secret":
    key = os.environ["FREEAI_DSH_MODEL_KEY"]
    assert key not in prompt["params"]["prompt"][0]["text"]

if action in ("write", "deny", "fail", "cancel", "bad_approval", "unknown"):
    call = {"toolCallId": "fixture-op", "title": "Write " + name, "kind": "execute",
            "rawInput": {"name": name}, "status": "pending"}
    if action != "unknown":
        update(identity, sessionUpdate="tool_call", **call)
    send(id="permission", method="session/request_permission",
         params={"sessionId": identity, "toolCall": call,
                 "options": [{"kind": "allow_once", "optionId": "write-once"},
                             {"kind": "reject_once", "optionId": "reject-once"}]})
    answer = read()
    assert answer["id"] == "permission"
    outcome = answer["result"]["outcome"]
    if outcome.get("optionId") == "write-once":
        if action == "fail":
            command = [sys.executable, "-B", "-c", "raise SystemExit(4)"]
        else:
            command = [sys.executable, "-B", "-c",
                       "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('written',encoding='utf-8')",
                       name]
        result = subprocess.run(command, cwd=state["cwd"], shell=False, check=False, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode == 0:
            state["changes"].append(name)
        update(identity, sessionUpdate="tool_call_update", toolCallId=call["toolCallId"],
               status="completed" if result.returncode == 0 else "failed")
    elif action != "unknown":
        assert outcome.get("optionId") == "reject-once"
        update(identity, sessionUpdate="tool_call_update", toolCallId=call["toolCallId"], status="completed")

if action == "long_task":
    state["received_prompt"] = prompt["params"]["prompt"][0]["text"]
state["turns"].append(name)
if action == "cancel":
    update(identity, sessionUpdate="agent_message_chunk", content={"type": "text", "text": "cancel-ready"})
    cancellation = read()
    assert cancellation["method"] == "session/cancel" and cancellation["params"]["sessionId"] == identity
    reply(prompt, stopReason="cancelled")
else:
    update(identity, sessionUpdate="agent_message_chunk", content={"type": "text", "text": "fixture reply"})
    reply(prompt, stopReason="end_turn")

close = read()
assert close["method"] == "session/close" and close["params"]["sessionId"] == identity
if action == "close_failure":
    send(id=close["id"], error={"code": -32603, "message": "Close failed"})
else:
    data_file.write_text(json.dumps(state), encoding="utf-8")
    reply(close)
"""


class FixtureTransport(ACPProcess):
    def request(self, method, params, *, timeout=30, **options):
        return super().request(method, params, timeout=min(timeout, 4), **options)


class FixtureRuntime:
    def __init__(self, port):
        self._request_lock = Lock()
        self._token = 'fixture-local-key-17'
        self.port = port
        self.starts = []

    def ensure_server(self, context, image_mode, cancel_event, model_id):
        if not self._request_lock.locked():
            raise AssertionError('Local model must be held for the external turn.')
        self.starts.append((context, image_mode, model_id))
        return f'http://127.0.0.1:{self.port}'

    def _alias(self, model_id):
        return f'fixture-{model_id}'


class HarnessAgentTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        self.runs = self.root / 'runs'
        self.peer = self.root / 'peer.py'
        self.peer.write_text(PEER, encoding='utf-8')
        self.events = []
        self.connections = []
        self.model = APIClient('https://example.test/v1', 'fixture-model', 'fixture-api-key-17')

    def runner(self, action, name, *, approve=lambda _: True, cancel_event=None, model=None, **options):
        active_model = model or self.model

        def on_event(event):
            self.events.append(event)
            if cancel_event is not None and event.get('type') == 'harness_text':
                cancel_event.set()

        def transport(argv, *, cwd, env, **callbacks):
            self.assertEqual(argv[:3], [sys.executable, '--profile', 'acp'])
            self.assertEqual(argv[3], '--patch')
            profile = Path(argv[4])
            self.assertEqual(profile.parent, Path(env['DSH_HOME']))
            self.assertTrue(Path(env['DSH_HOME']).is_relative_to(self.root))
            self.assertTrue(Path(env['TEMP']).is_relative_to(self.root))
            self.assertEqual(Path(env['TEMP']), Path(env['TMP']))
            if os.name == 'nt':
                self.assertTrue(Path(cwd).samefile(os.environ['SystemRoot']))
            patch_data = profile.read_text(encoding='utf-8')
            self.assertIn('FREEAI_DSH_MODEL_KEY', patch_data)
            key = env['FREEAI_DSH_MODEL_KEY']
            self.assertNotIn(key, patch_data)
            if isinstance(active_model, LocalProjectModel):
                self.assertTrue(active_model._runtime._request_lock.locked())
                self.assertIn(f'http://127.0.0.1:{active_model._runtime.port}/v1', patch_data)
            connection = FixtureTransport([sys.executable, '-B', '-u', str(self.peer), action, name],
                                    cwd=cwd, env=env, **callbacks)
            self.connections.append(connection)
            return connection

        return HarnessAgent(self.workspace, active_model, executable=sys.executable, runs_root=self.runs,
                            on_event=on_event, approve_command=approve, cancel_event=cancel_event,
                            transport_factory=transport, **options)

    def metadata(self, result):
        return load_harness_run(self.runs, result['run_dir'], self.workspace)

    def session(self, result):
        identity = self.metadata(result)['home_id']
        path = self.root / 'harness' / 'sessions' / identity / 'fixture-session.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def test_cancelled_command_turn_can_resume_same_durable_session_twice(self):
        cancellation = Event()
        first = self.runner('cancel', 'first.txt', cancel_event=cancellation).run('Write the first file.')
        self.assertEqual(first['status'], 'stopped')
        self.assertTrue(first['continuation_available'])
        self.assertEqual((self.workspace / 'first.txt').read_text(encoding='utf-8'), 'written')
        self.assertEqual(self.session(first)['changes'], ['first.txt'])

        second = self.runner('write', 'second.txt').run('Keep working.', resume_from=first['run_dir'],
                                                       recovery_confirmed=True)
        third = self.runner('write', 'third.txt').run('Keep working.', resume_from=second['run_dir'])
        self.assertEqual([second['status'], third['status']], ['review', 'review'])
        self.assertEqual(self.session(third)['changes'], ['first.txt', 'second.txt', 'third.txt'])
        self.assertEqual(self.metadata(first)['session_id'], self.metadata(third)['session_id'])
        self.assertEqual(self.metadata(first)['home_id'], self.metadata(third)['home_id'])
        self.assertEqual(third['task_state']['parent_run'], Path(second['run_dir']).name)
        self.assertTrue(all(connection._process.poll() is not None for connection in self.connections))

    def test_long_task_reaches_peer_and_survives_in_redacted_run_record(self):
        goal = '中' * 5400 + ' TAIL_ACCEPTANCE_MUST_BE_PRESENT'
        result = self.runner('long_task', 'long').run(goal)
        self.assertIn(goal, self.session(result)['received_prompt'])
        self.assertEqual((Path(result['run_dir']) / 'request.txt').read_text(encoding='utf-8'), goal)

    def test_invalid_inputs_leave_no_task_or_component(self):
        validate_task('a' * MAX_TASK_BYTES)
        for invalid in (' ', 'a' * (MAX_TASK_BYTES + 1), '\ud800'):
            with self.subTest(size=len(invalid)), self.assertRaises(ValueError):
                self.runner('reply', 'never').run(invalid)
        for invalid in (1, True):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self.runner('reply', 'never').run('Start', recovery_confirmed=invalid)
        self.assertFalse(self.runs.exists())
        self.assertFalse(self.connections)

    def test_unavailable_model_retains_current_request_without_losing_the_existing_head(self):
        first = self.runner('reply', 'one').run('Start.')
        runtime = FixtureRuntime(21987)
        model = LocalProjectModel(runtime=runtime, model_id='held')
        task = 'Keep the complete request. ' + 'x' * 16384 + ' END'
        with runtime._request_lock:
            failure = self.runner('reply', 'never', model=model).run(task, resume_from=first['run_dir'])
        self.assertEqual(failure['status'], 'failed')
        self.assertEqual((Path(failure['run_dir']) / 'request.txt').read_text(encoding='utf-8'), task)
        self.assertEqual(resolve_resume(self.runs, first['run_dir'], self.workspace), Path(first['run_dir']))

    def test_unrelated_resumed_prompt_preserves_interrupted_operation(self):
        first = self.runner('cancel', 'first.txt', cancel_event=Event()).run('Write and verify first.txt.')
        self.assertTrue(first['task_state']['uncertain'])
        second = self.runner('reply', 'hello').run('Just say hello.', resume_from=first['run_dir'])
        self.assertTrue(second['task_state']['uncertain'])
        self.assertEqual(second['status'], 'unfinished')
        third = self.runner('reply', 'confirmed').run('Continue.', resume_from=second['run_dir'], recovery_confirmed=True)
        self.assertFalse(third['task_state']['uncertain'])
        self.assertIn('user_confirmation', [row['tool'] for row in third['task_state']['observations']])

    def test_old_card_attaches_to_latest_journal_in_same_session(self):
        a = self.runner('write', 'a.txt').run('Write A.')
        b = self.runner('write', 'b.txt').run('Write B.', resume_from=a['run_dir'])
        c = self.runner('reply', 'c').run('Continue.', resume_from=a['run_dir'])
        self.assertEqual(self.session(c)['turns'], ['a.txt', 'b.txt', 'c'])
        self.assertEqual(c['task_state']['parent_run'], Path(b['run_dir']).name)
        self.assertIn('b.txt', json.dumps(c['task_state']['observations']))
        self.assertEqual(resolve_resume(self.runs, a['run_dir'], self.workspace), Path(c['run_dir']))
        stale = self.runner('reply', 'never')
        with self.assertRaises(ValueError):
            stale.run('Confirm old record.', resume_from=a['run_dir'], recovery_confirmed=True)
        self.assertIsNone(stale.run_dir)

    def test_concurrent_resume_leaves_one_session_writer(self):
        a = self.runner('write', 'a.txt').run('Write A.')
        entered, release = Event(), Event()
        def approve(_):
            entered.set()
            return release.wait(4)
        b = self.runner('write', 'b.txt', approve=approve)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(b.run, 'Write B.', resume_from=a['run_dir'])
            try:
                self.assertTrue(entered.wait(3))
                c = self.runner('reply', 'never')
                with self.assertRaisesRegex(ValueError, '锁定'):
                    c.run('Continue.', resume_from=a['run_dir'])
                self.assertIsNone(c.run_dir)
            finally:
                release.set()
            result = pending.result(timeout=5)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(len(self.connections), 2)

    def test_legacy_linear_history_migrates_without_modifying_the_parent_runs(self):
        a = self.runner('write', 'a.txt').run('Write A.')
        b = self.runner('write', 'b.txt').run('Write B.', resume_from=a['run_dir'])
        for run in (a, b):
            manifest = Path(run['run_dir']) / 'harness.json'
            old = json.loads(manifest.read_text(encoding='utf-8'))
            del old['lineage_version']
            manifest.write_text(json.dumps(old), encoding='utf-8')
        pointer = self.root / 'harness' / 'lineages' / Path(a['run_dir']).name / 'latest.json'
        pointer.unlink()
        self.assertEqual(resolve_resume(self.runs, a['run_dir'], self.workspace), Path(b['run_dir']))
        c = self.runner('reply', 'c').run('Continue.', resume_from=a['run_dir'])
        self.assertEqual(c['task_state']['parent_run'], Path(b['run_dir']).name)
        self.assertNotIn('lineage_version', self.metadata(a))
        self.assertEqual(json.loads(pointer.read_text()), Path(c['run_dir']).name)

    def test_ambiguous_legacy_branches_require_inspection(self):
        runs = []
        for name in ('a', 'b', 'c'):
            runs.append(self.runner('reply', name).run('Continue.', resume_from=runs[-1]['run_dir'] if runs else None))
        a, b, c = (Path(item['run_dir']) for item in runs)
        for directory in (a, b, c):
            metadata = directory / 'harness.json'
            old = json.loads(metadata.read_text(encoding='utf-8'))
            del old['lineage_version']
            metadata.write_text(json.dumps(old), encoding='utf-8')
        # In the legacy format B and C could both claim A as their parent.
        state = c / 'task.json'
        checkpoint = json.loads(state.read_text(encoding='utf-8'))
        checkpoint['parent_run'] = a.name
        state.write_text(json.dumps(checkpoint), encoding='utf-8')
        (self.root / 'harness' / 'lineages' / a.name / 'latest.json').unlink()
        with self.assertRaisesRegex(ValueError, '分支'):
            resolve_resume(self.runs, a, self.workspace)
        self.assertEqual(len(self.connections), 3)

    def test_denied_failed_and_unseen_operations_never_complete(self):
        for action in ('deny', 'fail', 'unknown', 'bad_approval'):
            with self.subTest(action=action):
                name = action + '.txt'
                approvals = []

                def approve(request):
                    approvals.append(request)
                    return {'deny': False, 'fail': True, 'unknown': True, 'bad_approval': 'yes'}[action]

                outcome = self.runner(action, name, approve=approve).run('Do the fixture operation.')
                self.assertEqual(outcome['status'], 'unfinished')
                self.assertFalse((self.workspace / name).exists())
                if action == 'unknown':
                    self.assertEqual(approvals, [])
                else:
                    self.assertEqual(len(approvals), 1)
                    self.assertIn(name, approvals[0]['operation'])

    def test_close_error_does_not_report_a_settled_review(self):
        outcome = self.runner('close_failure', 'failure').run('Finish the fixture.')
        self.assertNotIn(outcome['status'], ('review', 'completed'))

    def test_local_route_survives_new_loopback_port_for_same_selected_model(self):
        runtime1, runtime2 = FixtureRuntime(20121), FixtureRuntime(20122)
        selected1 = LocalProjectModel(runtime=runtime1, model_id='chosen-model')
        selected2 = LocalProjectModel(runtime=runtime2, model_id='chosen-model')
        first = self.runner('write', 'local-first.txt', model=selected1).run('Make first file.')
        self.assertEqual(first['status'], 'review')
        second = self.runner('write', 'local-second.txt', model=selected2).run('Make another file.',
                                                                               resume_from=first['run_dir'])
        self.assertEqual(second['status'], 'review')
        self.assertEqual(self.session(second)['changes'], ['local-first.txt', 'local-second.txt'])
        for runtime in (runtime1, runtime2):
            self.assertEqual(runtime.starts, [(16384, False, 'chosen-model')])
            self.assertFalse(runtime._request_lock.locked())

    def test_occupied_local_model_lock_never_launches_peer(self):
        runtime = FixtureRuntime(20125)
        model = LocalProjectModel(runtime=runtime, model_id='other-model')
        with runtime._request_lock:
            outcome = self.runner('write', 'never.txt', model=model).run('Use occupied runtime.')
            self.assertEqual(outcome['status'], 'failed')
            self.assertTrue(runtime._request_lock.locked())
        self.assertEqual(self.connections, [])
        self.assertEqual(runtime.starts, [])

    def test_child_environment_uses_explicit_allowlist(self):
        extra_environment = {'AWS_SECRET_ACCESS_KEY': 'fixture-aws-key',
                             'GITHUB_TOKEN': 'fixture-github-token',
                             'NODE_OPTIONS': 'fixture-node-options'}
        with patch.dict(os.environ, extra_environment):
            outcome = self.runner('check_environment', 'environment').run('Inspect fixture environment.')
        self.assertEqual(outcome['status'], 'review')
        self.assertEqual(self.session(outcome)['turns'], ['environment'])

    def test_unknown_permission_mode_is_rejected_before_launch(self):
        with self.assertRaises(ValueError):
            self.runner('write', 'never.txt', permission_mode='unrecognized')
        self.assertEqual(self.connections, [])
        self.assertFalse(self.runs.exists())

    def test_exact_api_credential_is_absent_from_saved_progress(self):
        key = 'fixture-raw-key-17'
        model = APIClient('https://example.test/v1', 'fixture-model', key)
        outcome = self.runner('check_secret', 'task', model=model).run(f'Check the value {key} safely.')
        self.assertEqual(outcome['status'], 'review')
        self.assertNotIn(key, outcome['answer'])
        for path in Path(outcome['run_dir']).glob('*'):
            if path.is_file():
                self.assertNotIn(key, path.read_text(encoding='utf-8'), path.name)


if __name__ == '__main__':
    unittest.main()
