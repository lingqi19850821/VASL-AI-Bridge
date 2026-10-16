"""Offline request-error classification; no account or service is contacted."""

import unittest

from vasl_ai_bridge.request_errors import classify_request_failure


class RequestErrorTests(unittest.TestCase):
    def test_native_codex_semantic_categories(self):
        for info, expected in (
            ("usageLimitExceeded", "usage"),
            ("rateLimitExceeded", "rate_limit"),
            ("unauthorized", "auth"),
            ("serverOverloaded", "service"),
            ("internalServerError", "service"),
            ("flexUnavailable", "service"),
            ({"httpConnectionFailed": {"httpStatusCode": None}}, "network"),
            ({"responseStreamConnectionFailed": {"httpStatusCode": None}}, "network"),
            ({"responseStreamDisconnected": {"httpStatusCode": None}}, "network"),
            ({"responseTooManyFailedAttempts": {"httpStatusCode": 503}}, "service"),
            ({"httpConnectionFailed": {"httpStatusCode": 401}}, "auth"),
            ({"httpConnectionFailed": {"httpStatusCode": 403}}, "unknown"),
            ({"httpConnectionFailed": {"httpStatusCode": 429}}, "unknown"),
        ):
            with self.subTest(info=info):
                failure = classify_request_failure({"message": "Request rejected", "codexErrorInfo": info})
                self.assertEqual(failure.kind, expected)
                self.assertEqual(failure.user_message, failure.message)

    def test_responses_codes_and_rpc_data_retain_specific_meaning(self):
        for code, param, expected in (
            ("model_not_found", "model", "model"),
            ("insufficient_quota", None, "usage"),
            ("rate_limit_exceeded", None, "rate_limit"),
            ("subscription_sharing_usage_limit_exceeded", None, "usage"),
            ("subscription_sharing_usage_unavailable", None, "service"),
            ("subscription_sharing_user_unavailable", None, "service"),
            ("subscription_sharing_unsupported_capability", "model", "model"),
            ("subscription_sharing_unsupported_capability", "reasoning.effort", "unknown"),
            ("subscription_sharing_user_not_eligible", None, "unknown"),
            ("subscription_sharing_route_not_supported", None, "unknown"),
            ("chatpass_v2_scope_not_authorized", None, "unknown"),
            ("chatpass_v2_invalid_authorization_context", None, "unknown"),
            ("subscription_sharing_invalid_user", None, "unknown"),
            ("invalid_refresh_token", None, "auth"),
            ("refresh_token_reused", None, "auth"),
            ("invalid_client", None, "unknown"),
        ):
            with self.subTest(code=code, param=param):
                error = {"code": -32000, "message": "The request was rejected.",
                         "data": {"error": {"code": code, "param": param}}}
                self.assertEqual(classify_request_failure(error).kind, expected)

    def test_unknown_status_does_not_invent_quota_or_model_permission(self):
        for status in (403, 429):
            with self.subTest(status=status):
                failure = classify_request_failure({"status": status, "detail": "Request rejected."})
                self.assertEqual(failure.kind, "unknown")
                self.assertIn("Request rejected.", failure.message)

    def test_explicit_codes_win_over_legacy_message_words(self):
        failure = classify_request_failure({"message": "You've hit your usage limit.",
                                            "codexErrorInfo": "rateLimitExceeded"})
        self.assertEqual(failure.kind, "rate_limit")
        failure = classify_request_failure({"message": "The model does not exist or you do not have access to it.",
                                            "code": "subscription_sharing_route_not_supported"})
        self.assertEqual(failure.kind, "unknown")
        failure = classify_request_failure({"message": "stream disconnected before completion",
                                            "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 503}}})
        self.assertEqual(failure.kind, "service")

    def test_narrow_legacy_messages_cover_known_denials_and_failures(self):
        for message, expected in (
            ("The 'gpt-example' model is not supported when using Codex with a ChatGPT account.", "model"),
            ('{"detail":"The model `example` does not exist or you do not have access to it."}', "model"),
            ("You do not have access to this model.", "model"),
            ("You\u2019ve hit your usage limit.", "usage"),
            ("Quota exceeded. Check your plan and billing details.", "usage"),
            ("Rate limit exceeded. Try later.", "rate_limit"),
            ("Your session has expired.", "auth"),
            ("Your refresh token has already been used.", "auth"),
            ("The connection was reset.", "network"),
            ("DNS lookup failed.", "network"),
            ("stream disconnected before completion", "network"),
            ("stream disconnected before completion: unexpected status 503 Service Unavailable", "service"),
            ("unexpected status 401 Unauthorized", "auth"),
            ("unexpected status 403 Forbidden", "unknown"),
            ("unexpected status 429", "unknown"),
            ("Selected model is at capacity. Please try a different model.", "service"),
            ("The service returned an unexpected error for this model.", "unknown"),
            ("Check your subscription settings.", "unknown"),
            ("This model's reasoning effort is not supported.", "unknown"),
            ("Unsupported region or scope.", "unknown"),
        ):
            with self.subTest(message=message):
                self.assertEqual(classify_request_failure(message).kind, expected)

    def test_bridge_exception_keeps_safe_structured_metadata(self):
        error = RuntimeError("Runtime request failed.")
        error.error_info = {"code": -32000, "data": {"codexErrorInfo": "usageLimitExceeded"}}
        failure = classify_request_failure(error)
        self.assertEqual(failure.kind, "usage")
        self.assertNotIn("Runtime request failed", failure.message)

    def test_known_user_messages_suppress_provider_details(self):
        for code in ("model_not_found", "usageLimitExceeded", "rateLimitExceeded", "unauthorized"):
            with self.subTest(code=code):
                failure = classify_request_failure({"codexErrorInfo": code,
                                                    "message": "Provider error_detail contains private diagnostic text"})
                self.assertNotIn("error_detail", failure.message)
                self.assertNotIn("private diagnostic", failure.message)
        failure = classify_request_failure({"code": "model_not_found"})
        self.assertEqual(failure.message, "This account cannot use the selected model. Choose another model or check your subscription.")
        failure = classify_request_failure({"codexErrorInfo": "unauthorized"})
        self.assertEqual(failure.message, "Your sign-in is no longer valid. Sign in with ChatGPT again.")

    def test_unknown_diagnostic_is_bounded_and_redacted_without_serializing_secrets(self):
        message = ("Unexpected failure. Bearer bearer-secret sk-private-key "
                   "eyJabc.eyJdef.signature access_token=access-secret "
                   'refresh_token="refresh-secret" api_key=key-secret client_secret=client-secret ' + "x" * 3000)
        failure = classify_request_failure({"message": message, "access_token": "unrelated-secret"})
        self.assertEqual(failure.kind, "unknown")
        self.assertTrue(failure.message.startswith("AI request failed. Unexpected failure."))
        self.assertLessEqual(len(failure.message), 1020)
        for secret in ("bearer-secret", "sk-private-key", "eyJabc", "access-secret", "refresh-secret",
                       "key-secret", "client-secret", "unrelated-secret"):
            self.assertNotIn(secret, failure.message)

    def test_malformed_or_future_payloads_remain_bounded_unknown_errors(self):
        for payload in (None, 429, [], {}, {"code": "future_code"}, "{invalid json",
                        {"data": {"codexErrorInfo": {"futureVariant": {"details": "new"}}}}):
            with self.subTest(payload=payload):
                failure = classify_request_failure(payload)
                self.assertEqual(failure.kind, "unknown")
                self.assertTrue(failure.message.startswith("AI request failed."))
        recursive = {}
        recursive["error"] = recursive
        self.assertEqual(classify_request_failure(recursive).kind, "unknown")


if __name__ == "__main__":
    unittest.main()
