"""Private, atomic JSON checkpoints kept outside the source checkout."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .session import Session, SessionError


SCHEMA_VERSION = 1
MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024
_SOURCE_ROOT = Path(__file__).resolve().parents[1]


class StorageError(ValueError):
    """A checkpoint cannot be saved or loaded safely."""


def default_data_directory() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "ASL_AI" if local else Path.home() / ".asl_ai"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StorageError(f"Checkpoint contains a duplicate JSON field: {key}.")
        result[key] = value
    return result


def _resolved_path(value: Path, name: str) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (TypeError, ValueError, OSError) as exc:
        raise StorageError(f"{name} is not a usable filesystem path.") from exc


class SessionStore:
    def __init__(self, root: Path):
        self.root = _resolved_path(root, "Checkpoint directory")

    def save(self, session: Session, path: Path | None = None) -> Path:
        if not isinstance(session, Session):
            raise StorageError("Only application sessions can be saved as checkpoints.")
        destination = _resolved_path(
            path if path is not None else self.root / "sessions" / f"{session.session_id}.json",
            "Checkpoint destination",
        )
        if destination.is_relative_to(_SOURCE_ROOT):
            raise StorageError("Private checkpoints must be saved outside the source repository.")
        try:
            payload = json.dumps(
                {"schema_version": SCHEMA_VERSION, "session": session.as_dict()},
                ensure_ascii=False, indent=2, allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, SessionError) as exc:
            raise StorageError(f"Session cannot be serialized: {exc}") from exc
        if len(payload) > MAX_CHECKPOINT_BYTES:
            raise StorageError("Checkpoint exceeds the supported 64 MiB size.")
        temporary: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".asl-checkpoint-", suffix=".tmp", dir=destination.parent, delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
        except OSError as exc:
            raise StorageError(f"Could not save checkpoint: {exc.strerror or exc}") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return destination

    def load(self, path: Path) -> Session:
        source = _resolved_path(path, "Checkpoint source")
        try:
            with source.open("rb") as handle:
                raw = handle.read(MAX_CHECKPOINT_BYTES + 1)
            if len(raw) > MAX_CHECKPOINT_BYTES:
                raise StorageError("Checkpoint exceeds the supported 64 MiB size.")
            data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
            if not isinstance(data, dict):
                raise StorageError("Checkpoint must contain a JSON object.")
            if type(data.get("schema_version")) is not int or data["schema_version"] != SCHEMA_VERSION:
                raise StorageError("Unsupported checkpoint schema version. Expected version 1.")
            if set(data) != {"schema_version", "session"}:
                raise StorageError("Checkpoint contains missing or unsupported fields.")
            return Session.from_dict(data["session"])
        except StorageError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, SessionError, RecursionError) as exc:
            raise StorageError(f"Checkpoint is invalid or damaged: {exc}") from exc
        except OSError as exc:
            raise StorageError(f"Could not open checkpoint: {exc.strerror or exc}") from exc
