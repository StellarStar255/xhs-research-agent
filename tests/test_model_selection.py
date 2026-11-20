import os
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_optimizations import IsolatedTest
from xhs_reader import agent, codex_backend, llm, paths, server, settings
from xhs_reader.persistence import atomic_json


class ModelSelectionTests(IsolatedTest):
    def test_codex_keeps_the_message_from_a_cli_error_event(self):
        msg = {"status": "running"}
        codex_backend._handle({}, msg, {"type": "error", "message": "Codex model is unavailable"})
        codex_backend._handle({}, msg, {"type": "turn.failed", "error": {}})
        self.assertEqual(msg, {"status": "error", "error": "Codex model is unavailable"})

    @unittest.skipUnless(os.name == 'nt', 'Windows installer layout')
    def test_codex_desktop_install_is_discovered_without_path_after_a_miss(self):
        executable = self.tmp / 'Programs' / 'OpenAI' / 'Codex' / 'bin' / 'codex.exe'
        with patch.dict(os.environ, {'LOCALAPPDATA': str(self.tmp), 'XHS_CODEX_PATH': ''}), \
             patch.object(settings.shutil, 'which', return_value=None), \
             patch.object(settings, '_CLI_CANDIDATES', []):
            self.assertIsNone(settings.find_codex())
            executable.parent.mkdir(parents=True)
            executable.touch()
            self.assertEqual(Path(settings.find_codex()), executable)
            executable.unlink()
            self.assertIsNone(settings.find_codex())

    def test_custom_models_and_effort_roundtrip(self):
        with patch.object(settings, 'codex_models', return_value=[]):
            result = server.save_settings(server.SettingsReq(backend='codex', api=server.ApiConf(),
                codex_model='test-codex-model', codex_effort='high', claude_model='claude-custom-20260101', claude_effort='medium'))
        self.assertEqual(result['codex_model'], 'test-codex-model')
        self.assertEqual(result['codex_effort'], 'high')
        self.assertEqual(settings.load()['claude_model'], 'claude-custom-20260101')

    def test_bad_model_or_effort_rejected(self):
        for kw in ({'codex_model':'bad model'}, {'claude_model':'--option'}, {'codex_effort':'invalid'}):
            with self.subTest(kw=kw), self.assertRaises(server.HTTPException):
                server.save_settings(server.SettingsReq(backend='codex', api=server.ApiConf(), **kw))

    def test_only_cached_visible_models_and_supported_efforts_exposed(self):
        atomic_json(self.tmp/'models_cache.json', {'identity':'private identity', 'models':[
            {'slug':'test-model','display_name':'Test Model','visibility':'list',
             'supported_reasoning_levels':[{'effort':'low'},{'effort':'high'},{'effort':'bogus'}]},
            {'slug':'hidden-model','visibility':'hide'},
            {'slug':'test-model','visibility':'list'}]})
        with patch.dict(os.environ, {'CODEX_HOME':str(self.tmp)}):
            result = settings.codex_models()
        self.assertEqual(result, [{'id':'test-model','name':'Test Model','efforts':['low','high']}])

    def test_corrupt_or_missing_catalog_allows_custom_models(self):
        with patch.dict(os.environ, {'CODEX_HOME':str(self.tmp)}):
            self.assertEqual(settings.codex_models(), [])
            (self.tmp/'models_cache.json').write_text('{', encoding='utf-8')
            self.assertEqual(settings.codex_models(), [])

    def test_unsupported_model_effort_rejected(self):
        with patch.object(settings, 'codex_models', return_value=[{'id':'test-model','name':'Test','efforts':['low']}]), self.assertRaises(server.HTTPException):
            server.save_settings(server.SettingsReq(backend='codex', api=server.ApiConf(), codex_model='test-model', codex_effort='high'))

    def test_codex_model_and_effort_passed_for_new_and_resumed_turns(self):
        chat = self.chat(backend='codex', model_config={'codex_model':'pinned-model','codex_effort':'high'})
        for thread in (None, 'thread-id'):
            chat['codex_thread'] = thread
            with patch.object(settings, 'find_codex', return_value='codex'), patch.object(codex_backend, '_features_to_disable', return_value=[]), patch.object(codex_backend.procutil, 'popen') as spawn:
                codex_backend.start(chat, 'question', [], 'turn')
                cmd = spawn.call_args.args[0]
                self.assertEqual(cmd[cmd.index('--model')+1], 'pinned-model')
                self.assertIn('model_reasoning_effort="high"', cmd)
                self.assertEqual('resume' in cmd, bool(thread))

    def test_default_does_not_force_model_or_effort(self):
        chat = self.chat(backend='codex', model_config={'codex_model':'','codex_effort':''})
        with patch.object(settings, 'find_codex', return_value='codex'), patch.object(codex_backend, '_features_to_disable', return_value=[]), patch.object(codex_backend.procutil, 'popen') as spawn:
            codex_backend.start(chat, 'question', [], 'turn')
        cmd = spawn.call_args.args[0]
        self.assertNotIn('--model', cmd)
        self.assertFalse(any('model_reasoning_effort=' in value for value in cmd))

    def test_claude_custom_model_and_effort_passed(self):
        chat = self.chat(backend='claude', model_config={'claude_model':'claude-concrete-id', 'claude_effort':'medium'})
        with patch.object(settings, 'find_claude', return_value='claude'), patch.object(settings, 'claude_efforts', return_value=['low','medium','high']), patch.object(agent.procutil, 'popen') as spawn:
            agent._start_claude(chat, 'question', [], 'turn')
        cmd = spawn.call_args.args[0]
        self.assertEqual(cmd[cmd.index('--model')+1], 'claude-concrete-id')
        self.assertEqual(cmd[cmd.index('--effort')+1], 'medium')

    def test_old_claude_without_effort_support_fails_clearly(self):
        chat = self.chat(backend='claude', model_config={'claude_model':'sonnet', 'claude_effort':'high'})
        with patch.object(settings, 'claude_efforts', return_value=[]), self.assertRaisesRegex(RuntimeError, '不支持所选思考强度'):
            agent._start_claude(chat, 'question', [], 'turn')

    def test_effort_capability_detection_uses_local_help(self):
        with patch.object(settings, '_effort_options', {}), patch.object(settings, 'find_claude', return_value='claude'), patch.object(settings.subprocess, 'run', return_value=SimpleNamespace(stdout='  --effort <level>\n    (low, medium, high, max)\n  --model name')) as run:
            self.assertEqual(settings.claude_efforts(), ['low','medium','high','max'])
            settings.claude_efforts()
            self.assertEqual(run.call_count, 1)

    def test_chat_snapshot_survives_global_changes(self):
        settings.update(lambda s: s.update(codex_model='first-model', codex_effort='low'))
        run = Mock()
        run.alive.return_value = False
        with patch.object(llm, 'start', return_value=run):
            cid = agent.send(None, 'first')
            settings.update(lambda s: s.update(codex_model='second-model', codex_effort='high'))
            agent.send(cid, 'followup')
        config = agent.load(cid)['model_config']
        self.assertEqual(config['codex_model'], 'first-model')
        self.assertEqual(config['codex_effort'], 'low')

    def test_existing_codex_chat_keeps_previous_default(self):
        self.chat(model_config={'api':{'model':'model-a'}})
        settings.update(lambda s: s.update(codex_model='new-global-model', codex_effort='high'))
        with patch.object(llm, 'start', return_value=Mock()):
            agent.send('test-chat', 'followup')
        config = agent.load('test-chat')['model_config']
        self.assertEqual(config['codex_model'], '')
        self.assertEqual(config['codex_effort'], '')

    def test_fresh_codex_session_carries_imported_conversation_context(self):
        chat = self.chat(backend='codex', messages=[
            {'role': 'user', 'text': '之前的提问'},
            {'role': 'assistant', 'parts': [{'type': 'text', 'text': '之前的回答'}]},
            {'role': 'user', 'text': '接着问'},
            {'role': 'assistant', 'parts': []},
        ])
        real_popen = codex_backend.procutil.popen

        def echo_stdin(cmd, **kwargs):
            self.assertEqual(cmd[-1], '-')
            self.assertLess(len(subprocess.list2cmdline(cmd)), 32767)
            return real_popen([sys.executable, '-c',
                               'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'], **kwargs)

        question = '-开头的长追问😀' * 6000
        with patch.object(settings, 'find_codex', return_value='codex'), \
             patch.object(codex_backend, '_features_to_disable', return_value=[]), \
             patch.object(codex_backend.procutil, 'popen', side_effect=echo_stdin):
            for thread in (None, 'existing-thread'):
                chat['codex_thread'] = thread
                run = codex_backend.start(chat, question, [], 'turn')
                prompt, error = run.proc.communicate(timeout=10)
                self.assertEqual(run.proc.returncode, 0, error)
                if thread:
                    self.assertEqual(prompt, question)
                else:
                    self.assertIn('之前的提问', prompt)
                    self.assertIn('之前的回答', prompt)
                    self.assertTrue(prompt.endswith(question))

    def test_codex_mcp_configuration_accepts_unicode_windows_paths(self):
        directory = 'C:\\Users\\中文😀\\Project Data'
        value = {'path': directory, 'args': ['第一行\n第二行', '\x7f']}
        self.assertEqual(tomllib.loads(f'path={codex_backend._toml(value["path"])}\n'
                                       f'args={codex_backend._toml(value["args"])}'), value)

    def test_relative_data_override_stays_absolute_for_child_processes(self):
        with patch.dict(os.environ, {'XHS_DATA_DIR': 'tmp/relative-xhs-data'}):
            data_dir = paths._data_dir()
        with patch.object(paths, 'DATA_DIR', data_dir):
            self.assertEqual(paths.child_env()['XHS_DATA_DIR'], str(data_dir))
            self.assertEqual(data_dir, Path.cwd() / 'tmp' / 'relative-xhs-data')
