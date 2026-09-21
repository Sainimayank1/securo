"""Working out what an upload is, before anything tries to read it as money.

Two questions, answered in this order and both from the file's bytes:
what kind of file is this, and whose statement is it. The uploaded name is
never the answer to either — it is attacker-controlled, and it is wrong often
enough in ordinary use to be no help: SBI's password-protected export keeps a
.xlsx name while being an encrypted OLE2 container, and several banks mail an
HTML table saved as .xls.
"""
from __future__ import annotations

from typing import Optional

from app.services.statement_import import readers, registry
from app.services.statement_import.models import (
    FORMAT_CSV,
    FORMAT_ENCRYPTED_OFFICE,
    FORMAT_HTML,
    FORMAT_PDF,
    FORMAT_UNKNOWN,
    FORMAT_XLS,
    FORMAT_XLSX,
    STATEMENT_TYPE_UNKNOWN,
    WARN_MALFORMED_FILE,
    WARN_OCR_REQUIRED,
    WARN_PASSWORD_REQUIRED,
    WARN_UNKNOWN_PROVIDER,
    WARN_UNSUPPORTED_FORMAT,
    Detection,
)
from app.services.statement_import.parsers.base import StatementSource

#: Formats an adapter can actually read.
READABLE_FORMATS = (FORMAT_CSV, FORMAT_XLSX, FORMAT_XLS, FORMAT_PDF)


def _unsupported(file_format: str, reason: str) -> Detection:
    return Detection(
        file_format=file_format,
        provider="unknown",
        statement_type=STATEMENT_TYPE_UNKNOWN,
        confidence=0.0,
        supported=False,
        reason=reason,
    )


def detect(filename: str, content: bytes, password: Optional[str] = None) -> tuple[Detection, Optional[StatementSource]]:
    """Name the format and the provider.

    Returns the verdict and, when the file is readable, the source the chosen
    adapter should parse — built once here so the file is decoded a single
    time rather than once per candidate adapter.
    """
    file_format = readers.sniff_format(content)

    if file_format == FORMAT_ENCRYPTED_OFFICE and not password:
        return _unsupported(FORMAT_XLSX, WARN_PASSWORD_REQUIRED), None
    if file_format == FORMAT_ENCRYPTED_OFFICE:
        # A password was supplied: unwrap now so the rest of the pipeline sees
        # an ordinary workbook. A wrong password surfaces as its own code.
        content = readers.decrypt_office(content, password)
        file_format = readers.sniff_format(content)
        if file_format != FORMAT_XLSX:
            return _unsupported(FORMAT_XLSX, WARN_MALFORMED_FILE), None
    if file_format == FORMAT_HTML:
        return _unsupported(FORMAT_HTML, WARN_UNSUPPORTED_FORMAT), None
    if file_format not in READABLE_FORMATS:
        return _unsupported(FORMAT_UNKNOWN, WARN_UNSUPPORTED_FORMAT), None

    source = StatementSource(filename, content, file_format, password)

    # Touch the file's content once, here, so an unreadable upload is reported
    # as a detection failure rather than blowing up mid-parse.
    try:
        has_text = bool(source.haystack.strip())
    except readers.StatementReadError as exc:
        return _unsupported(file_format, exc.code), None

    if not has_text:
        # A PDF that yields nothing is a scan. Securo will not ship a cloud OCR
        # dependency for a bank statement, so this is reported, not guessed at.
        return _unsupported(
            file_format, WARN_OCR_REQUIRED if file_format == FORMAT_PDF else WARN_MALFORMED_FILE
        ), None

    parser = registry.select_parser(source)
    if parser is None:
        return _unsupported(file_format, WARN_UNKNOWN_PROVIDER), source

    return (
        Detection(
            file_format=file_format,
            provider=parser.provider,
            statement_type=parser.statement_type,
            confidence=parser.sniff(source),
            supported=True,
        ),
        source,
    )
