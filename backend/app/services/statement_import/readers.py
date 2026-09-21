"""Turning uploaded bytes into rows or text, defensively.

A statement arrives as an untrusted upload, so every reader here caps what it
will expand before it expands it: a 40 KB spreadsheet that claims 8 GB of
uncompressed sheets is a zip bomb, not a bank statement, and a PDF with 20,000
pages is not one either. Nothing is written to disk — parsing happens in
memory, which is also why there is no temporary file to clean up afterwards.
"""
from __future__ import annotations

import csv
import io
import warnings
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.services.import_service import normalize_amount
from app.services.statement_import.models import (
    FORMAT_CSV,
    FORMAT_ENCRYPTED_OFFICE,
    FORMAT_HTML,
    FORMAT_PDF,
    FORMAT_UNKNOWN,
    FORMAT_XLS,
    FORMAT_XLSX,
)

#: Expansion guards. Generous for a real statement, tiny for an attack.
MAX_ZIP_ENTRIES = 512
MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_PDF_TEXT_CHARS = 4 * 1024 * 1024
#: See `_relax_pypdf_simple_font_limit`.
PYPDF_SIMPLE_FONT_WIDTH_LIMIT = 512
MAX_SHEET_ROWS = 20_000
MAX_SHEET_COLS = 64

#: OLE2 compound-file magic. Both a legacy .xls and an encrypted OOXML
#: container start with it, so the streams inside decide which one it is.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF-"


