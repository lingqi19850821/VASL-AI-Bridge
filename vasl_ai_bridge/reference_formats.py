"""Supported imports and bounded image readers for private local references.

Import validation applies only to newly selected files. Saved legacy originals
remain readable as archive data, even when they have no supported model reader.
"""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import PurePath

from PIL import Image, ImageOps, UnidentifiedImageError


class ReferenceTextError(ValueError):
    """A reference cannot be read locally in its current format."""


TEXT_REFERENCE_SUFFIXES = frozenset({
    ".txt", ".md", ".csv", ".tsv", ".json", ".log", ".html", ".htm",
    ".rst", ".xml", ".ini", ".yaml", ".yml",
})
IMAGE_REFERENCE_FORMATS = {
    ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".bmp": "BMP",
}
IMAGE_REFERENCE_SUFFIXES = frozenset(IMAGE_REFERENCE_FORMATS)
SUPPORTED_REFERENCE_SUFFIXES = TEXT_REFERENCE_SUFFIXES | IMAGE_REFERENCE_SUFFIXES | {".pdf", ".docx"}
REFERENCE_FILE_FILTER = (
    "Supported references (" + " ".join("*" + suffix for suffix in sorted(SUPPORTED_REFERENCE_SUFFIXES)) + ");;"
    "Documents (*.pdf *.docx);;"
    "Text files (" + " ".join("*" + suffix for suffix in sorted(TEXT_REFERENCE_SUFFIXES)) + ");;"
    "Images (*.png *.jpg *.jpeg *.webp *.bmp)"
)
MAX_REFERENCE_BYTES = 64 * 1024 * 1024
MAX_REFERENCE_IMAGE_BYTES = 24 * 1024 * 1024
MAX_REFERENCE_IMAGE_PIXELS = 32_000_000
MAX_REFERENCE_IMAGE_DIMENSION = 20_000


def is_supported_reference(name: str) -> bool:
    return PurePath(name).suffix.lower() in SUPPORTED_REFERENCE_SUFFIXES


def reference_capabilities(name: str) -> dict[str, bool | str]:
    """Describe available readers; a tool still validates its requested bytes."""
    suffix = PurePath(name).suffix.lower()
    image = suffix in IMAGE_REFERENCE_SUFFIXES
    pdf = suffix == ".pdf"
    text = suffix in TEXT_REFERENCE_SUFFIXES or suffix in {".docx", ".pdf"}
    return {
        "text_supported": text,
        "image_available": image,
        "page_images_available": pdf,
        "reader": "read_reference_image" if image else "read_reference_file" if text else "",
        "text_reader": "read_reference_file" if text else "",
        "image_reader": "read_reference_image" if image or pdf else "",
    }


def _open_reference_image(name: str, content: bytes) -> Image.Image:
    """Fully decode a static supported image before returning it to a reader."""
    if PurePath(name).suffix.lower() not in IMAGE_REFERENCE_SUFFIXES:
        raise ReferenceTextError("Image reading supports PNG, JPEG, WebP, and BMP references only.")
    if len(content) > MAX_REFERENCE_IMAGE_BYTES:
        raise ReferenceTextError("The reference image exceeds the 24 MiB image limit.")
    try:
        with Image.open(BytesIO(content)) as checked:
            if checked.format not in IMAGE_REFERENCE_FORMATS.values():
                raise ReferenceTextError("The reference image contains an unsupported format. Use PNG, JPEG, WebP, or BMP.")
            width, height = checked.size
            if (width < 1 or height < 1 or max(width, height) > MAX_REFERENCE_IMAGE_DIMENSION
                    or width * height > MAX_REFERENCE_IMAGE_PIXELS):
                raise ReferenceTextError("The reference image dimensions exceed the local image limit.")
            if getattr(checked, "n_frames", 1) != 1:
                raise ReferenceTextError("Animated images are unsupported; import a single still image.")
            checked.verify()
        with Image.open(BytesIO(content)) as image:
            image.load()
            # Retain the detected format and EXIF data after detaching the buffer.
            decoded = image.copy()
            decoded.format = image.format
            return decoded
    except ReferenceTextError:
        raise
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ReferenceTextError("The reference image is invalid or cannot be decoded locally.") from exc


def validate_reference_import(name: str, content: bytes) -> None:
    """Reject unsupported or unreadable new imports before persistent writes."""
    if not is_supported_reference(name):
        raise ReferenceTextError("Unsupported reference format. Import PDF, DOCX, text, PNG, JPEG, WebP, or BMP files.")
    if len(content) > MAX_REFERENCE_BYTES:
        raise ReferenceTextError("The reference exceeds the 64 MiB import limit.")
    suffix = PurePath(name).suffix.lower()
    if suffix in IMAGE_REFERENCE_SUFFIXES:
        # A compressed source can decode correctly yet exceed the transport
        # limit after PNG conversion, even when its filename ends in .png.
        render_reference_image(name, content)
        return
    from .reference_text import _pdf, extract_document
    if suffix == ".pdf":
        document, buffer = _pdf(content)
        document.close()
        buffer.close()
        # Scanned PDFs are supported visually even when text extraction is empty.
        return
    extract_document(name, content)


def render_reference_image(name: str, content: bytes) -> str:
    """Return exact PNG originals or an oriented PNG derived from another image."""
    with _open_reference_image(name, content) as image:
        if image.format == "PNG":
            png = content
        else:
            oriented = ImageOps.exif_transpose(image)
            try:
                if oriented.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                    converted = oriented.convert("RGB")
                    oriented.close()
                    oriented = converted
                output = BytesIO()
                oriented.save(output, format="PNG")
                png = output.getvalue()
            finally:
                oriented.close()
        if len(png) > MAX_REFERENCE_IMAGE_BYTES:
            raise ReferenceTextError("The rendered reference image exceeds the 24 MiB image limit.")
        return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
