"""Turn uploaded files into plain text.

Every attachment is reduced to text, including for the models that accept PDFs
natively. If some models read the original document and others read extracted
text, they are not answering the same question and the comparison is worthless.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePath
from typing import Literal

from docx import Document
from docx.document import Document as DocxDocument
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pypdf._encryption import PasswordType
from pypdf.errors import PyPdfError

Kind = Literal["text", "pdf", "docx"]

DEFAULT_MAX_CHARS = 500_000

# A PDF with pages but essentially no extractable characters is a scan.
_MIN_MEANINGFUL_CHARS = 16

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_DOCX_MARKER = "word/document.xml"

_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


@dataclass(frozen=True)
class Attachment:
    """One uploaded file, reduced to text."""

    filename: str
    text: str
    kind: Kind
    warning: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def extract(filename: str, data: bytes, max_chars: int = DEFAULT_MAX_CHARS) -> Attachment:
    """Extract text from an uploaded file.

    Never raises for bad input: a file we cannot read becomes empty text plus a
    warning, so one broken upload does not take down a run across N models.
    """
    kind = detect_kind(filename, data)

    if kind == "pdf":
        text, warnings = _extract_pdf(data)
    elif kind == "docx":
        text, warnings = _extract_docx(data)
    else:
        text, warnings = _extract_text(data)

    if len(text) > max_chars:
        warnings = [*warnings, f"truncated to {max_chars:,} of {len(text):,} characters"]
        text = text[:max_chars]

    return Attachment(
        filename=filename,
        text=text,
        kind=kind,
        warning="; ".join(warnings) if warnings else None,
    )


def detect_kind(filename: str, data: bytes) -> Kind:
    """Dispatch on the extension, falling back to the file's own bytes.

    Browsers report whatever the filesystem says, so an extension can be wrong
    or absent; the magic bytes are the more reliable signal when it is.
    """
    suffix = PurePath(filename).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"

    if data.startswith(_PDF_MAGIC):
        return "pdf"
    if data.startswith(_ZIP_MAGIC) and _is_docx_archive(data):
        return "docx"

    # Anything else is treated as text rather than rejected: source files come
    # with hundreds of different extensions and none of them are special.
    return "text"


def _is_docx_archive(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return _DOCX_MARKER in archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def _extract_text(data: bytes) -> tuple[str, list[str]]:
    if data.startswith(_UTF16_BOMS):
        # Decoded as latin-1 this would come out as text interleaved with null
        # bytes — garbage that would still look plausible enough to send.
        try:
            return data.decode("utf-16"), []
        except UnicodeDecodeError:
            pass

    try:
        return data.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        # latin-1 maps every byte, so this cannot fail. It can be wrong, hence
        # the warning.
        return data.decode("latin-1"), ["not valid UTF-8; decoded as latin-1"]


def _extract_pdf(data: bytes) -> tuple[str, list[str]]:
    try:
        reader = PdfReader(io.BytesIO(data))
    except PyPdfError as error:
        return "", [f"could not read PDF: {error}"]

    if reader.is_encrypted:
        try:
            # Many PDFs are encrypted with an empty user password purely to set
            # permissions; those open fine and should not be reported.
            if reader.decrypt("") == PasswordType.NOT_DECRYPTED:
                return "", ["PDF is password-protected; no text extracted"]
        except (PyPdfError, NotImplementedError) as error:
            return "", [f"PDF is encrypted and could not be opened: {error}"]

    try:
        page_texts = [page.extract_text() or "" for page in reader.pages]
    except PyPdfError as error:
        return "", [f"could not extract text from PDF: {error}"]

    text = "\n\n".join(
        stripped for page in page_texts if (stripped := _normalise_pdf_text(page).strip())
    )

    warnings: list[str] = []
    if page_texts and len(text.strip()) < _MIN_MEANINGFUL_CHARS:
        # Sending this to N models means paying N times for an answer about a
        # document none of them can see.
        warnings.append(
            f"no text layer in {len(page_texts)} page(s) — looks like a scanned "
            f"PDF, and OCR is not supported"
        )

    return text, warnings


def _normalise_pdf_text(page: str) -> str:
    """Drop the padding PDF extraction leaves behind.

    Print pipelines pad every line out to a fixed column width, so a page of
    prose arrives with hundreds of trailing spaces. They carry no meaning, they
    tokenise, and the cost is multiplied by every model in the run. Leading
    whitespace is kept, since it may be real indentation.
    """
    lines = [line.rstrip() for line in page.splitlines()]

    normalised: list[str] = []
    blank_run = 0
    for line in lines:
        if line:
            blank_run = 0
            normalised.append(line)
            continue
        blank_run += 1
        # One blank line separates paragraphs; more is layout, not content.
        if blank_run <= 1:
            normalised.append(line)

    return "\n".join(normalised)


def _extract_docx(data: bytes) -> tuple[str, list[str]]:
    try:
        document = Document(io.BytesIO(data))
    except (PackageNotFoundError, ValueError, KeyError, zipfile.BadZipFile) as error:
        return "", [f"could not read DOCX: {error}"]

    parts: list[str] = []
    for block in _iter_blocks(document):
        if isinstance(block, Paragraph):
            if block.text.strip():
                parts.append(block.text)
        else:
            table = _table_to_text(block)
            if table:
                parts.append(table)

    return "\n\n".join(parts), []


def _iter_blocks(document: DocxDocument) -> Iterator[Paragraph | Table]:
    """Walk paragraphs and tables in document order.

    `document.paragraphs` silently omits everything inside tables, which is an
    easy way to lose half a document without noticing.
    """
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _table_to_text(table: Table) -> str:
    rows: list[str] = []
    for row in table.rows:
        # Merged cells repeat in `row.cells`; collapse the runs so the text is
        # not duplicated across the span.
        cells: list[str] = []
        for cell in row.cells:
            value = cell.text.strip()
            if not cells or cells[-1] != value:
                cells.append(value)
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)
