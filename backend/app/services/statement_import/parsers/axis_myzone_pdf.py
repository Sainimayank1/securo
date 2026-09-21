"""Axis Bank "My Zone" RuPay credit-card statement (PDF).

Only the Account Summary table is a list of transactions. Everything else on
those two pages is prose or worked examples that happen to contain money:

  * the PAYMENT SUMMARY block, including Previous Balance and Total Payment Due
  * the EMI BALANCES block under the table
  * the Finance Charge calculation example (Rs. 36.99, 893.84, 1265.11 …)
  * the Schedule of charges
  * the Minimum Amount Due Calculation worked example, which is itself a
    dated table with a Cr/Db column

Three independent conditions keep those out, and a row has to satisfy all of
them: it must fall inside the Account Summary section, it must begin with a
DD/MM/YYYY date, and it must end with an amount carrying a Cr or Dr marker.
The bank's own payment summary is then used as an arithmetic check on what
was extracted, so a layout change that drops or invents a row is reported
instead of quietly importing.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from app.services.statement_import.models import (
    FORMAT_PDF,
    STATEMENT_TYPE_CREDIT_CARD,
    WARN_EMPTY_STATEMENT,
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
    last4,
    parse_statement_date,
    truncate,
)
from app.services.statement_import.readers import parse_money

PROVIDER = "axis_myzone"

_SIGNALS = (
    "my zone",
    "credit card statement",
    "transaction details",
    "merchant category",
)

_ROW_RE = re.compile(
    r"^\s*(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<rest>.+?)\s{2,}(?P<amount>[\d,]+\.\d{2})\s+(?P<marker>Cr|Dr)\s*$"
)
_TABLE_HEADER_RE = re.compile(r"\bDATE\b.*\bTRANSACTION\s+DETAILS\b", re.IGNORECASE)
#: Headings that end the table. Each one opens a block that is not transactions.
_TABLE_CLOSERS = (
    "emi balances",
    "end of statement",
    "finance charge calculation",
    "schedule of charges",
    "minimum amount due calculation",
    "important message",
)
_CARD_RE = re.compile(r"(\d{4,6}[X*x]{4,}\d{4})")
_PERIOD_RE = re.compile(r"(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})")
_REF_RE = re.compile(r"REF#\s*(\w+)", re.IGNORECASE)
_MONEY_TOKEN_RE = re.compile(r"\d[\d,]*\.\d{2}")
_SUMMARY_LABEL_RE = re.compile(r"previous balance.*total payment due", re.IGNORECASE)
_CHUNK_RE = re.compile(r"\S(?:.*?\S)?(?=\s{2,}|$)")


class AxisMyZonePdfParser:
    provider: str = PROVIDER
    statement_type: str = STATEMENT_TYPE_CREDIT_CARD
    file_formats: tuple[str, ...] = (FORMAT_PDF,)

    def sniff(self, source: StatementSource) -> float:
        if source.file_format not in self.file_formats:
            return 0.0
        haystack = source.haystack
        if "my zone" not in haystack or "merchant category" not in haystack:
            return 0.0
        return sum(1 for s in _SIGNALS if s in haystack) / len(_SIGNALS)

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
        pages = source.pages
        text = "\n".join(pages)

        card = _CARD_RE.search(text)
        statement.account = StatementAccountHint(
            masked_number=last4(card.group(1) if card else None),
            currency="INR",
            account_type="credit_card",
            institution="Axis Bank",
        )
        period = _PERIOD_RE.search(text)
        statement.period = StatementPeriod(
            start=parse_statement_date(period.group(1)) if period else None,
            end=parse_statement_date(period.group(2)) if period else None,
        )

        self._extract(statement, pages)
        if not statement.transactions:
            statement.warn(WARN_EMPTY_STATEMENT, "No transactions found in the statement table")
        self._check_against_summary(statement, text)
        return statement

    def _extract(self, statement: ParsedStatement, pages: list[str]) -> None:
        """Walk every page, collecting only rows inside the Account Summary."""
        in_table = False
        category_column: Optional[int] = None
        for page_number, page in enumerate(pages, start=1):
            for line_number, line in enumerate(page.splitlines(), start=1):
                lowered = line.lower()
                if any(closer in lowered for closer in _TABLE_CLOSERS):
                    in_table = False
                    continue
                if _TABLE_HEADER_RE.search(line):
                    in_table = True
                    category_column = self._category_column(line)
                    continue
                if not in_table:
                    continue

                match = _ROW_RE.match(line.rstrip())
                if not match:
                    continue
                txn_date = parse_statement_date(match.group("date"))
                amount = parse_money(match.group("amount"))
                if txn_date is None or amount is None:
                    continue
                if match.group("marker").lower() == "dr":
                    amount = -abs(amount)
                else:
                    amount = abs(amount)

                details, category = self._split_details(match.group("rest"), match.start("rest"), category_column)
                description, cut = truncate(clean_description(details), MAX_DESCRIPTION)
                reference = _REF_RE.search(details)
                metadata = {}
                if category:
                    metadata["merchant_category"] = category
                if cut:
                    metadata["truncated"] = "1"

                statement.transactions.append(
                    NormalizedTransaction(
                        date=txn_date,
                        description=description,
                        amount=amount,
                        currency="INR",
                        fx_rate=Decimal("1"),
                        reference_id=reference.group(1) if reference else None,
                        source_provider=self.provider,
                        source_row=line_number,
                        source_page=page_number,
                        category_name=None,
                        metadata=metadata,
                    )
                )

    @staticmethod
    def _category_column(header_line: str) -> Optional[int]:
        match = re.search(r"merchant\s+category", header_line, re.IGNORECASE)
        return match.start() if match else None

    @staticmethod
    def _split_details(rest: str, rest_offset: int, category_column: Optional[int]) -> tuple[str, Optional[str]]:
        """Separate the narration from the merchant category.

        Layout extraction keeps each field roughly under its own heading, so
        the split is made on where a chunk *starts* relative to the MERCHANT
        CATEGORY heading rather than on "the last chunk wins" — a narration
        with a wide internal gap would otherwise lose its tail to the
        category. The tolerance absorbs the header being centred over a
        left-aligned column.
        """
        chunks = [(m.group(0), rest_offset + m.start()) for m in _CHUNK_RE.finditer(rest)]
        if not chunks:
            return rest.strip(), None
        if category_column is None or len(chunks) == 1:
            return chunks[0][0], (" ".join(c for c, _ in chunks[1:]) or None)

        threshold = category_column - 12
        details = [c for c, start in chunks if start < threshold]
        category = [c for c, start in chunks if start >= threshold]
        if not details:  # Everything looked like a category; keep the first chunk as narration.
            details, category = [chunks[0][0]], [c for c, _ in chunks[1:]]
        return " ".join(details), (" ".join(category) or None)

    def _check_against_summary(self, statement: ParsedStatement, text: str) -> None:
        """Cross-check the extracted rows against the bank's payment summary.

        The header block states Previous Balance, Payments, Credits, Purchase,
        Cash Advance, Other Debit&Charges and Total Payment Due. Those are not
        transactions and are never imported, but they are exactly the totals
        the extracted rows have to add up to.
        """
        values = _summary_values(text)
        credits = sum((t.amount for t in statement.transactions if t.amount > 0), Decimal("0"))
        debits = -sum((t.amount for t in statement.transactions if t.amount < 0), Decimal("0"))

        if values is None:
            statement.balance = BalanceCheck(total=debits - credits)
            return

        previous, payments, refunds, purchase, cash, other, total_due = values
        expected_credits = payments + refunds
        expected_debits = purchase + cash + other
        if credits != expected_credits or debits != expected_debits:
            statement.warn(
                WARN_SUMMARY_MISMATCH,
                "Extracted rows do not add up to the statement's payment summary",
            )

        # For a card the headline figure is what is owed, so the movement is
        # charges minus payments — the opposite sign to a bank balance.
        movement = debits - credits
        expected_closing = previous + movement
        statement.balance = BalanceCheck(
            opening=previous,
            total=movement,
            expected_closing=expected_closing,
            statement_closing=total_due,
            difference=total_due - expected_closing,
            matches=total_due == expected_closing,
        )


def _summary_values(text: str) -> Optional[tuple[Decimal, ...]]:
    """The seven figures on the line under the payment-summary formula."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not _SUMMARY_LABEL_RE.search(line):
            continue
        for candidate in lines[index + 1 : index + 5]:
            tokens = _MONEY_TOKEN_RE.findall(candidate)
            if len(tokens) < 7:
                continue
            parsed: list[Decimal] = []
            for token in tokens[:7]:
                value = parse_money(token)
                if value is None:
                    break
                parsed.append(value)
            if len(parsed) == 7:
                return tuple(parsed)
    return None
