"""Response shapes for the statement-import preview.

`transactions` is deliberately a plain `list[TransactionImport]`: it is
exactly what `POST /api/transactions/import` already accepts, so the frontend
posts it straight back and no parallel import endpoint exists. Everything the
statement pipeline knows *about* those rows — which one came from which line,
what is wrong with it, whether the importer will skip it — travels alongside
in `rows`, indexed in step, rather than being bolted onto the shared DTO where
it would leak into the standard CSV preview.
"""
import uuid
from datetime import date as _Date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from app.schemas.transaction import TransactionImport


class StatementRow(BaseModel):
    """Per-row provenance and findings, parallel to `transactions`."""

    index: int
    warnings: list[str] = []
    duplicate: bool = False
    #: Where the row came from, so a warning can be traced back to the file.
    source_row: Optional[int] = None
    source_page: Optional[int] = None
    running_balance: Optional[Decimal] = None


class StatementAccountMatch(BaseModel):
    """What the statement says about its account, and what it matched.

    Only ever the last four characters of the identifier — the full account or
    card number is used to derive this during parsing and is never returned.
    """

    masked_number: Optional[str] = None
    currency: Optional[str] = None
    account_type: Optional[str] = None
    institution: Optional[str] = None
    #: Set only when exactly one open account is an unambiguous match.
    suggested_account_id: Optional[uuid.UUID] = None
    candidate_account_ids: list[uuid.UUID] = []


class StatementPeriodRead(BaseModel):
    start: Optional[_Date] = None
    end: Optional[_Date] = None


class StatementBalanceCheck(BaseModel):
    """The statement's own arithmetic, recomputed from the parsed rows.

    `total` is the movement of whatever figure the statement leads with: the
    account balance for a bank statement, the amount owed for a card one.
    """

    opening: Optional[Decimal] = None
    total: Optional[Decimal] = None
    expected_closing: Optional[Decimal] = None
    statement_closing: Optional[Decimal] = None
    difference: Optional[Decimal] = None
    matches: Optional[bool] = None


class StatementWarningRead(BaseModel):
    code: str
    row: Optional[int] = None
    detail: str = ""


class StatementImportPreview(BaseModel):
    supported: bool
    detected_format: str
    provider: str
    statement_type: str
    confidence: float
    account: StatementAccountMatch = StatementAccountMatch()
    period: StatementPeriodRead = StatementPeriodRead()
    transactions: list[TransactionImport] = []
    rows: list[StatementRow] = []
    warnings: list[StatementWarningRead] = []
    balance: StatementBalanceCheck = StatementBalanceCheck()
    duplicate_count: int = 0