class StatementReadError(Exception):
    """Raised with a warning code the API can hand straight to the user."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def sniff_format(content: bytes) -> str:
    """Name a file's format from its bytes, never from its name.

    An uploaded name is attacker-controlled and, in practice, wrong often
    enough on its own: several Indian banks mail an HTML table saved as
    ``.xls``, and SBI's password-protected export keeps the ``.xlsx``
    extension while actually being an encrypted OLE2 container.
    """
    if content.startswith(_PDF_MAGIC):
        return FORMAT_PDF
    if content.startswith(_ZIP_MAGIC):
        return FORMAT_XLSX if _zip_looks_like_xlsx(content) else FORMAT_UNKNOWN
    if content.startswith(_OLE2_MAGIC):
        return FORMAT_ENCRYPTED_OFFICE if _ole2_is_encrypted(content) else FORMAT_XLS
    head = content[:4096].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html") or head.startswith(b"<table"):
        return FORMAT_HTML
    try:
        decode_text(content)
    except StatementReadError:
        return FORMAT_UNKNOWN
    return FORMAT_CSV


def _zip_looks_like_xlsx(content: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False
    return "xl/workbook.xml" in names or any(n.startswith("xl/worksheets/") for n in names)


def _ole2_is_encrypted(content: bytes) -> bool:
    """True when an OLE2 container holds an encrypted OOXML package.

    olefile ships with msoffcrypto-tool, so this costs no extra dependency.
    """
    try:
        import olefile
    except ImportError:  # pragma: no cover - dependency is pinned in uv.lock
        return b"EncryptedPackage" in content[:8192]
    try:
        ole = olefile.OleFileIO(io.BytesIO(content))
    except Exception:
        return False
    try:
        return ole.exists("EncryptedPackage") or ole.exists("EncryptionInfo")
    finally:
        ole.close()


def decode_text(content: bytes) -> str:
    """Decode a text upload, trying the encodings banks actually emit."""
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            text = content.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        # latin-1 never fails, so a file that is really binary would slip
        # through as mojibake. Control characters give it away.
        if "\x00" in text:
            continue
        return text
    raise StatementReadError("malformed_file", "File is not readable text")


def read_csv_rows(content: bytes) -> list[list[str]]:
    """Read a CSV into rows, without assuming the header is on line 1.

    Bank CSVs open with a block of account details before the table starts, so
    this returns every line and lets the provider parser find its own header.
    """
    text = decode_text(content)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows: list[list[str]] = []
    for row in csv.reader(io.StringIO(text), dialect):
        rows.append([("" if cell is None else str(cell)) for cell in row])
        if len(rows) >= MAX_SHEET_ROWS:
            break
    return rows


def _guard_zip(content: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise StatementReadError("malformed_file", "Workbook could not be opened") from exc
    if len(infos) > MAX_ZIP_ENTRIES:
        raise StatementReadError("malformed_file", "Workbook has too many parts")
    if sum(i.file_size for i in infos) > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise StatementReadError("malformed_file", "Workbook expands to too much data")


def read_xlsx_rows(content: bytes, password: Optional[str] = None) -> list[list[str]]:
    """Read the first worksheet of an .xlsx as rows of strings."""
    if content.startswith(_OLE2_MAGIC):
        content = decrypt_office(content, password)
    _guard_zip(content)
    import openpyxl

    with warnings.catch_warnings():
        # openpyxl warns about parts it chooses to ignore (print settings,
        # unsupported extensions, a missing default style). None of that
        # affects cell values, and CI runs pytest with -W error.
        warnings.simplefilter("ignore", UserWarning)
        try:
            workbook = openpyxl.load_workbook(
                io.BytesIO(content), read_only=True, data_only=True
            )
        except Exception as exc:
            raise StatementReadError("malformed_file", "Workbook could not be read") from exc
        try:
            sheet = workbook[workbook.sheetnames[0]]
            rows: list[list[str]] = []
            for row in sheet.iter_rows(values_only=True):
                rows.append([_cell_to_text(v) for v in row[:MAX_SHEET_COLS]])
                if len(rows) >= MAX_SHEET_ROWS:
                    break
            return rows
        finally:
            workbook.close()


def read_xls_rows(content: bytes) -> list[list[str]]:
    """Read the first sheet of a legacy BIFF .xls as rows of strings."""
    import xlrd

    try:
        book = xlrd.open_workbook(file_contents=content)
    except Exception as exc:
        raise StatementReadError("malformed_file", "Workbook could not be read") from exc
    sheet = book.sheet_by_index(0)
    rows: list[list[str]] = []
    for index in range(min(sheet.nrows, MAX_SHEET_ROWS)):
        cells = sheet.row(index)[:MAX_SHEET_COLS]
        rows.append([_xls_cell_to_text(c, book.datemode) for c in cells])
    return rows


def _xls_cell_to_text(cell, datemode: int) -> str:
    import xlrd

    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            parts = xlrd.xldate_as_tuple(cell.value, datemode)
        except Exception:
            return ""
        return date(parts[0], parts[1], parts[2]).isoformat()
    return _cell_to_text(cell.value)


def _cell_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        # Excel stores every number as a float, so an amount typed as 2500.00
        # arrives as 2500.0 and a count as 13.0. Render whole numbers without
        # the trailing ".0" so downstream matching sees what the bank printed.
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def decrypt_office(content: bytes, password: Optional[str]) -> bytes:
    """Unwrap a password-protected OOXML container into a plain .xlsx.

    The password is used here and nowhere else: it is never persisted, never
    written to a log and never returned in a response.
    """
    if not password:
        raise StatementReadError("password_required", "This statement is password-protected")
    import msoffcrypto

    try:
        office_file = msoffcrypto.OfficeFile(io.BytesIO(content))
        office_file.load_key(password=password)
        decrypted = io.BytesIO()
        office_file.decrypt(decrypted)
    except Exception as exc:
        raise StatementReadError("wrong_password", "The password did not open this file") from exc
    return decrypted.getvalue()


def _relax_pypdf_simple_font_limit() -> None:
    """Let a simple font declare one width more than the PDF spec allows.

    pypdf caps a simple font's /Widths array at 256 entries, which is what the
    specification permits (codes 0-255). Axis Bank's statement generator emits
    257, and pypdf then refuses the file outright — no text at all, in any
    extraction mode. The array is a producer's off-by-one, not an attack, and
    the cap exists to stop a crafted file allocating unbounded memory, so this
    raises it to another small bound rather than removing it: 512 float widths
    cost nothing, and pypdf's own general-purpose ceiling is 100,000.

    Applied once, on import, so there is no global being mutated around a
    parse. Guarded because the constant is pypdf-internal: if a future release
    renames it, statements parse exactly as they do today and a file like this
    one is reported as unreadable instead of crashing.
    """
    try:
        from pypdf import _font
    except ImportError:  # pragma: no cover - pypdf is pinned in uv.lock
        return
    limit = getattr(_font, "MAX_SIMPLE_FONT_WIDTH_ENTRY_COUNT", None)
    if isinstance(limit, int) and limit < PYPDF_SIMPLE_FONT_WIDTH_LIMIT:
        setattr(_font, "MAX_SIMPLE_FONT_WIDTH_ENTRY_COUNT", PYPDF_SIMPLE_FONT_WIDTH_LIMIT)


def read_pdf_pages(content: bytes, password: Optional[str] = None) -> list[str]:
    """Extract each page's text with pypdf's layout mode.

    Layout mode keeps horizontal position, which is the whole reason a bank's
    table survives extraction as a table: columns stay separated by runs of
    spaces that the provider parsers split on. Nothing is sent anywhere — this
    is local, deterministic text extraction, not document understanding.
    """
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    _relax_pypdf_simple_font_limit()
    try:
        reader = PdfReader(io.BytesIO(content))
    except (PdfReadError, OSError, ValueError) as exc:
        raise StatementReadError("malformed_file", "PDF could not be opened") from exc

    if reader.is_encrypted:
        # Bank PDFs are routinely encrypted with an empty owner password,
        # which opens without the user supplying anything.
        opened = False
        for candidate in ("", password or ""):
            try:
                if reader.decrypt(candidate):
                    opened = True
                    break
            except Exception:
                continue
        if not opened:
            raise StatementReadError(
                "password_required" if not password else "wrong_password",
                "This PDF is password-protected",
            )

    if len(reader.pages) > MAX_PDF_PAGES:
        raise StatementReadError("malformed_file", "PDF has too many pages")

    pages: list[str] = []
    total = 0
    with warnings.catch_warnings():
        # pypdf warns about malformed-but-recoverable structures in files
        # produced by older bank templates; CI runs pytest with -W error.
        warnings.simplefilter("ignore")
        for page in reader.pages:
            try:
                text = page.extract_text(extraction_mode="layout") or ""
            except Exception:
                text = ""
            total += len(text)
            if total > MAX_PDF_TEXT_CHARS:
                raise StatementReadError("malformed_file", "PDF holds too much text")
            pages.append(text)
    return pages


#: Suffixes Indian statements hang off a balance or an amount.
_CREDIT_SUFFIXES = ("cr", "credit")
_DEBIT_SUFFIXES = ("dr", "db", "debit")


def parse_money(raw: str | None) -> Optional[Decimal]:
    """Parse a statement's money string, or None when there is no number.

    Handles what the four supported providers print: Indian lakh grouping
    ("1,09,654.52"), a currency symbol or code, a trailing Cr/Dr marker, and
    accounting parentheses. The digit grouping itself is handed to
    `import_service.normalize_amount`, which the CSV importer already uses.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    for token in ("₹", "INR", "Rs.", "Rs", "₹"):
        text = text.replace(token, "")
    text = text.strip()

    negative = False
    lowered = text.lower()
    for suffix in _DEBIT_SUFFIXES:
        if lowered.endswith(suffix):
            text, negative = text[: -len(suffix)].strip(), True
            break
    else:
        for suffix in _CREDIT_SUFFIXES:
            if lowered.endswith(suffix):
                text = text[: -len(suffix)].strip()
                break

    if text.startswith("(") and text.endswith(")"):
        text, negative = text[1:-1].strip(), True
    if text.startswith("+"):
        text = text[1:].strip()
    if text.startswith("-"):
        text, negative = text[1:].strip(), not negative

    text = normalize_amount(text).strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return -value if negative else value
