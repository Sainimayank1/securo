"""The contract every provider adapter implements.

Adding a bank means adding one module next to this one and one line in
`registry`. No core file grows a branch, which is the point: the detection,
normalization, validation and import steps never learn a provider's name.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from functools import cached_property
from typing import Optional, Protocol, runtime_checkable

from app.services.statement_import import readers
from app.services.statement_import.models import (
    FORMAT_CSV,
    FORMAT_PDF,
    FORMAT_XLS,
    FORMAT_XLSX,
    ParsedStatement,
)

#: Securo's `Transaction.description` column. Longer text is cut, with a
#: per-row warning, rather than failing the whole import at the database.
MAX_DESCRIPTION = 500
#: `Transaction.notes`.
MAX_NOTES = 1000


class StatementSource:
    """One upload, read at most once per representation.

    Parsers are asked to sniff the same file in turn, so each decode is cached
    here instead of being repeated per candidate.
    """

    def __init__(self, filename: str, content: bytes, file_format: str, password: Optional[str] = None):
        #: Kept only so a parser can break a tie; never trusted for detection
        #: and never written to a log.
        self.filename = filename or ""
        self.content = content
        self.file_format = file_format
        self.password = password

    @cached_property
    def rows(self) -> list[list[str]]:
        """Spreadsheet or CSV cells, as written."""
        if self.file_format == FORMAT_XLSX:
            return readers.read_xlsx_rows(self.content, self.password)
        if self.file_format == FORMAT_XLS:
            return readers.read_xls_rows(self.content)
        if self.file_format == FORMAT_CSV:
            return readers.read_csv_rows(self.content)
        return []

    @cached_property
    def text(self) -> str:
        """The whole file as text: raw for CSV, extracted for PDF."""
        if self.file_format == FORMAT_PDF:
            return "\n".join(self.pages)
        if self.file_format == FORMAT_CSV:
            return readers.decode_text(self.content)
        return "\n".join("\t".join(row) for row in self.rows)

    @cached_property
    def pages(self) -> list[str]:
        if self.file_format != FORMAT_PDF:
            return []
        return readers.read_pdf_pages(self.content, self.password)

    @cached_property
    def haystack(self) -> str:
        """Lower-cased text for detection signals. Never shown to anyone."""
        return self.text.lower()


@runtime_checkable
class StatementParser(Protocol):
    """A provider adapter."""

    provider: str
    statement_type: str
    #: Formats this adapter will even look at.
    file_formats: tuple[str, ...]

    def sniff(self, source: StatementSource) -> float:
        """Confidence in 0..1 that this adapter owns the file."""
        ...

    def parse(self, source: StatementSource) -> ParsedStatement:
        """Read the statement. Raises `readers.StatementReadError` on refusal."""
        ...


def signal_score(haystack: str, signals: tuple[str, ...]) -> float:
    """Fraction of a provider's marker phrases present in the file."""
    if not signals:
        return 0.0
    hits = sum(1 for signal in signals if signal.lower() in haystack)
    return hits / len(signals)


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_LONG_DATE_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})$")

#: Day-first everywhere here: all four supported providers are Indian, and
#: none of them emits a month-first date. A format that cannot be told apart
#: is better refused than guessed on a financial record.
_NUMERIC_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y")


def parse_statement_date(raw: str | None) -> Optional[date]:
    """Parse the date formats the supported statements print.

    Month names are matched against an explicit table rather than `%B`, whose
    meaning depends on the server's LC_TIME.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None

    match = _LONG_DATE_RE.match(text)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            try:
                return date(int(match.group(3)), month, int(match.group(1)))
            except ValueError:
                return None

    for fmt in _NUMERIC_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def clean_description(raw: str) -> str:
    """Collapse a statement's layout whitespace without losing its words."""
    return re.sub(r"\s+", " ", (raw or "").replace("\xa0", " ")).strip()


def dewrap_cell(raw: str) -> str:
    """Rejoin a description a bank hard-wrapped inside one cell.

    SBI wraps the narration at a fixed width and indents every line it
    produced by a single space, so the break falls mid-token: "…/HDFC/z" then
    " erodha.ic/Merc". Joining on a space would invent one — "z erodha.ic" —
    and joining blindly would fuse genuinely separate lines. So a continuation
    that carries the tell-tale indent is stitched back with nothing between,
    and one that does not is treated as its own segment.
    """
    lines = (raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) == 1:
        return clean_description(lines[0])
    out = lines[0].lstrip(" ")
    for line in lines[1:]:
        if line.startswith(" "):
            out += line[1:]
        else:
            out += " " + line
    return clean_description(out)


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Cut `text` to `limit`, reporting whether anything was lost."""
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


def last4(identifier: str | None) -> Optional[str]:
    """The last four characters of an account or card number.

    The full identifier never leaves the parser: this is all that is put in a
    response, and masking characters are dropped so "653046******3649" and
    "XXXXXX3649" both reduce to "3649".
    """
    if not identifier:
        return None
    compact = re.sub(r"[^0-9A-Za-z]", "", str(identifier))
    tail = compact[-4:]
    # Four digits, or nothing. A tail like "XX20" is a masking stub the
    # provider printed, and treating it as an identifier would let an account
    # be "matched" on characters that identify nothing.
    return tail if len(tail) == 4 and tail.isdigit() else None
