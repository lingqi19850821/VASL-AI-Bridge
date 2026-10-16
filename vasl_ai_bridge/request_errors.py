"""Conservative, local presentation of errors reported by an AI request."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


@dataclass(frozen=True)
class RequestFailure:
    kind: str
    message: str

    @property
    def user_message(self) -> str:
        return self.message


_MESSAGES = {
    "model": "This account cannot use the selected model. Choose another model or check your subscription.",
    "usage": "Your usage limit has been reached. Check your account's usage limits before trying again.",
    "rate_limit": "Too many requests. Please wait before trying again.",
    "auth": "Your sign-in is no longer valid. Sign in with ChatGPT again.",
    "network": "The connection was interrupted. Check your internet connection and try again.",
    "service": "The AI service is temporarily unavailable. Please try again later.",
    "unknown": "AI request failed.",
}


def _code(value: str) -> str:
    return re.sub(r"[_\s-]", "", value[:200]).lower()


_CODE_KINDS = {
    _code(code): kind
    for kind, codes in {
        "model": ("model_not_found", "model_access_denied", "model_not_supported"),
        "usage": ("usageLimitExceeded", "insufficient_quota", "quota_exceeded",
                  "subscription_sharing_usage_limit_exceeded"),
        "rate_limit": ("rateLimitExceeded", "too_many_requests"),
        "auth": ("unauthorized", "invalid_api_key", "invalid_grant", "invalid_refresh_token",
                 "token_expired", "refresh_token_expired", "refresh_token_invalidated",
                 "refresh_token_reused"),
        "service": ("serverOverloaded", "internalServerError", "flexUnavailable",
                    "subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable"),
    }.items()
    for code in codes
}

_CONNECTION_KINDS = {
    _code("httpConnectionFailed"), _code("responseStreamConnectionFailed"),
    _code("responseStreamDisconnected"), _code("responseTooManyFailedAttempts"),
}

# These restrictions do not establish that the selected model is unavailable.
_OTHER_RESTRICTIONS = {
    _code(code) for code in (
        "subscription_sharing_user_not_eligible", "subscription_sharing_route_not_supported",
        "subscription_sharing_invalid_user", "chatpass_v2_scope_not_authorized",
        "chatpass_v2_invalid_authorization_context", "unsupported_reasoning_effort",
        "invalid_client", "unsupported_country_region_territory",
    )
}

_TEXT_PATTERNS = {
    "model": (
        r"\bmodel\b.{0,160}\bis not supported when using codex with a chatgpt account\b",
        r"\bmodel\b.{0,160}\bdoes not exist or you do not have access(?: to it)?\b",
        r"\b(?:you|your account|this account) (?:do(?:es)? not have access to|cannot use|"
        r"(?:are|is) not (?:authorized|allowed) to use) (?:the |this |selected )?model\b",
    ),
    "usage": (
        r"\byou(?:'ve| have) (?:hit|reached|exceeded|exhausted) your usage limit\b",
        r"\busage limit (?:has been )?(?:reached|exceeded)\b",
        r"\b(?:quota exceeded|insufficient quota)\b",
    ),
    "rate_limit": (r"\brate limit (?:has been )?(?:reached|exceeded)\b", r"\btoo many requests\b"),
    "auth": (
        r"\b(?:access|refresh|authentication|auth) token (?:has |is |was )?(?:expired|invalid|revoked)\b",
        r"\byour (?:sign-in|session|authentication) (?:has |is )?expired\b",
        r"\brefresh token has already been used\b",
        r"\b(?:please )?sign in again\b",
    ),
    "network": (
        r"\b(?:connection|request) (?:was |has been )?(?:reset|timed out|closed)\b",
        r"\b(?:network is unreachable|network unreachable|failed to connect|error sending request)\b",
        r"\b(?:dns|name resolution) (?:lookup )?(?:failed|failure|error)\b",
        r"\bstream disconnected before completion\b",
    ),
    "service": (
        r"\b(?:service unavailable|internal server error|server overloaded)\b",
        r"\b(?:selected )?model is at capacity\b",
        r"\bcurrently experiencing high demand\b",
    ),
}


def _safe_diagnostic(text: str) -> str:
    """Retain useful unknown errors without displaying common credential forms."""
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[credential removed]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [credential removed]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[credential removed]", text)
    text = re.sub(
        r"(?i)([\"']?(?:access_token|refresh_token|id_token|api[_-]?key|client_secret)"
        r"[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;}]+)",
        r"\1[credential removed]", text,
    )
    return " ".join(text.split())[:1000]


def classify_request_failure(error: object) -> RequestFailure:
    """Classify known error evidence without guessing a subscription from HTTP status.

    Accept strings, error dictionaries and exceptions. A bridge exception may carry
    its already-redacted protocol error in ``error_info``. Recognized codes take
    precedence over narrow legacy message patterns; unknown codes remain useful
    diagnostics. This helper has no side effects and never retries a request.
    """
    codes: list[str] = []
    params: list[str] = []
    statuses: list[int] = []
    messages: list[str] = []
    nodes = 0

    def visit(value: Any, depth: int = 0) -> None:
        nonlocal nodes
        nodes += 1
        if depth > 6 or nodes > 80:
            return
        if isinstance(value, str):
            value = value[:12000]
            if value.strip():
                messages.append(value)
            if value.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(value)
                except (ValueError, RecursionError):
                    return
                visit(parsed, depth + 1)
        elif isinstance(value, dict):
            for key in ("code", "type"):
                if isinstance(value.get(key), str):
                    codes.append(_code(value[key]))
            if isinstance(value.get("param"), str):
                params.append(value["param"].lower())
            for key in ("status", "status_code", "httpStatusCode"):
                status = value.get(key)
                if isinstance(status, int) and not isinstance(status, bool):
                    statuses.append(status)
            info = value.get("codexErrorInfo")
            if isinstance(info, str):
                codes.append(_code(info))
            elif isinstance(info, dict):
                for name, payload in list(info.items())[:8]:
                    codes.append(_code(name))
                    visit(payload, depth + 1)
            for key in ("message", "detail", "additionalDetails", "error", "data"):
                if key in value:
                    visit(value[key], depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value[:8]:
                visit(item, depth + 1)

    if isinstance(error, BaseException):
        visit(getattr(error, "error_info", None))
        visit(str(error))
    else:
        visit(error)

    def result(kind: str) -> RequestFailure:
        return RequestFailure(kind, _MESSAGES[kind])

    # Exact semantic evidence wins over a status code or legacy diagnostic text.
    for code in codes:
        if code in _CODE_KINDS:
            return result(_CODE_KINDS[code])

    unsupported = _code("subscription_sharing_unsupported_capability") in codes
    if unsupported and "model" in params:
        return result("model")
    restricted = unsupported or bool(_OTHER_RESTRICTIONS.intersection(codes))
    reasoning_parameter = any(param == "reasoning" or param.startswith("reasoning.")
                              or param == "reasoning_effort" for param in params)
    text = "\n".join(messages).lower().replace("\u2019", "'")
    # Older runtimes sometimes retain the HTTP status only in their diagnostic.
    statuses.extend(int(match) for match in re.findall(
        r"\b(?:http(?:/\d(?:\.\d)?)?\s+|(?:unexpected|http)\s+status(?:\s+code)?[:=]?\s+)([1-5]\d\d)\b",
        text,
    ))
    if not restricted:
        if 401 in statuses:
            return result("auth")
        if any(500 <= status <= 599 for status in statuses):
            return result("service")
    if not restricted and not reasoning_parameter:
        # A rejected reasoning setting is not an account's model entitlement.
        reasoning_error = ("reasoning" in text and
                           bool(re.search(r"\b(?:not supported|unsupported|invalid)\b", text)))
        for kind in ("usage", "rate_limit", "auth", "model", "network", "service"):
            if kind == "model" and reasoning_error:
                continue
            if any(re.search(pattern, text) for pattern in _TEXT_PATTERNS[kind]):
                return result(kind)

    if not restricted:
        if not statuses and _CONNECTION_KINDS.intersection(codes):
            return result("network")

    # No bare 403 -> model or bare 429 -> exhausted-quota inference is safe.
    diagnostic = _safe_diagnostic(messages[0]) if messages else ""
    message = _MESSAGES["unknown"]
    if diagnostic and diagnostic != message:
        message += " " + diagnostic
    return RequestFailure("unknown", message)
