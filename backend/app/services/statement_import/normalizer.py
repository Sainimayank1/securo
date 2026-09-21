"""The one place a statement becomes Securo's own import DTO.

After this function the new code stops: `TransactionImport` is what the
existing `/api/transactions/import` endpoint already accepts, so duplicate
detection, category resolution, payee creation, rule evaluation, recurring
matching, FX stamping and the import log all run exactly as they do for an
OFX or a CSV, through `import_service.import_transactions`.
"""
from __future__ import annotations

from decimal import Decimal

from app.schemas.transaction import TransactionImport
from app.services.statement_import.models import NormalizedTransaction
from app.services.statement_import.parsers.base import MAX_NOTES, truncate


def to_transaction_import(transaction: NormalizedTransaction) -> TransactionImport:
    """Convert one normalized row.

    `external_id` is deliberately left unset. Securo keys duplicate detection
    on it when it is present, and treats it as a stable identifier the bank
    issued — a reference scraped out of a narration is neither guaranteed
    unique nor guaranteed to be the same string next month, and getting it
    wrong either lets duplicates through or suppresses real transactions. The
    reference is preserved in `notes` instead, where it is visible and
    searchable but is not load-bearing, and dedup falls back to the field
    fingerprint the CSV importer already uses.
    """
    notes, _ = truncate(_notes_for(transaction), MAX_NOTES)
    return TransactionImport(
        description=transaction.description,
        amount=abs(transaction.amount),
        date=transaction.date,
        type=transaction.transaction_type,
        external_id=None,
        currency=transaction.currency or None,
        fx_rate=transaction.fx_rate if transaction.fx_rate != Decimal("1") else None,
        payee_raw=transaction.payee,
        category_name=transaction.category_name,
        notes=notes or None,
    )


def _notes_for(transaction: NormalizedTransaction) -> str:
    """Keep the bank's own reference and category, and nothing invented."""
    parts: list[str] = []
    if transaction.reference_id:
        parts.append(f"Ref {transaction.reference_id}")
    category = transaction.metadata.get("merchant_category")
    if category:
        parts.append(category)
    bank = transaction.metadata.get("source_bank")
    if bank:
        parts.append(bank)
    carried = transaction.metadata.get("notes")
    if carried:
        parts.append(carried)
    return " · ".join(parts)


def to_transaction_imports(transactions: list[NormalizedTransaction]) -> list[TransactionImport]:
    return [to_transaction_import(t) for t in transactions]
