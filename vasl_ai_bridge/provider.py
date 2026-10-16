"""Explicit, bounded OpenAI Responses calls for the desktop companion.

Only ``respond`` can make a network request. Images are immutable session
observations, and function-call continuations stay in memory for that request.
The adapter supports images and tools; the user-selected model must support
both. No claim about a model's capabilities is inferred from its identifier.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import socket
from typing import TYPE_CHECKING, Callable
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

if TYPE_CHECKING:
    from .session import Observation


OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"
HTTP_TIMEOUT_SECONDS = 45.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_LOOK_NOW_CALLS = 3

Transport = Callable[[str, bytes, dict[str, str], float], bytes]


@dataclass(frozen=True)
class ProviderConfig:
    model: str
    endpoint: str = OPENAI_ENDPOINT


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_images: bool = True
    supports_tools: bool = True
    supports_offline: bool = False
    requires_api_key: bool = True


class ProviderError(Exception):
    """An error suitable for the UI; provider bodies and credentials are omitted."""


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("OpenAI redirected the request. Redirects are disabled.")


def _validate_endpoint(endpoint: str) -> None:
    # The first maintained provider has one explicit network destination.
    if endpoint != OPENAI_ENDPOINT:
        raise ProviderError("Use the official OpenAI HTTPS Responses endpoint.")


def _http_transport(
    endpoint: str, body: bytes, headers: dict[str, str], timeout: float
) -> bytes:
    _validate_endpoint(endpoint)
    # Do not inherit proxy environment variables or follow HTTP redirects.
    opener = build_opener(ProxyHandler({}), HTTPSHandler(), _RejectRedirect())
    request = Request(endpoint, data=body, headers=headers, method="POST")
    with opener.open(request, timeout=timeout) as response:
        if response.geturl() != OPENAI_ENDPOINT:
            raise ProviderError("The response destination was not the configured endpoint.")
        status = response.getcode()
        if not isinstance(status, int) or not 200 <= status < 300:
            raise ProviderError("OpenAI returned an unexpected HTTP status.")
        # Retain one extra byte to detect overflow without reading an
        # unbounded body. Parsing checks the limit after recording HTTP success.
        return response.read(MAX_RESPONSE_BYTES + 1)


def _image_content(observation: Observation) -> list[dict]:
    metadata = {
        "observation_id": observation.observation_id,
        "captured_at": observation.captured_at,
        "source": observation.source,
        "sha256": observation.sha256,
        "width": observation.width,
        "height": observation.height,
    }
    return [
        {"type": "input_text", "text": "Image evidence: " + json.dumps(metadata)},
        {"type": "input_image", "image_url": observation.data_url, "detail": "high"},
    ]


_INSTRUCTIONS = """You are a human-mediated ASL opponent beside VASSAL.
Give a short answer and at most one proposed action at a time. The human
performs all board operations, dice rolls, and LOS checks. A proposal does not
establish that an action happened. Use the confirmed session context and the
image evidence identifiers and times. State uncertainty; do not invent rules,
unit identities, missing game state, or source references. Ask for human
clarification when evidence is insufficient. Request look_now for a fresh
board view when needed. A frame is an observation, not a confirmed game event.
Do not expose private chain of thought; give only a concise decision explanation.
"""

_LOOK_NOW_TOOL = {
    "type": "function",
    "name": "look_now",
    "description": "Capture a fresh image of the configured local game source.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}


class OpenAIProvider:
    """Stateless Responses adapter with a testable, replaceable HTTP transport.

    ``transport(endpoint, body, headers, timeout)`` returns successful HTTP body
    bytes, or raises. The default transport is restricted to the official
    endpoint. An injected transport is intended for offline tests, not another
    provider. Callback statuses are ``prepared``, ``submitted`` (a successful
    HTTP response was received), and ``failed`` (receipt is not established).
    """

    capabilities = ProviderCapabilities()

    def __init__(
        self, config: ProviderConfig, api_key: str, transport: Transport | None = None
    ) -> None:
        self.config = config
        self._api_key = api_key
        self._transport = _http_transport if transport is None else transport
        self._validate_config()

    def _redact(self, message: str) -> str:
        if isinstance(self._api_key, str) and self._api_key:
            message = message.replace(self._api_key, "[redacted]")
            trimmed = self._api_key.strip()
            if trimmed:
                message = message.replace(trimmed, "[redacted]")
        return message

    @staticmethod
    def _guard(should_continue: Callable[[], bool]) -> None:
        if not should_continue():
            raise ProviderError("This request is no longer current. Its response was discarded.")

    def _validate_config(self) -> None:
        _validate_endpoint(self.config.endpoint)
        if not isinstance(self.config.model, str) or not self.config.model.strip():
            raise ProviderError("Configure an OpenAI model before sending.")
        if len(self.config.model) > 200 or any(ord(c) < 32 for c in self.config.model):
            raise ProviderError("The configured model identifier is invalid.")
        if not isinstance(self._api_key, str) or not self._api_key.strip():
            raise ProviderError("Enter an OpenAI API key before sending.")
        if len(self._api_key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in self._api_key):
            raise ProviderError("The OpenAI API key contains invalid characters.")

    def _post(self, payload: dict) -> bytes:
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ProviderError("This request exceeded the local size limit. Use smaller images.")
        headers = {
            "Authorization": "Bearer " + self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            result = self._transport(self.config.endpoint, body, headers, HTTP_TIMEOUT_SECONDS)
        except HTTPError as error:
            status = error.code
            if not isinstance(status, int) or not 100 <= status <= 599:
                raise ProviderError("OpenAI returned an HTTP error.") from None
            friendly = {
                400: "Check the selected model's image/tool support and the request configuration.",
                401: "Check the API key.",
                403: "Check this key's model and project permissions.",
                404: "Check the configured model identifier and its availability.",
                429: "Check the project's quota and rate limits before sending again.",
            }.get(status, "Try again only when you choose to send a new request.")
            raise ProviderError(f"OpenAI HTTP {status}. {friendly}") from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise ProviderError("The OpenAI connection failed or timed out. Receipt is unknown.") from None
        except ProviderError as error:
            raise ProviderError(self._redact(str(error))) from None
        except Exception:
            raise ProviderError("The OpenAI request failed. Receipt is unknown.") from None
        if not isinstance(result, bytes):
            raise ProviderError("The OpenAI transport returned an invalid response.")
        return result

    @staticmethod
    def _parse_response(raw: bytes) -> tuple[list[dict], list[dict], str]:
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("OpenAI's response exceeded the local size limit.")
        try:
            response = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise ProviderError("OpenAI returned malformed response data.") from None
        if not isinstance(response, dict):
            raise ProviderError("OpenAI returned an invalid response structure.")
        if response.get("error") is not None:
            raise ProviderError("OpenAI reported that this response failed.")
        if response.get("status") != "completed":
            raise ProviderError("OpenAI did not complete this response. Send again only when ready.")
        output = response.get("output")
        if not isinstance(output, list) or not output:
            raise ProviderError("OpenAI returned no usable output.")
        calls: list[dict] = []
        text: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                raise ProviderError("OpenAI returned an invalid output item.")
            kind = item.get("type")
            if kind == "function_call":
                calls.append(item)
            elif kind == "reasoning":
                # Opaque continuation records remain in memory only. Never
                # display them or promote them into durable decision records.
                continue
            elif kind == "message":
                if item.get("role") != "assistant" or item.get("status", "completed") != "completed":
                    raise ProviderError("OpenAI returned an incomplete assistant message.")
                content = item.get("content")
                if not isinstance(content, list):
                    raise ProviderError("OpenAI returned invalid assistant content.")
                for part in content:
                    if not isinstance(part, dict):
                        raise ProviderError("OpenAI returned invalid assistant content.")
                    if part.get("type") == "refusal":
                        raise ProviderError("The selected model refused this request.")
                    if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                        raise ProviderError("OpenAI returned unsupported assistant content.")
                    text.append(part["text"])
            else:
                raise ProviderError("OpenAI returned an unsupported tool or output type.")
        return output, calls, "\n".join(text).strip()

    def respond(
        self,
        context: str,
        user_message: str,
        observation: Observation,
        look_now: Callable[[], Observation],
        on_submission: Callable[[Observation, str], None],
        should_continue: Callable[[], bool],
    ) -> str:
        """Send one interaction, allowing at most three fresh-image tool calls."""
        try:
            return self._respond(
                context, user_message, observation, look_now, on_submission, should_continue
            )
        except ProviderError as error:
            raise ProviderError(self._redact(str(error))) from None
        except Exception:
            # Callback exceptions may contain local paths, user data, or keys.
            raise ProviderError("The request stopped because a local operation failed.") from None

    def _respond(
        self,
        context: str,
        user_message: str,
        observation: Observation,
        look_now: Callable[[], Observation],
        on_submission: Callable[[Observation, str], None],
        should_continue: Callable[[], bool],
    ) -> str:
        self._validate_config()
        self._guard(should_continue)
        if not isinstance(context, str) or not isinstance(user_message, str) or not user_message.strip():
            raise ProviderError("Type a message before sending.")
        initial_content = [{"type": "input_text", "text": self._redact(user_message)}]
        initial_content.extend(_image_content(observation))
        inputs: list[dict] = [{"role": "user", "content": initial_content}]
        current_observation = observation
        seen_observations = {observation.observation_id}
        seen_calls: set[str] = set()
        tool_count = 0

        while True:
            self._guard(should_continue)
            on_submission(current_observation, "prepared")
            self._guard(should_continue)
            payload = {
                "model": self.config.model,
                "instructions": _INSTRUCTIONS + "\nSession context (confirmed facts and pending work remain distinct):\n" + self._redact(context),
                "input": inputs,
                "tools": [_LOOK_NOW_TOOL],
                "parallel_tool_calls": False,
                "store": False,
                "include": ["reasoning.encrypted_content"],
                "max_output_tokens": 4096,
            }
            try:
                raw = self._post(payload)
            except ProviderError:
                on_submission(current_observation, "failed")
                raise
            # HTTP success establishes submission even if decoding fails or
            # the session changes while the response is in flight.
            on_submission(current_observation, "submitted")
            self._guard(should_continue)
            output, calls, answer = self._parse_response(raw)
            if not calls:
                if not answer:
                    raise ProviderError("OpenAI returned no text answer.")
                self._guard(should_continue)
                return self._redact(answer)
            if len(calls) != 1:
                raise ProviderError("OpenAI requested parallel tools, which this capture flow does not support.")
            call = calls[0]
            if call.get("name") != "look_now":
                raise ProviderError("OpenAI requested an unsupported tool.")
            call_id = call.get("call_id")
            if (
                not isinstance(call_id, str) or not call_id or len(call_id) > 64
                or any(ord(c) < 32 for c in call_id) or call_id in seen_calls
            ):
                raise ProviderError("OpenAI returned an invalid or repeated tool call identifier.")
            try:
                arguments = json.loads(call.get("arguments", ""))
            except (ValueError, TypeError, RecursionError):
                raise ProviderError("OpenAI returned invalid look_now arguments.") from None
            if arguments != {} or call.get("status", "completed") != "completed":
                raise ProviderError("OpenAI returned unsupported look_now arguments or status.")
            if tool_count >= MAX_LOOK_NOW_CALLS:
                raise ProviderError("The fresh-image limit was reached. Review the images before sending again.")
            self._guard(should_continue)
            fresh = look_now()
            self._guard(should_continue)
            if fresh.observation_id in seen_observations:
                raise ProviderError("look_now did not provide a newly captured observation.")
            seen_observations.add(fresh.observation_id)
            seen_calls.add(call_id)
            tool_count += 1
            # Replay all official output items, including any encrypted
            # reasoning continuation required by a reasoning model.
            inputs.extend(output)
            inputs.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": _image_content(fresh),
            })
            current_observation = fresh
