import threading
import time
from unittest.mock import Mock, patch

from test_optimizations import IsolatedTest
from xhs_reader import scraper, server, settings


class LoginSyncTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        settings.accept_notice()
        self.stack.enter_context(patch.object(server, '_status', {'value': None, 'at': 0}))
        self.stack.enter_context(patch.object(server, '_login', {'proc': None, 'state': 'idle', 'message': ''}))

    def test_qr_result_is_published_without_the_initiating_window_polling(self):
        release = threading.Event()
        proc = Mock(returncode=0)
        def complete_login():
            release.wait(3)
            return '已登录：测试\n', None
        proc.communicate.side_effect = complete_login
        workers = []
        thread_type = threading.Thread
        def make_worker(*args, **kwargs):
            thread = thread_type(*args, **kwargs)
            workers.append(thread)
            return thread
        with patch.object(scraper, 'status', return_value={'logged_in': False}) as probe, \
             patch.object(server.procutil, 'popen', return_value=proc) as spawn, \
             patch.object(server.threading, 'Thread', side_effect=make_worker):
            self.assertFalse(server.status()['logged_in'])
            self.assertEqual(server.login()['state'], 'waiting')
            try:
                self.assertTrue(server.status(refresh=True)['login_pending'])
                self.assertEqual(server.login()['state'], 'waiting')
                spawn.assert_called_once()
            finally:
                release.set()
                workers[0].join(3)
            self.assertTrue(server.status()['logged_in'])
            self.assertEqual(server.status()['nickname'], '测试')
            probe.assert_called_once_with(wait_s=0)

    def test_failed_probe_does_not_report_a_logout(self):
        server._status.update(value={'logged_in': True, 'nickname': '测试'}, at=time.monotonic())
        with patch.object(scraper, 'status', side_effect=OSError('浏览器暂时不可用')):
            self.assertTrue(server.status(refresh=True)['logged_in'])

    def test_login_failure_keeps_the_underlying_error(self):
        proc = Mock(returncode=1)
        proc.communicate.return_value = ('Traceback ...\n浏览器已关闭\n[PYI-1:ERROR] Failed to execute\n', None)
        server._finish_login(proc)
        self.assertEqual(server.login_status(), {'state': 'failed', 'message': '登录没有完成：浏览器已关闭'})
