"""Incremental, portable SQLite archives for ordinary chat sessions."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from .chat_session import ChatMessage, ChatSession, Reference, new_id, now_utc
from .session import Observation, Session, SessionError
from .storage import StorageError, default_data_directory


SCHEMA_VERSION = 1
_FORBIDDEN_KEYS = {"api_key", "apikey", "access_token", "refresh_token", "authorization", "client_secret"}
_METADATA_KEYS = {"session_id", "title", "save_report", "backend_mode", "backend_thread_id",
                  "created_at", "updated_at", "active_context", "legacy_metadata"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blobs (sha256 TEXT PRIMARY KEY, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS images (
    observation_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL REFERENCES blobs(sha256),
    captured_at TEXT NOT NULL, source TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY, position INTEGER NOT NULL, role TEXT NOT NULL,
    text TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS message_order ON messages(position);
CREATE TABLE IF NOT EXISTS attachments (
    message_id TEXT NOT NULL REFERENCES messages(message_id), position INTEGER NOT NULL,
    observation_id TEXT NOT NULL REFERENCES images(observation_id),
    PRIMARY KEY(message_id, position)
);
CREATE TABLE IF NOT EXISTS refs (
    reference_id TEXT PRIMARY KEY, position INTEGER NOT NULL, name TEXT NOT NULL,
    media_type TEXT NOT NULL, sha256 TEXT NOT NULL REFERENCES blobs(sha256),
    added_at TEXT NOT NULL, source_name TEXT NOT NULL, indexed_text TEXT, extraction_error TEXT NOT NULL
);
"""


