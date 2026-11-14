"""Exercise Chat Completions over localhost using a fixture credential."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
import threading
import unittest

from remote_api import APICancelled, APIClient, APIError, MAX_TOOL_ARGUMENT_BYTES


KEY = 'fixture-key-for-localhost'
MESSAGE = {'role': 'user', 'content': 'Read the sample document.'}
TOOL = {'type': 'function', 'function': {'name': 'read_file', 'parameters': {
    'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path'],
}}}
CALL = {'id': 'call_sample', 'type': 'function', 'function': {
    'name': 'read_file', 'arguments': '{"path":"sample.txt"}',
}}


def reply(finish, content=None, **fields):
    return {'choices': [{'finish_reason': finish,
                         'message': {'role': 'assistant', 'content': content, **fields}}]}


class ToolAPITests(unittest.TestCase):
    def setUp(self):
        self.requests, self.responses = Queue(), Queue()
        self.release = threading.Event()
        requests, responses, release = self.requests, self.responses, self.release

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                requests.put((self.path, self.headers.get('Authorization'), json.loads(body)))
                response = responses.get(timeout=3)
                if response is None:
                    release.wait(3)
                    return
                raw = json.dumps(response).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, args=(.01,), daemon=True)
        self.thread.start()
        self.client = APIClient(f'http://127.0.0.1:{self.server.server_port}/v1', 'fixture', KEY)

    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def test_round_trip_tools_and_final_text(self):
        self.responses.put(reply('tool_calls', tool_calls=[CALL], reasoning_content='fixture reasoning'))
        assistant = self.client.complete_tools([MESSAGE], [TOOL])
        self.assertEqual(assistant, {'role': 'assistant', 'content': None, 'tool_calls': [CALL],
                                     'reasoning_content': 'fixture reasoning'})
        path, authorization, payload = self.requests.get_nowait()
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(authorization, f'Bearer {KEY}')
        self.assertEqual(payload['messages'], [MESSAGE])
        self.assertEqual(payload['tools'], [TOOL])
        self.assertIs(payload['stream'], False)
        result = {'role': 'tool', 'tool_call_id': 'call_sample', 'content': 'Fixture document contents.'}
        history = [MESSAGE, assistant, result]
        self.responses.put(reply('stop', 'This is the fixture document.'))
        final = self.client.complete_tools(history, [TOOL])
        self.assertEqual(final, {'role': 'assistant', 'content': 'This is the fixture document.', 'tool_calls': []})
        self.assertEqual(self.requests.get_nowait()[2]['messages'], history)

    def test_rejects_truncated_filtered_and_invalid_calls(self):
        malformed = {**CALL, 'function': {**CALL['function'], 'arguments': '["' + KEY + '"]'}}
        oversized = {**CALL, 'function': {**CALL['function'], 'arguments': '{"path":"' + 'x' * MAX_TOOL_ARGUMENT_BYTES + '"}'}}
        responses = [reply('length', tool_calls=[CALL]), reply('content_filter', tool_calls=[CALL]),
                     reply('tool_calls', tool_calls=[CALL, CALL]), reply('tool_calls', tool_calls=[malformed]),
                     reply('tool_calls', tool_calls=[oversized]), reply('stop', 'text', tool_calls=[CALL])]
        for index, response in enumerate(responses):
            with self.subTest(index=index):
                self.responses.put(response)
                with self.assertRaises(APIError) as error:
                    self.client.complete_tools([MESSAGE], [TOOL])
                self.assertNotIn(KEY, str(error.exception))
                self.requests.get_nowait()

    def test_rejects_unmatched_history_and_invalid_definition_before_sending(self):
        assistant = {'role': 'assistant', 'content': None, 'tool_calls': [CALL]}
        wrong_result = {'role': 'tool', 'tool_call_id': 'wrong', 'content': 'result'}
        with self.assertRaisesRegex(APIError, '未配对'):
            self.client.complete_tools([MESSAGE, assistant], [TOOL])
        with self.assertRaisesRegex(APIError, '未配对'):
            self.client.complete_tools([MESSAGE, assistant, wrong_result], [TOOL])
        with self.assertRaisesRegex(APIError, '清单'):
            self.client.complete_tools([MESSAGE], [TOOL, TOOL])
        self.assertTrue(self.requests.empty())

    def test_context_rejection_does_not_send_or_trim(self):
        assistant = {'role': 'assistant', 'content': None, 'tool_calls': [CALL]}
        result = {'role': 'tool', 'tool_call_id': 'call_sample', 'content': '文' * 2048}
        with self.assertRaisesRegex(APIError, '超过估算容量'):
            self.client.complete_tools([MESSAGE, assistant, result], [TOOL], context=4096)
        self.assertTrue(self.requests.empty())

    def test_cancel_inflight_request(self):
        self.responses.put(None)
        cancellation = threading.Event()
        result = Queue()

        def run():
            try:
                result.put(self.client.complete_tools([MESSAGE], [TOOL], cancellation))
            except APIError as error:
                result.put(error)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.requests.get(timeout=3)
        cancellation.set()
        self.assertIsInstance(result.get(timeout=2), APICancelled)
        worker.join(timeout=1)

    def test_existing_text_generation(self):
        self.responses.put(reply('stop', 'Fixture answer.'))
        self.assertEqual(self.client.generate('A text prompt.'), 'Fixture answer.')
        payload = self.requests.get_nowait()[2]
        self.assertIs(payload['stream'], True)
        self.assertNotIn('tools', payload)


if __name__ == '__main__':
    unittest.main()
