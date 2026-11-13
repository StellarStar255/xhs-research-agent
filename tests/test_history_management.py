"""History lifecycle, portable backups, and search filters; no real user data."""
import json
import asyncio
from pathlib import Path
import stat
import unittest
from unittest.mock import patch, Mock
import zipfile

from test_optimizations import IsolatedTest
from xhs_reader import agent, backup, server, store
from xhs_reader.persistence import atomic_json


class HistoryManagementTests(IsolatedTest):
    def test_backup_preview_is_read_only_and_final_import_rechecks_duplicates(self):
        record = {'id': 'incoming', 'title': '导入预览', 'messages': []}
        archive = self.make_archive({'chats/incoming.json': json.dumps(record), 'chats/incoming/0.png': 'image'})
        result = backup.import_from(archive, preview=True)
        self.assertEqual(result['imported'], 1)
        self.assertEqual(result['attachments'], 1)
        self.assertFalse(agent._path('incoming').exists())
        self.chat('incoming', title='预览后新建')
        result = backup.import_from(archive, preview=True)
        self.assertEqual(result['duplicates'], ['导入预览'])
        self.assertEqual(backup.import_from(archive)['skipped'], 1)
        self.assertEqual(agent.load('incoming')['title'], '预览后新建')

    def test_same_revision_polls_reuse_snapshot_and_equal_length_rename_is_visible(self):
        self.chat(title='原名')
        with patch.object(agent, 'load', wraps=agent.load) as reads:
            first = server.chat('test-chat')
            second = server.chat('test-chat', since=first['revision'])
            self.assertTrue(second['unchanged'])
            self.assertEqual(reads.call_count, 1)
            agent.rename('test-chat', '新名')
            renamed = server.chat('test-chat', since=first['revision'])
            self.assertEqual(renamed['title'], '新名')
            self.assertNotEqual(first['revision'], renamed['revision'])

    def test_backup_import_rolls_back_new_data_on_commit_failure(self):
        self.chat('existing', title='保留本机记录')
        record = {'id': 'incoming', 'title': '导入', 'messages': []}
        archive = self.make_archive({
            'chats/incoming.json': json.dumps(record), 'chats/incoming/0.png': 'image',
            'research/session/meta.json': json.dumps({'id': 'session', 'status': 'done'}),
        })
        rename = Path.rename
        def fail_research(path, target):
            if path.name == 'session':
                raise OSError('disk full')
            return rename(path, target)
        with patch.object(Path, 'rename', fail_research):
            with self.assertRaises(OSError):
                backup.import_from(archive)
        self.assertFalse(agent._path('incoming').exists())
        self.assertFalse(agent.image_dir('incoming').exists())
        self.assertEqual(agent.load('existing')['title'], '保留本机记录')

    def test_import_discards_rendered_html_and_rebuilds_portable_history(self):
        record = {'id': 'incoming', 'title': '导入', 'backend': 'api', 'model_config': {'api': {'api_key': 'old-secret'}},
                  'messages': [{'role': 'assistant', 'draft': '', 'draft_html': '<img src=x onerror=alert(1)>',
                                'parts': [{'type': 'text', 'text': '**保留正文**', 'html': '<script>evil</script>'}]}]}
        archive = self.make_archive({'chats/incoming.json': json.dumps(record)})
        backup.import_from(archive)
        chat = agent.load('incoming')
        self.assertNotIn('draft_html', chat['messages'][0])
        self.assertNotIn('html', chat['messages'][0]['parts'][0])
        self.assertNotIn('api_key', chat['model_config']['api'])
        self.assertIn('保留正文', chat['llm_history'][0]['content'])
        self.assertIn('<strong>', server.chat('incoming')['messages'][0]['parts'][0]['html'])

    def test_backup_is_unavailable_to_authenticated_remote_clients(self):
        from starlette.requests import Request
        from unittest.mock import AsyncMock
        next_handler = AsyncMock()
        with patch.object(server.remote, 'is_remote', return_value=True), patch.object(server.remote, 'key_ok', return_value=True):
            request = Request({'type': 'http', 'method': 'POST', 'path': '/api/backup/export', 'query_string': b'',
                               'scheme': 'http', 'server': ('localhost', 8766), 'headers': [(b'host', b'localhost:8766')]})
            response = asyncio.run(server.remote_guard(request, next_handler))
            self.assertEqual(response.status_code, 403)
            next_handler.assert_not_awaited()

    def test_progress_only_uses_sessions_owned_by_the_current_step(self):
        with patch.object(store, 'list_sessions', return_value=[{'id': 'other', 'status': 'running'}, {'id': 'own', 'status': 'running'}]), \
             patch.object(store, 'read_meta', return_value={'log': ['正在打开当前笔记']}) as read:
            self.assertIsNone(server._running_progress(set()))
            self.assertEqual(server._running_progress({'own'})['session'], 'own')
            read.assert_called_once_with('own')

    def test_trash_restore_preserves_images_and_does_not_send_queue(self):
        self.chat(queued=[{'text': '排队消息', 'images': ['queued.png']}])
        images = agent.image_dir('test-chat')
        images.mkdir()
        (images / 'queued.png').write_bytes(b'image')
        agent.delete('test-chat')
        self.assertEqual(agent.list_chats(), [])
        self.assertEqual(agent.list_trash()[0]['id'], 'test-chat')
        self.assertFalse(agent._path('test-chat').exists())
        agent.restore('test-chat')
        self.assertEqual((images / 'queued.png').read_bytes(), b'image')
        chat = agent.load('test-chat')
        self.assertEqual(chat['messages'][1]['status'], 'stopped')
        self.assertEqual(chat['messages'][-1]['text'], '排队消息')
        self.assertTrue(chat['messages'][-1]['recovered_queue'])
        self.assertNotIn('queued', chat)
        agent._send_queued_later.assert_not_called()
        self.assertEqual(agent.list_trash(), [])
        agent.delete('test-chat')
        agent.purge('test-chat')
        self.assertEqual(agent.list_trash(), [])
        with self.assertRaises(FileNotFoundError):
            agent.restore('test-chat')

    def test_restore_collision_keeps_both_records_and_failed_save_rolls_back(self):
        self.chat(title='原对话')
        agent.image_dir('test-chat').mkdir()
        agent.delete('test-chat')
        self.chat(title='新对话')
        with self.assertRaises(RuntimeError):
            agent.restore('test-chat')
        self.assertEqual(agent.load('test-chat')['title'], '新对话')
        agent._path('test-chat').unlink()
        with patch.object(agent, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                agent.restore('test-chat')
        self.assertTrue((agent.CHATS_DIR / '.trash/test-chat/images').exists())
        self.assertFalse(agent.image_dir('test-chat').exists())

    def test_pin_archive_and_scoped_date_search_preserve_order(self):
        self.chat('old', title='标题身份证', updated='2026-10-01 12:00', messages=[
            {'role': 'user', 'text': '黄启亮'},
            {'role': 'assistant', 'parts': [{'type': 'text', 'text': '身份证'}]},
        ])
        self.chat('new', updated='2026-11-13 12:00')
        st = agent._path('old').stat()
        agent.organize('old', pinned=True)
        self.assertEqual(agent._path('old').stat().st_mtime_ns, st.st_mtime_ns)
        self.assertEqual(agent.list_chats()[0]['id'], 'old')
        self.assertEqual(len(server.chats('身份证 黄启亮')), 1)
        self.assertEqual(server.chats('身份证 黄启亮', scope='user'), [])
        self.assertEqual(server.chats('身份证', scope='user'), [])
        self.assertEqual(len(server.chats('身份证', scope='assistant')), 1)
        self.assertEqual(server.chats('身份证', since='2026-11-01'), [])
        self.assertEqual(len(server.chats('身份证', until='2026-10-01')), 1)
        agent.organize('old', archived=True)
        self.assertEqual([c['id'] for c in agent.list_chats()], ['new'])
        self.assertEqual([c['id'] for c in agent.list_chats(view='archived')], ['old'])
        self.assertEqual(len(agent.list_chats(view='all')), 2)
        agent.organize('old', archived=False, pinned=False)
        self.assertEqual(agent.list_chats()[0]['id'], 'new')
        for args in ({'scope': 'bad'}, {'since': 'invalid'}, {'since': '2026-11-14', 'until': '2026-11-13'}):
            with self.assertRaises(server.HTTPException):
                server.chats(**args)

    def make_archive(self, extras=None):
        f = self.tmp / 'custom.zip'
        with zipfile.ZipFile(f, 'w') as z:
            z.writestr('manifest.json', json.dumps(backup.MANIFEST))
            for name, content in (extras or {}).items():
                z.writestr(name, content)
        return f

    def test_backup_round_trip_with_attachments_trash_notes_and_duplicate_import(self):
        self.chat('active', claude_session='local-session', codex_thread='local-thread',
                  queued=[{'text': '未发送', 'images': []}])
        self.chat('deleted')
        agent.image_dir('active').mkdir()
        (agent.image_dir('active') / '000_0.png').write_bytes(b'chat image')
        agent.delete('deleted')
        note = store.RESEARCH_DIR / 'session'
        atomic_json(note / 'meta.json', {'id': 'session', 'status': 'done'})
        atomic_json(note / 'notes.json', [{'id': 'note', 'title': '证件'}])
        (note / 'images/note').mkdir(parents=True)
        (note / 'images/note/1.jpg').write_bytes(b'note image')
        (self.tmp / 'settings.json').write_text('{"api_key":"secret"}')
        archive = self.tmp / 'history.zip'
        backup.export_to(archive)
        with zipfile.ZipFile(archive) as z:
            self.assertNotIn('settings.json', z.namelist())
            self.assertIn('research/session/images/note/1.jpg', z.namelist())
        with patch.object(agent, 'CHATS_DIR', self.tmp / 'new/chats'), patch.object(store, 'RESEARCH_DIR', self.tmp / 'new/research'):
            result = backup.import_from(archive)
            self.assertEqual(result, {'imported': 1, 'skipped': 0, 'research': 1, 'trash': 1})
            restored = agent.load('active')
            self.assertNotIn('codex_thread', restored)
            self.assertNotIn('claude_session', restored)
            self.assertEqual(restored['messages'][1]['status'], 'stopped')
            self.assertEqual(restored['messages'][-1]['text'], '未发送')
            self.assertEqual((agent.image_dir('active') / '000_0.png').read_bytes(), b'chat image')
            self.assertEqual((store.RESEARCH_DIR / 'session/images/note/1.jpg').read_bytes(), b'note image')
            agent.rename('active', '本机修改')
            self.assertEqual(backup.import_from(archive)['skipped'], 2)
            self.assertEqual(agent.load('active')['title'], '本机修改')
            agent.restore('deleted')
            self.assertEqual(len(agent.list_chats()), 2)

    def test_import_validates_all_files_before_committing_and_rejects_unsafe_archives(self):
        valid = {'id': 'valid', 'title': '对话', 'messages': []}
        invalid_paths = ('../outside', '/tmp/outside', 'chats/../outside', 'chats//valid.json',
                         'settings.json', 'chats/valid/../../outside', 'chats\\valid.json')
        for name in invalid_paths:
            with self.subTest(name=name):
                f = self.make_archive({'chats/valid.json': json.dumps(valid), name: 'bad'})
                with self.assertRaises(ValueError):
                    backup.import_from(f)
                self.assertFalse(agent._path('valid').exists())
        f = self.make_archive({'chats/valid.json': json.dumps(valid), 'chats/bad.json': '{}'})
        with self.assertRaises(ValueError):
            backup.import_from(f)
        self.assertFalse(agent._path('valid').exists())
        f = self.make_archive()
        with zipfile.ZipFile(f, 'a') as z:
            info = zipfile.ZipInfo('chats/link.json')
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, '/tmp/target')
        with self.assertRaises(ValueError):
            backup.import_from(f)

    def test_backup_and_import_reject_running_workers(self):
        f = self.make_archive()
        run = Mock()
        run.alive.return_value = True
        agent._runs['active'] = run
        with self.assertRaises(RuntimeError):
            backup.export_to(self.tmp / 'out.zip')
        with self.assertRaises(RuntimeError):
            backup.import_from(f)


if __name__ == '__main__':
    unittest.main()
