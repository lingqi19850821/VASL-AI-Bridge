"""Named game checkpoints sharing one local database and original image bytes.

Checkpoint deletion removes a resume point, never the underlying game evidence.
Only the explicit screenshot cleanup operation removes original board images.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import fields
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from threading import RLock
from typing import Any

from .chat_session import ChatMessage, ChatSession, Reference, new_id, now_utc
from .chat_storage import ChatStore, _ensure_no_credentials, _json, _metadata
from .session import Observation
from .storage import StorageError


MANAGED_DIRECTORY = ".vasl-ai-bridge"
DATABASE_NAME = "game.aslgame"
DELETED_IMAGES_KEY = "asl_ai_deleted_images"
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS game_folder_info (key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS game_snapshots (snapshot_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,payload TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS game_current (singleton INTEGER PRIMARY KEY CHECK(singleton=1),updated_at TEXT NOT NULL,payload TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS game_points (point_id TEXT PRIMARY KEY,name TEXT NOT NULL,name_key TEXT NOT NULL UNIQUE,snapshot_id TEXT NOT NULL REFERENCES game_snapshots(snapshot_id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS game_deleted_images (observation_id TEXT PRIMARY KEY,payload TEXT NOT NULL)",
)


def _name(value: str, *, folder: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise StorageError("Choose a name from 1 to 120 characters.")
    value = value.strip()
    if any(ord(character) < 32 for character in value):
        raise StorageError("The name cannot contain control characters.")
    if folder and (value in {".", ".."} or re.search(r'[<>:"/\\|?*]', value)
                   or value.endswith((".", " "))
                   or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value)):
        raise StorageError("Choose a valid Windows game folder name.")
    return value


def _redirected(path: Path) -> bool:
    """Never follow a link or Windows reparse point during managed deletion."""
    if path.is_symlink() or path.is_junction():
        return True
    return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) if os.name == "nt" else False


def _plain_path(path: Path, boundary: Path) -> None:
    if not path.is_absolute() or not path.is_relative_to(boundary):
        raise StorageError("The managed game path must remain inside the selected game folder.")
    if _redirected(path) or path.resolve() != path:
        raise StorageError("Linked or redirected game files cannot be deleted by storage management.")


class GameFolderStore:
    """One owner lock serializes journal callbacks, UI saves and cleanup."""

    def __init__(self, chat_store: ChatStore):
        self.chat_store = chat_store
        self.root = chat_store.root
        self.lock = RLock()
        # Isolate ChatStore's source-path cache from portable legacy imports.
        self._writer = ChatStore(self.root)
        self.registry_path = self.root / "known-game-folders.json"

    @staticmethod
    def database_path(folder: Path) -> Path:
        return Path(folder).expanduser().resolve() / MANAGED_DIRECTORY / DATABASE_NAME

    def _path(self, folder: Path) -> Path:
        path = self.database_path(folder)
        if not path.is_file():
            raise StorageError("This game folder is missing or has moved. Open its new location; it was not recreated.")
        return path

    def _connect(self, folder: Path, *, readonly: bool = False) -> sqlite3.Connection:
        path = self._path(folder)
        connection = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            info = dict(connection.execute("SELECT key,value FROM game_folder_info"))
            identity = info.get("session_id")
            if (not isinstance(identity, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", identity) is None
                    or info.get("format_version") != "1"):
                raise StorageError("This is not a recognized game folder.")
        except Exception:
            connection.close()
            raise
        return connection

    @staticmethod
    def _snapshot(session: ChatSession) -> str:
        session.__post_init__()
        metadata = _metadata(session)
        # Future JSON-compatible session fields must also survive checkpoints.
        for item in fields(session):
            if item.name not in {"messages", "references"} and item.name not in metadata:
                metadata[item.name] = getattr(session, item.name)
        _ensure_no_credentials(metadata)
        deleted = session.legacy_metadata.get(DELETED_IMAGES_KEY, {})
        messages = []
        for message in session.messages:
            message.__post_init__()
            image_ids = [image.observation_id for image in message.attachments]
            image_ids.extend(identifier for identifier, marker in deleted.items()
                             if message.message_id in marker.get("message_ids", []) and identifier not in image_ids)
            messages.append({"message_id": message.message_id, "role": message.role, "text": message.text,
                             "created_at": message.created_at, "status": message.status, "image_ids": image_ids})
        return _json({"metadata": metadata, "messages": messages,
                      "references": [{"reference_id": item.reference_id, "name": item.name,
                                      "media_type": item.media_type, "sha256": item.sha256,
                                      "added_at": item.added_at, "source_name": item.source_name,
                                      "indexed_text": item.indexed_text, "extraction_error": item.extraction_error}
                                     for item in session.references]})

    @staticmethod
    def _identity(connection: sqlite3.Connection) -> str:
        return connection.execute("SELECT value FROM game_folder_info WHERE key='session_id'").fetchone()[0]

    def _write_current(self, connection: sqlite3.Connection, session: ChatSession, payload: str) -> None:
        if self._identity(connection) != session.session_id:
            raise StorageError("This game folder belongs to a different game.")
        deleted_ids = {row[0] for row in connection.execute("SELECT observation_id FROM game_deleted_images")}
        if any(image.observation_id in deleted_ids for message in session.messages for image in message.attachments):
            raise StorageError("A deleted screenshot cannot be restored by an old open view. Reopen the game and send a new capture.")
        previous = connection.execute("SELECT payload FROM game_current WHERE singleton=1").fetchone()
        retired = None
        if previous:
            prior = json.loads(previous[0])
            current = json.loads(payload)
            prior_messages, current_messages = prior["messages"], current["messages"]
            prior_ids = [item["message_id"] for item in prior_messages]
            current_ids = [item["message_id"] for item in current_messages]
            replaced_messages = any(before != after and before["status"] not in {"pending", "streaming"}
                                    for before, after in zip(prior_messages, current_messages))
            if (current_ids[:len(prior_ids)] != prior_ids or replaced_messages
                    or prior["references"] != current["references"]):
                # Rewinds, corrected originals and reference editions remain
                # available for explicit review. Streaming fragments need not
                # become additional permanent copies of the whole conversation.
                retired = previous[0]
        # Existing ChatStore validates image identities/checksums and writes only
        # new BLOBs. Its schema setup precedes DML; snapshot changes then share
        # that same transaction with the message and attachment changes.
        self._writer._write(connection, session, _metadata(session))
        if retired is not None:
            connection.execute("INSERT INTO game_snapshots VALUES (?,?,?)", (new_id(), now_utc(), retired))
        connection.execute("INSERT INTO game_current VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload",
                           (now_utc(), payload))

    def create_folder(self, parent: Path, name: str, session: ChatSession) -> Path:
        with self.lock:
            name = _name(name, folder=True)
            payload = self._snapshot(session)
            parent = Path(parent).expanduser().resolve()
            if not parent.is_dir():
                raise StorageError("Choose an existing parent folder.")
            folder = parent / name
            if folder.exists() and _redirected(folder):
                raise StorageError("Choose a game folder that is not a link or junction.")
            if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
                raise StorageError("Choose a new or empty game folder.")
            created_folder = not folder.exists()
            created_managed = False
            try:
                folder.mkdir(exist_ok=True)
                managed = folder / MANAGED_DIRECTORY
                managed.mkdir()
                created_managed = True
                path = managed / DATABASE_NAME
                with closing(sqlite3.connect(path)) as connection, connection:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute("INSERT INTO game_folder_info VALUES ('session_id',?)", (session.session_id,))
                    connection.execute("INSERT INTO game_folder_info VALUES ('format_version','1')")
                    connection.commit()
                    self._write_current(connection, session, payload)
            except Exception:
                # Only this operation's exact files are eligible for cleanup.
                if created_managed:
                    managed = folder / MANAGED_DIRECTORY
                    for suffix in ("", "-journal", "-wal", "-shm"):
                        (managed / (DATABASE_NAME + suffix)).unlink(missing_ok=True)
                    managed.rmdir()
                if created_folder and folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
                raise
            self._remember(folder, session.session_id)
            return folder

    def save_current(self, folder: Path, session: ChatSession) -> Path:
        with self.lock:
            payload = self._snapshot(session)
            try:
                with closing(self._connect(folder)) as connection, connection:
                    self._write_current(connection, session, payload)
            except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
                raise StorageError(f"Could not save this game folder: {exc}") from exc
            return self._path(folder)

    def save_point(self, folder: Path, session: ChatSession, name: str, overwrite: bool = False) -> dict:
        with self.lock:
            name = _name(name)
            payload = self._snapshot(session)
            try:
                with closing(self._connect(folder)) as connection, connection:
                    row = connection.execute("SELECT point_id,created_at,deleted FROM game_points WHERE name_key=?", (name.casefold(),)).fetchone()
                    if row and not row[2] and not overwrite:
                        raise StorageError("A save point with this name already exists. Choose overwrite or another name.")
                    # Validation above happens before any game content is written.
                    self._write_current(connection, session, payload)
                    point_id, created_at = (row[0], row[1]) if row else (new_id(), now_utc())
                    snapshot_id, updated_at = new_id(), now_utc()
                    connection.execute("INSERT INTO game_snapshots VALUES (?,?,?)", (snapshot_id, updated_at, payload))
                    connection.execute("INSERT INTO game_points VALUES (?,?,?,?,?,?,0) ON CONFLICT(point_id) DO UPDATE SET name=excluded.name,name_key=excluded.name_key,snapshot_id=excluded.snapshot_id,updated_at=excluded.updated_at,deleted=0",
                                       (point_id, name, name.casefold(), snapshot_id, created_at, updated_at))
                return {"point_id": point_id, "name": name, "created_at": created_at, "updated_at": updated_at,
                        "message_count": len(session.messages)}
            except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
                raise StorageError(f"Could not save this point: {exc}") from exc

    def list_points(self, folder: Path) -> list[dict]:
        with self.lock, closing(self._connect(folder, readonly=True)) as connection:
            rows = connection.execute("SELECT p.point_id,p.name,p.created_at,p.updated_at,s.payload FROM game_points p JOIN game_snapshots s USING(snapshot_id) WHERE p.deleted=0 ORDER BY p.updated_at,p.rowid")
            return [{"point_id": identifier, "name": name, "created_at": created, "updated_at": updated,
                     "message_count": len(json.loads(payload)["messages"])}
                    for identifier, name, created, updated, payload in rows]

    @staticmethod
    def _hydrate(connection: sqlite3.Connection, payload: str) -> ChatSession:
        snapshot = json.loads(payload)
        metadata = snapshot["metadata"]
        _ensure_no_credentials(metadata)
        deleted = {identifier: json.loads(value) for identifier, value in connection.execute("SELECT * FROM game_deleted_images")}
        used_deleted = {}
        messages = []
        for item in snapshot["messages"]:
            attachments = []
            for image_id in item["image_ids"]:
                if image_id in deleted:
                    used_deleted[image_id] = deleted[image_id]
                    continue
                row = connection.execute("SELECT i.*,b.data FROM images i JOIN blobs b USING(sha256) WHERE observation_id=?", (image_id,)).fetchone()
                if row is None:
                    raise StorageError("A game screenshot is missing without a deletion record.")
                identifier, digest, captured, source, width, height, png = row
                if hashlib.sha256(png).hexdigest() != digest:
                    raise StorageError("A game screenshot failed its checksum check.")
                attachments.append(Observation(identifier, captured, source, png, width, height))
            messages.append(ChatMessage(item["message_id"], item["role"], item["text"], item["created_at"], item["status"], attachments))
        references = []
        for item in snapshot["references"]:
            digest = item["sha256"]
            row = connection.execute("SELECT data FROM blobs WHERE sha256=?", (digest,)).fetchone()
            if row is None:
                raise StorageError("A saved reference file is missing.")
            content = row[0]
            if hashlib.sha256(content).hexdigest() != digest:
                raise StorageError("A saved reference failed its checksum check.")
            references.append(Reference(item["reference_id"], item["name"], item["media_type"], content,
                                        item["added_at"], item["source_name"], item["indexed_text"], item["extraction_error"]))
        legacy = metadata.setdefault("legacy_metadata", {})
        # The DB deletion ledger is authoritative even for an older checkpoint.
        legacy.pop(DELETED_IMAGES_KEY, None)
        if used_deleted:
            legacy[DELETED_IMAGES_KEY] = used_deleted
        return ChatSession(messages=messages, references=references, **metadata)

    def load_point(self, folder: Path, point_id: str | None = None) -> ChatSession:
        with self.lock:
            try:
                with closing(self._connect(folder, readonly=True)) as connection:
                    if point_id is None:
                        row = connection.execute("SELECT payload FROM game_current WHERE singleton=1").fetchone()
                    else:
                        row = connection.execute("SELECT s.payload FROM game_points p JOIN game_snapshots s USING(snapshot_id) WHERE p.point_id=? AND p.deleted=0", (point_id,)).fetchone()
                    if row is None:
                        raise StorageError("The requested save point no longer exists.")
                    session = self._hydrate(connection, row[0])
                    if session.session_id != self._identity(connection):
                        raise StorageError("This save point belongs to another game.")
                    if point_id is not None:
                        session.backend_thread_id = ""
                self._remember(Path(folder).resolve(), session.session_id)
                return session
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
                raise StorageError(f"Could not open this game: {exc}") from exc

    def load_history(self, folder: Path) -> ChatSession:
        """Explicit review access to all branches, including retired points."""
        with self.lock, closing(self._connect(folder, readonly=True)) as connection:
            payloads = [row[0] for row in connection.execute("SELECT payload FROM game_snapshots ORDER BY created_at,rowid")]
            current = connection.execute("SELECT payload FROM game_current WHERE singleton=1").fetchone()
            if current is None:
                raise StorageError("This game has no current progress.")
            payloads.append(current[0])
            result = self._hydrate(connection, current[0])
            records, references, versions = {}, {}, []
            for payload in payloads:
                snapshot = self._hydrate(connection, payload)
                for message in snapshot.messages:
                    earlier = records.get(message.message_id)
                    if earlier is not None and earlier.text != message.text:
                        versions.append({"message_id": earlier.message_id, "text": earlier.text,
                                         "created_at": earlier.created_at})
                    records[message.message_id] = message
                references.update({item.reference_id: item for item in snapshot.references})
            # Autosave originals not captured by a named point are also retained.
            for identifier, _, role, text, created, status in connection.execute("SELECT * FROM messages ORDER BY position,rowid"):
                if identifier not in records:
                    item = {"metadata": _metadata(result), "messages": [{"message_id": identifier, "role": role,
                            "text": text, "created_at": created, "status": status,
                            "image_ids": [row[0] for row in connection.execute("SELECT observation_id FROM attachments WHERE message_id=? ORDER BY position", (identifier,))]}], "references": []}
                    records[identifier] = self._hydrate(connection, _json(item)).messages[0]
            result.messages = sorted(records.values(), key=lambda item: datetime.fromisoformat(item.created_at.replace("Z", "+00:00")))
            result.references = list(references.values())
            result.backend_thread_id = ""
            result.legacy_metadata["asl_ai_review_all_branches"] = True
            if versions:
                result.legacy_metadata["asl_ai_previous_message_versions"] = versions
            deleted = {identifier: json.loads(value) for identifier, value in connection.execute("SELECT * FROM game_deleted_images")}
            if deleted:
                result.legacy_metadata[DELETED_IMAGES_KEY] = deleted
            return result

    def delete_point(self, folder: Path, point_id: str) -> None:
        with self.lock, closing(self._connect(folder)) as connection, connection:
            cursor = connection.execute("UPDATE game_points SET deleted=1,updated_at=? WHERE point_id=? AND deleted=0", (now_utc(), point_id))
            if cursor.rowcount != 1:
                raise StorageError("This save point no longer exists.")

    def delete_folder(self, folder: Path) -> dict:
        """Delete only recognized application data, preserving player files.

        Validate the complete small managed tree before deleting any file. No
        recursive filesystem deletion is used, and the shared experience library
        is outside the selected game directory and never part of this operation.
        """
        with self.lock:
            folder = Path(os.path.abspath(Path(folder).expanduser()))
            if folder == Path(folder.anchor):
                raise StorageError("Select a game folder rather than a drive root.")
            try:
                _plain_path(folder, folder)
                managed = folder / MANAGED_DIRECTORY
                _plain_path(managed, folder)
                database = managed / DATABASE_NAME
                _plain_path(database, managed)
                with closing(self._connect(folder, readonly=True)) as connection:
                    game_id = self._identity(connection)
                    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise StorageError("This game database is damaged; it was not deleted.")
                files, directories = [], []
                for child in managed.iterdir():
                    _plain_path(child, managed)
                    if child.name in {DATABASE_NAME + suffix for suffix in ("", "-journal", "-wal", "-shm")} and child.is_file():
                        files.append(child)
                    elif child.name == "memory" and child.is_dir():
                        directories.append(child)
                        for record in child.iterdir():
                            _plain_path(record, managed)
                            if not record.is_file() or not (record.name == game_id + ".json"
                                    or re.fullmatch(r"\.memory-[A-Za-z0-9_-]+\.tmp", record.name)):
                                raise StorageError("Unrecognized files were found in this game's managed data; nothing was deleted.")
                            files.append(record)
                    else:
                        raise StorageError("Unrecognized files were found in this game's managed data; nothing was deleted.")
                # Explicit absolute containment and redirection checks precede
                # each unlink as well as the initial all-or-nothing validation.
                for path in files:
                    _plain_path(folder, folder)
                    _plain_path(managed, folder)
                    _plain_path(path.parent, managed)
                    _plain_path(path, managed)
                    path.unlink()
                for directory in directories:
                    _plain_path(directory, managed)
                    directory.rmdir()
                managed.rmdir()
                removed_folder = not any(folder.iterdir())
                if removed_folder:
                    folder.rmdir()
                self._write_registry([item for item in self._registry() if Path(item["folder"]).resolve() != folder])
                self._writer._forget_digests()
                return {"folder": str(folder), "game_id": game_id, "removed_folder": removed_folder}
            except StorageError:
                raise
            except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                raise StorageError(f"Could not delete this game's managed data: {exc}") from exc

    def clear_screenshots(self, folder: Path) -> int:
        """Explicitly delete board originals across all points; keep references."""
        with self.lock, closing(self._connect(folder)) as connection:
            rows = list(connection.execute("SELECT * FROM images"))
            if not rows:
                return 0
            connection.execute("PRAGMA secure_delete=ON")
            with connection:
                for identifier, digest, captured, source, width, height in rows:
                    marker = {"observation_id": identifier, "sha256": digest, "captured_at": captured,
                              "source": source, "width": width, "height": height, "deleted_at": now_utc(),
                              "message_ids": [row[0] for row in connection.execute("SELECT DISTINCT message_id FROM attachments WHERE observation_id=?", (identifier,))]}
                    connection.execute("INSERT OR IGNORE INTO game_deleted_images VALUES (?,?)", (identifier, _json(marker)))
                connection.execute("DELETE FROM attachments")
                connection.execute("DELETE FROM images")
                retained = {row[0] for row in connection.execute("SELECT sha256 FROM refs")}
                for (payload,) in connection.execute("SELECT payload FROM game_snapshots UNION ALL SELECT payload FROM game_current"):
                    retained.update(item["sha256"] for item in json.loads(payload)["references"])
                # Reference files can themselves be PNGs, and historical editions
                # may use a digest no longer selected by current progress.
                for (digest,) in list(connection.execute("SELECT sha256 FROM blobs")):
                    if digest not in retained:
                        connection.execute("DELETE FROM blobs WHERE sha256=?", (digest,))
            # Reclaim database pages, rather than reporting a deletion that leaves
            # the same large disk allocation behind.
            connection.execute("VACUUM")
            self._writer._forget_digests()
            return len(rows)

    def describe(self, folder: Path) -> dict:
        with self.lock:
            path = self._path(folder)
            with closing(self._connect(folder, readonly=True)) as connection:
                game_id = self._identity(connection)
                image_count = connection.execute("SELECT COUNT(*) FROM images").fetchone()[0]
                deleted_count = connection.execute("SELECT COUNT(*) FROM game_deleted_images").fetchone()[0]
            folder = Path(folder).expanduser().resolve()
            return {"folder": str(folder), "path": str(folder), "database": str(path), "game_id": game_id,
                    "name": folder.name, "bytes": sum(item.stat().st_size for item in folder.rglob("*") if item.is_file()),
                    "screenshots": image_count, "deleted_screenshots": deleted_count, "points": self.list_points(folder)}

    def _remember(self, folder: Path, game_id: str) -> None:
        records = self._registry()
        # A moved folder has the same internal ID, but independent copied folders
        # may coexist. Only replace an old unavailable path automatically.
        records = [item for item in records if item["folder"] != str(folder)
                   and not (item["game_id"] == game_id and not self.database_path(Path(item["folder"])).is_file())]
        records.append({"game_id": game_id, "folder": str(folder), "name": folder.name, "opened_at": now_utc()})
        self._write_registry(records)

    def _write_registry(self, records: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, prefix=".game-folders-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(records, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.registry_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _registry(self) -> list[dict]:
        if not self.registry_path.exists():
            return []
        try:
            records = json.loads(self.registry_path.read_text(encoding="utf-8"))
            if not isinstance(records, list) or any(not isinstance(item, dict) or not isinstance(item.get("folder"), str)
                                                  or not isinstance(item.get("game_id"), str) for item in records):
                raise ValueError("invalid folder index")
            return records
        except (OSError, ValueError, TypeError) as exc:
            raise StorageError(f"Could not read the known game folders: {exc}") from exc

    def list_known_folders(self) -> list[dict]:
        with self.lock:
            records = []
            for item in self._registry():
                available = False
                try:
                    with closing(self._connect(Path(item["folder"]), readonly=True)) as connection:
                        available = self._identity(connection) == item["game_id"]
                except (StorageError, OSError, sqlite3.Error, ValueError, TypeError):
                    pass
                records.append(dict(item, available=available))
            return records
