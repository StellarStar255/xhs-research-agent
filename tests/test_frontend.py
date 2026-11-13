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
                search = page.locator('#chatSearch')
                search.fill('历史回答 历史问题')
                page.wait_for_selector('#chats .chat-item')
                page.wait_for_timeout(300)
                self.assertEqual(page.locator('#chats .chat-item').count(), 1)
                search.fill('历史问题 不存在')
                page.wait_for_selector('#chats .chat-search-state')
                self.assertIn('没有找到', page.locator('#chats').inner_text())
                self.assertEqual(page.evaluate('current'), 'test-chat')
                search.press('Escape')
                page.wait_for_selector('#chats .chat-item')
                self.assertEqual(search.input_value(), '')
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

    def test_suggestions_publish_async_avoid_repeats_and_ignore_old_requests(self):
        import uvicorn
        from playwright.sync_api import sync_playwright
        from xhs_reader import suggestions

        settings.accept_notice()
        self.stack.enter_context(patch.object(server, '_login_status', return_value={'logged_in':True}))
        starter = ['新用户可以先问些什么问题？' + str(i) for i in range(4)]
        personal = ['根据手冲预算推荐的后续问题？' + str(i) for i in range(12)]
        calls = []
        ready = False
        def batch(exclude=(), more=False):
            calls.append((list(exclude), more))
            items = [q for q in (personal if ready else starter) if q not in exclude][:4]
            return {'items':items, 'personal':ready, 'pending':not ready,
                    'revision':'generated-1' if ready else '', 'failed':False}
        self.stack.enter_context(patch.object(suggestions, 'get', side_effect=batch))
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
        deadline = time.monotonic() + 5
        while not srv.started and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(srv.started)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{port}/')
                page.wait_for_function("document.querySelector('#examples')?.dataset.personal === 'false'")
                self.assertIn('正在根据你的聊天生成推荐', page.locator('#exNote').inner_text())
                # Poll completion: the user never needs to click to receive generated suggestions.
                ready = True
                page.evaluate('async () => { await loadExamples(false, true); clearTimeout(examplesTimer); }')
                self.assertEqual(page.locator('#examples button').all_text_contents(), personal[:4])
                self.assertIn('最近的对话', page.locator('#exNote').inner_text())
                page.locator('#exMore').click()
                page.wait_for_function("document.querySelector('#examples button')?.textContent.endsWith('4')")
                self.assertEqual(page.locator('#examples button').all_text_contents(), personal[4:8])
                self.assertTrue(calls[-1][1])
                self.assertTrue(set(starter + personal[:4]).issubset(set(calls[-1][0])))
                # An older request returning after a new welcome screen must not overwrite it.
                page.evaluate("""() => {
                    clearTimeout(examplesTimer);
                    window.originalApi = api;
                    api = (path, ...args) => path.startsWith('/api/suggestions')
                      ? new Promise(resolve => window.finishOldExamples = resolve)
                      : originalApi(path, ...args);
                    void loadExamples();
                }""")
                page.wait_for_function("typeof window.finishOldExamples === 'function'")
                page.evaluate('api = window.originalApi; newChat()')
                page.wait_for_function("document.querySelector('#examples')?.dataset.personal === 'true'")
                page.evaluate("window.finishOldExamples({items:['过期请求不能覆盖新页面'], personal:false, pending:false, revision:'old'})")
                page.wait_for_timeout(50)
                self.assertNotIn('过期请求', page.locator('#examples').inner_text())
                page.set_viewport_size({'width':390, 'height':844})
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                self.assertFalse(errors, errors)
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
