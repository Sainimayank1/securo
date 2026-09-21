"""Which adapter owns a file.

Every adapter is asked to sniff the same source and the most confident one
wins, so adding a bank is adding a module and one entry to `PARSERS` — no
branch anywhere else learns its name. Ties go to whichever adapter appears
first here, which is why the generic CSV reader sits last and scores low: a
provider adapter that recognises a file is always the better answer, because
it also knows the file's period, balances and account.
"""
from __future__ import annotations

from typing import Optional

from app.services.statement_import.parsers.axis_csv import AxisCsvParser
from app.services.statement_import.parsers.axis_myzone_pdf import AxisMyZonePdfParser
from app.services.statement_import.parsers.base import StatementParser, StatementSource
from app.services.statement_import.parsers.generic_csv import GenericCsvParser
from app.services.statement_import.parsers.sbi_xlsx import SbiXlsxParser
from app.services.statement_import.parsers.supermoney_pdf import SuperMoneyPdfParser

#: A file has to look at least this much like a provider's layout before its
#: adapter is trusted with it. Below this nothing is imported and the user is
#: told the format was not recognised, which is the right outcome for a
#: financial record we cannot read confidently.
MIN_CONFIDENCE = 0.5

PARSERS: tuple[StatementParser, ...] = (
    SbiXlsxParser(),
    AxisCsvParser(),
    AxisMyZonePdfParser(),
    SuperMoneyPdfParser(),
    GenericCsvParser(),
)


def select_parser(source: StatementSource) -> Optional[StatementParser]:
    """The adapter most confident about this file, or None."""
    best: Optional[StatementParser] = None
    best_score = 0.0
    for parser in PARSERS:
        if source.file_format not in parser.file_formats:
            continue
        score = parser.sniff(source)
        if score > best_score:
            best, best_score = parser, score
    return best if best_score >= MIN_CONFIDENCE else None


def supported_providers() -> list[dict[str, object]]:
    """What the UI can advertise as importable."""
    return [
        {
            "provider": parser.provider,
            "statement_type": parser.statement_type,
            "file_formats": list(parser.file_formats),
        }
        for parser in PARSERS
    ]
