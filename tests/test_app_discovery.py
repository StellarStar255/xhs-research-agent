import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from xhs_reader import app, paths


class AppDiscoveryTests(unittest.TestCase):
    def test_loopback_instance_discovery_ignores_system_proxy(self):
        class PingHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'app': paths.APP_ID}).encode())

            def log_message(self, *_):
                pass

        with HTTPServer(('127.0.0.1', 0), PingHandler) as listener:
            worker = threading.Thread(target=listener.handle_request, daemon=True)
            worker.start()
            try:
                with patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}, clear=True):
                    self.assertEqual(app._ping(listener.server_port), {'app': paths.APP_ID})
            finally:
                worker.join(1)
