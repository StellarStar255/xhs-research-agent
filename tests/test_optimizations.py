"""Offline regression tests. All data and subprocesses use temporary directories."""
from contextlib import ExitStack, contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from xhs_reader import agent, images, llm, note_cache, paths, scraper, server, settings, store
from xhs_reader.history import compact_history, api_messages, _cost
from xhs_reader.persistence import atomic_json, file_lock, read_json
from xhs_reader.rendering import render_markdown
from xhs_reader.tools import ToolRunner
_REAL_SEND_QUEUED = agent._send_queued_later


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="xhs-tests-")))
        for module in (agent, images, note_cache, paths, scraper, settings, store):
            self.stack.enter_context(patch.object(module, "DATA_DIR", self.tmp))
        for module, name, value in (
            (agent, "CHATS_DIR", self.tmp / "chats"),
            (store, "RESEARCH_DIR", self.tmp / "research"),
            (settings, "SETTINGS_FILE", self.tmp / "settings.json"),
            (scraper, "LOCK_FILE", self.tmp / "browser.lock"),
            (scraper, "PROFILE_DIR", self.tmp / "profile"),
            (scraper, "USAGE_FILE", self.tmp / "usage.json"),
            (scraper, "COOLDOWN_FILE", self.tmp / "cooldown.json"),
            (images, "_TURN_FILE", self.tmp / "image_turns.json"),
            (agent, "_runs", {}), (agent, "_deleting", set()),
            (settings, "_found", {"claude": None, "codex": None}),
        ):
            self.stack.enter_context(patch.object(module, name, value))
        self.stack.enter_context(patch.object(agent, "_keep_awake"))
        self.stack.enter_context(patch.object(agent, "_send_queued_later"))
        settings.save({"backend": "api", "api": {"provider": "custom", "base_url": "https://a.example/v1",
                       "model": "model-a", "api_key": "key-a"}})

    def chat(self, cid="test-chat", **extra):
        chat = {"id": cid, "title": "测试", "backend": "api", "messages": [
            {"role": "user", "text": "问题", "images": []},
            {"role": "assistant", "parts": [], "draft": "", "status": "running", "started": time.time()}], **extra}
        agent._save(chat)
        return chat


class RenderingTests(unittest.TestCase):
    def test_dangerous_html_is_inert(self):
        cases = ['<img src=x onerror="alert(1)">', '<svg onload=alert(1)>x</svg>',
                 '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
                 '<script>alert(1)</script>', '<a href="javascript:alert(1)">click</a>',
                 '<a href="java&#x09;script:alert(1)">click</a>',
                 '<a href="data:text/html,evil">click</a>', '<a href="//evil.example">click</a>',
                 '<p style="position:fixed" onclick="evil()">hello</p>',
                 '<math><mtext><table><mglyph><style><!--</style><img src=x onerror=evil()>']
        for source in cases:
            with self.subTest(source=source):
                result = render_markdown(source)
                self.assertNotRegex(result, r'<(?:img|svg|iframe|script|math|style)\b')
                self.assertNotRegex(result, r'<[^>]+\s(?:on\w+|style)=')
                self.assertNotRegex(result, r'href="(?:javascript|data|//)')

    def test_markdown_features_and_safe_links_remain(self):
        text = '# 标题\n\n**粗体**\n\n- 一\n- 二\n\n|A|B|\n|-|-|\n|1|2|\n\n```python\nprint("<img>")\n```\n\n[来源](https://example.com/?a=1&b=2)'
        out = render_markdown(text)
        for tag in ('<h1>', '<strong>', '<ul>', '<table>', '<pre>', '<code>'):
            self.assertIn(tag, out)
        self.assertIn('href="https://example.com/?a=1&amp;b=2"', out)
        self.assertIn('rel="noopener noreferrer"', out)
        self.assertIn('&lt;img&gt;', out)

    def test_render_is_cached(self):
        render_markdown.cache_clear()
        render_markdown('cached'); render_markdown('cached')
        self.assertEqual(render_markdown.cache_info().hits, 1)


