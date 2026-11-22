"""Test the local project-model adapter without installed model assets."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Queue
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from local_project_model import LocalProjectModel
from local_runtime import LocalRuntime, RuntimeCancelled
from remote_api import APICancelled, APIError


TOKEN = 'fixture-local-runtime-token'
USER = {'role': 'user', 'content': 'Please read the fixture.'}
TOOLS = [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {
    'type': 'object', 'properties': {'path': {'type': 'string'}}}}}]
CALL = {'id': 'call_fixture', 'type': 'function', 'function': {
    'name': 'read_file', 'arguments': '{"path":"fixture.txt"}'}}


def stream(*deltas):
    lines = [f'data: {json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]})}'
             for delta, finish in deltas]
    return ('\n\n'.join(lines + ['data: [DONE]']) + '\n\n').encode('utf-8')


class FixtureRuntime:
    def __init__(self, url):
        self._request_lock = threading.Lock()
        self._token = TOKEN
        self.request_timeout = 10
        self.url = url
        self.starts = []
        self.stops = 0
        self.start_error = None

    def ensure_server(self, context, image_mode, cancel_event, model_id):
        self.starts.append((context, image_mode, cancel_event, model_id, self._request_lock.locked()))
        if self.start_error is not None:
            raise self.start_error
        return self.url

    def _alias(self, model_id):
        return 'fixture-' + model_id

    def shutdown(self):
        self.stops += 1


class LocalProjectModelTests(unittest.TestCase):
    def setUp(self):
        self.requests = Queue()
        self.replies = Queue()
        self.release = threading.Event()
        requests, replies, release = self.requests, self.replies, self.release

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.put((self.path, self.headers.get('Authorization'), payload))
                reply = replies.get(timeout=3)
                if reply is None:
                    release.wait(timeout=3)
                    return
                if isinstance(reply, bytes):
                    raw, content_type = reply, 'text/event-stream'
                else:
                    message, reason = reply
                    raw = json.dumps({'choices': [{'message': message, 'finish_reason': reason}]}).encode('utf-8')
                    content_type = 'application/json'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, args=(.01,), daemon=True)
        self.thread.start()
        self.runtime = FixtureRuntime(f'http://127.0.0.1:{self.server.server_port}')

    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def test_constructor_does_not_resolve_or_start_runtime(self):
        with patch('qwen_chat.runtime') as resolve:
            model = LocalProjectModel()
            self.assertEqual(model.model_id, 'text-default')
            self.assertEqual(model.max_output_tokens, 2048)
            resolve.assert_not_called()
        LocalProjectModel(self.runtime)
        self.assertEqual(self.runtime.starts, [])
        self.assertTrue(self.requests.empty())

    def test_local_tool_round_trip_and_explicit_model(self):
        model = LocalProjectModel(self.runtime, model_id='chat-qwen', max_output_tokens=777)
        self.replies.put(({'role': 'assistant', 'content': None, 'tool_calls': [CALL]}, 'tool_calls'))
        assistant = model.complete_tools([USER], TOOLS, context=8192)
        self.assertEqual(assistant['tool_calls'], [CALL])
        path, authorization, payload = self.requests.get_nowait()
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(authorization, 'Bearer ' + TOKEN)
        self.assertEqual(payload['model'], 'fixture-chat-qwen')
        self.assertEqual(payload['max_tokens'], 777)
        self.assertEqual(payload['temperature'], .1)
        self.assertFalse(payload['parallel_tool_calls'])
        self.assertEqual(payload['tools'], TOOLS)
        self.assertEqual(self.runtime.starts, [(8192, False, None, 'chat-qwen', True)])
        result = {'role': 'tool', 'content': 'File contents.', 'tool_call_id': CALL['id']}
        messages = [USER, assistant, result]
        self.replies.put(({'role': 'assistant', 'content': 'Read the file.'}, 'stop'))
        final = model.complete_tools(messages, TOOLS, context=8192)
        self.assertEqual(final, {'role': 'assistant', 'content': 'Read the file.', 'tool_calls': []})
        self.assertEqual(self.requests.get_nowait()[2]['messages'], messages)
        self.assertFalse(self.runtime._request_lock.locked())

    def test_local_tool_adapter_forwards_reasoning_callback(self):
        self.replies.put(stream(
            ({'reasoning_content': 'Inspect.'}, None),
            ({'tool_calls': [dict(CALL, index=0)]}, 'tool_calls'),
        ))
        thoughts = []
        model = LocalProjectModel(self.runtime)
        assistant = model.complete_tools([USER], TOOLS, on_reasoning=thoughts.append)
        self.assertIs(self.requests.get_nowait()[2]['stream'], True)
        self.assertEqual(thoughts, ['Inspect.'])
        self.assertEqual(assistant, {'role': 'assistant', 'content': None, 'tool_calls': [CALL],
                                     'reasoning_content': 'Inspect.'})

    def test_ordinary_local_chat_separates_thoughts_and_keeps_the_default_setting(self):
        self.replies.put(stream(
            ({'content': '<th'}, None),
            ({'content': 'ink>A fixture thought</think>'}, None),
            ({'content': 'The answer.'}, 'stop'),
        ))
        runtime = LocalRuntime(manager=SimpleNamespace())
        runtime._base_url = self.runtime.url
        self.addCleanup(runtime.shutdown)
        tokens, thoughts = [], []
        with patch.object(runtime, 'ensure_server'), patch.object(
                runtime, '_messages', return_value=([USER], 256, 10)):
            answer = runtime.generate('A fixture question.', on_token=tokens.append, on_reasoning=thoughts.append)
        payload = self.requests.get_nowait()[2]
        self.assertIs(payload['chat_template_kwargs']['enable_thinking'], False)
        self.assertEqual(payload['reasoning_effort'], 'none')
        self.assertEqual(answer, 'The answer.')
        self.assertEqual(tokens, ['The answer.'])
        self.assertEqual(thoughts, ['A fixture thought'])
        self.assertEqual(runtime.last_reasoning, 'A fixture thought')

    def test_cancel_during_http_releases_lock_and_managed_runtime(self):
        self.replies.put(None)
        cancelled, results = threading.Event(), Queue()

        def work():
            try:
                results.put(LocalProjectModel().complete_tools([USER], TOOLS, cancel_event=cancelled))
            except Exception as error:
                results.put(error)

        with patch('qwen_chat.runtime', return_value=self.runtime):
            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            self.requests.get(timeout=3)
            self.assertTrue(self.runtime._request_lock.locked())
            cancelled.set()
            self.assertIsInstance(results.get(timeout=3), APICancelled)
            worker.join(timeout=1)
        self.assertEqual(self.runtime.stops, 1)
        self.assertFalse(self.runtime._request_lock.locked())

    def test_startup_cancellation_leaves_injected_runtime_owned_by_caller(self):
        self.runtime.start_error = RuntimeCancelled(TOKEN)
        with self.assertRaises(APICancelled) as outcome:
            LocalProjectModel(self.runtime).complete_tools([USER], TOOLS)
        self.assertNotIn(TOKEN, str(outcome.exception))
        self.assertEqual(self.runtime.stops, 0)
        self.assertFalse(self.runtime._request_lock.locked())
        self.assertTrue(self.requests.empty())

    def test_startup_error_is_sanitized_and_lock_released(self):
        self.runtime.start_error = RuntimeError(TOKEN)
        with self.assertRaisesRegex(APIError, '无法启动') as outcome:
            LocalProjectModel(self.runtime).complete_tools([USER], TOOLS)
        self.assertNotIn(TOKEN, str(outcome.exception))
        self.assertFalse(self.runtime._request_lock.locked())
        self.assertTrue(self.requests.empty())

    def test_busy_model_explains_how_to_release_without_exposing_owner(self):
        from compute_guard import ComputeBusy
        self.runtime.start_error = ComputeBusy({'kind': 'inference', 'pid': 999, 'description': TOKEN})
        with self.assertRaisesRegex(APIError, '释放模型') as outcome:
            LocalProjectModel(self.runtime).complete_tools([USER], TOOLS)
        self.assertNotIn(TOKEN, str(outcome.exception))
        self.assertFalse(self.runtime._request_lock.locked())
        self.assertTrue(self.requests.empty())

    def test_invalid_runtime_address_is_rejected_before_http(self):
        for url in ('https://example.com', 'http://192.0.2.10:80', self.runtime.url + '/not-root'):
            with self.subTest(url=url), patch('remote_api._opener') as opener:
                self.runtime.url = url
                with self.assertRaisesRegex(APIError, '本机服务地址'):
                    LocalProjectModel(self.runtime).complete_tools([USER], TOOLS)
                opener.assert_not_called()
        self.assertFalse(self.runtime._request_lock.locked())


if __name__ == '__main__':
    unittest.main()
