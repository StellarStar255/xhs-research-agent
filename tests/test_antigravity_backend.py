import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from test_optimizations import IsolatedTest
from xhs_reader import agent, antigravity_backend, procutil, server, settings


class AntigravityTests(IsolatedTest):
    def test_settings_select_backend_for_new_chat(self):
        with patch.object(settings, "find_antigravity", return_value="/tmp/agy"), \
             patch.object(antigravity_backend, "start") as start:
            server.save_settings(server.SettingsReq(backend="antigravity", api=server.ApiConf(),
                                                    antigravity_model="gemini-flash", antigravity_effort="low"))
            cid = agent.send(None, "开始调研")
            self.assertEqual(agent.load(cid)["backend"], "antigravity")
            self.assertEqual(start.call_args.args[1], "开始调研")
            self.assertEqual(start.call_args.args[0]["model_config"]["antigravity_model"], "gemini-flash")
            self.assertEqual(start.call_args.args[0]["model_config"]["antigravity_effort"], "low")

    def test_acp_applies_model_before_choosing_model_specific_effort(self):
        session = "session-id"
        model = {"id": "model", "type": "select", "currentValue": "pro",
                 "options": [{"value": "pro"}, {"value": "flash"}]}
        effort = {"id": "thought_level", "type": "select", "currentValue": "high",
                  "options": [{"value": "high"}]}
        flash = {**model, "currentValue": "flash"}
        flash_effort = {**effort, "options": [{"value": "high"}, {"value": "low"}]}
        results = [{"protocolVersion": 1}, {"sessionId": session, "configOptions": [model, effort]},
                   {"configOptions": [flash, flash_effort]},
                   {"configOptions": [flash, {**flash_effort, "currentValue": "low"}]},
                   {"stopReason": "end_turn"}]
        proc = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO("\n".join(
            json.dumps({"id": i, "result": result}) for i, result in enumerate(results, 1))))
        acp = antigravity_backend.Conversation("test-chat", proc, "test")
        acp.ask(str(self.tmp), [{"type": "text", "text": "你好"}], "flash", "low")
        messages = [json.loads(line) for line in proc.stdin.getvalue().splitlines()]
        self.assertEqual([m["method"] for m in messages[2:]],
                         ["session/set_config_option", "session/set_config_option", "session/prompt"])
        self.assertEqual([m["params"]["value"] for m in messages[2:4]], ["flash", "low"])

    def test_cli_auth_failure_finishes_turn_and_cleans_workspace(self):
        script = """
import json
import sys
hello = json.loads(input())
print(json.dumps({"id": hello["id"], "result": {"protocolVersion": 1}}), flush=True)
session = json.loads(input())
print(json.dumps({"id": session["id"], "error": {
    "code": -32000, "message": "Authentication required"}}), flush=True)
sys.stdin.read()
"""
        spawn = procutil.popen
        workspaces = []

        def fake_cli(cmd, **kwargs):
            self.assertEqual(cmd, ["/test/agy", "acp", "--agent", "xhs-research", "--sandbox"])
            workspaces.append(Path(kwargs["cwd"]))
            return spawn([sys.executable, "-u", "-c", script], **kwargs)

        self.stack.enter_context(patch.object(settings, "find_antigravity", return_value="/test/agy"))
        settings.update(lambda conf: conf.update(backend="antigravity"))
        with patch.object(procutil, "popen", side_effect=fake_cli):
            cid = agent.send(None, "开始调研")
            run = agent._runs.get(cid)
            if run:
                self.assertTrue(run.done.wait(5))
        reply = agent.load(cid)["messages"][-1]
        self.assertEqual(reply["status"], "error")
        self.assertIn("以 Google 账号登录", reply["error"])
        self.assertFalse(workspaces[0].exists())

    def test_acp_stream_and_permission_scope(self):
        self.chat()
        session = "session-id"
        tool = {"toolCallId": "search", "name": "mcp__xhs_test__search_xiaohongshu",
                "rawInput": {"keyword": "测试"}}
        options = [{"kind": "allow_once", "optionId": "allow"},
                   {"kind": "reject_once", "optionId": "reject"}]

        def notify(**update):
            return {"method": "session/update", "params": {"sessionId": session, "update": update}}

        def permission(rid, call):
            return {"id": rid, "method": "session/request_permission",
                    "params": {"sessionId": session, "toolCall": call, "options": options}}

        events = [
            {"id": 1, "result": {"protocolVersion": 1}},
            {"id": 2, "result": {"sessionId": session}},
            notify(sessionUpdate="tool_call", status="pending", **tool),
            permission(91, {"toolCallId": "search"}),
            permission(92, {"name": "run_command", "toolCallId": "shell"}),
            notify(sessionUpdate="tool_call_update", toolCallId="search", status="completed",
                   rawOutput={"content": [{"type": "text", "text": "SESSION result-id"}]}),
            notify(sessionUpdate="agent_message_chunk", content={"type": "text", "text": "调研完成"}),
            {"id": 3, "result": {"stopReason": "end_turn"}},
        ]
        proc = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(
            "\n".join(json.dumps(event) for event in events)))
        acp = antigravity_backend.Conversation("test-chat", proc, "test")
        antigravity_backend._write_workspace(self.tmp, "test")
        prompt = [{"type": "text", "text": "调研"}, {"type": "image", "mimeType": "image/png", "data": "YQ=="}]
        acp.ask(str(self.tmp), prompt)
        sent = {msg["id"]: msg for msg in map(json.loads, proc.stdin.getvalue().splitlines())}
        mcp = json.loads((self.tmp / ".agents" / "mcp_config.json").read_text())["mcpServers"]["xhs_test"]
        self.assertEqual(mcp["env"]["XHS_TURN_ID"], "test")
        self.assertTrue(mcp["tools"]["search_xiaohongshu"]["eager"])
        self.assertEqual(sent[2]["params"]["cwd"], str(self.tmp))
        self.assertEqual(sent[3]["params"]["prompt"], prompt)
        self.assertEqual(sent[91]["result"]["outcome"]["optionId"], "allow")
        self.assertEqual(sent[92]["result"]["outcome"]["optionId"], "reject")
        acp.flush_text()
        step, answer = agent.load("test-chat")["messages"][-1]["parts"]
        self.assertEqual((step["keyword"], step["session"], step["status"]), ("测试", "result-id", "done"))
        self.assertEqual(answer["text"], "调研完成")