class PersistenceTests(IsolatedTest):
    def test_failed_atomic_replace_keeps_original(self):
        f = self.tmp / 'record.json'
        atomic_json(f, {"value": "旧"})
        with patch('xhs_reader.persistence.os.replace', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                atomic_json(f, {"value": "新"})
        self.assertEqual(read_json(f), {"value": "旧"})
        self.assertEqual(list(self.tmp.glob('.*.tmp')), [])
        if os.name != 'nt':
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_settings_transactions_preserve_concurrent_changes(self):
        errors = []
        def change(i):
            try:
                settings.update(lambda s: s.update({f'field_{i}': i}))
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=change, args=(i,)) for i in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertFalse(errors)
        conf = settings.load()
        for i in range(20): self.assertEqual(conf[f'field_{i}'], i)

    def test_process_lock_serializes_and_survives_owner_exit(self):
        code = '''import sys,time,os
from pathlib import Path
from xhs_reader.persistence import file_lock
root=Path(sys.argv[1])
with file_lock(root/'shared.lock', timeout=5):
    print('locked', flush=True)
    with (root/'events').open('a') as f:
        f.write('enter '+sys.argv[2]+'\\n'); f.flush()
        time.sleep(.15)
        f.write('exit '+sys.argv[2]+'\\n')
    if sys.argv[2]=='crash': os._exit(0)
'''
        env = {**os.environ, 'PYTHONPATH': str(paths.ROOT), 'XHS_DATA_DIR': str(self.tmp)}
        first = subprocess.Popen([sys.executable, '-c', code, str(self.tmp), 'crash'], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(first.stdout.readline().strip(), 'locked')
        second = subprocess.Popen([sys.executable, '-c', code, str(self.tmp), 'second'], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for p in (first, second):
            _, err = p.communicate(timeout=10)
            self.assertEqual(p.returncode, 0, err)
        self.assertEqual((self.tmp/'events').read_text().splitlines(),
                         ['enter crash', 'exit crash', 'enter second', 'exit second'])

    def test_same_second_sessions_are_unique(self):
        with patch.object(store.time, 'strftime', return_value='fixed'):
            a, b = store.new_session('同一关键词'), store.new_session('同一关键词')
        self.assertNotEqual(a, b)

    def test_bad_chat_and_session_are_isolated(self):
        self.chat()
        (agent.CHATS_DIR/'broken.json').write_text('{')
        sid = store.new_session('正常')
        d = store.RESEARCH_DIR/'broken'; d.mkdir()
        (d/'meta.json').write_text('[]')
        self.assertEqual([c['id'] for c in agent.list_chats()], ['test-chat'])
        self.assertEqual([c['id'] for c in store.list_sessions()], [sid])
        self.assertTrue(list(d.glob('meta.json.corrupt-*')))
        agent.recover()
        self.assertEqual(agent.load('test-chat')['messages'][-1]['status'], 'error')

    def test_invalid_settings_are_preserved_for_recovery(self):
        settings.SETTINGS_FILE.write_text('{')
        self.assertIsInstance(settings.load()['api'], dict)
        self.assertTrue(list(self.tmp.glob('settings.json.corrupt-*')))

    def test_image_budget_is_atomic(self):
        amounts = []
        threads = [threading.Thread(target=lambda: amounts.append(images._take_budget('turn', 4))) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(amounts), images.MAX_PER_TURN)

    def test_invalid_image_budget_does_not_reset_counter(self):
        images._TURN_FILE.write_text('{')
        with self.assertRaises(images.ImageError): images._take_budget('turn', 4)
        atomic_json(images._TURN_FILE, {'turn': {'used': 'invalid', 'at': time.time()}})
        with self.assertRaises(images.ImageError): images._take_budget('turn', 4)


class CacheAndBudgetTests(IsolatedTest):
    def test_stale_pid_marker_does_not_keep_browser_busy(self):
        scraper.LOCK_FILE.write_text('999999 0')
        self.assertFalse(scraper.browser_busy())
        with file_lock(self.tmp/'browser.guard'):
            self.assertTrue(scraper.browser_busy())

    def test_note_expiry_does_not_follow_session_write(self):
        sid = store.new_session('测试')
        old = {'id': 'old', 'opened': True, 'fetched_at': time.time()-8*86400}
        new = {'id': 'new', 'opened': True, 'fetched_at': time.time()}
        store.save_notes(sid, [old, new])
        self.assertIsNone(scraper.cached_detail('old'))
        self.assertEqual(scraper.cached_detail('new')['id'], 'new')
        store.save_notes(sid, store.read_notes(sid))
        self.assertIsNone(scraper.cached_detail('old'))

    def test_legacy_import_is_once_and_time_survives_rewrite(self):
        sid = store.new_session('旧缓存')
        f = store.path(sid)/'notes.json'
        atomic_json(f, [{'id': 'old', 'comments': []}])
        old_time = time.time()-8*86400
        os.utime(f, (old_time, old_time))
        self.assertIsNone(scraper.cached_detail('old'))
        notes = store.read_notes(sid)
        store.save_notes(sid, notes)
        self.assertEqual(notes[0]['fetched_at'], old_time)
        with patch.object(note_cache, 'read_json', side_effect=AssertionError('rescanned')):
            self.assertIsNone(scraper.cached_detail('old'))

    def test_budget_rechecked_after_waiting_for_browser(self):
        @contextmanager
        def browser(**kw):
            atomic_json(scraper.USAGE_FILE, [time.time()]*scraper.HOURLY_CAP)
            yield Mock()
        with patch.object(scraper, 'browser', browser), patch.object(scraper, '_note_detail') as detail:
            with self.assertRaises(scraper.Limited):
                scraper.open_notes([{'id': 'new'}], progress=lambda *_: None)
            detail.assert_not_called()

    def test_cooldown_rechecked_after_waiting(self):
        @contextmanager
        def browser(**kw):
            scraper._start_cooldown('test')
            yield Mock()
        with patch.object(scraper, 'browser', browser), patch.object(scraper, '_note_detail') as detail:
            with self.assertRaises(scraper.Limited):
                scraper.open_notes([{'id': 'new'}], progress=lambda *_: None)
            detail.assert_not_called()

    def test_failed_navigation_consumes_budget_and_removes_listener(self):
        page = Mock()
        page.goto.side_effect = RuntimeError('navigation failed')
        with self.assertRaises(RuntimeError):
            scraper._note_detail(page, {'id': 'new'}, 20)
        self.assertEqual(scraper.limits()['hour_left'], scraper.HOURLY_CAP-1)
        page.remove_listener.assert_called_once()

    def test_cached_notes_do_not_launch_browser(self):
        sid = store.new_session('缓存')
        store.save_notes(sid, [{'id':'cached', 'opened':True}])
        with patch.object(scraper, 'browser', side_effect=AssertionError('browser opened')):
            result = scraper.open_notes([{'id':'cached'}], progress=lambda *_:None)
        self.assertTrue(result[0]['from_cache'])
        self.assertEqual(scraper.limits()['hour_left'], scraper.HOURLY_CAP)

    def test_damaged_usage_cannot_reset_budget(self):
        scraper.USAGE_FILE.write_text('{')
        with self.assertRaises(RuntimeError): scraper.limits()


class ModelAndHistoryTests(IsolatedTest):
    def test_chat_uses_pinned_model_and_endpoint_with_separate_key(self):
        chat = self.chat(model_config={'api': {'provider':'custom', 'base_url':'https://a.example/v1', 'model':'model-a'}})
        req = server.SettingsReq(backend='api', api=server.ApiConf(provider='custom',
            base_url='https://b.example/v1', model='model-b', api_key='key-b'))
        server.save_settings(req)
        conf = llm.chat_conf(chat)
        self.assertEqual(conf['api']['model'], 'model-a')
        self.assertEqual(conf['api']['base_url'], 'https://a.example/v1')
        self.assertEqual(conf['api']['api_key'], 'key-a')
        self.assertNotIn('api_key', chat['model_config']['api'])
        self.assertNotIn('api_credentials', settings.public())

    def test_new_endpoint_does_not_receive_old_key(self):
        req = server.SettingsReq(backend='api', api=server.ApiConf(base_url='https://b.example', model='model-b'))
        with self.assertRaises(server.HTTPException): server.save_settings(req)
        self.assertEqual(settings.load()['api']['base_url'], 'https://a.example/v1')

    def test_history_compacts_whole_turns_preserving_tool_pairs(self):
        history = []
        for i in range(12):
            history += [{'role':'user','content':'问题'+str(i)},
                        {'role':'assistant','content':'分析', 'tool_calls':[{'id':str(i),'type':'function',
                          'function':{'name':'read_previous_notes','arguments':'{}'}}]},
                        {'role':'tool','tool_call_id':str(i),'content':f'SESSION session-{i}\n'+ '证据'*4000},
                        {'role':'assistant','content':'答案'*700}]
        history.append({'role':'user','content':'当前问题'})
        result = compact_history(history)
        self.assertLessEqual(sum(_cost(m) for m in result), 24000)
        self.assertEqual(result[-1]['content'], '当前问题')
        self.assertIn('证据索引', result[0]['content'])
        calls = {tc['id'] for m in result for tc in m.get('tool_calls', [])}
        answers = {m['tool_call_id'] for m in result if m['role']=='tool'}
        self.assertEqual(calls, answers)
        self.assertTrue(all('context_summary' not in m for m in api_messages(result)))
        self.assertEqual(len(history), 49)

    def test_only_current_turn_images_are_inlined(self):
        history = [{'role':'user','content':[{'type':'image_ref','name':'old.png'}]},
                   {'role':'assistant','content':'旧答案'},
                   {'role':'user','content':[{'type':'text','text':'问题'},{'type':'image_ref','name':'new.png'}]},
                   {'role':'user','content':[{'type':'image_path','path':'tool.png'}]}]
        out = compact_history(history)
        text = json.dumps(out)
        self.assertNotIn('old.png', text)
        self.assertIn('new.png', text)
        self.assertIn('tool.png', text)

    def test_oversized_current_question_fails_before_request(self):
        with self.assertRaisesRegex(RuntimeError, '上下文预算'):
            compact_history([{'role':'user','content':'字'*30000}])

    def test_tool_results_shrink_within_current_turn(self):
        history = [{'role':'user','content':'问题'}]
        for i in range(8):
            history += [{'role':'assistant','content':'', 'tool_calls':[{'id':str(i),'type':'function',
                        'function':{'name':'read_previous_notes','arguments':'{}'}}]},
                        {'role':'tool','tool_call_id':str(i),'content':'资料'*10000}]
        out = compact_history(history)
        self.assertLessEqual(sum(_cost(m) for m in out), 24000)
        self.assertEqual(len(out), len(history))


class LifecycleTests(IsolatedTest):
    def test_api_tool_loop_streams_answer_and_persists_valid_history(self):
        self.chat()
        run = llm.ApiRun('test-chat', 'turn')
        run.tools.call = Mock(return_value={'text':'SESSION evidence-1\n资料', 'is_error':False, 'images':[]})
        requests = []
        def stream(*a, **kw):
            requests.append(kw)
            if len(requests) == 1:
                call = SimpleNamespace(index=0, id='call1', function=SimpleNamespace(
                    name='read_previous_notes', arguments='{"session":"evidence-1"}'))
                delta = SimpleNamespace(content=None, tool_calls=[call])
            else:
                delta = SimpleNamespace(content='最终回答', tool_calls=[])
            return iter([SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])])
        with patch.object(llm, 'client', return_value=(Mock(), 'model-a')), patch.object(llm, '_create', stream):
            agent._runs['test-chat'] = run
            run.start()
            self.assertTrue(run.done.wait(3))
            run.thread.join(2)
        chat = agent.load('test-chat')
        self.assertEqual(chat['messages'][-1]['status'], 'done')
        self.assertEqual(chat['messages'][-1]['parts'][-1]['text'], '最终回答')
        self.assertEqual(len(requests), 2)
        self.assertEqual([m['role'] for m in chat['llm_history']], ['user','assistant','tool','assistant'])
        self.assertEqual(chat['llm_history'][2]['tool_call_id'], 'call1')
        self.assertIn('资料', requests[1]['messages'][-1]['content'])
        self.assertNotIn('test-chat', agent._runs)

    def test_queue_is_sent_once_after_stop(self):
        calls = []
        entered = threading.Event()
        def worker(run):
            question = agent.load(run.cid)['messages'][-2]['text']
            calls.append(question)
            if question == 'first':
                entered.set()
                run.cancelled.wait(3)
            else:
                agent.update(run.cid, lambda c,m: m['parts'].append({'type':'text','text':'完成'}))
        with patch.object(llm, 'run_turn', worker), patch.object(agent, '_send_queued_later', _REAL_SEND_QUEUED):
            cid = agent.send(None, 'first')
            self.assertTrue(entered.wait(2))
            agent.submit(cid, 'second'); agent.submit(cid, 'third')
            agent.stop(cid)
            deadline = time.monotonic()+3
            while time.monotonic()<deadline:
                if len(calls)==2 and not agent.is_running(cid): break
                time.sleep(.01)
            self.assertEqual(calls, ['first','second\n\nthird'])
            chat = agent.load(cid)
            self.assertEqual(len(chat['messages']), 4)
            self.assertEqual(chat['messages'][-1]['status'], 'done')
            self.assertFalse(chat['queued'])

    def test_retry_thread_start_failure_cleans_registered_run(self):
        chat = self.chat(claude_session='old-session')
        chat['messages'][-1].update(status='error', error_kind='error_during_execution')
        agent._save(chat)
        previous = Mock(done=threading.Event(), stopping=False)
        retry = Mock(done=threading.Event())
        retry.start.side_effect = RuntimeError('thread failed')
        agent._runs['test-chat'] = previous
        with patch.object(agent, '_start_claude', return_value=retry):
            self.assertTrue(agent._retry_without_resume('test-chat', previous))
        self.assertNotIn('test-chat', agent._runs)
        self.assertTrue(previous.done.is_set())
        retry.stop.assert_called_once()
        self.assertEqual(agent.load('test-chat')['messages'][-1]['status'], 'error')

    def test_start_failure_settles_persisted_message(self):
        with patch.object(llm, 'start', side_effect=RuntimeError('cannot start')):
            with self.assertRaises(RuntimeError): agent.send(None, '问题')
        chats = agent.list_chats()
        self.assertEqual(len(chats), 1)
        self.assertFalse(chats[0]['running'])
        self.assertEqual(agent.load(chats[0]['id'])['messages'][-1]['status'], 'error')

    def test_runs_are_registered_before_worker_start(self):
        observed = []
        def fake_start(cid, turn):
            run = Mock()
            run.alive.return_value = True
            run.start.side_effect = lambda: observed.append(agent._runs.get(cid) is run)
            return run
        with patch.object(llm, 'start', fake_start): agent.send(None, '问题')
        self.assertEqual(observed, [True])

    def test_cancel_blocked_model_request_and_delete(self):
        self.chat()
        entered, release = threading.Event(), threading.Event()
        cl = Mock()
        cl.close.side_effect = release.set
        def blocked(*a, **kw):
            entered.set(); release.wait(5)
            return iter([])
        run = llm.ApiRun('test-chat', 'turn')
        with patch.object(llm, 'client', return_value=(cl, 'model-a')), patch.object(llm, '_create', blocked):
            agent._runs['test-chat'] = run
            run.start()
            self.assertTrue(entered.wait(3))
            started = time.monotonic()
            agent.stop('test-chat')
            self.assertTrue(run.done.wait(2))
            self.assertLess(time.monotonic()-started, 2)
            self.assertEqual(agent.load('test-chat')['messages'][-1]['status'], 'stopped')
            agent.delete('test-chat')
            run.thread.join(2)
            self.assertFalse(agent._path('test-chat').exists())

    def test_delete_waits_for_final_write_and_suppresses_queue(self):
        self.chat(queued=[{'id':'q','text':'排队问题','images':[]}])
        run = llm.ApiRun('test-chat')
        def worker(r):
            r.cancelled.wait(3)
            agent.update(r.cid, lambda c,m: m.update(draft='最后写入'))
        with patch.object(llm, 'run_turn', worker):
            agent._runs['test-chat'] = run; run.start()
            agent.delete('test-chat')
            run.thread.join(2)
        self.assertFalse(agent._path('test-chat').exists())
        agent._send_queued_later.assert_not_called()

    def test_cancelled_tools_do_not_spawn(self):
        runner = ToolRunner('turn', cancelled=lambda:True)
        with patch('xhs_reader.tools.procutil.popen') as spawn:
            r = runner.call('search_xiaohongshu', {'keyword':'测试'})
        self.assertTrue(r['is_error']); spawn.assert_not_called()

    def test_process_registration_closes_stop_spawn_race(self):
        run = llm.ApiRun('test-chat')
        run.stopped = True
        proc = Mock(); proc.poll.return_value = None
        with patch.object(llm.procutil, 'kill_tree') as kill:
            run._on_proc(proc)
            kill.assert_called_once_with(proc)

    def test_claude_deltas_are_batched_without_losing_text(self):
        self.chat()
        events = [{'type':'stream_event', 'event':{'type':'content_block_delta',
                  'delta':{'type':'text_delta','text':'字'}}} for _ in range(100)]
        events += [{'type':'assistant','message':{'content':[{'type':'text','text':'字'*100}]}},
                   {'type':'result','subtype':'success','usage':{}}]
        proc = Mock(stdout=io.StringIO('\n'.join(json.dumps(e) for e in events)), returncode=0)
        with patch.object(agent, '_save', wraps=agent._save) as save:
            agent._pump_events('test-chat', proc)
        self.assertLess(save.call_count, 10)
        self.assertEqual(agent.load('test-chat')['messages'][-1]['parts'][0]['text'], '字'*100)