def _ensure_no_credentials(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in _FORBIDDEN_KEYS:
                raise StorageError("Credentials cannot be stored in conversation metadata.")
            _ensure_no_credentials(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            _ensure_no_credentials(child)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _metadata(session: ChatSession) -> dict[str, Any]:
    data = {"session_id": session.session_id, "title": session.title,
            "save_report": session.save_report, "backend_mode": session.backend_mode,
            "backend_thread_id": session.backend_thread_id, "created_at": session.created_at,
            "updated_at": session.updated_at, "active_context": session.active_context,
            "legacy_metadata": session.legacy_metadata}
    _ensure_no_credentials(data)
    return data


def _read_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


class ChatStore:
    """Create no files until a user operation first saves or imports data."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root if root is not None else default_data_directory()).expanduser().resolve()
        self._session_paths: dict[str, Path] = {}
        self._digest_session_id: str | None = None
        self._digest_cache: dict[int, tuple[bytes, str]] = {}

    def _digest(self, content: bytes) -> str:
        # Attachments are immutable bytes. An autosave must not rehash every
        # previous screenshot or bind every previous BLOB to SQLite again.
        cached = self._digest_cache.get(id(content))
        if cached is not None and cached[0] is content:
            return cached[1]
        digest = hashlib.sha256(content).hexdigest()
        self._digest_cache[id(content)] = (content, digest)
        return digest

    def _forget_digests(self) -> None:
        self._digest_cache.clear()
        self._digest_session_id = None

    def default_path(self, session: ChatSession) -> Path:
        return self.root / "sessions" / f"{session.session_id}.aslgame"

    def save(self, session: ChatSession, path: Path | None = None) -> Path:
        if not isinstance(session, ChatSession):
            raise StorageError("Only conversation sessions can be saved.")
        if session.session_id != self._digest_session_id:
            self._forget_digests()
            self._digest_session_id = session.session_id
        # Validate before creating directories or touching an existing archive.
        try:
            session.__post_init__()
            metadata = _metadata(session)
            _json(metadata)
        except (TypeError, ValueError) as exc:
            raise StorageError(f"Conversation cannot be serialized: {exc}") from exc
        destination = Path(path if path is not None else self.default_path(session)).expanduser().resolve()
        if destination.suffix.casefold() == ".json":
            raise StorageError("Choose an .aslgame filename; a legacy JSON checkpoint must not be overwritten.")
        source = self._session_paths.get(session.session_id)
        temporary: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Same-file autosaves are transactions. Save As is a portable SQLite
            # backup followed by a transaction and an atomic destination replace.
            default_destination = self.default_path(session)
            copy_required = not destination.exists() or (
                source is not None and source != destination and destination != default_destination)
            if copy_required:
                with tempfile.NamedTemporaryFile(prefix=".aslgame-", suffix=".tmp",
                                                 dir=destination.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                target = temporary
            else:
                target = destination
            with closing(sqlite3.connect(target)) as connection, connection:
                connection.execute("PRAGMA foreign_keys=ON")
                if temporary is not None and source is not None and source.exists():
                    with closing(_read_connection(source)) as original:
                        original.backup(connection)
                self._write(connection, session, metadata)
            # Close the database before replacing it on Windows.
            if temporary is not None:
                os.replace(temporary, destination)
                temporary = None
            self._session_paths[session.session_id] = destination
            return destination
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise StorageError(f"Could not save conversation: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _write(self, connection: sqlite3.Connection, session: ChatSession, metadata: dict[str, Any]) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise StorageError("Unsupported conversation archive schema version.")
        connection.executescript(_SCHEMA)
        existing_id = connection.execute("SELECT value FROM metadata WHERE key='session_id'").fetchone()
        if existing_id is not None and json.loads(existing_id[0]) != session.session_id:
            raise StorageError("This archive belongs to a different conversation.")
        if version == 0:
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        for key, value in metadata.items():
            connection.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                               "WHERE metadata.value IS NOT excluded.value", (key, _json(value)))
        seen_images: dict[str, Observation] = {}
        saved_images = {row[0]: tuple(row) for row in connection.execute("SELECT * FROM images")}
        saved_blobs = {row[0] for row in connection.execute("SELECT sha256 FROM blobs")}
        for position, message in enumerate(session.messages):
            message.__post_init__()
            connection.execute("INSERT INTO messages VALUES (?,?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET "
                               "position=excluded.position,role=excluded.role,text=excluded.text,"
                               "created_at=excluded.created_at,status=excluded.status WHERE "
                               "messages.position IS NOT excluded.position OR messages.role IS NOT excluded.role OR "
                               "messages.text IS NOT excluded.text OR messages.created_at IS NOT excluded.created_at OR "
                               "messages.status IS NOT excluded.status",
                               (message.message_id, position, message.role, message.text, message.created_at, message.status))
            current = [(row[0], row[1]) for row in connection.execute(
                "SELECT position,observation_id FROM attachments WHERE message_id=? ORDER BY position", (message.message_id,))]
            proposed = [(index, image.observation_id) for index, image in enumerate(message.attachments)]
            for image in message.attachments:
                previous = seen_images.get(image.observation_id)
                if previous is not None and previous != image:
                    raise StorageError("An image identifier cannot refer to different screenshots.")
                seen_images[image.observation_id] = image
                digest = self._digest(image.png)
                record = (image.observation_id, digest, image.captured_at, image.source, image.width, image.height)
                existing = saved_images.get(image.observation_id)
                if existing is not None and tuple(existing) != record:
                    raise StorageError("A saved screenshot must not change under its existing identifier.")
                if digest not in saved_blobs:
                    connection.execute("INSERT INTO blobs VALUES (?,?)", (digest, image.png))
                    saved_blobs.add(digest)
                if existing is None:
                    connection.execute("INSERT INTO images VALUES (?,?,?,?,?,?)", record)
                    saved_images[image.observation_id] = record
            if current != proposed:
                connection.execute("DELETE FROM attachments WHERE message_id=?", (message.message_id,))
                connection.executemany("INSERT INTO attachments VALUES (?,?,?)",
                                       [(message.message_id, index, image_id) for index, image_id in proposed])
        for position, reference in enumerate(session.references):
            reference.__post_init__()
            digest = self._digest(reference.content)
            if digest not in saved_blobs:
                connection.execute("INSERT INTO blobs VALUES (?,?)", (digest, reference.content))
                saved_blobs.add(digest)
            connection.execute("INSERT INTO refs VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(reference_id) DO UPDATE SET "
                               "position=excluded.position,name=excluded.name,media_type=excluded.media_type,"
                               "sha256=excluded.sha256,added_at=excluded.added_at,source_name=excluded.source_name,"
                               "indexed_text=excluded.indexed_text,extraction_error=excluded.extraction_error WHERE "
                               "refs.position IS NOT excluded.position OR refs.name IS NOT excluded.name OR "
                               "refs.media_type IS NOT excluded.media_type OR refs.sha256 IS NOT excluded.sha256 OR "
                               "refs.added_at IS NOT excluded.added_at OR refs.source_name IS NOT excluded.source_name OR "
                               "refs.indexed_text IS NOT excluded.indexed_text OR refs.extraction_error IS NOT excluded.extraction_error",
                               (reference.reference_id, position, reference.name, reference.media_type, digest,
                                reference.added_at, reference.source_name, reference.indexed_text, reference.extraction_error))

    def load(self, path: Path) -> ChatSession:
        source = Path(path).expanduser().resolve()
        try:
            with source.open("rb") as handle:
                header = handle.read(16)
            if header != b"SQLite format 3\x00":
                session = self._load_legacy(source)
                self._forget_digests()
                return session
            with closing(_read_connection(source)) as connection:
                if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    raise StorageError("Unsupported conversation archive schema version.")
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise StorageError("Conversation archive is damaged.")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise StorageError("Conversation archive has missing attachments.")
                metadata = {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM metadata")}
                if set(metadata) != _METADATA_KEYS:
                    raise StorageError("Conversation archive metadata is incomplete or unsupported.")
                _ensure_no_credentials(metadata)
                images: dict[str, Observation] = {}
                for record in connection.execute("SELECT images.*,blobs.data FROM images JOIN blobs USING(sha256)"):
                    image_id, digest, captured_at, capture_source, width, height, png = record
                    if hashlib.sha256(png).hexdigest() != digest:
                        raise StorageError("Saved screenshot checksum does not match its image.")
                    images[image_id] = Observation(image_id, captured_at, capture_source, png, width, height)
                links: dict[str, list[Observation]] = {}
                for message_id, _, image_id in connection.execute("SELECT * FROM attachments ORDER BY message_id,position"):
                    links.setdefault(message_id, []).append(images[image_id])
                messages = [ChatMessage(message_id, role, text, created_at, status, links.get(message_id, []))
                            for message_id, _, role, text, created_at, status in
                            connection.execute("SELECT * FROM messages ORDER BY position,rowid")]
                references = []
                for record in connection.execute("SELECT refs.*,blobs.data FROM refs JOIN blobs USING(sha256) ORDER BY position"):
                    reference_id, _, name, media_type, digest, added_at, source_name, indexed_text, extraction_error, content = record
                    if hashlib.sha256(content).hexdigest() != digest:
                        raise StorageError("Saved reference checksum does not match its file.")
                    references.append(Reference(reference_id, name, media_type, content, added_at, source_name,
                                                indexed_text, extraction_error))
                session = ChatSession(messages=messages, references=references, **metadata)
            self._session_paths[session.session_id] = source
            self._forget_digests()
            return session
        except StorageError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, SessionError) as exc:
            raise StorageError(f"Could not open conversation: {exc}") from exc

    def import_reference(self, session: ChatSession, path: Path, *, archive_path: Path | None = None) -> Reference:
        source = Path(path).expanduser().resolve()
        try:
            content = source.read_bytes()
            from .reference_formats import validate_reference_import
            validate_reference_import(source.name, content)
            media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            reference = Reference(new_id(), source.name, media_type, content, source_name=source.name)
            reference.text()
            session.references.append(reference)
            session.updated_at = now_utc()
            try:
                self.save(session, archive_path)
            except Exception:
                session.references.remove(reference)
                raise
            return reference
        except (OSError, ValueError) as exc:
            raise StorageError(f"Could not import reference: {exc}") from exc

    def _load_legacy(self, path: Path) -> ChatSession:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise StorageError(f"Legacy checkpoint contains a duplicate JSON field: {key}.")
                result[key] = value
            return result

        data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("session"), dict):
            raise StorageError("Unsupported legacy checkpoint schema version.")
        original = data["session"]
        # Validate legacy data without saving or changing the original file.
        legacy = Session.from_dict(original)
        messages = [ChatMessage(new_id(), message.role, message.text, message.created_at) for message in legacy.messages]
        session = ChatSession(session_id=legacy.session_id, title=legacy.scenario, messages=messages,
                              backend_mode="", legacy_metadata={key: value for key, value in original.items()
                                                                  if key not in {"messages", "observations", "submissions"}})
        if messages:
            session.created_at = messages[0].created_at
        attached = set()
        # Attach each saved observation once. Preserve all submission metadata
        # separately so prepared/submitted states do not duplicate image bytes.
        session.legacy_metadata["submissions"] = [
            {key: value for key, value in item.items() if key != "observation"} |
            {"observation_id": item["observation"]["observation_id"]} for item in original["submissions"]]
        for submission in legacy.submissions:
            image = submission.observation
            if image.observation_id in attached:
                continue
            candidates = [message for message in messages if message.role == "user" and
                          datetime.fromisoformat(message.created_at) <= datetime.fromisoformat(submission.created_at)]
            if candidates:
                candidates[-1].attachments.append(image)
            else:
                session.add_message("system", "Screenshot preserved from the imported checkpoint.", attachments=[image])
            attached.add(image.observation_id)
        for image in legacy.observations:
            if image.observation_id not in attached:
                session.add_message("system", "Unsubmitted local preview preserved from the imported checkpoint.",
                                    attachments=[image])
                attached.add(image.observation_id)
        report = []
        for label, value in (("Scenario", legacy.scenario), ("Phase", legacy.phase),
                             ("Recorded game notes", legacy.confirmed_facts), ("Last requested action", legacy.pending_action)):
            if value:
                report.append(f"{label}: {value}")
        session.save_report = "\n\n".join(report)
        return session
