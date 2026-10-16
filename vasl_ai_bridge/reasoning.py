"""Normalize runtime model capabilities without inventing supported efforts."""

from __future__ import annotations

from typing import Any


REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

_LABELS = {"none": "None", "minimal": "Minimal", "low": "Low", "medium": "Medium",
           "high": "High", "xhigh": "Extra high", "max": "Max", "ultra": "Ultra"}


def valid_model_identifier(value: Any, *, allow_empty: bool = False) -> bool:
    """Accept bounded ordinary model identifiers, never control characters."""
    return (isinstance(value, str) and (bool(value) or allow_empty)
            and value == value.strip() and len(value) <= 200
            and not any(ord(character) < 32 or ord(character) == 127 for character in value))


def model_identifier(model: dict[str, Any]) -> str:
    for name in ("model", "id", "slug"):
        value = model.get(name)
        if valid_model_identifier(value):
            return value
    return ""


def _reasoning_rows(model: dict[str, Any]) -> list[Any]:
    for name in ("supportedReasoningEfforts", "supported_reasoning_efforts", "supported_reasoning_levels"):
        rows = model.get(name)
        if isinstance(rows, list):
            return rows
    return []


def _row_effort(row: Any) -> str:
    if isinstance(row, str):
        return row if row in REASONING_EFFORTS else ""
    if isinstance(row, dict):
        for name in ("reasoningEffort", "reasoning_effort", "effort"):
            value = row.get(name)
            if isinstance(value, str) and value in REASONING_EFFORTS:
                return value
    return ""


def supported_reasoning_efforts(model: dict[str, Any]) -> tuple[str, ...]:
    """Keep only advertised protocol effort values, in increasing effort order."""
    advertised = {_row_effort(row) for row in _reasoning_rows(model)}
    return tuple(effort for effort in REASONING_EFFORTS if effort in advertised)


def default_reasoning_effort(model: dict[str, Any]) -> str:
    """Return a reported, supported default; empty means no known default."""
    supported = supported_reasoning_efforts(model)
    for name in ("defaultReasoningEffort", "default_reasoning_effort", "default_reasoning_level"):
        value = model.get(name)
        if isinstance(value, str) and value in supported:
            return value
    return ""


def reasoning_effort_label(effort: str) -> str:
    return _LABELS.get(effort, "Model default" if not effort else effort)


def reasoning_effort_description(model: dict[str, Any], effort: str) -> str:
    for row in _reasoning_rows(model):
        if isinstance(row, dict) and _row_effort(row) == effort:
            description = row.get("description")
            if isinstance(description, str):
                return description
    return ""
