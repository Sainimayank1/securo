"""Axis Bank account statement, as exported from net banking as CSV.

The file opens with an unlabelled block of account details and only then
reaches its table:

    Tran Date,CHQNO,PARTICULARS,DR,CR,BAL,SOL

The column *labels* are not reliable. In the exports this adapter was built
against, the "DR" column carries money coming in and "CR" money going out —
the opposite of what the headings say, and consistent across every row when
checked against BAL. Trusting the heading would silently invert the sign of a
whole statement, so the running balance decides instead: both readings are
scored against the balance chain, and the one the bank's own arithmetic agrees
with wins. If neither fits, no reading is assumed correct and every row is
flagged for the user to look at.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from app.services.statement_import.models import (
    FORMAT_CSV,
    STATEMENT_TYPE_BANK,
    WARN_AMBIGUOUS_DIRECTION,
    WARN_EMPTY_STATEMENT,
    WARN_INVALID_AMOUNT,
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
    last4,
    parse_statement_date,
    truncate,
)
from app.services.statement_import.readers import parse_money

PROVIDER = "axis"

_HEADER_CELLS = ("tran date", "particulars", "dr", "cr", "bal")
_ACCOUNT_RE = re.compile(r"statement of account no\s*[-:]\s*(\w+)", re.IGNORECASE)
_PERIOD_RE = re.compile(
    r"from\s*:\s*([0-9][0-9\-/.]+)\s+to\s*:\s*([0-9][0-9\-/.]+)", re.IGNORECASE
)
_CURRENCY_RE = re.compile(r"currency\s*:-?\s*([A-Za-z]{3})", re.IGNORECASE)


def _find_header(rows: list[list[str]]) -> Optional[int]:
    for index, row in enumerate(rows):
        cells = [clean_description(c).lower() for c in row]
        if all(want in cells for want in _HEADER_CELLS):
            return index
    return None


class AxisCsvParser:
    provider: str = PROVIDER
    statement_type: str = STATEMENT_TYPE_BANK
    file_formats: tuple[str, ...] = (FORMAT_CSV,)

    def sniff(self, source: StatementSource) -> float:
        if source.file_format not in self.file_formats:
            return 0.0
        if _find_header(source.rows) is None:
            return 0.0
        return 1.0 if "axis bank" in source.haystack else 0.8

    def parse(self, source: StatementSource) -> ParsedStatement:
        rows = source.rows
        header = _find_header(rows)
        statement = ParsedStatement(
            detection=Detection(
                file_format=source.file_format,
                provider=self.provider,
                statement_type=self.statement_type,
                confidence=self.sniff(source),
                supported=True,
            )
        )
        if header is None:
            statement.warn(WARN_EMPTY_STATEMENT, "No transaction table found")
            return statement

        preamble = "\n".join(" ".join(row) for row in rows[:header])
        account_match = _ACCOUNT_RE.search(preamble)
        currency_match = _CURRENCY_RE.search(preamble)
        statement.account = StatementAccountHint(
            masked_number=last4(account_match.group(1) if account_match else None),
            currency=(currency_match.group(1).upper() if currency_match else "INR"),
            account_type="checking",
            institution="Axis Bank",
        )
        period_match = _PERIOD_RE.search(preamble)
        if period_match:
            statement.period = StatementPeriod(
                start=parse_statement_date(period_match.group(1)),
                end=parse_statement_date(period_match.group(2)),
            )
        else:
            statement.period = StatementPeriod()

        raw_rows = self._collect(rows, header)
        if not raw_rows:
            statement.warn(WARN_EMPTY_STATEMENT, "No transactions in the statement period")
            return statement

        orientation, ambiguous = _choose_orientation(raw_rows)
        if ambiguous:
            statement.warn(
                WARN_AMBIGUOUS_DIRECTION,
                "The DR/CR columns do not agree with the running balance",
            )

        currency = statement.account.currency or "INR"
        for entry in raw_rows:
            warnings: list[str] = []
            debit, credit = entry["dr"], entry["cr"]
            magnitude = debit if debit else credit
            if magnitude is None or magnitude == 0:
                amount = Decimal("0")
                warnings.append(WARN_INVALID_AMOUNT)
            else:
                sign = orientation if debit else -orientation
                amount = sign * abs(magnitude)
            if ambiguous:
                warnings.append(WARN_AMBIGUOUS_DIRECTION)

            description, cut = truncate(entry["particulars"], MAX_DESCRIPTION)
            statement.transactions.append(
                NormalizedTransaction(
                    date=entry["date"],
                    description=description,
                    amount=amount,
                    currency=currency,
                    fx_rate=Decimal("1"),
                    reference_id=entry["cheque"],
                    source_provider=self.provider,
                    source_row=entry["line"],
                    running_balance=entry["balance"],
                    warnings=warnings,
                    metadata={"truncated": "1"} if cut else {},
                )
            )

        self._balance(statement)
        return statement

    def _collect(self, rows: list[list[str]], header: int) -> list[dict]:
        """Pull the table rows out, stopping at the legal text below it."""
        collected: list[dict] = []
        for offset, row in enumerate(rows[header + 1 :], start=header + 2):
            cells = [clean_description(c) for c in row] + [""] * 7
            txn_date = parse_statement_date(cells[0])
            if txn_date is None:
                # Blank separator or the start of the footer. Axis prints
                # several paragraphs of disclaimers after the last row, and
                # none of them opens with a date.
                if not any(cells[:6]):
                    continue
                break
            cheque = cells[1] if cells[1] not in {"", "-", "0"} else None
            collected.append(
                {
                    "line": offset,
                    "date": txn_date,
                    "cheque": cheque,
                    "particulars": clean_description(cells[2]),
                    "dr": parse_money(cells[3]),
                    "cr": parse_money(cells[4]),
                    "balance": parse_money(cells[5]),
                }
            )
        return collected

    def _balance(self, statement: ParsedStatement) -> None:
        with_balance = [t for t in statement.transactions if t.running_balance is not None]
        total = sum((t.amount for t in statement.transactions), Decimal("0"))
        opening: Optional[Decimal] = None
        closing: Optional[Decimal] = None
        if with_balance:
            first, last = with_balance[0], with_balance[-1]
            if first.running_balance is not None:
                # Axis prints no opening figure, so it is the balance before
                # the first line: what the first line landed on, undone.
                opening = first.running_balance - first.amount
            closing = last.running_balance
        expected = opening + total if opening is not None else None
        statement.balance = BalanceCheck(
            opening=opening,
            total=total,
            expected_closing=expected,
            statement_closing=closing,
            difference=(closing - expected) if (closing is not None and expected is not None) else None,
            matches=(closing == expected) if (closing is not None and expected is not None) else None,
        )


def _choose_orientation(entries: list[dict]) -> tuple[Decimal, bool]:
    """Decide whether the DR column means money in (+1) or money out (-1).

    Returns the sign to apply to a DR amount, and whether the choice had to be
    guessed. Scoring is exact-Decimal equality against consecutive balances;
    a statement whose own arithmetic is inconsistent scores badly under both
    readings and is reported rather than resolved.
    """
    chain = [e for e in entries if e["balance"] is not None]
    conventional = Decimal("-1")  # DR = debit = money out, if nothing says otherwise
    if len(chain) < 2:
        return conventional, True

    def score(dr_sign: Decimal) -> int:
        hits = 0
        for previous, current in zip(chain, chain[1:]):
            debit, credit = current["dr"], current["cr"]
            magnitude = debit if debit else credit
            if magnitude is None:
                continue
            sign = dr_sign if debit else -dr_sign
            if current["balance"] - previous["balance"] == sign * abs(magnitude):
                hits += 1
        return hits

    steps = len(chain) - 1
    inflow, outflow = score(Decimal("1")), score(conventional)
    if inflow == steps and outflow < steps:
        return Decimal("1"), False
    if outflow == steps and inflow < steps:
        return conventional, False
    return conventional, True
