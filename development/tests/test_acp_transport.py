"""Exercise ACP stdio using disposable Python subprocesses."""

import os
from pathlib import Path
import sys
import tempfile
from threading import Event, get_ident
import time
import unittest
from unittest.mock import patch

from acp_transport import (
    ACPCancelled, ACPError, ACPMethodNotFound, ACPProcess, ACPProtocolError, ACPRemoteError, ACPTimeout,
    MAX_LINE_BYTES, QUEUE_SIZE,
)


DEVELOPMENT = Path(__file__).resolve().parents[1]
HELPER = r"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def send(**fields):
    sys.stdout.buffer.write(json.dumps({"jsonrpc": "2.0", **fields}, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def read():
    return json.loads(sys.stdin.buffer.readline().decode("utf-8"))


mode = sys.argv[1]
if mode == "flood":
    for index in range(500):
        send(method="note", params={"index": index})
    sys.exit(0)
if mode == "unresponsive_writer":
    time.sleep(30)
    sys.exit(0)

request = read()
if mode == "approval":
    assert request == {"jsonrpc": "2.0", "method": "session/prepare", "params": {}}
    request = read()
    assert request["method"] == "session/prompt"
    assert request["params"]["prompt"] == "line one\n下一行"
    send(method="session/update", params={"text": "处理中"})
    send(id="perm-from-agent", method="session/request_permission", params={"sessionId": "s1"})
    approved = read()
    assert approved["id"] == "perm-from-agent"
    assert approved["result"] == {"outcome": {"outcome": "selected", "optionId": "test-option"}}
    send(id=27, method="unknown/method", params={})
    unsupported = read()
    assert unsupported["id"] == 27 and unsupported["error"]["code"] == -32601
    send(id=request["id"], result={"stopReason": "end_turn"})
    request = read()
    send(id=request["id"], result={"stillAlive": True})
elif mode in ("cancel", "ignore_cancel"):
    send(method="session/update", params={"active": True})
    cancel = read()
    assert cancel == {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "s1"}}
    Path("cancellation-received").write_bytes(b"received")
    if mode == "ignore_cancel":
        time.sleep(30)
    else:
        send(id=123, method="session/request_permission", params={"sessionId": "s1"})
        permission = read()
        assert permission["id"] == 123
        assert permission["result"] == {"outcome": {"outcome": "cancelled"}}
        send(id=request["id"], result={"stopReason": "cancelled"})
        request = read()
        send(id=request["id"], result={"stillAlive": True})
elif mode == "block_permission":
    send(id=7, method="session/request_permission", params={"sessionId": "s1"})
    cancel = read()
    assert cancel["method"] == "session/cancel"
    permission = read()
    assert permission["id"] == 7
    assert permission["result"] == {"outcome": {"outcome": "cancelled"}}
    Path("cancellation-received").write_bytes(b"received")
    send(id=request["id"], result={"stopReason": "cancelled"})
elif mode == "eof":
    secret = os.environ["FREEAI_FIXTURE_CREDENTIAL"]
    sys.stderr.buffer.write(secret.encode("utf-8") * 20000)
    sys.stderr.flush()
elif mode == "bad_json":
    sys.stdout.buffer.write(b'{"fixture-not-json":\n')
    sys.stdout.buffer.flush()
    time.sleep(30)
elif mode == "bad_utf8":
    sys.stdout.buffer.write(b'\xff\n')
    sys.stdout.buffer.flush()
    time.sleep(30)
elif mode == "wrong_id":
    send(id=request["id"] + 1, result={})
    time.sleep(30)
elif mode == "unterminated":
    sys.stdout.buffer.write(b'{}')
elif mode == "oversize":
    sys.stdout.buffer.write(b'x' * int(sys.argv[2]))
    sys.stdout.buffer.flush()
    time.sleep(30)
elif mode == "remote_error":
    secret = os.environ["FREEAI_FIXTURE_CREDENTIAL"]
    send(id=request["id"], error={"code": -32602, "message": secret, "data": secret})
    request = read()
    send(id=request["id"], result={})
elif mode == "tree":
    child = ("from pathlib import Path; import time; "
             "Path('descendant-ready').write_bytes(b'ready'); time.sleep(1); "
             "Path('descendant-survived').write_bytes(b'survived')")
    subprocess.Popen([sys.executable, "-B", "-c", child])
    for _ in range(100):
        if Path("descendant-ready").exists():
            send(id=request["id"], result={"started": True})
            break
        time.sleep(.02)
    time.sleep(30)
elif mode == "timeout":
    time.sleep(30)
"""


class ACPTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.helper = self.root / 'stdio_fixture.py'
        self.helper.write_text(HELPER, encoding='utf-8')

    def agent(self, mode, *args, **options):
        connection = ACPProcess(
            [sys.executable, '-B', '-u', str(self.helper), mode, *args],
            cwd=self.root, **options,
        )
        self.addCleanup(connection.close)
        return connection

    def test_notifications_and_approval_are_dispatched_on_request_thread(self):
        calls = []

        def notification(method, params):
            calls.append((get_ident(), method, params))

        def request(method, params):
            calls.append((get_ident(), method, params))
            if method != 'session/request_permission':
                raise ACPMethodNotFound
            return {'outcome': {'outcome': 'selected', 'optionId': 'test-option'}}

        with self.agent('approval', on_notification=notification, on_request=request) as connection:
            connection.notify('session/prepare', {})
            result = connection.request('session/prompt', {'prompt': 'line one\n下一行'})
            self.assertEqual(result, {'stopReason': 'end_turn'})
            self.assertEqual(connection.request('fixture/reusable', {}), {'stillAlive': True})
        self.assertEqual(calls, [
            (get_ident(), 'session/update', {'text': '处理中'}),
            (get_ident(), 'session/request_permission', {'sessionId': 's1'}),
            (get_ident(), 'unknown/method', {}),
        ])

    def test_cooperative_cancellation_sends_notification_and_declines_permission(self):
        cancelled = Event()

        def notification(method, _params):
            self.assertEqual(method, 'session/update')
            cancelled.set()

        def request(_method, _params):
            self.fail('The permission callback must not run during cancellation.')

        with self.agent('cancel', on_notification=notification, on_request=request) as connection:
            with self.assertRaises(ACPCancelled):
                connection.request('session/prompt', {}, cancel_event=cancelled,
                                   cancel_params={'sessionId': 's1'})
            self.assertTrue((self.root / 'cancellation-received').is_file())
            self.assertEqual(connection.request('fixture/reusable', {}), {'stillAlive': True})

    def test_cancellation_while_approval_callback_returns_sends_cancel_first(self):
        cancelled = Event()

        def request(_method, _params):
            cancelled.set()
            return {'outcome': {'outcome': 'selected', 'optionId': 'late'}}

        with self.agent('block_permission', on_request=request) as connection:
            with self.assertRaises(ACPCancelled):
                connection.request('session/prompt', {'sessionId': 's1'}, cancel_event=cancelled)
            self.assertTrue((self.root / 'cancellation-received').is_file())

    def test_uncooperative_cancel_is_bounded_and_closes_process(self):
        cancelled = Event()
        with patch('acp_transport.CANCEL_GRACE', 0.3):
            connection = self.agent('ignore_cancel', on_notification=lambda *_: cancelled.set())
            start = time.monotonic()
            with self.assertRaises(ACPCancelled):
                connection.request('session/prompt', {'sessionId': 's1'}, cancel_event=cancelled)
            self.assertLess(time.monotonic() - start, 2)
        self.assertTrue((self.root / 'cancellation-received').is_file())
        self.assertIsNotNone(connection._process.poll())

    def test_pre_cancelled_request_does_not_send_or_close_connection(self):
        cancelled = Event()
        cancelled.set()
        with self.agent('timeout') as connection:
            with self.assertRaises(ACPCancelled):
                connection.request('session/prompt', {'sessionId': 's1'}, cancel_event=cancelled)
            self.assertIsNone(connection._process.poll())
            self.assertTrue(connection._outgoing.empty())

    def test_eof_drains_stderr_without_exposing_environment(self):
        secret = 'fixture-credential-not-reported'
        connection = self.agent('eof', env={**os.environ, 'FREEAI_FIXTURE_CREDENTIAL': secret})
        with self.assertRaisesRegex(ACPError, '提前关闭') as failure:
            connection.request('fixture/eof', {})
        self.assertNotIn(secret, str(failure.exception))
        self.assertIsNotNone(connection._process.poll())

    def test_remote_error_keeps_process_usable_without_exposing_payload(self):
        secret = 'fixture-credential-not-reported'
        with self.agent('remote_error', env={**os.environ, 'FREEAI_FIXTURE_CREDENTIAL': secret}) as connection:
            with self.assertRaises(ACPRemoteError) as failure:
                connection.request('fixture/remote-error', {})
            self.assertEqual(failure.exception.code, -32602)
            self.assertNotIn(secret, str(failure.exception))
            self.assertEqual(connection.request('fixture/reusable', {}), {})

    def test_invalid_messages_close_process(self):
        for mode, hint in (('bad_json', 'JSON-RPC'), ('bad_utf8', 'UTF-8'),
                           ('wrong_id', '请求编号'), ('unterminated', '换行')):
            with self.subTest(mode=mode):
                connection = self.agent(mode)
                with self.assertRaisesRegex(ACPProtocolError, hint):
                    connection.request('fixture/invalid', {})
                self.assertIsNotNone(connection._process.poll())

    def test_oversize_output_closes_process(self):
        connection = self.agent('oversize', str(MAX_LINE_BYTES + 1))
        with self.assertRaisesRegex(ACPProtocolError, '4 MiB'):
            connection.request('fixture/oversize', {})
        self.assertIsNotNone(connection._process.poll())

    def test_oversize_request_is_rejected_before_writing(self):
        with self.agent('timeout') as connection:
            with self.assertRaisesRegex(ACPProtocolError, '4 MiB'):
                connection.request('fixture/oversize', {'text': 'x' * MAX_LINE_BYTES})
            self.assertTrue(connection._outgoing.empty())
            self.assertIsNone(connection._process.poll())

    def test_output_queue_is_bounded_while_no_request_is_running(self):
        with self.agent('flood') as connection:
            deadline = time.monotonic() + 2
            while not connection._incoming.full() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(connection._incoming.qsize(), QUEUE_SIZE)
        self.assertIsNotNone(connection._process.poll())

    def test_response_timeout_closes_process(self):
        connection = self.agent('timeout')
        with self.assertRaises(ACPTimeout):
            connection.request('fixture/timeout', {}, timeout=.2)
        self.assertIsNotNone(connection._process.poll())

    def test_full_pipe_does_not_block_send_timeout(self):
        connection = self.agent('unresponsive_writer')
        with self.assertRaisesRegex(ACPTimeout, '发送消息超时'):
            connection.request('fixture/timeout', {'text': 'x' * (1024 * 1024)}, timeout=.2)
        self.assertIsNotNone(connection._process.poll())

    def test_close_terminates_descendants(self):
        with self.agent('tree') as connection:
            self.assertEqual(connection.request('fixture/tree', {}), {'started': True})
        time.sleep(1.15)
        self.assertIsNotNone(connection._process.poll())
        self.assertFalse((self.root / 'descendant-survived').exists())

    def test_shell_string_is_rejected(self):
        with self.assertRaises(ValueError):
            ACPProcess('python -c "ignored"', cwd=self.root)


if __name__ == '__main__':
    unittest.main()
