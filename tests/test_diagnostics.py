import json
import asyncio
from pathlib import Path
from unittest.mock import patch

from test_optimizations import IsolatedTest
from xhs_reader import diagnostics, server, settings


class DiagnosticsTests(IsolatedTest):
    def test_native_draft_export_saves_exact_text_and_images(self):
        from starlette.requests import Request
        home = self.tmp / 'home'; home.mkdir()
        value = {'format':'xhs-drafts','version':1,'drafts':[{'text':'完整草稿','images':[{'media_type':'image/png','data':'aW1hZ2U='}]}]}
        body = json.dumps(value).encode()
        async def receive():
            return {'type':'http.request','body':body,'more_body':False}
        request = Request({'type':'http','method':'POST','path':'/api/drafts/export','headers':[],'query_string':b''},receive)
        with patch.object(Path, 'home', return_value=home):
            result = asyncio.run(server.export_drafts(request))
        self.assertEqual(json.loads(Path(result['saved']).read_text(encoding='utf-8')),value)
        self.assertTrue(Path(result['saved']).parent == home / 'Downloads')

    def test_redacts_configured_credentials_headers_urls_and_environment(self):
        settings.update(lambda s: s.update(api={**s['api'], 'api_key':'configured-secret'},
                                           api_credentials={'endpoint':'saved-secret'}, mobile={'token':'phone-secret'}))
        text = ('configured-secret saved-secret phone-secret environment-secret\n'
                'Authorization: Bearer unknown-auth\nCookie: a=first-cookie; b=second-cookie\n'
                'api_key="unknown-api" password=unknown-password token=unknown-token\n'
                'https://username:password@example.com/path?k=hidden-query#private-fragment\n'
                '/api/client?k=access-query sk-1234567890abcdefghijkl ghp_1234567890abcdefghijkl')
        with patch.dict('os.environ', {'CUSTOM_API_KEY':'environment-secret'}):
            result = json.loads(server.diagnostic_report(server.DiagnosticReq(operation='GET /api/chats?q=private-search',error=text))['report'])
        output = json.dumps(result)
        for secret in ('configured-secret','saved-secret','phone-secret','environment-secret','unknown-auth','first-cookie',
                       'second-cookie','unknown-api','unknown-password','unknown-token','username:password','hidden-query',
                       'private-fragment','access-query','private-search','sk-1234567890abcdefghijkl','ghp_1234567890abcdefghijkl'):
            self.assertNotIn(secret, output)
        self.assertIn('example.com/path', output)
        self.assertIn('应用版本', result)

    def test_report_does_not_read_logs_or_conversations(self):
        self.chat(title='私密标题', messages=[{'role':'user','text':'私密聊天正文'}])
        (self.tmp / 'app.log').write_text('私密日志内容')
        result = diagnostics.report('请求失败', '正常错误描述')
        for text in ('私密标题','私密聊天正文','私密日志内容','key-a'):
            self.assertNotIn(text,result)
        self.assertIn('正常错误描述', result)
