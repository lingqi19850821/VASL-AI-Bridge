"""Shared input limits for local Note projection and widget construction."""

NOTE_FIELDS = ("assumptions", "goal", "expected_result")
MAX_NOTE_FIELD_CHARS = 2000  # Matches the existing intent recording contract.
MAX_NOTES_PER_MESSAGE = 100
MAX_VISIBLE_NOTES = 500
MAX_NOTE_JOURNAL_BYTES = 8 * 1024 * 1024


class NoteValidationError(ValueError):
    """Note input cannot be displayed within the viewer's resource limits."""


def check_note_count(count: int, *, per_message: bool = False) -> None:
    maximum = MAX_NOTES_PER_MESSAGE if per_message else MAX_VISIBLE_NOTES
    if count > maximum:
        message = (
            "One reply exceeds the display limit of {limit} Notes. Original records were not changed."
            if per_message else
            "This conversation exceeds the display limit of {limit} Notes. Original records were not changed."
        )
        raise NoteValidationError(message.format(limit=maximum))


def validate_note(note: dict) -> None:
    """Validate without normalizing, truncating or copying saved content."""
    if (not isinstance(note, dict) or len(note) != len(NOTE_FIELDS) + 1
            or not isinstance(note.get("entry_id"), str)
            or not 1 <= len(note["entry_id"]) <= 128
            or any(not isinstance(note.get(field), str) for field in NOTE_FIELDS)):
        raise NoteValidationError("Notes contain an invalid record. Original records were not changed.")
    if any(len(note[field]) > MAX_NOTE_FIELD_CHARS for field in NOTE_FIELDS):
        raise NoteValidationError(
            "A Note field exceeds the display limit of {limit} characters. Original records were not changed."
            .format(limit=MAX_NOTE_FIELD_CHARS))


def validate_notes(notes: list[dict]) -> None:
    """Reject a whole reply before copying data or allocating any widgets."""
    if not isinstance(notes, list):
        raise NoteValidationError("Notes contain an invalid record. Original records were not changed.")
    check_note_count(len(notes), per_message=True)
    identifiers = set()
    for note in notes:
        validate_note(note)
        if note["entry_id"] in identifiers:
            raise NoteValidationError("Notes contain an invalid record. Original records were not changed.")
        identifiers.add(note["entry_id"])
