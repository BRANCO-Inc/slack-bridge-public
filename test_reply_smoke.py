from __future__ import annotations

import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


class LocalReplyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        self.server.requests.append(
            (self.path, self.headers.get("X-Bridge-Token", ""), body)
        )
        authorized = hmac.compare_digest(self.headers.get("X-Bridge-Token", ""), self.server.auth_token)
        self.send_response(200 if authorized else 401)
        self.send_header("Content-Length", "0")
        self.end_headers()


class LocalReplyServer(ThreadingHTTPServer):
    def __init__(self, address, auth_token: str):
        super().__init__(address, LocalReplyHandler)
        self.auth_token = auth_token
        self.requests = []


class ReplySmokeTests(unittest.TestCase):
    def test_reply_wrapper_posts_done_wait_and_note_to_authenticated_local_server(self):
        auth_token = "test-local-reply-token"
        server = LocalReplyServer(("127.0.0.1", 0), auth_token)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as data_root:
                for index, (argument, expected_action) in enumerate(
                    (("--done", "done"), ("--wait", "wait"), ("--note", "continue")),
                    start=1,
                ):
                    environment = os.environ.copy()
                    environment.update(
                        {
                            "BRIDGE_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                            "CASE_REPLY_MAX_ATTEMPTS": "1",
                            "CC_CASE_ID": "case-local",
                            "CC_PANE_ID": "%1",
                            "CC_TURN_ID": f"turn-{index}",
                            "CC_WINDOW_NAME": "worker",
                            "SLACK_BRIDGE_AUTH_TOKEN": auth_token,
                            "SLACK_BRIDGE_DATA_ROOT": data_root,
                            "SLACK_BRIDGE_PYTHON_BIN": sys.executable,
                            "SLACK_BRIDGE_WORKER_SESSION_ID": "worker-local",
                        }
                    )
                    process = subprocess.Popen(
                        [str(PROJECT_ROOT / "case_reply.sh"), argument, expected_action],
                        cwd=PROJECT_ROOT,
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    process_id = process.pid
                    try:
                        stdout, stderr = process.communicate(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                        self.fail("case_reply.sh did not exit")

                    self.assertEqual(process.returncode, 0)
                    self.assertNotIn(auth_token, stdout)
                    self.assertNotIn(auth_token, stderr)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(process_id, 0)

            self.assertEqual(len(server.requests), 3)
            for index, (path, token, body) in enumerate(server.requests, start=1):
                self.assertEqual(path, "/bridge/case_reply")
                self.assertEqual(token, auth_token)
                self.assertEqual(body["case_id"], "case-local")
                self.assertEqual(body["turn_id"], f"turn-{index}")
                self.assertEqual(body["completion_action"], ("done", "wait", "continue")[index - 1])
                self.assertTrue(body["final_attempt"])
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
        self.assertFalse(server_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
