"""Offline boundary and image/tool-flow tests; never contact an API."""

import base64
import json
import struct
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import zlib

from vasl_ai_bridge.provider import (
    HTTP_TIMEOUT_SECONDS,
    MAX_RESPONSE_BYTES,
    OPENAI_ENDPOINT,
    OpenAIProvider,
    ProviderConfig,
    ProviderError,
    _RejectRedirect,
    _http_transport,
)
from vasl_ai_bridge.session import Observation


def image(number=1):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes([0, number, 0, 0, 255])))
    png += chunk(b"IEND", b"")
    return Observation(f"frame_{number}", f"2026-10-06T12:00:{number:02d}+00:00", "test window", png, 1, 1)


def response(output=None, **changes):
    value = {"status": "completed", "output": [{
        "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "Check LOS first."}],
    }] if output is None else output}
    value.update(changes)
    return json.dumps(value).encode()


def tool(call_id="call_1", name="look_now", arguments="{}"):
    return {"type": "function_call", "id": "fc_" + call_id,
            "call_id": call_id, "name": name, "arguments": arguments,
            "status": "completed"}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.events = []
        self.first = image()

    def transport(self, *replies):
        replies = iter(replies)

        def send(endpoint, body, headers, timeout):
            self.calls.append((endpoint, json.loads(body), headers, timeout))
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        return send

    def send(self, provider, **changes):
        args = {
            "context": "American Turn 2, Rally Phase. No action confirmed.",
            "user_message": "Check this position.", "observation": self.first,
            "look_now": lambda: image(2),
            "on_submission": lambda obs, status: self.events.append((obs.observation_id, status)),
            "should_continue": lambda: True,
        }
        args.update(changes)
        return provider.respond(**args)

    def provider(self, transport):
        return OpenAIProvider(ProviderConfig("user-model"), "test-secret", transport)

    def test_constructor_has_no_network_side_effect(self):
        with patch("vasl_ai_bridge.provider.build_opener") as opener:
            provider = OpenAIProvider(ProviderConfig("user-model"), "test-secret")
            self.assertTrue(provider.capabilities.supports_images)
            self.assertTrue(provider.capabilities.supports_tools)
            opener.assert_not_called()

    def test_request_uses_exact_displayed_png_and_explicit_destination(self):
        answer = self.send(self.provider(self.transport(response())))
        endpoint, payload, headers, timeout = self.calls[0]
        self.assertEqual(answer, "Check LOS first.")
        self.assertEqual(endpoint, OPENAI_ENDPOINT)
        self.assertEqual(timeout, HTTP_TIMEOUT_SECONDS)
        self.assertEqual(headers["Authorization"], "Bearer test-secret")
        content = payload["input"][0]["content"]
        encoded = next(part["image_url"] for part in content if part["type"] == "input_image")
        self.assertEqual(base64.b64decode(encoded.split(",", 1)[1]), self.first.png)
        self.assertIn(self.first.observation_id, content[1]["text"])
        self.assertIn(self.first.captured_at, content[1]["text"])
        self.assertEqual(payload["model"], "user-model")
        self.assertFalse(payload["store"])
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertNotIn("previous_response_id", payload)
        self.assertEqual(self.events, [("frame_1", "prepared"), ("frame_1", "submitted")])

    def test_look_now_appends_output_and_exact_fresh_image(self):
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
        provider = self.provider(self.transport(response([reasoning, tool()]), response()))
        with patch.object(self, "fresh", create=True, return_value=image(2)) as fresh:
            self.send(provider, look_now=fresh)
            fresh.assert_called_once()
        second = self.calls[1][1]
        self.assertEqual(second["input"][1:3], [reasoning, tool()])
        output = second["input"][3]
        self.assertEqual(output["type"], "function_call_output")
        self.assertEqual(output["call_id"], "call_1")
        encoded = output["output"][1]["image_url"]
        self.assertEqual(base64.b64decode(encoded.split(",", 1)[1]), image(2).png)
        self.assertNotIn("previous_response_id", second)
        self.assertEqual(self.events, [
            ("frame_1", "prepared"), ("frame_1", "submitted"),
            ("frame_2", "prepared"), ("frame_2", "submitted"),
        ])

    def test_fresh_image_tool_limit_is_bounded(self):
        provider = self.provider(self.transport(*(response([tool(f"call_{n}")]) for n in range(4))))
        fresh = iter(image(n) for n in range(2, 5))
        captured = []

        def capture():
            observation = next(fresh)
            captured.append(observation)
            return observation

        with self.assertRaisesRegex(ProviderError, "limit"):
            self.send(provider, look_now=capture)
        self.assertEqual(len(captured), 3)
        self.assertEqual(len(self.calls), 4)

    def test_repeat_call_id_does_not_capture_twice(self):
        provider = self.provider(self.transport(response([tool()]), response([tool()])))
        captured = []
        with self.assertRaisesRegex(ProviderError, "repeated"):
            self.send(provider, look_now=lambda: captured.append(image(2)) or captured[-1])
        self.assertEqual(len(captured), 1)

    def test_look_now_rejects_old_observation(self):
        provider = self.provider(self.transport(response([tool()])))
        with self.assertRaisesRegex(ProviderError, "newly captured"):
            self.send(provider, look_now=lambda: self.first)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_tool_and_parallel_calls_are_explicit_errors(self):
        for output in ([tool(name="roll_dice")], [tool("a"), tool("b")]):
            with self.subTest(output=output):
                provider = self.provider(self.transport(response(output)))
                with self.assertRaisesRegex(ProviderError, "support"):
                    self.send(provider, look_now=lambda: self.fail("Must not capture"))

    def test_tool_arguments_do_not_change_local_capture_behavior(self):
        for arguments in ('{"source":"other"}', "[]", "not-json"):
            provider = self.provider(self.transport(response([tool(arguments=arguments)])))
            with self.assertRaisesRegex(ProviderError, "arguments"):
                self.send(provider, look_now=lambda: self.fail("Must not capture"))

    def test_missing_key_or_model_is_local(self):
        for config, key in ((ProviderConfig(""), "secret"), (ProviderConfig("model"), "")):
            with self.subTest(config=config):
                with self.assertRaises(ProviderError):
                    self.send(OpenAIProvider(config, key, self.transport(response())))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.events, [])

    def test_other_endpoints_are_rejected_before_transport(self):
        for endpoint in ("http://api.openai.com/v1/responses", "https://other.example/v1/responses",
                         OPENAI_ENDPOINT + "?key=secret", OPENAI_ENDPOINT + "/",
                         "https://api.openai.com:443/v1/responses"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(ProviderError, "official"):
                    self.send(OpenAIProvider(ProviderConfig("model", endpoint), "secret", self.transport(response())))
        self.assertEqual(self.calls, [])

    def test_redirects_are_rejected_even_to_same_host(self):
        handler = _RejectRedirect()
        with self.assertRaisesRegex(ProviderError, "Redirects"):
            handler.redirect_request(None, None, 307, "redirect", {}, OPENAI_ENDPOINT)

    def test_default_http_transport_disables_proxies_and_limits_read(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def geturl(self):
                return OPENAI_ENDPOINT

            def getcode(self):
                return 200

            def read(self, limit):
                self.limit = limit
                return b"{}"

        received = FakeResponse()
        with patch("vasl_ai_bridge.provider.build_opener") as builder:
            builder.return_value.open.return_value = received
            _http_transport(OPENAI_ENDPOINT, b"{}", {}, HTTP_TIMEOUT_SECONDS)
            handlers = builder.call_args.args
            proxy = next(h for h in handlers if hasattr(h, "proxies"))
            self.assertEqual(proxy.proxies, {})
            self.assertTrue(any(isinstance(h, _RejectRedirect) for h in handlers))
            self.assertEqual(received.limit, MAX_RESPONSE_BYTES + 1)

    def test_network_failures_do_not_leak_credentials_or_retry(self):
        failures = [RuntimeError("test-secret provider body"), ProviderError("test-secret"),
                    HTTPError(OPENAI_ENDPOINT, 401, "test-secret", {}, None)]
        for failure in failures:
            self.calls.clear()
            self.events.clear()
            with self.assertRaises(ProviderError) as caught:
                self.send(self.provider(self.transport(failure)))
            self.assertNotIn("test-secret", str(caught.exception))
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.events[-1], ("frame_1", "failed"))

    def test_malformed_failed_and_incomplete_responses_are_errors(self):
        for reply in (b"not-json", b"[]", response(error={"message": "test-secret"}),
                      response(status="incomplete"), response(output=[]),
                      b'{"status":"completed","output":[]}'):
            with self.subTest(reply=reply):
                self.events.clear()
                with self.assertRaises(ProviderError) as caught:
                    self.send(self.provider(self.transport(reply)))
                self.assertNotIn("test-secret", str(caught.exception))
                self.assertEqual(self.events[-1], ("frame_1", "submitted"))

    def test_oversized_successful_body_is_rejected(self):
        with self.assertRaisesRegex(ProviderError, "size limit"):
            self.send(self.provider(self.transport(b"x" * (MAX_RESPONSE_BYTES + 1))))

    def test_stale_request_before_send_has_no_network(self):
        with self.assertRaisesRegex(ProviderError, "no longer current"):
            self.send(self.provider(self.transport(response())), should_continue=lambda: False)
        self.assertEqual(self.calls, [])

    def test_pause_after_preparation_prevents_send(self):
        current = [True]

        def prepared(obs, status):
            self.events.append((obs.observation_id, status))
            current[0] = False

        with self.assertRaisesRegex(ProviderError, "no longer current"):
            self.send(self.provider(self.transport(response())),
                      on_submission=prepared, should_continue=lambda: current[0])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.events, [("frame_1", "prepared")])

    def test_correction_during_capture_prevents_image_upload(self):
        current = [True]

        def capture():
            current[0] = False
            return image(2)

        with self.assertRaisesRegex(ProviderError, "discarded"):
            self.send(self.provider(self.transport(response([tool()]))),
                      look_now=capture, should_continue=lambda: current[0])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.events[-1], ("frame_1", "submitted"))

    def test_request_size_limit_prevents_transport(self):
        with patch("vasl_ai_bridge.provider.MAX_REQUEST_BYTES", 16):
            with self.assertRaisesRegex(ProviderError, "smaller images"):
                self.send(self.provider(self.transport(response())))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.events[-1], ("frame_1", "failed"))

    def test_correction_during_http_discards_response_and_prevents_tool(self):
        current = [True]

        def transport(*args):
            current[0] = False
            return response([tool()])

        with self.assertRaisesRegex(ProviderError, "discarded"):
            self.send(self.provider(transport), should_continue=lambda: current[0],
                      look_now=lambda: self.fail("Must not capture after correction"))
        self.assertEqual(self.events[-1], ("frame_1", "submitted"))

    def test_local_callback_failure_is_safe(self):
        def callback(*args):
            raise RuntimeError("test-secret private callback details")

        with self.assertRaises(ProviderError) as caught:
            self.send(self.provider(self.transport(response())), on_submission=callback)
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
