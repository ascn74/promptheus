# 03 — Attachment text extraction

Turn uploaded files into plain text.

**Depends on:** nothing

## Files

- `src/promptheus/attachments.py`
- `tests/test_attachments.py`
- `tests/fixtures/` — small sample files

## Scope

```python
@dataclass(frozen=True)
class Attachment:
    filename: str
    text: str
    kind: Literal["text", "pdf", "docx"]
    warning: str | None = None
```

- `extract(filename: str, data: bytes) -> Attachment` — dispatches on
  extension, falling back to content sniffing.
- Plain text and source code: decode as UTF-8, falling back to latin-1, with
  invalid bytes replaced rather than raising.
- PDF: `pypdf`, page text joined with `\n\n`.
- DOCX: `python-docx` — paragraphs **and table cell text**, which is easy to
  forget and silently loses content.

## Design notes

**Everything becomes text, including for models that accept PDF natively.**
This is deliberate: 105 of the 345 models take PDFs directly, but if some
models read the original PDF and others read extracted text, they are not
answering the same question and the comparison is worthless.

**Scanned PDFs are the interesting failure.** A PDF with no text layer
extracts to an empty string. Silently sending that to nine models means paying
for nine answers about a file none of them saw. Detect it — extracted text
under a small threshold while the file has pages — and set `warning`. The UI
surfaces it before the run starts. OCR is explicitly not in scope.

**Encrypted PDFs** raise from `pypdf`; catch it and report as a warning rather
than a traceback.

**Cap the size** of any single extraction (`max_attachment_chars`, default
500,000) and note the truncation in `warning`. Without this, one large PDF
across nine models is a surprising bill.

## Acceptance criteria

- UTF-8 text, latin-1 text, a PDF with a text layer, and a DOCX containing a
  table all extract correctly.
- A PDF with no text layer returns empty text plus a warning, and does not
  raise.
- An encrypted PDF is reported as a warning, not an exception.
- Text past the cap is truncated and flagged.
- An unknown extension is treated as text rather than rejected.

## Tests

Generate fixtures programmatically where practical (`pypdf` can write a
minimal PDF, `python-docx` a minimal DOCX) so the repository does not carry
opaque binaries. Keep any committed fixture under a few KB.

## Out of scope

OCR, images (attachments are text-only in this iteration), and per-attachment
token counting (plan 04).
