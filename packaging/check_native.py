"""Exercise the actual native WebView against an isolated source or frozen server.

Run with the build Python, optionally --binary '.../XHS Research Agent'. No login,
model call, or user history is used. Only files created by this run are removed.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j2ioAAAAASUVORK5CYII='


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary')
    args = parser.parse_args()
    # WebView2 can release its profile handles shortly after the window exits.
    with tempfile.TemporaryDirectory(prefix='xhs-native-', ignore_cleanup_errors=True) as folder:
        data = Path(folder)
        os.environ['XHS_DATA_DIR'] = str(data)
        os.environ['XHS_UPDATE_API'] = 'http://127.0.0.1:1/no-update'
        from xhs_reader import agent, app, server, settings
        import webview
        settings.save({'backend':'api','ui':'window','api':{'provider':'custom','base_url':'http://127.0.0.1:1','model':'fixture','api_key':'fixture-only'}})
        agent._save({'id':'upgrade-chat','title':'升级前的对话','backend':'api','messages':[
            {'role':'user','text':'中断前的问题','images':[]},
            {'role':'assistant','status':'running','draft':'中断前的部分回答','parts':[]}],
            'queued':[{'text':'尚未发送的问题','images':[]}]})
        agent._save({'id':'second','title':'另一段对话','backend':'api','messages':[]})
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        process = None; http_server = None; created_files = []
        def http(path, method='GET'):
            with opener.open(urllib.request.Request(url+path,method=method),timeout=10) as response:
                return json.load(response)
        if args.binary:
            env = dict(os.environ, XHS_NO_BROWSER='1', XHS_PORT=str(port))
            process = subprocess.Popen([args.binary],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        else:
            import uvicorn
            agent.recover()
            http_server = uvicorn.Server(uvicorn.Config(server.app,host='127.0.0.1',port=port,log_level='error'))
            threading.Thread(target=http_server.run,daemon=True).start()
        try:
            deadline = time.monotonic()+30
            while True:
                try: http('/api/ping'); break
                except Exception:
                    if time.monotonic()>deadline: raise RuntimeError('native test server did not start')
                    time.sleep(.1)
            result = {}; started = threading.Event(); original_create = webview.create_window
            def create(*a, **kw):
                window = original_create(*a, **kw)
                def evaluate(code):
                    finished = threading.Event(); value = []
                    def done(response): value.append(response); finished.set()
                    window.evaluate_js('(async()=>{try{'+code+'}catch(e){return {ok:false,error:String(e.stack||e)}}})()',callback=done)
                    if not finished.wait(30): raise RuntimeError('native JavaScript timed out')
                    if not value or not isinstance(value[0],dict) or not value[0].get('ok'):
                        raise AssertionError(str(value))
                    return value[0]
                def wait_ready(reloaded=False):
                    deadline = time.monotonic()+30
                    while time.monotonic()<deadline:
                        try:
                            if window.evaluate_js("typeof activeDraftKey!=='undefined' && typeof lastChat!=='undefined' && (!window.__nativePhase) && typeof showHistoryManagement==='function'"):
                                return
                        except Exception: pass
                        time.sleep(.1)
                    try:
                        state = window.evaluate_js("({url:String(location.href),ready:document.readyState,title:document.title,input:!!document.getElementById('input'),script:typeof showHistoryManagement,bridge:typeof window.pywebview})")
                    except Exception as e: state = str(e)
                    raise RuntimeError('native page did not become ready: '+json.dumps({'renderer':str(window.gui.renderer),'state':state},ensure_ascii=True))
                def test():
                    if started.is_set(): return
                    started.set()
                    try:
                        print(json.dumps({'native_renderer':str(window.gui.renderer)}),flush=True)
                        wait_ready()
                        evaluate("""
                            document.querySelector('#notice').classList.remove('show');
                            await openChat('upgrade-chat');
                            if(!document.querySelector('[data-continue]') || !document.querySelector('[data-reuse]')) throw Error('interrupted recovery missing');
                            document.querySelector('#input').value='原生窗口草稿'; draftEdits++;
                            pending=[{media_type:'image/png',data:'"""+PNG+"""',url:'data:image/png;base64,"""+PNG+"""'}];
                            await saveComposerDraft(); await draftWrites;
                            await openChat('second'); await openChat('upgrade-chat');
                            if(document.querySelector('#input').value!=='原生窗口草稿' || pending.length!==1) throw Error('native draft switch failed');
                            window.__nativePhase='reload'; return {ok:true};
                        """)
                        # WebView2 treats an identical fragment URL as same-document
                        # navigation. Force a new document so its bridge-ready event resets.
                        window.load_url(url+'/?native_reload=1#c=upgrade-chat')
                        wait_ready(True)
                        saved = evaluate("""
                            document.querySelector('#notice').classList.remove('show'); await openChat('upgrade-chat'); await draftLoadPromise;
                            if(document.querySelector('#input').value!=='原生窗口草稿' || pending.length!==1) throw Error('native persistent draft failed');
                            await showHistoryManagement();
                            const drafts=await collectDrafts(); if(drafts.length!==1) throw Error('draft management list failed');
                            const draft=await downloadDrafts(drafts);
                            const backup=await api('/api/backup/export?save=true',{method:'POST'});
                            return {ok:true,draft:draft.saved,backup:backup.saved};
                        """)
                        for name in (saved['draft'],saved['backup']):
                            p=Path(name)
                            if not p.is_file() or not p.name.startswith('种草调研助手-'): raise AssertionError('native export missing')
                            created_files.append(p)
                        draft=json.loads(Path(saved['draft']).read_text(encoding='utf-8'))
                        if draft['drafts'][0]['text']!='原生窗口草稿' or len(draft['drafts'][0]['images'])!=1: raise AssertionError('draft export lost data')
                        encoded=base64.b64encode(Path(saved['backup']).read_bytes()).decode()
                        evaluate("""
                            const bytes=Uint8Array.from(atob('"""+encoded+"""'),c=>c.charCodeAt(0));
                            const dt=new DataTransfer(); dt.items.add(new File([bytes],'native-backup.zip',{type:'application/zip'}));
                            const file=document.querySelector('#backupFile'); file.files=dt.files; file.dispatchEvent(new Event('change'));
                            const wait=async fn=>{for(let i=0;i<200;i++){if(fn())return;await new Promise(r=>setTimeout(r,50));}throw Error('native import timeout')};
                            await wait(()=>!!document.querySelector('#backupConfirm'));
                            document.querySelector('#backupConfirm').click();
                            await wait(()=>document.querySelector('#backupResult').textContent.includes('跳过 2'));
                            await showDiagnostics({operation:'原生窗口验证',error:'Authorization: Bearer native-secret\\npassword=native-password'});
                            const report=document.querySelector('#diagnosticReport').textContent;
                            if(report.includes('native-secret')||report.includes('native-password'))throw Error('diagnostics leaked credentials');
                            if(document.querySelector('#diagnosticCopy').disabled)throw Error('diagnostic copy unavailable');
                            return {ok:true};
                        """)
                        result.update(ok=True, renderer=str(window.gui.renderer), persistent_drafts=True, native_exports=True, zip_file_input=True, duplicate_import=True, recovery=True, diagnostics=True)
                    except Exception as e: result.update(ok=False,error=str(e))
                    finally:
                        # Uses the real window's quit path (sets quitting before destroy on Windows).
                        server.on_shutdown()
                window.events.loaded += lambda: threading.Thread(target=test,daemon=True).start()
                return window
            with patch.object(webview,'create_window',create):
                if not app._run_window(url,port): raise RuntimeError('native WebView unavailable')
            if not result.get('ok'): raise AssertionError(result)
            print(json.dumps(result,ensure_ascii=True))
        finally:
            try: http('/api/shutdown','POST')
            except Exception: pass
            if process:
                try: process.wait(10)
                except subprocess.TimeoutExpired: process.terminate(); process.wait(5)
            if http_server: http_server.should_exit=True
            for path in created_files:
                if path.name.startswith('种草调研助手-'): path.unlink(missing_ok=True)


if __name__=='__main__': main()
