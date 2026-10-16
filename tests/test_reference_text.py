"""Offline reference extraction against actual text, DOCX, and Qt PDF data."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from io import BytesIO
import unittest
from zipfile import ZipFile

from PySide6.QtWidgets import QApplication

from vasl_ai_bridge.reference_text import ReferenceTextError, extract_document, render_pdf_page
from vasl_ai_bridge.reference_formats import validate_reference_import


APP = QApplication.instance() or QApplication(["Reference tests"])


def pdf_fixture(*, searchable=True):
    streams = ([b"BT /F1 14 Tf 40 740 Td (Synthetic rule alpha) Tj ET", b"BT /F1 14 Tf 40 740 Td (Synthetic rule beta) Tj ET"]
               if searchable else [b"q 0 0 1 rg 40 740 12 12 re f Q", b"q 0 1 0 rg 40 740 12 12 re f Q"])
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ] + [b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream" for stream in streams]
    data = b"%PDF-1.4\n"
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(number).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    start = len(data)
    data += b"xref\n0 8\n0000000000 65535 f \n" + b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:])
    return data + b"trailer\n<< /Size 8 /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF\n"


class ReferenceTextTests(unittest.TestCase):
    def test_plain_text_and_unknown_file(self):
        self.assertEqual(extract_document("rule.md", b"A squad may move.")[0].text, "A squad may move.")
        with self.assertRaises(ReferenceTextError):
            extract_document("data.bin", b"\x00\x01")

    def test_docx_extracts_paragraphs(self):
        output = BytesIO()
        with ZipFile(output, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>First rule.</w:t></w:r></w:p><w:p><w:r><w:t>Second rule.</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(extract_document("rules.docx", output.getvalue())[0].text, "First rule.\nSecond rule.")

    def test_text_import_accepts_bom_encodings_and_rejects_binary_controls(self):
        for content in ("Synthetic 中文 rule".encode("utf-8-sig"), "Synthetic 中文 rule".encode("utf-16"), "Synthetic 中文 rule".encode("gb18030")):
            with self.subTest(content=content):
                self.assertIn("中文", extract_document("rule.txt", content)[0].text)
                validate_reference_import("rule.txt", content)
        for content in (b"\x00\x01\x02\x03", b"PK\x03\x04fake zip", b"word\x00binary"):
            with self.subTest(content=content), self.assertRaises(ReferenceTextError):
                validate_reference_import("renamed.txt", content)
        for suffix in ("ini", "yaml", "yml"):
            self.assertIn("rule", extract_document("notes." + suffix, b"rule: synthetic")[0].text)

    def test_import_accepts_scanned_pdf_but_rejects_invalid_documents(self):
        validate_reference_import("scan.pdf", pdf_fixture(searchable=False))
        with self.assertRaisesRegex(ReferenceTextError, "no searchable text"):
            extract_document("scan.pdf", pdf_fixture(searchable=False))
        for name, content in (("bad.pdf", b"not a pdf"), ("bad.docx", b"not a zip")):
            with self.subTest(name=name), self.assertRaises(ReferenceTextError):
                validate_reference_import(name, content)
        output = BytesIO()
        with ZipFile(output, "w") as archive:
            archive.writestr("word/document.xml", "<not-a-document/>")
        with self.assertRaises(ReferenceTextError):
            validate_reference_import("invalid-root.docx", output.getvalue())

    def test_pdf_text_page_numbers_and_image(self):
        content = pdf_fixture()
        pages = extract_document("rules.pdf", content)
        self.assertEqual([page.page for page in pages], [1, 2])
        self.assertIn("alpha", pages[0].text)
        self.assertIn("beta", pages[1].text)
        self.assertTrue(render_pdf_page(content, 2).startswith("data:image/png;base64,"))
        with self.assertRaises(ReferenceTextError):
            render_pdf_page(content, 3)


if __name__ == "__main__":
    unittest.main()
