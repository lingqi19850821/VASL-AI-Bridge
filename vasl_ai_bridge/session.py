"""Application-owned game state, ordered events, and submitted image evidence."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import re
import struct
from typing import Any
from uuid import UUID, uuid4
import zlib


MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_SIDE = 8192
MAX_IMAGE_PIXELS = 40_000_000
MAX_TEXT_LENGTH = 1_000_000
MAX_UNSUBMITTED_OBSERVATIONS = 8
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class SessionError(ValueError):
    """A session operation or checkpoint contains invalid state."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _text(value: Any, name: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT_LENGTH:
        raise SessionError(f"{name} must be text of at most {MAX_TEXT_LENGTH} characters.")
    if nonempty and not value.strip():
        raise SessionError(f"{name} must not be empty.")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise SessionError(f"{name} is not a valid identifier.")
    return value


def _timestamp(value: Any, name: str) -> str:
    _text(value, name, nonempty=True)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SessionError(f"{name} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SessionError(f"{name} must include its time zone.")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise SessionError(f"{name} must be an integer of at least {minimum}.")
    return value


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise SessionError(f"{name} must be true or false.")
    return value


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionError(f"{name} must be a JSON object.")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise SessionError(f"{name} must be a JSON array.")
    return value


def _validate_png(png: bytes, width: int, height: int) -> None:
    if not isinstance(png, bytes) or not png or len(png) > MAX_IMAGE_BYTES:
        raise SessionError("Observation image must be PNG bytes of at most 16 MiB.")
    if png[:8] != _PNG_SIGNATURE:
        raise SessionError("Observation image is not a PNG file.")
    _integer(width, "Observation width", minimum=1)
    _integer(height, "Observation height", minimum=1)
    if max(width, height) > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
        raise SessionError("Observation image dimensions exceed the supported limit.")
    offset = 8
    saw_header = False
    saw_data = False
    saw_end = False
    while offset < len(png):
        if offset + 12 > len(png):
            raise SessionError("Observation PNG contains a truncated chunk.")
        length = struct.unpack_from(">I", png, offset)[0]
        chunk_type = png[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(png):
            raise SessionError("Observation PNG contains a truncated chunk.")
        payload = png[offset + 8 : offset + 8 + length]
        checksum = struct.unpack_from(">I", png, offset + 8 + length)[0]
        if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != checksum:
            raise SessionError("Observation PNG contains a damaged chunk.")
        if not saw_header:
            if chunk_type != b"IHDR" or length != 13:
                raise SessionError("Observation PNG has no valid image header.")
            png_width, png_height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if (png_width, png_height) != (width, height):
                raise SessionError("Observation dimensions do not match the PNG header.")
            allowed_depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if (
                depth not in allowed_depths.get(color, set())
                or compression != 0
                or filtering != 0
                or interlace not in (0, 1)
            ):
                raise SessionError("Observation PNG has unsupported image settings.")
            saw_header = True
        elif chunk_type == b"IHDR":
            raise SessionError("Observation PNG contains duplicate image headers.")
        if chunk_type == b"IDAT":
            saw_data = True
        if chunk_type == b"IEND":
            if length != 0 or end != len(png):
                raise SessionError("Observation PNG has an invalid end marker.")
            saw_end = True
        offset = end
    if not saw_header or not saw_data or not saw_end:
        raise SessionError("Observation PNG is incomplete.")


@dataclass(frozen=True)
class Observation:
    observation_id: str
    captured_at: str
    source: str
    png: bytes
    width: int
    height: int

    def __post_init__(self) -> None:
        _identifier(self.observation_id, "Observation ID")
        _timestamp(self.captured_at, "Capture time")
        _text(self.source, "Observation source", nonempty=True)
        _validate_png(self.png, self.width, self.height)

    @property
    def data_url(self) -> str:
        return "data:image/png;base64," + base64.b64encode(self.png).decode("ascii")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.png).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "captured_at": self.captured_at,
            "source": self.source,
            "png_base64": base64.b64encode(self.png).decode("ascii"),
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Observation:
        data = _mapping(value, "Observation")
        encoded = data.get("png_base64")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise SessionError("Observation PNG data exceeds the supported size.")
        try:
            png = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SessionError("Observation PNG data is not valid base64.") from exc
        digest = data.get("sha256")
        if not isinstance(digest, str) or hashlib.sha256(png).hexdigest() != digest:
            raise SessionError("Observation PNG checksum does not match its saved image.")
        try:
            return cls(
                data["observation_id"], data["captured_at"], data["source"], png,
                data["width"], data["height"],
            )
        except KeyError as exc:
            raise SessionError(f"Observation is missing {exc.args[0]}.") from exc


@dataclass(frozen=True)
class ChatMessage:
    role: str
    text: str
    created_at: str

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant", "system"}:
            raise SessionError("Message role must be user, assistant, or system.")
        _text(self.text, "Message text", nonempty=True)
        _timestamp(self.created_at, "Message time")

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "text": self.text, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ChatMessage:
        data = _mapping(value, "Message")
        try:
            return cls(data["role"], data["text"], data["created_at"])
        except KeyError as exc:
            raise SessionError(f"Message is missing {exc.args[0]}.") from exc


@dataclass(frozen=True)
class Event:
    event_id: int
    kind: str
    text: str
    created_at: str
    state_version: int

    def __post_init__(self) -> None:
        _integer(self.event_id, "Event ID", minimum=1)
        _identifier(self.kind, "Event kind")
        _text(self.text, "Event text", nonempty=True)
        _timestamp(self.created_at, "Event time")
        _integer(self.state_version, "Event state version")

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "kind": self.kind, "text": self.text,
            "created_at": self.created_at, "state_version": self.state_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Event:
        data = _mapping(value, "Event")
        try:
            return cls(data["event_id"], data["kind"], data["text"], data["created_at"], data["state_version"])
        except KeyError as exc:
            raise SessionError(f"Event is missing {exc.args[0]}.") from exc


@dataclass(frozen=True)
class RequestSnapshot:
    request_id: str
    state_version: int
    session_id: str
    observation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.request_id, "Request ID")
        _integer(self.state_version, "Request state version")
        _identifier(self.session_id, "Request session ID")
        if not isinstance(self.observation_ids, tuple) or not self.observation_ids:
            raise SessionError("Request must include observation identifiers.")
        for observation_id in self.observation_ids:
            _identifier(observation_id, "Request observation ID")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise SessionError("Request observation identifiers must be unique.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id, "state_version": self.state_version,
            "session_id": self.session_id, "observation_ids": list(self.observation_ids),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RequestSnapshot:
        data = _mapping(value, "Request")
        try:
            ids = _list(data["observation_ids"], "Request observation IDs")
            return cls(data["request_id"], data["state_version"], data["session_id"], tuple(ids))
        except KeyError as exc:
            raise SessionError(f"Request is missing {exc.args[0]}.") from exc


@dataclass(frozen=True)
class Submission:
    request_id: str
    observation: Observation
    status: str
    created_at: str
    state_version: int

    def __post_init__(self) -> None:
        _identifier(self.request_id, "Submission request ID")
        if not isinstance(self.observation, Observation):
            raise SessionError("Submission must include its exact observation.")
        if self.status not in {"prepared", "submitted", "failed"}:
            raise SessionError("Submission status must be prepared, submitted, or failed.")
        _timestamp(self.created_at, "Submission time")
        _integer(self.state_version, "Submission state version")

    @property
    def observation_id(self) -> str:
        return self.observation.observation_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id, "observation": self.observation.as_dict(),
            "status": self.status, "created_at": self.created_at, "state_version": self.state_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Submission:
        data = _mapping(value, "Submission")
        try:
            return cls(
                data["request_id"], Observation.from_dict(data["observation"]), data["status"],
                data["created_at"], data["state_version"],
            )
        except KeyError as exc:
            raise SessionError(f"Submission is missing {exc.args[0]}.") from exc


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: str(uuid4()))
    scenario: str = ""
    phase: str = ""
    confirmed_facts: str = ""
    pending_action: str = ""
    pending_action_stale: bool = False
    state_version: int = 0
    paused: bool = False
    reconciliation_required: bool = False
    messages: list[ChatMessage] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    submissions: list[Submission] = field(default_factory=list)
    active_request: RequestSnapshot | None = field(default=None, init=False)
    rejected_observation_ids: set[str] = field(default_factory=set, init=False)
    _invalidated_request_ids: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            UUID(self.session_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise SessionError("Session ID must be a UUID.") from exc
        for name in ("scenario", "phase", "confirmed_facts", "pending_action"):
            _text(getattr(self, name), name.replace("_", " ").capitalize())
        _integer(self.state_version, "Session state version")
        _boolean(self.paused, "Paused")
        _boolean(self.pending_action_stale, "Pending action needs review")
        _boolean(self.reconciliation_required, "Reconciliation required")

    def _event(self, kind: str, text: str, *, changes_state: bool = False) -> Event:
        _text(text, "Event text", nonempty=True)
        if changes_state:
            self.state_version += 1
        event = Event(
            self.events[-1].event_id + 1 if self.events else 1,
            kind, text, utc_now(), self.state_version,
        )
        self.events.append(event)
        return event

    def _observation(self, observation_id: str) -> Observation | None:
        for observation in self.observations:
            if observation.observation_id == observation_id:
                return observation
        for submission in self.submissions:
            if submission.observation_id == observation_id:
                return submission.observation
        return None

    def add_observation(self, observation: Observation) -> None:
        if not isinstance(observation, Observation):
            raise SessionError("Expected an Observation.")
        existing = self._observation(observation.observation_id)
        if existing is not None:
            if existing != observation:
                raise SessionError("An observation ID cannot identify two different images.")
            return
        if observation.observation_id in self.rejected_observation_ids:
            raise SessionError("A rejected observation ID cannot be reused.")
        self.observations.append(observation)
        protected = set(self.active_request.observation_ids) if self.active_request else set()
        while len(self.observations) > MAX_UNSUBMITTED_OBSERVATIONS:
            discard_index = next(
                (index for index, item in enumerate(self.observations) if item.observation_id not in protected),
                0,
            )
            self.observations.pop(discard_index)

    def record_message(self, role: str, text: str) -> ChatMessage:
        message = ChatMessage(role, text, utc_now())
        self.messages.append(message)
        self._event(f"message_{role}", text)
        return message

    def record_confirmation(
        self, text: str, confirmed_facts: str | None = None, phase: str | None = None,
        *, clear_pending: bool = True,
    ) -> None:
        _text(text, "Confirmation", nonempty=True)
        _boolean(clear_pending, "Clear pending action")
        if confirmed_facts is not None:
            _text(confirmed_facts, "Confirmed facts")
        if phase is not None:
            _text(phase, "Phase")
        if confirmed_facts is not None:
            self.confirmed_facts = confirmed_facts
        if phase is not None:
            self.phase = phase
        if clear_pending:
            self.pending_action = ""
            self.pending_action_stale = False
        elif self.pending_action:
            self.pending_action_stale = True
        self._event("confirmation", text, changes_state=True)

    def record_correction(self, text: str, confirmed_facts: str | None = None) -> None:
        _text(text, "Correction", nonempty=True)
        if confirmed_facts is not None:
            self.confirmed_facts = _text(confirmed_facts, "Confirmed facts")
        if self.pending_action:
            self.pending_action_stale = True
        self._event("correction", text, changes_state=True)

    def reject_observation(self, observation_id: str, reason: str) -> None:
        _identifier(observation_id, "Rejected observation ID")
        _text(reason, "Rejection reason", nonempty=True)
        if self._observation(observation_id) is None:
            raise SessionError("Cannot reject an observation that is not in this session.")
        if observation_id in self.rejected_observation_ids:
            raise SessionError("This observation has already been rejected.")
        self.rejected_observation_ids.add(observation_id)
        if self.pending_action:
            self.pending_action_stale = True
        self._event("observation_rejected", f"{observation_id}: {reason}", changes_state=True)

    def begin_request(self, observation: Observation) -> RequestSnapshot:
        if self.paused or self.reconciliation_required:
            raise SessionError("Reconcile and resume this session before requesting a decision.")
        if self.active_request is not None:
            raise SessionError("A decision request is already active.")
        if observation.observation_id in self.rejected_observation_ids:
            raise SessionError("Cannot request a decision using a rejected observation.")
        self.add_observation(observation)
        snapshot = RequestSnapshot(str(uuid4()), self.state_version, self.session_id, (observation.observation_id,))
        self.active_request = snapshot
        self._event("request_started", f"Request {snapshot.request_id}; observation {observation.observation_id}.")
        return snapshot

    def is_request_current(self, snapshot: RequestSnapshot) -> bool:
        evidence_ids = set(snapshot.observation_ids)
        evidence_ids.update(
            item.observation_id for item in self.submissions if item.request_id == snapshot.request_id
        )
        return (
            self.active_request == snapshot
            and snapshot.session_id == self.session_id
            and snapshot.state_version == self.state_version
            and snapshot.request_id not in self._invalidated_request_ids
            and not self.paused
            and not self.reconciliation_required
            and not self.rejected_observation_ids.intersection(evidence_ids)
        )

    def complete_request(self, snapshot: RequestSnapshot, text: str) -> bool:
        _text(text, "Model response", nonempty=True)
        if snapshot.session_id != self.session_id:
            raise SessionError("The response belongs to another session.")
        current = self.is_request_current(snapshot)
        if self.active_request == snapshot:
            self.active_request = None
        self._invalidated_request_ids.discard(snapshot.request_id)
        if current:
            self.record_message("assistant", text)
            self.pending_action = text
            self.pending_action_stale = False
            self._event("request_completed", f"Request {snapshot.request_id} returned a pending proposal.")
        else:
            self.record_message("assistant", f"[Stale response; request {snapshot.request_id}]\n{text}")
            self._event("response_stale", f"Request {snapshot.request_id} did not update the pending action.")
        return current

    def fail_request(self, snapshot: RequestSnapshot, reason: str) -> None:
        _text(reason, "Request failure", nonempty=True)
        if snapshot.session_id != self.session_id:
            raise SessionError("The request belongs to another session.")
        if self.active_request == snapshot:
            self.active_request = None
        self._invalidated_request_ids.discard(snapshot.request_id)
        self._event("request_failed", f"Request {snapshot.request_id}: {reason}")

    def record_submission(self, snapshot: RequestSnapshot, observation: Observation, status: str) -> Submission:
        if snapshot.session_id != self.session_id:
            raise SessionError("The submission belongs to another session.")
        if observation.observation_id not in snapshot.observation_ids and self.active_request != snapshot:
            raise SessionError("The submitted image is not associated with this request.")
        existing = self._observation(observation.observation_id)
        if existing is not None and existing != observation:
            raise SessionError("The submitted image differs from its observation identifier.")
        submission = Submission(snapshot.request_id, observation, status, utc_now(), snapshot.state_version)
        previous = [
            item for item in self.submissions
            if item.request_id == snapshot.request_id and item.observation_id == observation.observation_id
        ]
        if previous and previous[-1].status == "submitted" and status != "submitted":
            raise SessionError("An image already submitted to the model cannot become unsubmitted.")
        self.submissions.append(submission)
        if status == "submitted":
            self.observations[:] = [item for item in self.observations if item.observation_id != observation.observation_id]
        self._event("image_" + status, f"Request {snapshot.request_id}; observation {observation.observation_id}.")
        return submission

    def pause(self) -> None:
        if self.paused:
            return
        self.paused = True
        if self.active_request is not None:
            self._invalidated_request_ids.add(self.active_request.request_id)
        self._event("paused", "Session paused; active assessments are no longer current.")

    def resume(self) -> None:
        if self.reconciliation_required:
            raise SessionError("Confirm the current board against this checkpoint before resuming.")
        if not self.paused:
            return
        self.paused = False
        self._event("resumed", "Session explicitly resumed by the human.")

    def reconcile(self, text: str) -> None:
        _text(text, "Board reconciliation", nonempty=True)
        self.reconciliation_required = False
        self.paused = True
        if self.active_request is not None:
            self._invalidated_request_ids.add(self.active_request.request_id)
        self._event("reconciled", text, changes_state=True)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "scenario": self.scenario,
            "phase": self.phase,
            "confirmed_facts": self.confirmed_facts,
            "pending_action": self.pending_action,
            "pending_action_stale": self.pending_action_stale,
            "state_version": self.state_version,
            "paused": self.paused,
            "reconciliation_required": self.reconciliation_required,
            "messages": [item.as_dict() for item in self.messages],
            "events": [item.as_dict() for item in self.events],
            "observations": [item.as_dict() for item in self.observations],
            "submissions": [item.as_dict() for item in self.submissions],
            "active_request": self.active_request.as_dict() if self.active_request else None,
            "rejected_observation_ids": sorted(self.rejected_observation_ids),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Session:
        data = _mapping(value, "Session")
        required = {
            "session_id", "scenario", "phase", "confirmed_facts", "pending_action", "pending_action_stale", "state_version",
            "paused", "reconciliation_required", "messages", "events", "observations", "submissions",
            "active_request", "rejected_observation_ids",
        }
        if missing := required.difference(data):
            raise SessionError(f"Session is missing {sorted(missing)[0]}.")
        if unknown := set(data).difference(required):
            raise SessionError(f"Session contains an unsupported field: {sorted(unknown)[0]}.")
        session = cls(
            session_id=data["session_id"], scenario=data["scenario"], phase=data["phase"],
            confirmed_facts=data["confirmed_facts"], pending_action=data["pending_action"],
            pending_action_stale=data["pending_action_stale"],
            state_version=data["state_version"], paused=data["paused"],
            reconciliation_required=data["reconciliation_required"],
            messages=[ChatMessage.from_dict(item) for item in _list(data["messages"], "Messages")],
            events=[Event.from_dict(item) for item in _list(data["events"], "Events")],
            observations=[Observation.from_dict(item) for item in _list(data["observations"], "Observations")],
            submissions=[Submission.from_dict(item) for item in _list(data["submissions"], "Submissions")],
        )
        previous_id = 0
        previous_version = 0
        for event in session.events:
            if event.event_id != previous_id + 1 or not previous_version <= event.state_version <= session.state_version:
                raise SessionError("Saved game events are not in a consistent order.")
            previous_id, previous_version = event.event_id, event.state_version
        if previous_version != session.state_version:
            raise SessionError("Saved state version does not match its final game event.")
        if len(session.observations) > MAX_UNSUBMITTED_OBSERVATIONS:
            raise SessionError("Saved checkpoint has too many recent unsubmitted observations.")
        image_by_id: dict[str, Observation] = {}
        recent_ids: set[str] = set()
        for observation in session.observations:
            if observation.observation_id in recent_ids:
                raise SessionError("Saved checkpoint contains duplicate recent observations.")
            recent_ids.add(observation.observation_id)
        for observation in [*session.observations, *(item.observation for item in session.submissions)]:
            previous = image_by_id.setdefault(observation.observation_id, observation)
            if previous != observation:
                raise SessionError("Saved observation identifier refers to different images.")
        for submission in session.submissions:
            if submission.state_version > session.state_version:
                raise SessionError("Saved submission refers to a future game state.")
        rejected = _list(data["rejected_observation_ids"], "Rejected observation IDs")
        session.rejected_observation_ids = {_identifier(item, "Rejected observation ID") for item in rejected}
        if len(session.rejected_observation_ids) != len(rejected):
            raise SessionError("Saved rejected observation identifiers contain duplicates.")
        active_data = data["active_request"]
        if active_data is not None:
            active = RequestSnapshot.from_dict(active_data)
            if active.session_id != session.session_id or active.state_version > session.state_version:
                raise SessionError("Saved active request does not belong to this session state.")
            if not set(active.observation_ids).issubset(image_by_id):
                raise SessionError("Saved active request is missing its observation evidence.")
            session._event("request_interrupted", f"Request {active.request_id} was interrupted by checkpoint reload.")
        session.paused = True
        session.reconciliation_required = True
        session._event("checkpoint_loaded", "Checkpoint loaded. Reconcile the current board, then explicitly resume.")
        return session
