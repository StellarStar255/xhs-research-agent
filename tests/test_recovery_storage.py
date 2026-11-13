from unittest.mock import Mock

from test_optimizations import IsolatedTest
from xhs_reader import agent, note_cache, server, storage, store


class RecoveryStorageTests(IsolatedTest):
    def test_recovery_preserves_partial_answer_full_queued_text_and_images(self):
        long_text = '完整的排队问题' * 100
        chat = self.chat(queued=[{'text': long_text, 'images': ['queued.png']}],
                         codex_thread='stale-thread', claude_session='stale-session', llm_history=[])
        chat['messages'][-1].update(draft='已经生成的部分答案', last_activity=10, started=5,
                                    parts=[{'type': 'search', 'status': 'running', 'keyword': '证件'}])
        agent._save(chat)
        agent.image_dir('test-chat').mkdir()
        (agent.image_dir('test-chat') / 'queued.png').write_bytes(b'image')
        agent.recover()
        restored = agent.load('test-chat')
        answer = restored['messages'][1]
        self.assertEqual(answer['status'], 'error')
        self.assertEqual(answer['error_kind'], 'interrupted')
        self.assertEqual(answer['elapsed'], 5)
        self.assertEqual(answer['parts'][-1]['text'], '已经生成的部分答案')
        self.assertEqual(answer['parts'][0]['status'], 'error')
        self.assertEqual(restored['messages'][-1]['text'], long_text)
        self.assertEqual(restored['messages'][-1]['images'], ['queued.png'])
        self.assertNotIn('queued', restored)
        self.assertNotIn('codex_thread', restored)
        self.assertNotIn('claude_session', restored)
        self.assertIn('已经生成的部分答案', restored['llm_history'][-1]['content'])
        reused = server.reuse_message('test-chat', 2)
        self.assertEqual(reused['text'], long_text)
        self.assertEqual(len(reused['images']), 1)
        self.assertEqual(reused['missing_images'], 0)
        modified = agent._path('test-chat').stat().st_mtime_ns
        agent.recover()
        self.assertEqual(agent._path('test-chat').stat().st_mtime_ns, modified)
        agent._send_queued_later.assert_not_called()

    def test_recovery_preserves_orphan_queue_and_skips_live_worker(self):
        self.chat('queued', messages=[], queued=[{'text': '未发出', 'images': []}])
        self.chat('live')
        run = Mock(); run.alive.return_value = True
        agent._runs['live'] = run
        agent.recover()
        self.assertTrue(agent.load('queued')['messages'][0]['recovered_queue'])
        self.assertEqual(agent.load('live')['messages'][-1]['status'], 'running')

    def test_selected_cleanup_preserves_active_chats_images_and_research(self):
        self.chat('active')
        self.chat('deleted')
        for cid in ('active', 'deleted'):
            agent.image_dir(cid).mkdir()
            (agent.image_dir(cid) / '0.png').write_bytes(b'image')
        sid = store.new_session('笔记')
        store.save_notes(sid, [{'id': 'note', 'opened': True, 'desc': 'cached' * 5000}])
        agent.delete('deleted')
        before = storage.usage()
        self.assertGreater(before['categories']['trash']['bytes'], 0)
        self.assertGreater(before['categories']['note_cache']['bytes'], 0)
        notes_before = (store.path(sid) / 'notes.json').read_bytes()
        result = storage.cleanup(['trash', 'note_cache'])
        self.assertGreater(result['freed_bytes'], 0)
        self.assertEqual(agent.list_trash(), [])
        self.assertEqual(agent.load('active')['id'], 'active')
        self.assertEqual((agent.image_dir('active') / '0.png').read_bytes(), b'image')
        self.assertEqual((store.path(sid) / 'notes.json').read_bytes(), notes_before)
        self.assertEqual(note_cache.lookup_many(['note']), {})
        with self.assertRaises(ValueError):
            storage.cleanup(['research'])
        with self.assertRaises(ValueError):
            storage.cleanup([])

    def test_cleanup_blocks_live_tasks_and_does_not_follow_symlinks(self):
        self.chat()
        agent.delete('test-chat')
        run = Mock(); run.alive.return_value = True
        agent._runs['active'] = run
        with self.assertRaises(RuntimeError):
            storage.cleanup(['trash'])
        self.assertEqual(len(agent.list_trash()), 1)
        agent._runs.clear()
        outside = self.tmp / 'outside'; outside.mkdir()
        (outside / 'private.txt').write_bytes(b'protected')
        (agent.CHATS_DIR / 'linked').symlink_to(outside, target_is_directory=True)
        self.assertEqual(storage.usage()['categories']['chat_images']['bytes'], 0)
        self.assertEqual((outside / 'private.txt').read_bytes(), b'protected')

    def test_explicit_trash_cleanup_handles_corrupt_records_without_following_links(self):
        root = agent.CHATS_DIR / '.trash'
        (root / 'broken').mkdir(parents=True)
        (root / 'broken/record.json').write_text('{broken')
        outside = self.tmp / 'outside'; outside.mkdir()
        (outside / 'keep.txt').write_text('保留')
        (root / 'link').symlink_to(outside, target_is_directory=True)
        storage.cleanup(['trash'])
        self.assertEqual(list(root.iterdir()), [])
        self.assertEqual((outside / 'keep.txt').read_text(), '保留')
