"""Optional offline Chrome integration check: XHS_BROWSER_TEST=1 unittest discover."""
import os
import socket
import threading
import time
import unittest
from unittest.mock import patch

from test_optimizations import IsolatedTest
from xhs_reader import agent, server, settings


@unittest.skipUnless(os.environ.get('XHS_BROWSER_TEST') == '1', 'optional local Chrome check')
class FrontendTests(IsolatedTest):
    def test_incremental_updates_preserve_history_and_block_html(self):
        import uvicorn
        from playwright.sync_api import sync_playwright

        settings.accept_notice()
        chat = self.chat()
        chat['messages'] = [
            {'role':'user','text':'历史问题','images':[]},
            {'role':'assistant','parts':[{'type':'text','text':'**历史回答** [来源](https://example.com)'}],
             'status':'done','draft':''},
            {'role':'user','text':'当前问题','images':[]},
            {'role':'assistant','parts':[], 'status':'running','draft':'正在回答','started':time.time()},
        ]
        agent._save(chat)
        self.stack.enter_context(patch.object(server, '_login_status', return_value={'logged_in':True, 'nickname':'测试'}))
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        port = sock.getsockname()[1]
        srv = uvicorn.Server(uvicorn.Config(server.app, log_level='error'))
        thread = threading.Thread(target=srv.run, kwargs={'sockets':[sock]}, daemon=True)
        thread.start()
        def shutdown():
            srv.should_exit = True
            thread.join(5)
            sock.close()
        self.addCleanup(shutdown)
        deadline = time.monotonic()+5
        while not srv.started and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertTrue(srv.started)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{port}/#c=test-chat')
                page.wait_for_selector('#thread .msg:nth-child(4)')
                page.evaluate("window.oldNode = document.querySelector('#thread .msg'); window.oldAnswer = document.querySelectorAll('#thread .msg')[1]")
                agent.update('test-chat', lambda c,m: m.update(draft='新增文字 <img src=x onerror="window.injected=1">'))
                page.evaluate("async () => { await openChat('test-chat'); clearTimeout(pollTimer); }")
                self.assertEqual(page.locator('#thread .msg').count(), 4)
                self.assertIn('新增文字', page.locator('#thread').inner_text())
                self.assertTrue(page.evaluate("window.oldNode === document.querySelector('#thread .msg')"))
                self.assertTrue(page.evaluate("window.oldAnswer === document.querySelectorAll('#thread .msg')[1]"))
                self.assertIsNone(page.evaluate('window.injected'))
                self.assertEqual(page.locator('.md img').count(), 0)
                page.evaluate("async () => { await openChat('test-chat'); clearTimeout(pollTimer); }")
                self.assertTrue(page.evaluate("window.oldAnswer === document.querySelectorAll('#thread .msg')[1]"))
                # A network failure must retain the selected chat and its messages.
                page.route('**/api/chats/test-chat?*', lambda route: route.fulfill(status=503, body='{}', content_type='application/json'))
                page.evaluate("async () => { await openChat('test-chat'); clearTimeout(pollTimer); }")
                self.assertEqual(page.evaluate('current'), 'test-chat')
                self.assertEqual(page.locator('#thread .msg').count(), 4)
                page.set_viewport_size({'width':390,'height':844})
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                # Settings expose a concrete Codex model, optional effort, and custom IDs.
                self.stack.enter_context(patch.object(settings, 'claude_available', return_value=True))
                self.stack.enter_context(patch.object(settings, 'codex_available', return_value=True))
                self.stack.enter_context(patch.object(settings, 'claude_efforts', return_value=['low','medium','high']))
                self.stack.enter_context(patch.object(settings, 'codex_models', return_value=[
                    {'id':'test-model-a','name':'Test model','efforts':['low','high']}]))
                settings.update(lambda s: s.update(backend='codex'))
                page.evaluate('openSettings()')
                self.assertTrue(page.locator('#sCodex').is_visible())
                self.assertFalse(page.locator('#sClaude').is_visible())
                self.assertFalse(page.locator('#sCodexCustomField').is_visible())
                self.assertEqual(page.locator('#sCodexEffort').input_value(), '')
                page.locator('#sCodexModel').select_option('test-model-a')
                self.assertEqual(page.locator('#sCodexEffort option').count(), 3)
                page.locator('#sCodexEffort').select_option('high')
                page.locator('#sSave').click()
                page.wait_for_function("!document.querySelector('#modal').classList.contains('show')")
                self.assertEqual(settings.load()['codex_model'], 'test-model-a')
                self.assertEqual(settings.load()['codex_effort'], 'high')
                page.evaluate('openSettings()')
                self.assertEqual(page.locator('#sCodexModel').input_value(), 'test-model-a')
                self.assertEqual(page.locator('#sCodexEffort').input_value(), 'high')
                page.locator('#sCodexModel').select_option('__custom__')
                self.assertTrue(page.locator('#sCodexCustomField').is_visible())
                self.assertEqual(page.locator('#sCodexEffort').input_value(), '')
                page.locator('#sSave').click()
                self.assertIn('请填写具体模型 ID', page.locator('#sOut').inner_text())
                page.locator('#sCodexCustom').fill('test-custom-id')
                page.locator('input[name=backend][value=claude]').check()
                self.assertTrue(page.locator('#sClaude').is_visible())
                self.assertFalse(page.locator('#sCodex').is_visible())
                page.locator('#sClaudeModel').select_option('__custom__')
                page.locator('#sClaudeCustom').fill('claude-specific-id')
                page.locator('#sClaudeEffort').select_option('medium')
                page.locator('#sSave').click()
                page.wait_for_function("!document.querySelector('#modal').classList.contains('show')")
                self.assertEqual(settings.load()['claude_model'], 'claude-specific-id')
                self.assertEqual(settings.load()['claude_effort'], 'medium')
                self.assertEqual(settings.load()['codex_model'], 'test-custom-id')
                self.assertFalse(errors, errors)
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
