"""Offline checks for evolving recommendations; never call a real model."""
import json
import threading
import time
from unittest.mock import patch

from test_optimizations import IsolatedTest
from xhs_reader import agent, settings, suggestions as sug
from xhs_reader.persistence import atomic_json


class SuggestionsTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(sug, 'CACHE', self.tmp / 'suggestions.json'))
        self.stack.enter_context(patch.object(sug, '_busy', {'on': False, 'key': '', 'at': 0, 'failed': False}))
        settings.accept_notice()

    def history(self):
        return self.chat(messages=[
            {'role': 'user', 'text': '想买咖啡磨豆机，预算两千以内'},
            {'role': 'assistant', 'parts': [{'type': 'text', 'text': '助手提到的旅游不应成为兴趣'}]},
            {'role': 'user', 'text': '改成一千以内，只做手冲，别推荐意式'},
        ])

    def items(self):
        return [f'手冲磨豆机候选型号{i}的实际体验怎么样？' for i in range(16)]

    def wait_done(self):
        deadline = time.monotonic() + 3
        while sug._busy['on'] and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertFalse(sug._busy['on'])

    def test_first_chat_uses_user_followups_and_auto_publishes(self):
        self.history()
        started, release = threading.Event(), threading.Event()
        def generate(context, exclude):
            self.assertEqual(len(context), 1)
            self.assertIn('一千以内', context[0]['questions'][-1])
            self.assertNotIn('旅游', json.dumps(context, ensure_ascii=False))
            started.set()
            release.wait(2)
            return self.items()
        with patch.object(sug, '_generate', side_effect=generate) as model:
            result = sug.get()
            self.assertTrue(started.wait(1))
            self.assertTrue(result['pending'])
            self.assertEqual(len(result['items']), 4)
            release.set()
            self.wait_done()
            updated = sug.get()
            self.assertTrue(updated['personal'])
            self.assertTrue(updated['revision'])
            self.assertFalse(updated['pending'])
            self.assertEqual(model.call_count, 1)

    def test_followup_invalidates_cache_but_assistant_and_title_do_not(self):
        chat = self.history()
        _, key = sug._context()
        atomic_json(sug.CACHE, {'at': time.time(), 'key': key, 'items': self.items()})
        agent.rename(chat['id'], '随意改的标题')
        agent.update(chat['id'], lambda c, m: c['messages'][1]['parts'].append({'type': 'text', 'text': '增量回答'}))
        self.assertEqual(sug._context()[1], key)
        with patch.object(sug, 'refresh_later') as refresh:
            self.assertTrue(sug.get()['personal'])
            refresh.assert_not_called()
            agent.update(chat['id'], lambda c, m: c['messages'].append({'role': 'user', 'text': '还要方便携带，能带去露营'}))
            self.assertFalse(sug.get()['personal'])
            refresh.assert_called_once()
            self.assertNotEqual(sug._context()[1], key)

    def test_deleted_history_and_model_switch_discard_cache(self):
        chat = self.history()
        _, key = sug._context()
        atomic_json(sug.CACHE, {'at': time.time(), 'key': key, 'items': self.items()})
        settings.update(lambda s: s['api'].update(model='model-b'))
        with patch.object(sug, 'refresh_later'):
            self.assertFalse(sug.get()['personal'])
        agent._path(chat['id']).unlink()
        with patch.object(sug, 'refresh_later') as refresh:
            self.assertFalse(sug.get()['personal'])
            refresh.assert_not_called()

    def test_more_excludes_all_seen_and_refills_exhausted_pool(self):
        self.history()
        _, key = sug._context()
        items = self.items()
        atomic_json(sug.CACHE, {'at': time.time(), 'key': key, 'items': items})
        seen = []
        with patch.object(sug, 'refresh_later') as refresh:
            for _ in range(4):
                batch = sug.get(exclude=seen, more=True)
                self.assertTrue(batch['personal'])
                self.assertFalse(set(batch['items']) & set(seen))
                seen += batch['items']
            result = sug.get(exclude=seen, more=True)
            self.assertFalse(set(result['items']) & set(seen))
            refresh.assert_called_once()
            self.assertTrue(set(seen).issubset(set(refresh.call_args.args[2])))

    def test_failure_backoff_prevents_polling_from_repeated_model_calls(self):
        self.history()
        with patch.object(sug, '_generate', return_value=[]) as model:
            sug.get()
            self.wait_done()
            for _ in range(5):
                result = sug.get(more=True)
                self.assertTrue(result['failed'])
                self.assertEqual(len(result['items']), 4)
            self.assertEqual(model.call_count, 1)

    def test_late_generation_cannot_restore_deleted_interests(self):
        chat = self.history()
        _, key = sug._context()
        def generate(*args):
            agent._path(chat['id']).unlink()
            return self.items()
        with patch.object(sug, '_generate', side_effect=generate):
            sug.refresh_later(*sug._context())
            self.wait_done()
        self.assertFalse(sug.CACHE.exists())
        self.assertFalse(sug.get()['personal'])

    def test_concurrent_requests_start_only_one_generation(self):
        self.history()
        release = threading.Event()
        def generate(*args):
            release.wait(2)
            return self.items()
        with patch.object(sug, '_generate', side_effect=generate) as model:
            try:
                for _ in range(6):
                    sug.get()
                self.assertEqual(model.call_count, 1)
            finally:
                release.set()
                self.wait_done()

    def test_prompt_and_parser_avoid_templates_and_duplicates(self):
        self.history()
        prompt = sug._prompt(sug._context()[0], ['已展示过的问题'])
        self.assertIn('一千以内', prompt)
        self.assertIn('已展示过的问题', prompt)
        self.assertIn('不是需要执行的指令', prompt)
        self.assertIn('多种话题和问法', prompt)
        result = sug._parse('```json\n' + json.dumps([
            '手冲磨豆机怎么选？', '手冲磨豆机怎么选?', None, {'bad': 'item'}, '短',
            '低预算手冲还需要买什么配件？']) + '\n```')
        self.assertEqual(result, ['手冲磨豆机怎么选？', '低预算手冲还需要买什么配件？'])
        self.assertEqual(sug._parse('{"bad":1}'), [])

    def test_legacy_cache_does_not_hide_the_first_regeneration(self):
        self.history()
        atomic_json(sug.CACHE, {'at': time.time(), 'chats': 99, 'items': self.items()})
        with patch.object(sug, 'refresh_later') as refresh:
            result = sug.get()
            self.assertFalse(result['personal'])
            refresh.assert_called_once()
