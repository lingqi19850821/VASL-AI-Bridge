"""Plain conversation records, attachments, and locally supplied references."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Iterable
from uuid import uuid4

from .session import Observation
from .reference_formats import reference_capabilities


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id() -> str:
    return str(uuid4())


@dataclass
class ChatMessage:
    message_id: str
    role: str
    text: str = ""
    created_at: str = field(default_factory=now_utc)
    status: str = "completed"
    attachments: list[Observation] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.message_id, str) or not self.message_id:
            raise ValueError("A message must have an identifier.")
        if self.role not in {"user", "assistant", "system", "tool"}:
            raise ValueError("Unsupported conversation role.")
        if not isinstance(self.text, str):
            raise ValueError("Message text must be a string.")
        if self.status not in {"pending", "streaming", "completed", "interrupted", "failed"}:
            raise ValueError("Unsupported message status.")
        _validate_time(self.created_at)
        if any(not isinstance(image, Observation) for image in self.attachments):
            raise ValueError("Screenshot attachments must be Observation records.")


@dataclass
class Reference:
    reference_id: str
    name: str
    media_type: str
    content: bytes
    added_at: str = field(default_factory=now_utc)
    source_name: str = ""
    indexed_text: str | None = None
    extraction_error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reference_id, str) or not self.reference_id:
            raise ValueError("A reference must have an identifier.")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("A reference must have a display name.")
        if not isinstance(self.content, bytes):
            raise ValueError("Reference content must be bytes.")
        _validate_time(self.added_at)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def size_bytes(self) -> int:
        return len(self.content)

    def text(self) -> str:
        """Cache local extraction while retaining the original file bytes."""
        if reference_capabilities(self.name)["image_available"]:
            # Older saves cached an unsupported-text error for valid images.
            # Image references now have a reader without inventing OCR text.
            self.indexed_text = ""
            self.extraction_error = ""
            return ""
        if self.indexed_text is not None:
            return self.indexed_text
        from .reference_text import ReferenceTextError, indexed_text
        try:
            self.indexed_text = indexed_text(self.name, self.content)
            self.extraction_error = ""
        except ReferenceTextError as exc:
            self.indexed_text = ""
            self.extraction_error = str(exc)
        return self.indexed_text


def _validate_time(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Record time must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Record time must include its time zone.")


def _match_offset(text: str, query: str) -> int:
    folded = text.casefold()
    offset = folded.find(query.casefold()) if query else 0
    if offset < 0 or len(folded) == len(text):
        return offset
    # Case folding may expand a character, so folded offsets are not always
    # valid offsets into the original text used by the read-reference tool.
    folded_position = 0
    for original_position, character in enumerate(text):
        folded_position += len(character.casefold())
        if folded_position > offset:
            return original_position
    return len(text)


def _snippet(text: str, query: str, width: int = 700) -> str:
    start = max(0, _match_offset(text, query) - 120)
    return text[start:start + width]


def _physical_page(reference: Reference, text: str, offset: int) -> int | None:
    if not reference.name.casefold().endswith(".pdf"):
        return None
    page = None
    for marker in re.finditer(r"(?m)^\[Page ([1-9][0-9]*)\][ \t]*$", text):
        if marker.start() > offset:
            break
        page = int(marker.group(1))
    return page


@dataclass
class ChatSession:
    session_id: str = field(default_factory=new_id)
    title: str = ""
    messages: list[ChatMessage] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    save_report: str = ""
    backend_mode: str = ""
    backend_thread_id: str = ""
    created_at: str = field(default_factory=now_utc)
    updated_at: str = field(default_factory=now_utc)
    active_context: list[dict[str, Any]] = field(default_factory=list)
    legacy_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.session_id) is None:
            raise ValueError("A conversation must have an identifier.")
        _validate_time(self.created_at)
        _validate_time(self.updated_at)
        if len({message.message_id for message in self.messages}) != len(self.messages):
            raise ValueError("Conversation contains duplicate message identifiers.")
        if len({reference.reference_id for reference in self.references}) != len(self.references):
            raise ValueError("Conversation contains duplicate reference identifiers.")

    def add_message(self, role: str, text: str = "", *,
                    attachments: Iterable[Observation] = (), status: str = "completed") -> ChatMessage:
        message = ChatMessage(new_id(), role, text, status=status, attachments=list(attachments))
        self.messages.append(message)
        self.updated_at = now_utc()
        return message

    def upsert_message(self, message: ChatMessage) -> ChatMessage:
        if not isinstance(message, ChatMessage):
            raise ValueError("Expected a conversation message.")
        for index, existing in enumerate(self.messages):
            if existing.message_id == message.message_id:
                self.messages[index] = message
                break
        else:
            self.messages.append(message)
        self.updated_at = now_utc()
        return message

    def get_message(self, message_id: str) -> ChatMessage | None:
        return next((message for message in self.messages if message.message_id == message_id), None)

    def search_history(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        needle = query.casefold()
        return [{"message_id": message.message_id, "role": message.role,
                 "created_at": message.created_at, "status": message.status,
                 "text": _snippet(message.text, query),
                 "image_ids": [image.observation_id for image in message.attachments],
                 "images": [{"observation_id": image.observation_id, "captured_at": image.captured_at,
                             "source": image.source} for image in message.attachments]}
                for message in self.messages if needle in message.text.casefold()][:max(0, min(limit, 100))]

    def search_references(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        needle = query.casefold()
        results: list[dict[str, Any]] = []
        for reference in self.references:
            text = reference.text()
            match_offset = _match_offset(text, query) if text else -1
            if needle in reference.name.casefold() or match_offset >= 0:
                excerpt_offset = max(0, match_offset - 120)
                results.append({"reference_id": reference.reference_id, "name": reference.name,
                                "media_type": reference.media_type, "size_bytes": reference.size_bytes,
                                "excerpt": text[excerpt_offset:excerpt_offset + 700],
                                "excerpt_offset": excerpt_offset,
                                "match_offset": match_offset if match_offset >= 0 else None,
                                "physical_page": _physical_page(reference, text, match_offset if match_offset >= 0 else excerpt_offset),
                                **reference_capabilities(reference.name),
                                "indexing_error": reference.extraction_error or
                                    ("No text extraction is available for this file type."
                                     if not text and not reference_capabilities(reference.name)["image_available"] else "")})
        return results[:max(0, min(limit, 100))]

    def read_reference(self, reference_id: str) -> Reference:
        for reference in self.references:
            if reference.reference_id == reference_id:
                return reference
        raise ValueError("The requested reference is not in this conversation.")

    def read_history(self, message_id: str, *, surrounding: int = 2) -> list[ChatMessage]:
        for index, message in enumerate(self.messages):
            if message.message_id == message_id:
                count = max(0, min(surrounding, 20))
                return self.messages[max(0, index - count):index + count + 1]
        raise ValueError("The requested message is not in this conversation.")
