"""A slow successful model response must outlive the connection-only deadline."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest

from remote_api import APIClient, APICancelled, APIError


class APITimeoutTests(unittest.TestCase):
    def setUp(self):
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.count = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                owner.count += 1
                owner.arrived.set()
                owner.release.wait(3)
                data = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'Done'},
                                               'finish_reason': 'stop'}]}).encode()
                try:
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.client = APIClient(f'http://127.0.0.1:{self.server.server_port}/v1', 'local-test', 'test-only-key')
        self.client.connect_timeout = .15
        self.client.request_timeout = 2

    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)

    def call(self, cancel=None):
        return self.client.complete_tools([{'role': 'user', 'content': 'Do a task.'}], [], cancel_event=cancel)

    def test_delayed_first_response_uses_generation_deadline(self):
        def finish():
            self.arrived.wait(1)
            time.sleep(.4)
            self.release.set()
        timer = threading.Thread(target=finish, daemon=True)
        timer.start()
        self.addCleanup(timer.join, 1)
        self.assertEqual(self.call()['content'], 'Done')
        self.assertEqual(self.count, 1)

    def test_total_deadline_still_stops_waiting_without_retry(self):
        self.client.request_timeout = .25
        start = time.monotonic()
        with self.assertRaises(APIError):
            self.call()
        self.assertLess(time.monotonic() - start, 1.2)
        self.assertEqual(self.count, 1)
        self.assertFalse(self.client._request_lock.locked())

    def test_cancel_interrupts_pending_response(self):
        cancel = threading.Event()
        def stop():
            self.arrived.wait(1)
            cancel.set()
        thread = threading.Thread(target=stop, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 1)
        with self.assertRaises(APICancelled):
            self.call(cancel)
        self.assertEqual(self.count, 1)
        self.assertFalse(self.client._request_lock.locked())


if __name__ == '__main__':
    unittest.main()
