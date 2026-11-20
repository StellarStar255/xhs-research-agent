import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from xhs_reader import app, paths


class AppDiscoveryTests(unittest.TestCase):
    def test_reuses_same_profile_on_fallback_even_when_first_port_is_free(self):
        profile = paths.data_profile()
        with patch.dict(os.environ, XHS_PORT='8766'), \
             patch.object(app, '_is_free', side_effect=lambda port: port != 8768), \
             patch.object(app, '_ping', return_value={'profile': profile}), \
             patch.object(app, '_replace_older', return_value=False):
            self.assertEqual(app._pick_port(), (8768, True))

    def test_leaves_another_profiles_old_instance_running(self):
        with patch.dict(os.environ, XHS_PORT='8766'), \
             patch.object(app, '_is_free', side_effect=lambda port: port != 8766), \
             patch.object(app, '_ping', return_value={'profile': 'other', 'version': '0.1.0'}), \
             patch.object(app, '_replace_older') as replace:
            self.assertEqual(app._pick_port(), (8767, False))
            replace.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows startup regression')
    def test_simultaneous_launch_and_fallback_reopen_use_one_server(self):
        for port in random.sample(range(22000, 29000), 30):
            try:
                holder = app._bind_port(port)
            except OSError:
                continue
            if app._is_free(port + 1):
                break
            holder.close()
        else:
            self.fail('Could not reserve two consecutive local ports')

        processes = []
        with tempfile.TemporaryDirectory(prefix='xhs-launch-test-') as folder:
            env = dict(os.environ, XHS_NO_BROWSER='1', XHS_PORT=str(port), XHS_DATA_DIR=folder,
                       XHS_UPDATE_API='http://127.0.0.1:1/no-update')

            def launch():
                child = subprocess.Popen([sys.executable, '-m', 'xhs_reader'], env=env,
                                         cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL)
                processes.append(child)
                return child

            try:
                first, second = launch(), launch()
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    observed = app._ping(port + 1)
                    if observed and any(child.poll() is not None for child in (first, second)):
                        break
                    time.sleep(.1)
                self.assertIsNotNone(observed)
                self.assertEqual(sorted(child.poll() is None for child in (first, second)), [False, True])
                holder.close()
                self.assertEqual(launch().wait(10), 0)
                self.assertIsNone(app._ping(port))
                req = urllib.request.Request(f'http://127.0.0.1:{port + 1}/api/shutdown', method='POST')
                with app._loopback.open(req, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                for child in processes:
                    self.assertEqual(child.wait(10), 0)
            finally:
                holder.close()
                for child in processes:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(5)

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
