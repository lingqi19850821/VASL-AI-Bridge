"""Readable local tactical journals and a shared, model-independent lesson library.

The application supplies trusted turn identity. Model arguments contain bounded
content and opaque source IDs, never filesystem paths or identity metadata.
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
from typing import Any

from .chat_session import ChatSession, now_utc


MEMORY_TOOL_NAMES = frozenset({
    "record_turn_memory", "search_tactical_journal", "read_tactical_journal",
    "search_experience", "read_experience", "record_review",
})
_SCHEMA = 1
_MAX_RECORD_CHARS = 48_000


class GameMemoryError(ValueError):
    """A memory operation was invalid or could not be persisted safely."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)


def _text(value: Any, name: str, maximum: int = 4000, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise GameMemoryError(f"{name} must be text of at most {maximum} characters.")
    value = value.strip()
    if required and not value:
        raise GameMemoryError(f"{name} must not be empty.")
    return value


def _integer(arguments: dict, name: str, default: int, maximum: int) -> int:
    value = arguments.get(name, default)
    if type(value) is not int or value < 0 or value > maximum:
        raise GameMemoryError(f"{name} is outside its permitted range.")
    return value


def _items(value: Any, name: str, maximum: int = 30) -> list:
    if not isinstance(value, list) or len(value) > maximum:
        raise GameMemoryError(f"{name} must be a list of at most {maximum} items.")
    return value


def _identifier(value: Any, name: str) -> str:
    value = _text(value, name, 128, required=True)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is None:
        raise GameMemoryError(f"{name} must be an opaque record identifier.")
    return value


def _identity(prefix: str, value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return prefix + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:28]


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _lesson_classification(item: dict, *, allow_untagged: bool = False) -> tuple[str, str]:
    """Validate declared applicability without inferring ASL or scenario meaning."""
    if allow_untagged and "scope" not in item and "scenario_name" not in item:
        return "unclassified", ""
    scope = item.get("scope")
    if not isinstance(scope, str) or scope not in {"general", "scenario"}:
        raise GameMemoryError("A lesson scope must be general or scenario.")
    scenario_name = _text(item.get("scenario_name"), "scenario_name", 200)
    if scope == "scenario" and not scenario_name:
        raise GameMemoryError("A scenario lesson requires a scenario name.")
    if scope == "general" and scenario_name:
        raise GameMemoryError("A general lesson must have an empty scenario name.")
    return scope, scenario_name


def _experience_filter(arguments: dict) -> tuple[str, str]:
    scope = arguments.get("scope", "general")
    if not isinstance(scope, str) or scope not in {"general", "scenario", "unclassified"}:
        raise GameMemoryError("An experience lookup scope must be general, scenario, or unclassified.")
    scenario_name = _text(arguments.get("scenario_name", ""), "scenario_name", 200)
    if scope == "scenario" and not scenario_name:
        raise GameMemoryError("A scenario lookup requires a scenario name.")
    if scope != "scenario" and scenario_name:
        raise GameMemoryError("Only a scenario lookup may include a scenario name.")
    return scope, scenario_name


def _matches_experience_scope(item: dict, scope: str, scenario_name: str) -> bool:
    stored_scope, stored_name = _lesson_classification(item, allow_untagged=True)
    return stored_scope == scope and (scope != "scenario" or _normalized(stored_name) == _normalized(scenario_name))


class MemoryService:
    """Immediate, atomic JSON writes protected against concurrent tool callbacks.

    Real games keep their journal in the selected game folder. The root fallback
    is only for unassigned/legacy sessions; it is removed after durable adoption.
    The shared lesson library intentionally does not rewind with a checkpoint.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    @property
    def experience_path(self) -> Path:
        return self.root / "memory" / "experience.json"

    def _paths(self, session: ChatSession, context: dict | None) -> tuple[Path, Path]:
        session_id = _identifier(session.session_id, "session_id")
        fallback = self.root / "memory" / "games" / (session_id + ".json")
        folder = (context or {}).get("game_folder")
        if folder is None:
            return fallback, fallback
        folder = Path(folder).expanduser().resolve()
        database = folder / ".vasl-ai-bridge" / "game.aslgame"
        if not database.is_file():
            raise GameMemoryError("This game folder is missing or has moved. Open its new location before recording memory.")
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                identity = connection.execute("SELECT value FROM game_folder_info WHERE key='session_id'").fetchone()
                if identity is None or identity[0] != session_id:
                    raise GameMemoryError("The selected memory folder belongs to another game.")
        except sqlite3.Error as exc:
            raise GameMemoryError("The selected memory folder cannot be verified; no journal was created.") from exc
        target = folder / ".vasl-ai-bridge" / "memory" / (session_id + ".json")
        if not target.resolve().is_relative_to(folder):
            raise GameMemoryError("The journal location must remain inside its game folder.")
        return target, fallback

    @staticmethod
    def _read(path: Path, default: dict) -> dict:
        if not path.exists():
            return default
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise GameMemoryError("A local memory record cannot be read; it was not replaced.") from exc
        if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
                or data.get("schema_version") != _SCHEMA):
            raise GameMemoryError("Unsupported local memory schema; the original was not replaced.")
        return data

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        encoded = _json(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             prefix=".memory-", suffix=".tmp",
                                             dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _journal(self, session: ChatSession, context: dict | None) -> tuple[dict, Path, Path]:
        target, fallback = self._paths(session, context)
        empty = {"schema_version": _SCHEMA, "session_id": session.session_id, "entries": []}
        data = self._read(target, empty)
        self._validate_journal(data, session.session_id)
        if target != fallback and fallback.exists():
            previous = self._read(fallback, empty)
            self._validate_journal(previous, session.session_id)
            existing = {item.get("entry_id") for item in data["entries"]}
            data["entries"].extend(item for item in previous["entries"] if item.get("entry_id") not in existing)
            data["entries"].sort(key=lambda item: item.get("recorded_at", ""))
        return data, target, fallback

    @staticmethod
    def _validate_journal(data: dict, session_id: str) -> None:
        if data.get("session_id") != session_id or not isinstance(data.get("entries"), list):
            raise GameMemoryError("The tactical journal belongs to another game or is invalid.")
        identifiers = set()
        for item in data["entries"]:
            if (not isinstance(item, dict) or not isinstance(item.get("entry_id"), str)
                    or item["entry_id"] in identifiers
                    or item.get("kind") not in {"intent", "review", "memory_checkpoint"}
                    or not isinstance(item.get("visibility_message_id"), str)
                    or not isinstance(item.get("recorded_at"), str)
                    or not isinstance(item.get("model"), str)
                    or not isinstance(item.get("source_message_ids"), list)
                    or any(not isinstance(source, str) for source in item["source_message_ids"])
                    or not isinstance(item.get("processed_message_ids", []), list)
                    or any(not isinstance(source, str) for source in item.get("processed_message_ids", []))):
                raise GameMemoryError("The tactical journal contains invalid records; it was not replaced.")
            identifiers.add(item["entry_id"])
            required_fields = {"intent": ("action", "goal"), "review": ("title", "text"),
                               "memory_checkpoint": ()}[item["kind"]]
            if any(not isinstance(item.get(field), str) for field in required_fields):
                raise GameMemoryError("The tactical journal contains incomplete records; it was not replaced.")

    def _library(self) -> dict:
        data = self._read(self.experience_path, {"schema_version": _SCHEMA, "lessons": []})
        if not isinstance(data.get("lessons"), list):
            raise GameMemoryError("The shared experience library is invalid.")
        identifiers = set()
        for item in data["lessons"]:
            if (not isinstance(item, dict) or not isinstance(item.get("lesson_id"), str)
                    or item["lesson_id"] in identifiers
                    or any(not isinstance(item.get(field), str) for field in ("title", "lesson", "conditions", "status"))
                    or not isinstance(item.get("revisions"), list)
                    or any(not isinstance(revision, dict)
                           or not isinstance(revision.get("revision_id"), str)
                           or not isinstance(revision.get("source_evidence"), list)
                           or any(not isinstance(source, dict) or not isinstance(source.get("images"), list)
                                  or any(not isinstance(image, dict) or not isinstance(image.get("observation_id"), str)
                                         for image in source["images"]) for source in revision["source_evidence"])
                           for revision in item["revisions"])):
                raise GameMemoryError("The shared experience library contains invalid records; it was not replaced.")
            _lesson_classification(item, allow_untagged=True)
            identifiers.add(item["lesson_id"])
        return data

    @staticmethod
    def _visible(entry: dict, session: ChatSession) -> bool:
        visible = {message.message_id for message in session.messages}
        return (entry.get("visibility_message_id") in visible
                and all(item in visible for item in entry.get("source_message_ids", [])))

    def _visible_entries(self, journal: dict, session: ChatSession, *, checkpoints: bool = False) -> list[dict]:
        return [item for item in journal["entries"] if self._visible(item, session)
                and (checkpoints or item.get("kind") != "memory_checkpoint")]

    def notes_by_message(self, session: ChatSession, context: dict | None = None) -> dict[str, list[dict]]:
        """Read original intent fields for the local viewer without writing memory.

        Both the associated reply and every source must belong to the loaded
        conversation, so an earlier save point cannot reveal later intentions.
        This is a UI reader, not a model tool or a new recording contract.
        """
        with self._lock:
            journal, _, _ = self._journal(session, context)
            visible = {message.message_id for message in session.messages}
            replies = {message.message_id for message in session.messages if message.role == "assistant"}
            result: dict[str, list[dict]] = {}
            for entry in journal["entries"]:
                reply = entry["visibility_message_id"]
                if (entry["kind"] != "intent" or reply not in replies
                        or any(source not in visible for source in entry["source_message_ids"])):
                    continue
                fields = {name: entry.get(name, "") for name in ("assumptions", "goal", "expected_result")}
                if any(not isinstance(value, str) for value in fields.values()):
                    raise GameMemoryError("The tactical journal contains invalid records; it was not replaced.")
                result.setdefault(reply, []).append({"entry_id": entry["entry_id"], **fields})
            return result

    def pending_message_ids(self, session: ChatSession, context: dict | None = None) -> list[str]:
        with self._lock:
            journal, _, _ = self._journal(session, context)
            processed = {source for item in self._visible_entries(journal, session, checkpoints=True)
                         for source in item.get("processed_message_ids", [])}
            excluded = self._application_sources(context)
            return [message.message_id for message in session.messages
                    if message.role == "user" and message.status == "completed"
                    and message.message_id not in processed and message.message_id not in excluded]

    def catalog(self, session: ChatSession, context: dict | None = None) -> dict:
        with self._lock:
            journal, _, _ = self._journal(session, context)
            library = self._library()
            visible = self._visible_entries(journal, session)
            lessons = library["lessons"]
            scopes = {scope: sum(_lesson_classification(item, allow_untagged=True)[0] == scope for item in lessons)
                      for scope in ("general", "scenario", "unclassified")}
            general = [item for item in lessons if _matches_experience_scope(item, "general", "")]
            return {"session_id": session.session_id, "journal_entries": len(visible),
                    "pending_message_ids": self.pending_message_ids(session, context),
                    "shared_lesson_count": len(lessons),
                    "lesson_scope_counts": scopes,
                    "recent_lessons": [{"lesson_id": item["lesson_id"], "title": item.get("title", ""),
                                        "status": item.get("status", "candidate"),
                                        "scope": "general", "scenario_name": ""}
                                       for item in general[-10:]],
                    "note": "Recent lessons include general candidates only. Scenario lessons require a named scenario lookup; "
                            "untagged records require an explicit unclassified lookup. The journal follows this checkpoint's "
                            "visible messages. Shared lessons are not rewound."}

    def prompt_context(self, session: ChatSession, context: dict | None = None) -> str:
        catalog = self.catalog(session, context)
        return ("Local tactical memory and shared experience for this message:\n" + _json(catalog)
                + "\n\n")

    @staticmethod
    def _application_sources(context: dict | None) -> set[str]:
        context = context or {}
        values = context.get("application_message_ids", [])
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise GameMemoryError("Application-message provenance is invalid; original memory was preserved.")
        excluded = set(values)
        if context.get("mode") in {"save", "review"}:
            excluded.add(context.get("anchor_message_id"))
        return excluded

    @classmethod
    def _sources(cls, session: ChatSession, value: Any, *, user_only: bool = False,
                 context: dict | None = None) -> list[str]:
        result = []
        excluded = cls._application_sources(context)
        for item in _items(value, "source_message_ids"):
            identifier = _identifier(item, "source_message_id")
            message = session.get_message(identifier)
            if (message is None or message.status != "completed" or identifier in excluded
                    or (user_only and message.role != "user")):
                raise GameMemoryError("A source must be a completed original message in this checkpoint.")
            if identifier not in result:
                result.append(identifier)
        return result

    @staticmethod
    def _evidence(session: ChatSession, identifiers: list[str]) -> list[dict]:
        deleted = session.legacy_metadata.get("asl_ai_deleted_images", {})
        if not isinstance(deleted, dict):
            deleted = {}
        records = []
        for identifier in identifiers:
            message = session.get_message(identifier)
            images = [{"observation_id": image.observation_id, "captured_at": image.captured_at,
                       "original_available": True} for image in message.attachments
                      if image.observation_id not in deleted]
            images.extend({"observation_id": image_id, "captured_at": value.get("captured_at"),
                           "original_available": False, "reason": "Deleted by the player"}
                          for image_id, value in deleted.items()
                          if isinstance(value, dict) and identifier in value.get("message_ids", []))
            records.append({"message_id": identifier, "role": message.role,
                            "created_at": message.created_at, "text_excerpt": message.text[:2400],
                            "excerpt_truncated": len(message.text) > 2400, "images": images})
        return records

    @staticmethod
    def _trusted(session: ChatSession, context: dict | None) -> dict:
        context = context or {}
        model = _text(context.get("model", ""), "trusted model", 200, required=True)
        anchor = _identifier(context.get("anchor_message_id", ""), "trusted anchor_message_id")
        anchor_message = session.get_message(anchor)
        if anchor_message is None or anchor_message.role != "user" or anchor_message.status != "completed":
            raise GameMemoryError("The trusted anchor is not in this checkpoint.")
        visibility = _identifier(context.get("assistant_message_id", ""), "trusted assistant_message_id")
        assistant = session.get_message(visibility)
        if assistant is None or assistant.role != "assistant":
            raise GameMemoryError("The trusted assistant message is not in this checkpoint.")
        return {"session_id": session.session_id, "model": model,
                "thread_id": _text(context.get("thread_id", ""), "trusted thread_id", 200),
                "turn_id": _text(context.get("turn_id", ""), "trusted turn_id", 200),
                "anchor_message_id": anchor, "visibility_message_id": visibility,
                "recorded_at": now_utc()}

    def _record(self, session: ChatSession, arguments: dict, context: dict | None) -> dict:
        trusted = self._trusted(session, context)
        if set(arguments) - {"intents", "lessons", "processed_message_ids"}:
            raise GameMemoryError("Only bounded memory content may be supplied by the model.")
        journal, target, fallback = self._journal(session, context)
        library = self._library()
        existing_entries = {item["entry_id"]: item for item in journal["entries"]}
        written_intents, written_lessons = [], []
        for item in _items(arguments.get("intents", []), "intents", 12):
            if not isinstance(item, dict) or set(item) - {
                    "action", "goal", "assumptions", "expected_result", "change_conditions",
                    "previous_entry_id", "source_message_ids"}:
                raise GameMemoryError("An intent must contain only the supported fields.")
            sources = self._sources(session, item.get("source_message_ids", [trusted["anchor_message_id"]]), context=context)
            if not sources:
                raise GameMemoryError("An intent needs an original source message.")
            payload = {field: _text(item.get(field, ""), field, 2000, required=field in {"action", "goal"})
                       for field in ("action", "goal", "assumptions", "expected_result", "change_conditions")}
            previous = item.get("previous_entry_id", "")
            if previous:
                previous = _identifier(previous, "previous_entry_id")
                if (previous not in existing_entries or existing_entries[previous].get("kind") != "intent"
                        or not self._visible(existing_entries[previous], session)):
                    raise GameMemoryError("An intent revision must reference a visible earlier journal entry.")
            payload.update({"source_message_ids": sources, "previous_entry_id": previous})
            identifier = _identity("intent-", [session.session_id, trusted["visibility_message_id"], payload])
            if identifier not in existing_entries:
                record = {**trusted, **payload, "entry_id": identifier, "kind": "intent",
                          "interpretation": "Contemporaneous stated intent; not hidden reasoning."}
                journal["entries"].append(record)
                existing_entries[identifier] = record
            written_intents.append(identifier)
        for item in _items(arguments.get("lessons", []), "lessons", 12):
            if not isinstance(item, dict) or set(item) - {
                    "title", "lesson", "conditions", "observed_result", "evidence_kind",
                    "source_message_ids", "journal_entry_ids", "existing_lesson_id", "relationship",
                    "scope", "scenario_name"}:
                raise GameMemoryError("A lesson must contain only the supported fields.")
            scope, scenario_name = _lesson_classification(item)
            sources = self._sources(session, item.get("source_message_ids", []), user_only=True, context=context)
            if not sources:
                raise GameMemoryError("A candidate lesson needs the player's reported evidence, not a plan alone.")
            kind = item.get("evidence_kind")
            if kind not in {"reported_outcome", "reported_correction"}:
                raise GameMemoryError("Lessons require a reported outcome or correction.")
            payload = {field: _text(item.get(field, ""), field, 3000 if field != "title" else 200,
                                   required=field != "conditions")
                       for field in ("title", "lesson", "conditions", "observed_result")}
            payload.update({"scope": scope, "scenario_name": scenario_name})
            journal_ids = []
            for identifier in _items(item.get("journal_entry_ids", []), "journal_entry_ids"):
                identifier = _identifier(identifier, "journal_entry_id")
                if identifier not in existing_entries or not self._visible(existing_entries[identifier], session):
                    raise GameMemoryError("Lesson journal evidence must be visible in this checkpoint.")
                journal_ids.append(identifier)
            existing_id = item.get("existing_lesson_id", "")
            if existing_id:
                existing_id = _identifier(existing_id, "existing_lesson_id")
            match = next((record for record in library["lessons"] if record["lesson_id"] == existing_id), None)
            if existing_id and match is None:
                raise GameMemoryError("The experience record to revise does not exist.")
            if match is not None and not _matches_experience_scope(match, scope, scenario_name):
                raise GameMemoryError("A lesson revision must keep its existing scope and scenario name.")
            relationship = item.get("relationship", "supports")
            if relationship not in {"supports", "contradicts", "revises"}:
                raise GameMemoryError("Unsupported experience relationship.")
            signature = _identity("lesson-", [scope, _normalized(scenario_name),
                                               _normalized(payload["lesson"]), _normalized(payload["conditions"])])
            if match is None:
                match = next((record for record in library["lessons"] if record["lesson_id"] == signature), None)
                if match is not None and not _matches_experience_scope(match, scope, scenario_name):
                    raise GameMemoryError("A lesson revision must keep its existing scope and scenario name.")
            if match is None:
                if relationship != "supports":
                    raise GameMemoryError("A revision or counterexample requires an existing lesson ID.")
                match = {"lesson_id": signature, "title": payload["title"], "lesson": payload["lesson"],
                         "conditions": payload["conditions"], "status": "candidate",
                         "scope": scope, "scenario_name": scenario_name,
                         "created_at": trusted["recorded_at"], "revisions": []}
                library["lessons"].append(match)
            # Equivalent case/spacing in the lookup must not change the stored
            # scenario title or turn the same source/content into new evidence.
            payload["scenario_name"] = match["scenario_name"]
            revision = {**trusted, **payload, "source_message_ids": sources,
                        "journal_entry_ids": journal_ids, "evidence_kind": kind,
                        "relationship": relationship, "source_evidence": self._evidence(session, sources)}
            revision_id = _identity("evidence-", [match["lesson_id"], session.session_id, sources,
                                                   payload, relationship, journal_ids])
            if not any(record["revision_id"] == revision_id for record in match["revisions"]):
                revision["revision_id"] = revision_id
                match["revisions"].append(revision)
                match["updated_at"] = trusted["recorded_at"]
                if relationship == "contradicts":
                    match["status"] = "contradicted"
                elif relationship == "revises":
                    match["status"] = "candidate"
            written_lessons.append(match["lesson_id"])
        processed = self._sources(session, arguments.get("processed_message_ids", []), user_only=True, context=context)
        checkpoint_payload = {"source_message_ids": processed, "processed_message_ids": processed,
                              "intent_ids": written_intents, "lesson_ids": written_lessons}
        checkpoint_id = _identity("memory-", [session.session_id, trusted["visibility_message_id"], checkpoint_payload])
        if checkpoint_id not in existing_entries:
            journal["entries"].append({**trusted, **checkpoint_payload, "entry_id": checkpoint_id,
                                       "kind": "memory_checkpoint"})
        # The watermark is acknowledged only after every required write succeeds.
        # A partial disk failure is safe to retry: IDs make both writes idempotent.
        if written_lessons:
            self._write(self.experience_path, library)
        self._write(target, journal)
        if target != fallback and fallback.exists():
            fallback.unlink()
        return {"saved": True, "intent_ids": written_intents, "lesson_ids": written_lessons,
                "processed_message_ids": processed,
                "pending_message_ids": self.pending_message_ids(session, context),
                "note": "Lessons are unreviewed candidates. General lessons are shared by default; scenario lessons "
                        "require a lookup for the named scenario."}

    def _review(self, session: ChatSession, arguments: dict, context: dict | None) -> dict:
        trusted = self._trusted(session, context)
        if set(arguments) - {"title", "text", "source_message_ids"}:
            raise GameMemoryError("A review accepts only title, text, and original source IDs.")
        sources = self._sources(session, arguments.get("source_message_ids", []), context=context)
        if not sources:
            raise GameMemoryError("A review needs original source messages.")
        payload = {"title": _text(arguments.get("title", ""), "title", 200, required=True),
                   "text": _text(arguments.get("text", ""), "text", 20000, required=True),
                   "source_message_ids": sources}
        journal, target, fallback = self._journal(session, context)
        identifier = _identity("review-", [session.session_id, trusted["visibility_message_id"], payload])
        if not any(item["entry_id"] == identifier for item in journal["entries"]):
            journal["entries"].append({**trusted, **payload, "entry_id": identifier, "kind": "review",
                                       "scope": "Available recorded play through the source messages; game need not be finished."})
        self._write(target, journal)
        if target != fallback and fallback.exists():
            fallback.unlink()
        return {"saved": True, "review_id": identifier, "game_ended": False}

    def _source_images(self, game_id: str, identifiers: set[str]) -> dict[str, tuple[bool | None, str]]:
        """Refresh old lesson provenance without exposing user filesystem paths.

        The folder registry is application-owned, never supplied by the model.
        Missing/corrupt indices are uncertainty, not proof of image availability.
        All database access here is read-only, including when a file is missing.
        """
        unknown = {identifier: (None, "Original availability could not be verified") for identifier in identifiers}
        try:
            registry = json.loads((self.root / "known-game-folders.json").read_text(encoding="utf-8"))
            if not isinstance(registry, list):
                return unknown
            folders = [Path(item["folder"]) for item in reversed(registry)
                       if isinstance(item, dict) and item.get("game_id") == game_id
                       and isinstance(item.get("folder"), str)]
        except (OSError, ValueError, UnicodeError):
            return unknown
        if not folders:
            return unknown
        states = {identifier: (False, "The original game folder is unavailable") for identifier in identifiers}
        verified = False
        for folder in folders:
            database = folder.expanduser().resolve() / ".vasl-ai-bridge" / "game.aslgame"
            if not database.is_file():
                continue
            try:
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                    row = connection.execute("SELECT value FROM game_folder_info WHERE key='session_id'").fetchone()
                    if row is None or row[0] != game_id:
                        continue
                    available = {row[0] for row in connection.execute("SELECT observation_id FROM images")}
                    deleted = {row[0] for row in connection.execute("SELECT observation_id FROM game_deleted_images")}
                    verified = True
                    for identifier in identifiers:
                        if identifier in available:
                            states[identifier] = (True, "Original remains in the source game folder")
                        elif states[identifier][0] is not True:
                            states[identifier] = (False, "Deleted by the player" if identifier in deleted
                                                  else "Original is not in the source game folder")
            except (OSError, sqlite3.Error):
                return unknown
        if not verified and any((folder / ".vasl-ai-bridge" / "game.aslgame").is_file() for folder in folders):
            return unknown
        return states

    def _lesson_for_read(self, lesson: dict) -> dict:
        record = deepcopy(lesson)
        scope, scenario_name = _lesson_classification(lesson, allow_untagged=True)
        record.update({"scope": scope, "scenario_name": scenario_name})
        game_images: dict[str, set[str]] = {}
        for revision in record["revisions"]:
            game_id = revision.get("session_id", "")
            images = game_images.setdefault(game_id, set())
            images.update(image["observation_id"] for source in revision["source_evidence"] for image in source["images"])
        states = {game_id: self._source_images(game_id, images) for game_id, images in game_images.items() if images}
        for revision in record["revisions"]:
            for source in revision["source_evidence"]:
                for image in source["images"]:
                    previous = image.get("original_available")
                    available, reason = states[revision.get("session_id", "")][image["observation_id"]]
                    # A deleted marker is durable even if the source registry is
                    # temporarily unreadable or no longer tracks the game.
                    if previous is False and available is not True:
                        available, reason = False, image.get("reason", "Deleted by the player")
                    image["original_available_at_recording"] = previous
                    image["original_available"] = available
                    image["availability_note"] = reason
        return record

    def handle_tool(self, session: ChatSession, name: str, arguments: dict,
                    *, context: dict | None = None) -> dict:
        if name not in MEMORY_TOOL_NAMES or not isinstance(arguments, dict):
            raise GameMemoryError("Unsupported local memory operation.")
        if len(_json(arguments)) > _MAX_RECORD_CHARS:
            raise GameMemoryError("The memory request is too large; use smaller records.")
        allowed = {
            "record_turn_memory": {"intents", "lessons", "processed_message_ids"},
            "record_review": {"title", "text", "source_message_ids"},
            "search_tactical_journal": {"query", "limit"},
            "read_tactical_journal": {"entry_id", "offset", "limit"},
            "search_experience": {"query", "limit", "scope", "scenario_name"},
            "read_experience": {"lesson_id", "offset", "max_chars", "scope", "scenario_name"},
        }
        if set(arguments) - allowed[name]:
            raise GameMemoryError("Only the registered bounded fields are accepted by this memory tool.")
        if any(type(arguments[field]) is not int or arguments[field] < 1
               for field in ("limit", "max_chars") if field in arguments):
            raise GameMemoryError("A memory page size must be a positive integer.")
        with self._lock:
            if name == "record_turn_memory":
                return self._record(session, arguments, context)
            if name == "record_review":
                return self._review(session, arguments, context)
            if name in {"search_tactical_journal", "read_tactical_journal"}:
                journal, _, _ = self._journal(session, context)
                entries = self._visible_entries(journal, session)
                if name == "search_tactical_journal":
                    query = _text(arguments.get("query", ""), "query", 2000).casefold()
                    limit = max(1, _integer(arguments, "limit", 20, 30))
                    matches = [{"entry_id": item["entry_id"], "kind": item["kind"],
                                "recorded_at": item["recorded_at"], "model": item["model"],
                                "source_message_ids": item["source_message_ids"],
                                "excerpt": (item.get("text") or item.get("goal", ""))[:1000]}
                               for item in entries if query in _json(item).casefold()]
                    return {"matches": matches[:limit], "total_matches": len(matches)}
                identifier = arguments.get("entry_id", "")
                if identifier:
                    identifier = _identifier(identifier, "entry_id")
                    record = next((item for item in entries if item["entry_id"] == identifier), None)
                    if record is None:
                        raise GameMemoryError("This journal entry is not visible in the loaded checkpoint.")
                    return {"entry": record}
                offset = _integer(arguments, "offset", 0, 10_000_000)
                limit = max(1, _integer(arguments, "limit", 10, 30))
                selected = []
                for item in entries[offset:offset + limit]:
                    if selected and len(_json(selected + [item])) > _MAX_RECORD_CHARS:
                        break
                    selected.append(item)
                following = offset + len(selected)
                return {"entries": selected, "total_entries": len(entries),
                        "next_offset": following if following < len(entries) else None}
            library = self._library()
            scope, scenario_name = _experience_filter(arguments)
            if name == "search_experience":
                query = _text(arguments.get("query", ""), "query", 2000).casefold()
                limit = max(1, _integer(arguments, "limit", 20, 30))
                matches = [{"lesson_id": item["lesson_id"], "title": item["title"],
                            "status": item["status"], "excerpt": item["lesson"][:800],
                            "conditions": item["conditions"][:500], "evidence_count": len(item["revisions"]),
                            "scope": _lesson_classification(item, allow_untagged=True)[0],
                            "scenario_name": _lesson_classification(item, allow_untagged=True)[1]}
                           for item in library["lessons"] if _matches_experience_scope(item, scope, scenario_name)
                           and query in _json(item).casefold()]
                return {"matches": matches[:limit], "total_matches": len(matches),
                        "scope": scope, "scenario_name": scenario_name,
                        "note": "Only the requested applicability scope is included. Candidates can be wrong; "
                                "unclassified records have no established applicability. Shared experience is not rewound."}
            identifier = _identifier(arguments.get("lesson_id", ""), "lesson_id")
            record = next((item for item in library["lessons"] if item["lesson_id"] == identifier), None)
            if record is None:
                raise GameMemoryError("This shared experience record does not exist.")
            if not _matches_experience_scope(record, scope, scenario_name):
                raise GameMemoryError("This experience record does not match the requested scope and scenario name.")
            text = _json(self._lesson_for_read(record))
            offset = _integer(arguments, "offset", 0, 100_000_000)
            maximum = max(1, _integer(arguments, "max_chars", 12000, 24000))
            following = offset + min(maximum, max(0, len(text) - offset))
            return {"lesson_id": identifier, "status": record["status"], "text": text[offset:following],
                    "scope": scope, "scenario_name": _lesson_classification(record, allow_untagged=True)[1],
                    "total_chars": len(text), "next_text_offset": following if following < len(text) else None,
                    "note": "Source excerpts are preserved. Original image availability depends on the source game folder."}
