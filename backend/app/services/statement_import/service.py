"""Orchestration: an upload in, a preview out.

This module is the only part of the statement feature that touches the
database, and it touches it for exactly two reasons — to work out which of
the user's accounts a statement belongs to, and to ask Securo's own importer
which rows it is going to treat as duplicates. Writing rows is not done here
at all: the preview hands back `TransactionImport` objects, and the existing
`POST /api/transactions/import` endpoint takes it from there.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.schemas.transaction import TransactionImport
from app.services import import_service
from app.services.statement_import import detector, normalizer, registry, validator
from app.services.statement_import.models import (
    WARN_MIXED_SOURCE_ACCOUNTS,
    WARN_NO_ACCOUNT_MATCH,
    WARN_ROW_LIMIT,
    Detection,
    ParsedStatement,
)
from app.services.statement_import.readers import StatementReadError

#: A statement with more rows than this is not a statement. The cap exists so
#: one upload cannot turn into an unbounded number of duplicate lookups.
MAX_TRANSACTIONS = 5_000

#: Statement types that can only sensibly land in a card account, and the
#: account types that can hold an ordinary bank statement.
_CARD_TYPES = {"credit_card"}
_BANK_TYPES = {"checking", "savings"}


class StatementPreview:
    """Everything the preview screen needs, in one value."""

    def __init__(
        self,
        statement: ParsedStatement,
        transactions: list[TransactionImport],
        duplicate_indexes: set[int],
        suggested_account_id: Optional[uuid.UUID],
        candidate_account_ids: list[uuid.UUID],
    ):
        self.statement = statement
        self.transactions = transactions
        self.duplicate_indexes = duplicate_indexes
        self.suggested_account_id = suggested_account_id
        self.candidate_account_ids = candidate_account_ids


async def preview(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    filename: str,
    content: bytes,
    password: Optional[str] = None,
    account_id: Optional[uuid.UUID] = None,
    default_currency: str = "INR",
) -> StatementPreview:
    """Detect, parse, validate and price up a statement without writing it."""
    detection, source = detector.detect(filename, content, password)
    if not detection.supported or source is None:
        statement = ParsedStatement(detection=detection)
        if detection.reason:
            statement.warn(detection.reason)
        return StatementPreview(statement, [], set(), None, [])

    parser = registry.select_parser(source)
    if parser is None:  # pragma: no cover - detect() already refuses these
        statement = ParsedStatement(detection=detection)
        statement.warn(WARN_ROW_LIMIT)
        return StatementPreview(statement, [], set(), None, [])

    statement = parser.parse(source)
    if len(statement.transactions) > MAX_TRANSACTIONS:
        statement.transactions = statement.transactions[:MAX_TRANSACTIONS]
        statement.warn(
            WARN_ROW_LIMIT,
            f"Only the first {MAX_TRANSACTIONS} rows of this file were read",
        )

    validator.validate(statement, default_currency=default_currency)
    rows = validator.usable(statement)
    transactions = normalizer.to_transaction_imports(rows)
    statement.transactions = rows

    suggested, candidates = await match_account(session, workspace_id, statement)
    if suggested is None:
        statement.warn(WARN_NO_ACCOUNT_MATCH)

    target = account_id or suggested
    duplicates = (
        await find_duplicates(session, target, transactions) if target else set()
    )

    transactions = await import_service.enrich_with_category_suggestions(
        session, workspace_id, transactions
    )
    return StatementPreview(statement, transactions, duplicates, suggested, candidates)


async def find_duplicates(
    session: AsyncSession,
    account_id: uuid.UUID,
    transactions: list[TransactionImport],
) -> set[int]:
    """Indexes of rows the importer will skip as already present.

    Uses `import_service.find_duplicate` — the same predicate the import
    itself runs — including its rule that a row already claimed by an earlier
    line cannot be claimed again. That is what makes two overlapping
    statements behave: the days they share match what is already stored and
    are skipped, and the days only the second one covers are not.
    """
    duplicates: set[int] = set()
    claimed: set[uuid.UUID] = set()
    for index, transaction in enumerate(transactions):
        match = await import_service.find_duplicate(
            session, account_id, transaction, exclude_ids=claimed
        )
        if match is not None:
            claimed.add(match.id)
            duplicates.add(index)
    return duplicates


async def match_account(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    statement: ParsedStatement,
) -> tuple[Optional[uuid.UUID], list[uuid.UUID]]:
    """Find the account this statement belongs to.

    A suggestion is only made when exactly one open account in the workspace
    carries the same last four characters and is the right kind of account for
    the statement. Anything less certain returns no suggestion and leaves the
    choice to the user: importing a card statement into a savings account, or
    one person's statement into another's account, is not a mistake worth
    risking to save a click.
    """
    hint = statement.account
    if not hint.masked_number:
        return None, []
    if statement.warnings and any(w.code == WARN_MIXED_SOURCE_ACCOUNTS for w in statement.warnings):
        # The file draws on more than one funding account; no single Securo
        # account is the right answer.
        return None, []

    result = await session.execute(
        select(Account).where(
            Account.workspace_id == workspace_id,
            Account.is_closed == False,  # noqa: E712 - SQL comparison, not identity
            Account.masked_number == hint.masked_number,
        )
    )
    candidates = list(result.scalars().all())
    if statement.detection.statement_type == "credit_card":
        allowed = _CARD_TYPES
    elif statement.detection.statement_type == "bank_account":
        allowed = _BANK_TYPES
    else:
        allowed = None
    if allowed is not None:
        typed = [a for a in candidates if a.type in allowed]
        if typed:
            candidates = typed

    if hint.currency:
        same_currency = [a for a in candidates if a.currency == hint.currency]
        if same_currency:
            candidates = same_currency

    ids = [a.id for a in candidates]
    return (ids[0] if len(ids) == 1 else None), ids


__all__ = [
    "MAX_TRANSACTIONS",
    "StatementPreview",
    "StatementReadError",
    "Detection",
    "find_duplicates",
    "match_account",
    "preview",
]
