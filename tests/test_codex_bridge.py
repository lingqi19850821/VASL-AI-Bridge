"""Offline app-server protocol tests; no executable, login, or API is contacted."""

import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from vasl_ai_bridge.codex_bridge import (
    ASL_INSTRUCTIONS, BridgeError, CodexBridge, DYNAMIC_TOOLS,
    MAX_TOOL_TEXT_CHARS, RUNTIME_CONFIG, _inputs,
)


PNG_URL = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\noriginal").decode()


class FakeOutput:
    def __init__(self):
        self.lines = queue.Queue()

    def readline(self, size):
        return self.lines.get(timeout=5)

    def close(self):
        self.lines.put("")


class FakeInput:
    def __init__(self, process):
        self.process = process

    def write(self, line):
        request = json.loads(line)
        self.process.requests.append(request)
        self.process.respond(request)

    def flush(self):
        pass

    def close(self):
        pass


class FakeProcess:
    """A queue-backed JSONL server, including notifications and server requests."""

    def __init__(self):
        self.stdout = FakeOutput()
        self.stdin = FakeInput(self)
        self.requests = []
        self.returncode = None
        self.turns = 0
        self.mode = None

    def emit(self, frame):
        self.stdout.lines.put(json.dumps(frame) + "\n")

    def respond(self, request):
        if "method" not in request or "id" not in request:
            return
        method = request["method"]
        params = request.get("params", {})
        result = {}
        if method == "initialize":
            result = {"userAgent": "fake-app-server"}
        elif method == "account/login/start":
            if params["type"] == "chatgpt":
                result = {"type": "chatgpt", "loginId": "login-1", "authUrl": "https://chatgpt.com/test-login"}
            else:
                self.mode = "apiKey"
                result = {"type": "apiKey"}
        elif method == "account/read":
            result = {"account": {"type": self.mode} if self.mode else None,
                      "requiresOpenaiAuth": True}
        elif method == "account/logout":
            self.mode = None
        elif method == "model/list":
            if params.get("cursor"):
                result = {"data": [{"id": "model-2", "model": "model-2", "displayName": "Second model"}], "nextCursor": None}
            else:
                result = {"data": [{"id": "gpt-6.1-sol", "model": "gpt-6.1-sol", "displayName": "GPT-6.1 Sol"}], "nextCursor": "next"}
        elif method in {"thread/start", "thread/resume"}:
            result = {"thread": {"id": params.get("threadId", "thread-1")}}
        elif method == "turn/start":
            self.turns += 1
            result = {"turn": {"id": f"turn-{self.turns}", "status": "inProgress"}}
        elif method == "turn/steer":
            result = {"turnId": params["expectedTurnId"]}
        elif method == "never/reply":
            return
        self.emit({"id": request["id"], "result": result})

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0
        self.stdout.lines.put("")

    def kill(self):
        self.terminate()

    def wait(self, timeout):
        self.returncode = 0
        return 0


class CodexBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.process = FakeProcess()
        self.launch = patch("vasl_ai_bridge.codex_bridge.subprocess.Popen", return_value=self.process).start()
        self.addCleanup(patch.stopall)
        self.bridge = CodexBridge(Path(self.temporary.name) / "runtime", executable="fake-codex", rpc_timeout=0.2)
        self.addCleanup(self.bridge.close)

    def start(self):
        self.bridge.start()
        self.bridge.start_thread()

    def event(self, method):
        for _ in range(20):
            result = self.bridge.poll_event(timeout=0.05)
            if result and result.get("method") == method:
                return result
        self.fail(f"Expected {method} notification")

    def request(self, method):
        return next(request for request in reversed(self.process.requests) if request.get("method") == method)

    def test_constructor_does_not_launch_or_create_runtime(self):
        self.launch.assert_not_called()
        self.assertFalse(self.bridge.runtime_dir.exists())
        self.assertFalse(self.bridge.authenticated)

    def test_start_is_isolated_and_ephemeral_without_inference_or_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "parent-secret", "CODEX_ACCESS_TOKEN": "parent-token"}):
            self.bridge.start()
        call = self.launch.call_args
        command = call.args[0]
        environment = call.kwargs["env"]
        self.assertNotIn("parent-secret", str(command))
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("CODEX_ACCESS_TOKEN", environment)
        self.assertEqual(Path(environment["CODEX_HOME"]), self.bridge.runtime_dir / "codex-home")
        self.assertIn('cli_auth_credentials_store="ephemeral"', command)
        self.assertIn("features.shell_tool=false", command)
        self.assertIn("features.unified_exec=false", command)
        self.assertIn("features.code_mode_host=true", command)
        self.assertIn('approval_policy="on-request"', command)
        self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual([frame["method"] for frame in self.process.requests], ["initialize", "initialized"])
        self.assertTrue(self.request("initialize")["params"]["capabilities"]["experimentalApi"])
        self.assertFalse((self.bridge.runtime_dir / "codex-home" / "auth.json").exists())

    def test_authentication_is_explicit_and_key_does_not_enter_command_or_disk(self):
        self.bridge.start()
        info = self.bridge.login("chatgpt")
        self.assertEqual(info["authUrl"], "https://chatgpt.com/test-login")
        self.assertFalse(self.bridge.authenticated)
        self.bridge.login("apiKey", "test-api-secret")
        self.assertTrue(self.bridge.authenticated)
        self.assertEqual(self.bridge.account()["account"]["type"], "apiKey")
        self.assertNotIn("test-api-secret", str(self.launch.call_args))
        self.assertEqual(self.request("account/login/start")["params"]["apiKey"], "test-api-secret")
        for path in self.bridge.runtime_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"test-api-secret", path.read_bytes())
        self.bridge.logout()
        self.assertFalse(self.bridge.authenticated)
        self.assertFalse(self.bridge._secrets)

    def test_subscription_local_completion_cancel_and_logout_are_exposed(self):
        self.bridge.start()
        self.bridge.login()
        self.assertEqual(self.request("account/login/start")["params"],
                         {"type": "chatgpt", "useHostedLoginSuccessPage": False,
                          "codexStreamlinedLogin": False})
        self.process.emit({"method": "account/updated", "params": {"authMode": "chatgpt", "planType": "plus"}})
        self.process.emit({"method": "account/login/completed", "params": {"loginId": "login-1", "success": True, "error": None}})
        self.event("account/login/completed")
        self.assertTrue(self.bridge.authenticated)
        self.bridge.cancel_login("login-1")
        self.assertEqual(self.request("account/login/cancel")["params"], {"loginId": "login-1"})
        self.bridge.logout()
        self.assertEqual(self.request("account/logout")["params"], {})
        self.assertFalse(self.bridge.authenticated)
        self.assertIsNone(self.bridge.auth_mode)
        self.assertIsNone(self.bridge.account()["account"])

    def test_model_catalog_is_paginated_without_model_substitution(self):
        self.bridge.start()
        self.assertEqual([row["model"] for row in self.bridge.models()], ["gpt-6.1-sol", "model-2"])
        self.bridge.start_thread("user-chosen-model")
        params = self.request("thread/start")["params"]
        self.assertEqual(params["model"], "user-chosen-model")
        self.assertFalse(params["allowProviderModelFallback"])
        self.assertEqual(params["sandbox"], "read-only")
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertEqual(params["environments"], [])
        self.assertEqual(params["baseInstructions"], ASL_INSTRUCTIONS)
        self.assertEqual(params["developerInstructions"], "")

    def test_default_prompt_is_sent_once_on_thread_start_and_resume(self):
        self.bridge.start()
        for supplied in (False, True):
            arguments = {"instructions": ""} if supplied else {}
            for method, action in (("thread/start", self.bridge.start_thread),
                                   ("thread/resume", lambda **kwargs: self.bridge.resume("old-game", **kwargs))):
                with self.subTest(method=method, explicit_empty_instructions=supplied):
                    action(**arguments)
                    params = self.request(method)["params"]
                    self.assertEqual(params["baseInstructions"], ASL_INSTRUCTIONS)
                    self.assertEqual(params["developerInstructions"], "")
                    instruction_values = [params[key] for key in ("baseInstructions", "developerInstructions")]
                    self.assertEqual([value for value in instruction_values if value], [ASL_INSTRUCTIONS])

    def test_custom_prompt_replaces_default_on_thread_start_and_resume(self):
        self.bridge.start()
        custom = "Use this game's supplied opponent instructions.\nKeep the selected language."
        for method, action in (("thread/start", self.bridge.start_thread),
                               ("thread/resume", lambda **kwargs: self.bridge.resume("old-game", **kwargs))):
            with self.subTest(method=method):
                action(instructions=custom)
                params = self.request(method)["params"]
                self.assertEqual(params["baseInstructions"], custom)
                self.assertEqual(params["developerInstructions"], "")
                instruction_values = [params[key] for key in ("baseInstructions", "developerInstructions")]
                self.assertEqual([value for value in instruction_values if value], [custom])

    def test_streaming_messages_images_steering_interrupt_and_resume(self):
        self.start()
        self.bridge.start_turn("Observe this board.", [PNG_URL])
        self.assertEqual(self.request("turn/start")["params"]["input"], [
            {"type": "text", "text": "Observe this board."}, {"type": "image", "url": PNG_URL},
        ])
        self.process.emit({"method": "item/agentMessage/delta", "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "message-1", "delta": "Move to K9."}})
        self.assertEqual(self.event("item/agentMessage/delta")["params"]["delta"], "Move to K9.")
        self.bridge.steer("Actually the squad is at K9.")
        self.assertEqual(self.request("turn/steer")["params"]["expectedTurnId"], "turn-1")
        self.bridge.interrupt()
        self.assertEqual(self.request("turn/interrupt")["params"], {"threadId": "thread-1", "turnId": "turn-1"})
        self.process.emit({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "interrupted", "error": None}}})
        self.event("turn/completed")
        self.assertIsNone(self.bridge.turn_id)
        self.assertEqual(self.bridge.resume("saved-thread"), "saved-thread")
        self.bridge.start_turn("Continue the same game.")
        self.assertEqual(self.request("turn/start")["params"]["threadId"], "saved-thread")

    def test_reasoning_effort_is_sent_each_turn_and_unknown_values_are_rejected(self):
        self.start()
        self.bridge.start_turn("Use the selected highest level.", effort="ultra")
        self.assertEqual(self.request("turn/start")["params"]["effort"], "ultra")
        self.process.emit({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}})
        self.event("turn/completed")
        self.bridge.start_turn("Return to a reported default.", effort="low")
        self.assertEqual(self.request("turn/start")["params"]["effort"], "low")
        self.process.emit({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-2", "status": "completed"}}})
        self.event("turn/completed")
        with self.assertRaises(BridgeError):
            self.bridge.start_turn("Unsupported effort", effort="made-up")

    def test_new_threads_advertise_reference_image_reader_and_resume_preserves_instructions(self):
        self.bridge.tool_handler = lambda name, args: {}
        self.bridge.start()
        self.bridge.start_thread()
        names = {tool["name"] for tool in self.request("thread/start")["params"]["dynamicTools"]}
        self.assertIn("read_reference_image", names)
        self.assertNotIn("read_reference_page", names)
        self.bridge.resume("old-game", model="gpt-6-astra", instructions="Game instructions")
        params = self.request("thread/resume")["params"]
        self.assertEqual(params["baseInstructions"], "Game instructions")
        self.assertEqual(params["developerInstructions"], "")
        self.assertNotIn("dynamicTools", params)

    def test_compaction_does_not_replace_or_truncate_original_chat_text(self):
        self.start()
        text = "first-turn strategy " * 2000
        self.bridge.start_turn(text)
        self.assertEqual(self.request("turn/start")["params"]["input"][0]["text"], text)
        for method in ("item/started", "item/completed"):
            notification = {"method": method, "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {"id": "compact-1", "type": "contextCompaction"}}}
            self.process.emit(notification)
            self.assertEqual(self.event(method), notification)
        self.assertEqual(self.bridge.thread_id, "thread-1")
        self.assertEqual(self.bridge.turn_id, "turn-1")

    def test_fast_completed_turn_cannot_become_active_again(self):
        self.start()
        original = self.process.respond

        def complete_first(request):
            if request.get("method") == "turn/start":
                self.process.emit({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}})
            original(request)

        self.process.respond = complete_first
        self.assertEqual(self.bridge.start_turn("A short question."), "turn-1")
        self.assertIsNone(self.bridge.turn_id)

    def test_declared_tools_return_original_images_and_reject_other_requests(self):
        self.bridge.tool_handler = lambda name, args: {"success": True, "contentItems": [{"type": "inputImage", "imageUrl": PNG_URL}]}
        self.start()
        self.assertEqual(len(self.request("thread/start")["params"]["dynamicTools"]), len(DYNAMIC_TOOLS))
        self.bridge._answer_server_request({"id": 80, "method": "item/tool/call", "params": {"tool": "read_game_image", "threadId": "thread-1", "arguments": {"observation_id": "image-1"}}})
        self.assertEqual(self.process.requests[-1]["result"]["contentItems"][0]["imageUrl"], PNG_URL)
        self.bridge._answer_server_request({"id": 81, "method": "item/tool/call", "params": {"tool": "shell", "threadId": "thread-1", "arguments": {"command": "anything"}}})
        self.assertFalse(self.process.requests[-1]["result"]["success"])
        self.bridge._answer_server_request({"id": 82, "method": "item/commandExecution/requestApproval", "params": {}})
        self.assertEqual(self.process.requests[-1]["result"], {"decision": "decline"})
        self.bridge._answer_server_request({"id": 83, "method": "item/permissions/requestApproval", "params": {}})
        self.assertEqual(self.process.requests[-1]["result"]["permissions"], {})

    def test_images_cannot_read_files_or_fetch_remote_urls(self):
        for value in ("https://example.com/secret.png", "C:/private.png", "data:image/png;base64,not-base64", "data:image/png;base64,YWJj"):
            with self.assertRaises(BridgeError):
                _inputs("Inspect.", [value])
        with self.assertRaises(BridgeError):
            _inputs("", [])

    def test_timeout_cleans_pending_request_and_error_redacts_credentials(self):
        self.bridge.start()
        self.bridge.login("apiKey", "exact-private-secret")
        with self.assertRaises(BridgeError):
            self.bridge._rpc("never/reply", {})
        self.assertFalse(self.bridge._pending)
        self.assertNotIn("exact-private-secret", self.bridge._safe_error("Server rejected exact-private-secret and Bearer hidden-token"))
        self.assertNotIn("hidden-token", self.bridge._safe_error("Bearer hidden-token"))

    def test_missing_runtime_does_not_silently_fall_back_or_download(self):
        self.bridge.executable = None
        with patch("vasl_ai_bridge.codex_bridge.find_codex_executable", return_value=None):
            with self.assertRaisesRegex(BridgeError, "runtime was not found"):
                self.bridge.start()
        self.launch.assert_not_called()
        self.assertFalse(self.bridge.runtime_dir.exists())

    def test_reference_schema_matches_local_callback_pagination(self):
        tool = next(tool for tool in DYNAMIC_TOOLS if tool["name"] == "read_reference_file")
        self.assertIn("offset", tool["inputSchema"]["properties"])
        self.assertIn("max_chars", tool["inputSchema"]["properties"])
        self.assertFalse(RUNTIME_CONFIG["features.shell_tool"])
        self.assertTrue(RUNTIME_CONFIG["features.code_mode_host"])
        self.assertEqual(RUNTIME_CONFIG["features.code_mode.excluded_tool_namespaces"], ["clock"])
        self.assertEqual(RUNTIME_CONFIG["web_search"], "disabled")
        for feature in ("unified_exec", "apps", "plugins", "browser_use", "computer_use"):
            self.assertFalse(RUNTIME_CONFIG[f"features.{feature}"])
        message_tool = next(tool for tool in DYNAMIC_TOOLS if tool["name"] == "read_game_message")
        self.assertEqual(message_tool["inputSchema"]["required"], ["message_id"])
        self.assertEqual(message_tool["inputSchema"]["properties"]["max_chars"]["maximum"], 24000)

    def test_dynamic_tool_callback_rejects_foreign_threads_and_namespaces(self):
        called = []
        self.bridge.tool_handler = lambda name, args: called.append(name) or {"text": "allowed"}
        self.start()
        for params in (
            {"tool": "read_reference_file", "threadId": "different-thread", "arguments": {"reference_id": "rules"}},
            {"tool": "read_reference_file", "threadId": "thread-1", "namespace": "external", "arguments": {"reference_id": "rules"}},
            {"tool": "exec_command", "threadId": "thread-1", "arguments": {"cmd": "whoami"}},
        ):
            self.bridge._answer_server_request({"id": 90, "method": "item/tool/call", "params": params})
            self.assertFalse(self.process.requests[-1]["result"]["success"])
        self.assertEqual(called, [])

    def test_nested_error_diagnostics_redact_credentials_and_keep_normal_text(self):
        self.bridge._secrets.add("exact-private-secret")
        self.bridge._dispatch({"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-1", "status": "failed",
                "error": {"message": "Failed exact-private-secret", "details": [{"reason": "Bearer hidden-token"}]}}}})
        error = self.bridge.poll_event()["params"]["turn"]["error"]
        self.assertNotIn("exact-private-secret", json.dumps(error))
        self.assertNotIn("hidden-token", json.dumps(error))
        self.bridge._dispatch({"method": "error", "params": {"error": {
            "message": "Rejected exact-private-secret", "extra": {"credential": "sk-secret-token"}}}})
        self.assertNotIn("exact-private-secret", json.dumps(self.bridge.poll_event()))
        text = "Recorded strategic intention. " * 200
        self.bridge._dispatch({"method": "item/completed", "params": {
            "item": {"type": "agentMessage", "text": text}}})
        self.assertEqual(self.bridge.poll_event()["params"]["item"]["text"], text)

    def test_rpc_error_preserves_redacted_structured_access_details(self):
        self.bridge.start()
        self.bridge._secrets.add("exact-private-secret")
        def reject(request):
            self.process.emit({"id": request["id"], "error": {
                "code": -32000, "message": "Rejected exact-private-secret",
                "data": {"codexErrorInfo": "usageLimitExceeded", "detail": "Bearer hidden-token"}}})
        with patch.object(self.process, "respond", side_effect=reject):
            with self.assertRaises(BridgeError) as caught:
                self.bridge.models()
        error = caught.exception
        self.assertEqual(error.error_info["code"], -32000)
        self.assertEqual(error.error_info["data"]["codexErrorInfo"], "usageLimitExceeded")
        self.assertNotIn("exact-private-secret", str(error))
        self.assertNotIn("hidden-token", json.dumps(error.error_info))
        self.assertNotIn("exact-private-secret", json.dumps(error.error_info))

    def test_tool_output_keeps_valid_json_and_pagination_or_returns_bounded_error(self):
        value = {"text": "original source", "next_offset": 12000, "total_chars": 200000}
        result = self.bridge._tool_result(value)
        self.assertTrue(result["success"])
        self.assertEqual(json.loads(result["contentItems"][0]["text"]), value)
        too_large = self.bridge._tool_result({"text": "x" * MAX_TOOL_TEXT_CHARS, "next_offset": 12000})
        self.assertFalse(too_large["success"])
        self.assertIn("request fewer records", too_large["contentItems"][0]["text"])
        explicit = self.bridge._tool_result({"contentItems": [{"type": "inputText", "text": "x" * (MAX_TOOL_TEXT_CHARS + 1)}]})
        self.assertFalse(explicit["success"])


if __name__ == "__main__":
    unittest.main()

