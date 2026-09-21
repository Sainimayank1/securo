"""SuperMoney transaction-history export (PDF).

    Transaction History
    1 September 2026 to 4 September 2026

    Name              Bank             Amount     Date               Status
    GAGANBHUTANI      Utkarsh XX20     -27.00     3 September 2026   SUCCESS

Amounts are already signed, so no direction has to be inferred. Only SUCCESS
rows are imported; anything pending, failed or reversed is left out and
reported, because a payment that has not settled is not a transaction on the
account yet.

SuperMoney is an aggregator, so its Bank column names the funding account per
*row*. A file whose rows draw on more than one account is flagged: the import
lands in a single Securo account, and picking one for a mixed file would put
someone else's transactions in it.
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.services.statement_import.models import (
    FORMAT_PDF,
    STATEMENT_TYPE_WALLET,
    WARN_EMPTY_STATEMENT,
    WARN_MIXED_SOURCE_ACCOUNTS,
    WARN_NON_SUCCESS_SKIPPED,
    Detection,
    NormalizedTransaction,
    ParsedStatement,
    StatementAccountHint,
    StatementPeriod,
)
from app.services.statement_import.parsers.base import (
    MAX_DESCRIPTION,
    StatementSource,
    clean_description,
    last4,
    parse_statement_date,
    truncate,
)
from app.services.statement_import.readers import parse_money

PROVIDER = "supermoney"

_SIGNALS = ("transaction history", "status", "amount", "bank")
_HEADER_RE = re.compile(r"\bName\b.*\bBank\b.*\bAmount\b.*\bDate\b.*\bStatus\b", re.IGNORECASE)
_PERIOD_RE = re.compile(
    r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s+to\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})", re.IGNORECASE
)
_AMOUNT_RE = re.compile(r"^[+-]?[\d,]+(?:\.\d{1,2})?$")
_SUCCESS = "success"


class SuperMoneyPdfParser:
    provider: str = PROVIDER
    statement_type: str = STATEMENT_TYPE_WALLET
    file_formats: tuple[str, ...] = (FORMAT_PDF,)

    def sniff(self, source: StatementSource) -> float:
        if source.file_format not in self.file_formats:
            return 0.0
        if "transaction history" not in source.haystack:
            return 0.0
        if not any(_HEADER_RE.search(line) for line in source.text.splitlines()):
            return 0.0
        return sum(1 for s in _SIGNALS if s in source.haystack) / len(_SIGNALS)

    def parse(self, source: StatementSource) -> ParsedStatement:
        statement = ParsedStatement(
            detection=Detection(
                file_format=source.file_format,
                provider=self.provider,
                statement_type=self.statement_type,
                confidence=self.sniff(source),
                supported=True,
            )
        )
        text = "\n".join(source.pages)
        period = _PERIOD_RE.search(text)
        statement.period = StatementPeriod(
            start=parse_statement_date(period.group(1)) if period else None,
            end=parse_statement_date(period.group(2)) if period else None,
        )

        banks: set[str] = set()
        skipped: list[str] = []
        in_table = False
        for page_number, page in enumerate(source.pages, start=1):
            for line_number, line in enumerate(page.splitlines(), start=1):
                if _HEADER_RE.search(line):
                    in_table = True
                    continue
                if not in_table or not line.strip():
                    continue
                row = self._split(line)
                if row is None:
                    continue
                name, bank, raw_amount, raw_date, status = row

                if status.lower() != _SUCCESS:
                    skipped.append(status)
                    continue
                txn_date = parse_statement_date(raw_date)
                amount = parse_money(raw_amount)
                if txn_date is None or amount is None:
                    continue

                banks.add(bank)
                description, cut = truncate(clean_description(name), MAX_DESCRIPTION)
                statement.transactions.append(
                    NormalizedTransaction(
                        date=txn_date,
                        description=description,
                        amount=amount,
                        currency="INR",
                        fx_rate=Decimal("1"),
                        source_provider=self.provider,
                        source_row=line_number,
                        source_page=page_number,
                        payee=description or None,
                        metadata={"source_bank": bank} | ({"truncated": "1"} if cut else {}),
                    )
                )

        if skipped:
            statement.warn(
                WARN_NON_SUCCESS_SKIPPED,
                f"{len(skipped)} row(s) were not successful and were left out",
            )
        if len(banks) > 1:
            statement.warn(
                WARN_MIXED_SOURCE_ACCOUNTS,
                "This file draws on more than one funding account",
            )
        if not statement.transactions:
            statement.warn(WARN_EMPTY_STATEMENT, "No successful transactions in this file")

        only_bank = next(iter(banks)) if len(banks) == 1 else None
        statement.account = StatementAccountHint(
            masked_number=last4(only_bank) if only_bank else None,
            currency="INR",
            account_type=None,
            institution=only_bank,
        )
        return statement

    @staticmethod
    def _split(line: str) -> tuple[str, str, str, str, str] | None:
        """Read one table row, or None when the line is not one.

        The column gaps are runs of spaces, but a name or a bank may itself
        contain a single space, so the shape is checked rather than the count:
        a row is only a row when a signed amount, a "3 September 2026" date
        and a status word land in the right places.
        """
        chunks = [c for c in re.split(r"\s{2,}", line.strip()) if c]
        if len(chunks) < 5:
            return None
        name, bank, amount, date_text, status = chunks[0], chunks[1], chunks[2], chunks[3], chunks[4]
        if not _AMOUNT_RE.match(amount.replace(" ", "")):
            return None
        if parse_statement_date(date_text) is None:
            return None
        if not status.isalpha():
            return None
        return name, bank, amount, date_text, status
