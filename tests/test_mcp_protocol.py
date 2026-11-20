import json
import os
import subprocess
import sys

from test_optimizations import IsolatedTest
from xhs_reader import store


class McpProtocolTests(IsolatedTest):
    def test_stdio_is_utf8_and_survives_invalid_requests(self):
        sid = "测试会话"
        store.write_meta(sid, {"id": sid, "keyword": "中文笔记"})
        requests = [
            b"null",
            b"\xff",
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":42}}',
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "read_previous_notes", "arguments": {"session": sid}}},
                       ensure_ascii=False).encode("utf-8"),
        ]
        proc = subprocess.run(
            [sys.executable, "-m", "xhs_reader.cli", "mcp"],
            env=dict(os.environ, XHS_DATA_DIR=str(self.tmp), PYTHONUTF8="0", PYTHONIOENCODING="cp1252"),
            input=b"\n".join(requests) + b"\n", capture_output=True, timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))
        invalid, invalid_bytes, invalid_args, listing, call = map(json.loads, proc.stdout.splitlines())
        self.assertEqual(invalid["error"]["code"], -32600)
        self.assertEqual(invalid_bytes["error"]["code"], -32700)
        self.assertEqual(invalid_args["error"]["code"], -32602)
        self.assertIn("小红书", listing["result"]["tools"][0]["description"])
        self.assertFalse(call["result"]["isError"])
        self.assertIn("中文笔记", call["result"]["content"][0]["text"])
