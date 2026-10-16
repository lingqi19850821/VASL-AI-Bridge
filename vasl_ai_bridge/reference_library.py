"""A private reusable reference library with remembered per-game selection.

Only selected Reference snapshots are passed to a ChatSession. Game archives
continue to contain their own original bytes and do not depend on this library.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import mimetypes
from pathlib import Path
import sqlite3
from typing import Iterable

from .chat_session import Reference, new_id
from .reference_formats import MAX_REFERENCE_BYTES, ReferenceTextError, is_supported_reference, validate_reference_import
from .storage import StorageError, default_data_directory


_SCHEMA = """
CREATE TABLE IF NOT EXISTS refs (
    reference_id TEXT PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL, media_type TEXT NOT NULL, content BLOB NOT NULL,
    added_at TEXT NOT NULL, source_name TEXT NOT NULL,
    indexed_text TEXT, extraction_error TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS selected (
    reference_id TEXT PRIMARY KEY REFERENCES refs(reference_id),
    position INTEGER UNIQUE NOT NULL
);
"""
_COLUMNS = "r.reference_id,r.sha256,r.name,r.media_type,r.content,r.added_at,r.source_name,r.indexed_text,r.extraction_error"


def _reference(record: tuple) -> Reference:
    reference_id, digest, name, media_type, content, added_at, source_name, text, error = record
    if hashlib.sha256(content).hexdigest() != digest:
        raise StorageError("A reference library file failed its checksum check.")
    return Reference(reference_id, name, media_type, content, added_at, source_name, text, error)


class ReferenceLibrary:
    """Create storage only on an import or a deliberate selection change."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root if root is not None else default_data_directory()).expanduser().resolve()
        self.path = self.root / "reference-library.sqlite3"

    def _read(self, query: str, parameters: tuple = ()) -> list[Reference]:
        if not self.path.exists():
            return []
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as connection:
                return [_reference(record) for record in connection.execute(query, parameters)]
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise StorageError(f"Could not read the local reference library: {exc}") from exc

    def list_references(self) -> list[Reference]:
        return self._read(f"SELECT {_COLUMNS} FROM refs r ORDER BY r.rowid")

    def selected_references(self) -> list[Reference]:
        return self._read(f"SELECT {_COLUMNS} FROM selected s JOIN refs r USING(reference_id) ORDER BY s.position")

    def import_file(self, path: Path) -> Reference:
        try:
            source = Path(path).expanduser().resolve()
            if not is_supported_reference(source.name):
                validate_reference_import(source.name, b"")
            if source.stat().st_size > MAX_REFERENCE_BYTES:
                raise ReferenceTextError("The reference exceeds the 64 MiB import limit.")
            content = source.read_bytes()
            validate_reference_import(source.name, content)
            digest = hashlib.sha256(content).hexdigest()
            existing = self._read(f"SELECT {_COLUMNS} FROM refs r WHERE r.sha256=?", (digest,))
            if existing:
                if not is_supported_reference(existing[0].name):
                    # An explicit supported import supplies a usable reader name
                    # for a byte-identical legacy original. Saved game snapshots
                    # and input Reference objects retain their prior metadata.
                    previous = existing[0]
                    supported = Reference(previous.reference_id, source.name,
                                          mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                                          previous.content, previous.added_at, source.name)
                    supported.text()
                    with closing(sqlite3.connect(self.path)) as connection, connection:
                        connection.execute(
                            "UPDATE refs SET name=?,media_type=?,source_name=?,indexed_text=?,extraction_error=? "
                            "WHERE reference_id=? AND sha256=?",
                            (supported.name, supported.media_type, supported.source_name,
                             supported.indexed_text, supported.extraction_error, supported.reference_id, digest),
                        )
                    return supported
                return existing[0]
            reference = Reference(new_id(), source.name,
                                  mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                                  content, source_name=source.name)
            return self.adopt_references([reference])[0]
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise StorageError(f"Could not import reference: {exc}") from exc

    def adopt_references(self, references: Iterable[Reference]) -> list[Reference]:
        """Import saved originals without replacing the remembered selection.

        Returned objects use the library's canonical IDs when identical bytes
        were imported earlier. Input references and their saved IDs are intact.
        """
        references = list(references)
        if not references:
            return []
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(_SCHEMA)
                adopted = []
                for reference in references:
                    reference.__post_init__()
                    digest = reference.sha256
                    existing = connection.execute(f"SELECT {_COLUMNS} FROM refs r WHERE r.sha256=?", (digest,)).fetchone()
                    if existing:
                        adopted.append(_reference(existing))
                        continue
                    reference_id = reference.reference_id
                    if connection.execute("SELECT 1 FROM refs WHERE reference_id=?", (reference_id,)).fetchone():
                        reference_id = new_id()
                    # Cache extraction on a fresh snapshot, leaving saved inputs unchanged.
                    snapshot = Reference(reference_id, reference.name, reference.media_type,
                                         reference.content, reference.added_at, reference.source_name,
                                         reference.indexed_text, reference.extraction_error)
                    snapshot.text()
                    connection.execute("INSERT INTO refs VALUES (?,?,?,?,?,?,?,?,?)",
                                       (snapshot.reference_id, digest, snapshot.name, snapshot.media_type,
                                        snapshot.content, snapshot.added_at, snapshot.source_name,
                                        snapshot.indexed_text, snapshot.extraction_error))
                    adopted.append(snapshot)
                return adopted
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise StorageError(f"Could not update the local reference library: {exc}") from exc

    def set_selected(self, reference_ids: Iterable[str]) -> list[Reference]:
        """Remember the chosen library IDs; unknown IDs leave selection intact."""
        identifiers = list(dict.fromkeys(reference_ids))
        if not self.path.exists():
            if identifiers:
                raise StorageError("The requested reference is not in the local library.")
            return []
        try:
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute("PRAGMA foreign_keys=ON")
                selected = []
                for identifier in identifiers:
                    record = connection.execute(f"SELECT {_COLUMNS} FROM refs r WHERE r.reference_id=?", (identifier,)).fetchone()
                    if record is None:
                        raise StorageError("The requested reference is not in the local library.")
                    selected.append(_reference(record))
                connection.execute("DELETE FROM selected")
                connection.executemany("INSERT INTO selected VALUES (?,?)",
                                       [(reference.reference_id, position) for position, reference in enumerate(selected)])
                return selected
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise StorageError(f"Could not remember the reference selection: {exc}") from exc

    def remove_references(self, reference_ids: Iterable[str], *,
                          selected_ids: Iterable[str] | None = None) -> list[Reference]:
        """Remove imported copies and remember the remaining game selection.

        All IDs are checked before anything changes. When supplied, selected_ids
        is the current checkbox order, including any checked references being
        removed. Original source files are never opened or removed here.
        """
        try:
            identifiers = list(dict.fromkeys(reference_ids))
            chosen_ids = None if selected_ids is None else list(dict.fromkeys(selected_ids))
            if not self.path.exists():
                if identifiers or chosen_ids:
                    raise StorageError("The requested reference is not in the local library.")
                return []
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)) as connection, connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                for identifier in identifiers:
                    if connection.execute("SELECT 1 FROM refs WHERE reference_id=?", (identifier,)).fetchone() is None:
                        raise StorageError("The requested reference is not in the local library.")
                if chosen_ids is None:
                    records = connection.execute(
                        f"SELECT {_COLUMNS} FROM selected s JOIN refs r USING(reference_id) ORDER BY s.position"
                    ).fetchall()
                else:
                    records = []
                    for identifier in chosen_ids:
                        record = connection.execute(
                            f"SELECT {_COLUMNS} FROM refs r WHERE r.reference_id=?", (identifier,)
                        ).fetchone()
                        if record is None:
                            raise StorageError("The requested reference is not in the local library.")
                        records.append(record)
                removed = set(identifiers)
                selected = [_reference(record) for record in records if record[0] not in removed]
                connection.execute("DELETE FROM selected")
                connection.executemany("DELETE FROM refs WHERE reference_id=?", [(identifier,) for identifier in identifiers])
                connection.executemany(
                    "INSERT INTO selected VALUES (?,?)",
                    [(reference.reference_id, position) for position, reference in enumerate(selected)],
                )
                return selected
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise StorageError(f"Could not remove references from the local library: {exc}") from exc

    def compact(self) -> None:
        """Reclaim library space after a committed removal, without touching sources."""
        try:
            if not self.path.exists():
                return
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)) as connection:
                connection.execute("VACUUM")
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise StorageError(f"Could not reclaim local reference library space: {exc}") from exc
