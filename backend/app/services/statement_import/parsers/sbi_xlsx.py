"""State Bank of India account statement, as exported from net banking.

Layout (the metadata block's height varies, so nothing here is row-indexed):

    ... account details, two columns of "Label  :  value" ...
    Date | Details | Ref No/Cheque No | Debit | Credit | Balance
    ... one row per transaction ...
    (blank)
    Statement Summary : 01-09-2026  To  14-09-2026
    Brought Forward | Dr Count | Cr Count | Total Debits | Total Credits | Closing Balance
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from app.services.statement_import.models import (
    FORMAT_XLS,
    FORMAT_XLSX,
    STATEMENT_TYPE_BANK,
    WARN_EMPTY_STATEMENT,
    WARN_INVALID_AMOUNT,
    WARN_SUMMARY_MISMATCH,
    BalanceCheck,
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
    dewrap_cell,
    last4,
    parse_statement_date,
    truncate,
)
from app.services.statement_import.readers import parse_money

PROVIDER = "sbi"

_HEADER_CELLS = ("date", "details", "debit", "credit", "balance")
_PERIOD_RE = re.compile(r"statement\s+from\s*:\s*(\S+)\s+to\s+(\S+)", re.IGNORECASE)
_LABEL_RE = re.compile(r"^\s*(.+?)\s*:\s*(.*?)\s*$")


def _find_header(rows: list[list[str]]) -> Optional[int]:
    for index, row in enumerate(rows):
        cells = {clean_description(c).lower() for c in row if c}
        if all(any(cell == want or cell.startswith(want) for cell in cells) for want in _HEADER_CELLS):
            return index
    return None


def _labels(rows: list[list[str]], until: int) -> dict[str, str]:
    """Collect the "Label : value" pairs in the block above the table."""
    found: dict[str, str] = {}
    for row in rows[:until]:
        for cell in row:
            # Split before cleaning: the header block puts several labels in
            # one cell, separated by newlines that `clean_description` folds
            # into spaces.
            for raw_line in (cell or "").replace("\r", "\n").split("\n"):
                match = _LABEL_RE.match(clean_description(raw_line))
                if match:
                    found.setdefault(match.group(1).strip().lower(), match.group(2).strip())
    return found


class SbiXlsxParser:
    provider: str = PROVIDER
    statement_type: str = STATEMENT_TYPE_BANK
    file_formats: tuple[str, ...] = (FORMAT_XLSX, FORMAT_XLS)

    def sniff(self, source: StatementSource) -> float:
        if source.file_format not in self.file_formats:
            return 0.0
        if _find_header(source.rows) is None:
            return 0.0
        # The table shape alone is not unique to SBI; the bank's own name in
        # the sheet is what makes the match certain.
        return 1.0 if "state bank of india" in source.haystack else 0.65

    def parse(self, source: StatementSource) -> ParsedStatement:
        rows = source.rows
        header = _find_header(rows)
        detection = Detection(
            file_format=source.file_format,
            provider=self.provider,
            statement_type=self.statement_type,
            confidence=self.sniff(source),
            supported=True,
        )
        statement = ParsedStatement(detection=detection)
        if header is None:
            statement.warn(WARN_EMPTY_STATEMENT, "No transaction table found")
            return statement

        labels = _labels(rows, header)
        statement.account = StatementAccountHint(
            masked_number=last4(labels.get("account number")),
            currency=(labels.get("currency") or "INR").upper()[:3],
            account_type="savings" if "sb" in (labels.get("product") or "").lower() else "checking",
            institution="State Bank of India",
        )
        statement.period = self._period(rows, header)

        end = self._parse_rows(statement, rows, header)
        self._balance(statement, rows, end, labels)
        if not statement.transactions:
            statement.warn(WARN_EMPTY_STATEMENT, "No transactions in the statement period")
        return statement

    def _period(self, rows: list[list[str]], header: int) -> StatementPeriod:
        for row in rows[:header]:
            for cell in row:
                match = _PERIOD_RE.search(clean_description(cell))
                if match:
                    return StatementPeriod(
                        start=parse_statement_date(match.group(1)),
                        end=parse_statement_date(match.group(2)),
                    )
        return StatementPeriod()

    def _parse_rows(self, statement: ParsedStatement, rows: list[list[str]], header: int) -> int:
        """Read transaction rows; return the index where the table stopped."""
        currency = statement.account.currency or "INR"
        index = header + 1
        while index < len(rows):
            row = rows[index]
            cells = [clean_description(c) for c in row] + [""] * 6
            raw_date, raw_details, raw_ref, raw_debit, raw_credit, raw_balance = cells[:6]

            if not any(cells[:6]):
                break
            if raw_date.lower().startswith("statement summary"):
                break

            txn_date = parse_statement_date(raw_date)
            if txn_date is None:
                # Not a transaction line — a repeated header on a paginated
                # export, or the start of the summary block.
                index += 1
                if raw_date or raw_details:
                    continue
                break

            debit = parse_money(raw_debit)
            credit = parse_money(raw_credit)
            warnings: list[str] = []
            if debit and debit != 0:
                amount = -abs(debit)
            elif credit and credit != 0:
                amount = abs(credit)
            else:
                amount = Decimal("0")
                warnings.append(WARN_INVALID_AMOUNT)

            description, cut = truncate(dewrap_cell(row[1] if len(row) > 1 else ""), MAX_DESCRIPTION)
            reference = raw_ref if raw_ref and raw_ref not in {"-", "0"} else None

            statement.transactions.append(
                NormalizedTransaction(
                    date=txn_date,
                    description=description,
                    amount=amount,
                    currency=currency,
                    fx_rate=Decimal("1"),
                    reference_id=reference,
                    source_provider=self.provider,
                    source_row=index + 1,
                    running_balance=parse_money(raw_balance),
                    warnings=warnings,
                    metadata={"truncated": "1"} if cut else {},
                )
            )
            index += 1
        return index

    def _balance(
        self, statement: ParsedStatement, rows: list[list[str]], start: int, labels: dict[str, str]
    ) -> None:
        """Read the summary block and turn it into an opening/closing check."""
        opening: Optional[Decimal] = None
        closing: Optional[Decimal] = None
        total_debits: Optional[Decimal] = None
        total_credits: Optional[Decimal] = None

        for index in range(start, len(rows)):
            cells = [clean_description(c).lower() for c in rows[index]]
            if any(cell.startswith("brought forward") for cell in cells) and index + 1 < len(rows):
                values = rows[index + 1]
                opening = parse_money(values[0] if values else None)
                if len(values) > 3:
                    total_debits = parse_money(values[3])
                if len(values) > 4:
                    total_credits = parse_money(values[4])
                if len(values) > 5:
                    closing = parse_money(values[5])
                break

        if closing is None:
            closing = parse_money(labels.get("clear balance"))

        total = sum((t.amount for t in statement.transactions), Decimal("0"))
        expected = opening + total if opening is not None else None
        statement.balance = BalanceCheck(
            opening=opening,
            total=total,
            expected_closing=expected,
            statement_closing=closing,
            difference=(closing - expected) if (closing is not None and expected is not None) else None,
            matches=(closing == expected) if (closing is not None and expected is not None) else None,
        )

        # The bank prints its own debit and credit totals. When those disagree
        # with the rows we read, a row was missed or misread — say so rather
        # than let a clean-looking preview hide it.
        parsed_debits = -sum((t.amount for t in statement.transactions if t.amount < 0), Decimal("0"))
        parsed_credits = sum((t.amount for t in statement.transactions if t.amount > 0), Decimal("0"))
        if total_debits is not None and total_debits != parsed_debits:
            statement.warn(WARN_SUMMARY_MISMATCH, "Debit total differs from the statement summary")
        if total_credits is not None and total_credits != parsed_credits:
            statement.warn(WARN_SUMMARY_MISMATCH, "Credit total differs from the statement summary")