class IncrementalAndExportTests(IsolatedTest):
    def test_chat_poll_unchanged_skips_markdown(self):
        chat = self.chat(llm_history=[{'role':'tool','content':'private history'}])
        chat['messages'][-1]['parts'] = [{'type':'text','text':'**回答**'}]
        agent._save(chat)
        first = server.chat('test-chat')
        with patch.object(server, '_md', side_effect=AssertionError('rerendered')):
            second = server.chat('test-chat', since=first['revision'])
        self.assertTrue(second['unchanged'])
        self.assertNotIn('llm_history', first)

    def test_delta_contains_changed_tail(self):
        self.chat()
        first = server.chat('test-chat')
        agent.update('test-chat', lambda c,m: m.update(draft='新内容'))
        delta = server.chat('test-chat', since=first['revision'], after=1)
        self.assertEqual(delta['from_index'], 1)
        self.assertEqual(len(delta['messages']), 1)
        self.assertIn('新内容', delta['messages'][0]['draft_html'])

    def test_export_uses_same_sanitizer(self):
        from xhs_reader import export
        chat = self.chat()
        chat['messages'][-1].update(status='done', parts=[{'type':'text', 'text':'<img src=x onerror=evil()>\n\n**回答**'}])
        agent._save(chat)
        _, page = export.build_html('test-chat')
        self.assertNotIn('onerror', page)
        self.assertIn('<strong>回答</strong>', page)


if __name__ == '__main__':
    unittest.main()
