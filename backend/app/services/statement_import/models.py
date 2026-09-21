"""Typed model for a bank statement between the file and Securo's importer.

Everything in here describes a statement *as the bank wrote it*. The step that
turns these rows into Securo's own `TransactionImport` lives in `normalizer`,
and nothing downstream of that boundary is re-implemented here: duplicate
detection, category resolution, payee creation, recurring matching and the
import log all stay in `import_service`.

Money is `Decimal` throughout. A statement is the one place in the app where a
rounding error is not a cosmetic bug, and the balance chain in `validator`
compares totals for exact equality.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional


# Warning codes. Every one of these has a matching `statementImport.warnings.*`
# key in the locale files — the frontend renders `t()` of the code, so adding a
# code here without adding the translations makes the i18n parity test fail.
WARN_UNSUPPORTED_FORMAT = "unsupported_format"
WARN_UNKNOWN_PROVIDER = "unknown_provider"
WARN_PASSWORD_REQUIRED = "password_required"
WARN_WRONG_PASSWORD = "wrong_password"
WARN_OCR_REQUIRED = "ocr_required"
WARN_MALFORMED_FILE = "malformed_file"
WARN_EMPTY_STATEMENT = "empty_statement"
WARN_INVALID_DATE = "invalid_date"
WARN_INVALID_AMOUNT = "invalid_amount"
WARN_NO_AMOUNT = "no_amount"
WARN_ZERO_AMOUNT = "zero_amount"
WARN_MISSING_DESCRIPTION = "missing_description"
WARN_DESCRIPTION_TRUNCATED = "description_truncated"
WARN_BALANCE_MISMATCH = "balance_mismatch"
WARN_AMBIGUOUS_DIRECTION = "ambiguous_direction"
WARN_DUPLICATE = "duplicate"
WARN_DUPLICATE_IN_FILE = "duplicate_in_file"
WARN_MIXED_SOURCE_ACCOUNTS = "mixed_source_accounts"
WARN_NO_ACCOUNT_MATCH = "no_account_match"
WARN_NON_SUCCESS_SKIPPED = "non_success_skipped"
WARN_ROW_LIMIT = "row_limit"
WARN_SUMMARY_MISMATCH = "summary_mismatch"


# File formats the detector can name. `encrypted_office` and `html` are
# recognised so the user gets a straight answer instead of a parse failure.
FORMAT_CSV = "csv"
FORMAT_XLSX = "xlsx"
FORMAT_XLS = "xls"
FORMAT_PDF = "pdf"
FORMAT_ENCRYPTED_OFFICE = "encrypted_office"
FORMAT_HTML = "html"
FORMAT_UNKNOWN = "unknown"

STATEMENT_TYPE_BANK = "bank_account"
STATEMENT_TYPE_CREDIT_CARD = "credit_card"
STATEMENT_TYPE_WALLET = "wallet"
STATEMENT_TYPE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Detection:
    """What the detector concluded about a file, from its *content*."""

    file_format: str
    provider: str
    statement_type: str
    confidence: float
    supported: bool
    #: Warning code explaining an unsupported file; None when supported.
    reason: Optional[str] = None


@dataclass
class StatementAccountHint:
    """What the statement says about the account it belongs to.

    Only ever the last four characters of the identifier. The full account or
    card number is read during parsing to derive this and is never stored,
    returned or logged.
    """

    masked_number: Optional[str] = None
    currency: Optional[str] = None
    #: One of Securo's account types, when the statement implies one.
    account_type: Optional[str] = None
    institution: Optional[str] = None


@dataclass
class StatementPeriod:
    start: Optional[date] = None
    end: Optional[date] = None


@dataclass
class NormalizedTransaction:
    """One statement line, normalized but not yet Securo's own DTO.

    `amount` is *signed*: negative is money out, positive is money in. The
    conversion to Securo's `abs(amount)` + `type` pair happens in `normalizer`.
    """

    date: date
    description: str
    amount: Decimal
    currency: str
    fx_rate: Decimal = Decimal("1")
    #: The bank's own identifier for the line (cheque no, RRN, REF#), when the
    #: statement has a dedicated field for it. Never derived from free text.
    reference_id: Optional[str] = None
    source_provider: str = ""
    #: 1-based row in the sheet/CSV, or line within the page, for PDFs.
    source_row: Optional[int] = None
    source_page: Optional[int] = None
    #: Running balance after this line, when the statement prints one.
    running_balance: Optional[Decimal] = None
    payee: Optional[str] = None
    category_name: Optional[str] = None
    metadata: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def transaction_type(self) -> str:
        """Securo's direction word for this line."""
        return "credit" if self.amount > 0 else "debit"


@dataclass
class StatementWarning:
    """A problem with the file as a whole, or with one identified row.

    `detail` is shown to the user, so it must never carry an account number,
    a full card number or anything else the statement holds about its owner.
    """

    code: str
    row: Optional[int] = None
    detail: str = ""


@dataclass
class BalanceCheck:
    """Opening/closing arithmetic for statements that print balances.

    Deliberately not called "reconciliation": in Securo that word already means
    matching transactions against invoices and recurring bills.
    """

    opening: Optional[Decimal] = None
    total: Optional[Decimal] = None
    expected_closing: Optional[Decimal] = None
    statement_closing: Optional[Decimal] = None
    difference: Optional[Decimal] = None
    #: None when the statement gives nothing to check against.
    matches: Optional[bool] = None


@dataclass
class ParsedStatement:
    detection: Detection
    account: StatementAccountHint = field(default_factory=StatementAccountHint)
    period: StatementPeriod = field(default_factory=StatementPeriod)
    transactions: list[NormalizedTransaction] = field(default_factory=list)
    warnings: list[StatementWarning] = field(default_factory=list)
    balance: BalanceCheck = field(default_factory=BalanceCheck)

    def warn(self, code: str, detail: str = "", row: Optional[int] = None) -> None:
        self.warnings.append(StatementWarning(code=code, row=row, detail=detail))
