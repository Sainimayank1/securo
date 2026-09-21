"""Checking a parsed statement before anyone is shown a number.

Two kinds of check live here. Per-row validation catches what the parser could
not turn into a usable transaction. The balance chain catches what parsing got
*wrong*: where a statement prints a running balance, every line has to account
for the distance between the one before it and the one after, and a line that
does not means a row was misread, missed or invented.

Nothing here rewrites an amount. A statement that fails to add up is flagged
for the person importing it to look at — silently "fixing" a financial record
to make a total work is the one thing this module must never do.
"""
from __future__ import annotations

from decimal import Decimal

from app.services.statement_import.models import (
    WARN_BALANCE_MISMATCH,
    WARN_DUPLICATE_IN_FILE,
    WARN_INVALID_AMOUNT,
    WARN_MISSING_DESCRIPTION,
    WARN_ZERO_AMOUNT,
    ParsedStatement,
)

#: Fallback for a row whose narration column was empty. Securo requires a
#: description, and inventing one from the amount or the date would put words
#: in the bank's mouth.
UNLABELLED = "(no description)"


def validate(statement: ParsedStatement, default_currency: str = "INR") -> ParsedStatement:
    """Annotate every row with what is wrong with it. Mutates in place."""
    _validate_rows(statement, default_currency)
    _validate_balance_chain(statement)
    _flag_duplicates_within_file(statement)
    return statement


def _validate_rows(statement: ParsedStatement, default_currency: str) -> None:
    for transaction in statement.transactions:
        if not transaction.description.strip():
            transaction.description = UNLABELLED
            _flag(transaction, WARN_MISSING_DESCRIPTION)

        if transaction.amount == 0:
            # Securo's CSV importer accepts a zero-amount row, so this is a
            # warning rather than a rejection — the existing import semantics
            # decide, not this module.
            _flag(transaction, WARN_ZERO_AMOUNT)

        if not transaction.currency or len(transaction.currency) != 3:
            transaction.currency = default_currency
        transaction.currency = transaction.currency.upper()

        if transaction.fx_rate is None or transaction.fx_rate <= 0:
            transaction.fx_rate = Decimal("1")


def _validate_balance_chain(statement: ParsedStatement) -> None:
    """Check each printed balance against the one before it plus the amount.

    Exact `Decimal` equality on purpose. These are figures the bank printed to
    the paisa, and a tolerance here would be a tolerance for the parser having
    read the wrong column.
    """
    chain = [t for t in statement.transactions if t.running_balance is not None]
    if not chain:
        return

    opening = statement.balance.opening
    first = chain[0]
    first_balance = first.running_balance
    if opening is not None and first_balance is not None:
        if first_balance != opening + first.amount:
            _flag(first, WARN_BALANCE_MISMATCH)

    for previous, current in zip(chain, chain[1:]):
        before, after = previous.running_balance, current.running_balance
        if before is None or after is None:
            continue
        if after != before + current.amount:
            _flag(current, WARN_BALANCE_MISMATCH)


def _flag_duplicates_within_file(statement: ParsedStatement) -> None:
    """Mark rows the file itself repeats.

    Securo's importer treats two identical rows as one, so a statement that
    genuinely lists the same amount to the same payee twice on one day would
    quietly lose the second. It is still imported as the existing rules say;
    the point is that the preview admits it rather than showing a count the
    import will not match.
    """
    seen: set[tuple] = set()
    for transaction in statement.transactions:
        fingerprint = (
            transaction.date,
            transaction.amount,
            transaction.description,
            transaction.currency,
        )
        if fingerprint in seen:
            _flag(transaction, WARN_DUPLICATE_IN_FILE)
        seen.add(fingerprint)


def _flag(transaction, code: str) -> None:
    if code not in transaction.warnings:
        transaction.warnings.append(code)


def has_blocking_problem(statement: ParsedStatement) -> bool:
    """Whether anything found here should stop the import outright.

    Only a statement with nothing usable in it does. Everything else — a
    balance that does not tie out, an ambiguous direction, a duplicate — is
    shown and left to the person importing, which is the same call Securo's
    CSV import already makes with its failed rows.
    """
    return not statement.transactions


def usable(statement: ParsedStatement) -> list:
    """Rows worth offering for import."""
    return [t for t in statement.transactions if WARN_INVALID_AMOUNT not in t.warnings]
