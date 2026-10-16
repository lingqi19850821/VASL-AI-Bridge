"""Local reference extraction and page images using the installed Qt runtime.

Original files stay in the game archive. These derived pages are navigation
and retrieval aids, not authoritative replacements for the source document.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePath
import re
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
from zlib import error as ZipDecompressionError

from .reference_formats import ReferenceTextError, TEXT_REFERENCE_SUFFIXES


@dataclass(frozen=True, slots=True)
class TextPage:
    page: int
    text: str


MAX_TEXT_BYTES = 32 * 1024 * 1024
MAX_PDF_PAGES = 3000


def _pdf(content: bytes):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtPdf import QPdfDocument

    buffer = QBuffer()
    buffer.setData(QByteArray(content))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        raise ReferenceTextError("The PDF could not be opened locally.")
    document = QPdfDocument()
    document.load(buffer)
    if document.status() != QPdfDocument.Status.Ready:
        document.close()
        buffer.close()
        raise ReferenceTextError("The PDF is unreadable or requires a password.")
    if not 1 <= document.pageCount() <= MAX_PDF_PAGES:
        document.close()
        buffer.close()
        raise ReferenceTextError("The PDF has no pages or exceeds the supported page count.")
    return document, buffer


def extract_document(name: str, content: bytes) -> list[TextPage]:
    """Read plain text, DOCX, or PDF without uploading the original file."""
    suffix = PurePath(name).suffix.lower()
    if suffix == ".pdf":
        document, buffer = _pdf(content)
        try:
            pages = []
            size = 0
            for index in range(document.pageCount()):
                text = document.getAllText(index).text()
                size += len(text.encode("utf-8"))
                if size > MAX_TEXT_BYTES:
                    raise ReferenceTextError("The extracted text exceeds the local indexing limit.")
                pages.append(TextPage(index + 1, text))
            if not any(page.text.strip() for page in pages):
                raise ReferenceTextError("This PDF has no searchable text. The AI can request a page image; OCR is not performed.")
            return pages
        finally:
            document.close()
            buffer.close()
    if suffix == ".docx":
        try:
            with ZipFile(BytesIO(content)) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > MAX_TEXT_BYTES:
                    raise ReferenceTextError("The document text exceeds the local indexing limit.")
                root = ElementTree.fromstring(archive.read(info))
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            if root.tag != namespace + "document":
                raise ReferenceTextError("The DOCX document XML is invalid.")
            paragraphs = ["".join(node.itertext()) for node in root.iter(namespace + "p")]
            return [TextPage(1, "\n".join(paragraphs))]
        except (BadZipFile, ZipDecompressionError, KeyError, ElementTree.ParseError, OSError, RuntimeError, NotImplementedError) as exc:
            raise ReferenceTextError("The DOCX file could not be read locally.") from exc
    if suffix not in TEXT_REFERENCE_SUFFIXES:
        raise ReferenceTextError("This file is preserved locally, but its format has no text reader.")
    if len(content) > MAX_TEXT_BYTES:
        raise ReferenceTextError("The text file exceeds the local indexing limit.")
    encodings = ("utf-16",) if content.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            text = content.decode(encoding)
            if any((ord(character) < 32 and character not in "\t\n\r\f")
                   or 127 <= ord(character) <= 159 for character in text):
                continue
            if suffix in {".html", ".htm"}:
                text = re.sub(r"<[^>]*>", " ", text)
            return [TextPage(1, text)]
        except UnicodeError:
            continue
    raise ReferenceTextError("The file's text encoding is not supported.")


def indexed_text(name: str, content: bytes) -> str:
    pages = extract_document(name, content)
    return "\n\n".join(f"[Page {page.page}]\n{page.text}" for page in pages)


def render_pdf_page(content: bytes, page: int) -> str:
    """Return a bounded PNG data URL for a requested one-based PDF page."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize

    if type(page) is not int or page < 1:
        raise ReferenceTextError("Specify a page number starting at 1.")
    document, buffer = _pdf(content)
    try:
        if page > document.pageCount():
            raise ReferenceTextError("The requested page is outside this PDF.")
        points = document.pagePointSize(page - 1)
        scale = 1800 / max(points.width(), points.height(), 1)
        size = QSize(max(1, round(points.width() * scale)), max(1, round(points.height() * scale)))
        image = document.render(page - 1, size)
        output = QByteArray()
        target = QBuffer(output)
        if image.isNull() or not target.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(target, "PNG"):
            raise ReferenceTextError("The PDF page image could not be rendered.")
        target.close()
        return "data:image/png;base64," + base64.b64encode(bytes(output)).decode("ascii")
    finally:
        document.close()
        buffer.close()
