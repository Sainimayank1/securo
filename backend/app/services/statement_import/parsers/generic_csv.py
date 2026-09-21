"""Securo's own CSV, reached through the statement importer.

Someone with a folder of files should not have to remember which tab a given
one belongs on. A plain `date,description,amount,currency,fx_rate` file
dropped here is handed straight to `import_service.parse_csv` — the same
function the standard import page calls, with the same column auto-detection,
the same date handling and the same failed-row reporting. Nothing about the
existing CSV behaviour is reimplemented or altered; this adapter only carries
the result into the statement pipeline's shape.
"""
from __future__ import annotations

from decimal import Decimal

from app.services import import_service
from app.services.statement_import.models import (
    FORMAT_CSV,
    STATEMENT_TYPE_UNKNOWN,
    WARN_EMPTY_STATEMENT,
    Detection,
    NormalizedTransaction,
    ParsedStatement,
    StatementAccountHint,
    StatementPeriod,
)
from app.services.statement_import.parsers.base import StatementSource

PROVIDER = "generic"


class GenericCsvParser:
    provider: str = PROVIDER
    statement_type: str = STATEMENT_TYPE_UNKNOWN
    file_formats: tuple[str, ...] = (FORMAT_CSV,)

    def sniff(self, source: StatementSource) -> float:
        if source.file_format not in self.file_formats:
            return 0.0
        try:
            columns = {c.lower() for c in import_service.detect_csv_columns(source.content)}
        except Exception:
            return 0.0
        has_date = bool(columns & {"date", "data", "dt", "transaction_date", "data_transacao"})
        has_amount = bool(columns & {"amount", "valor", "value", "quantia"})
        # Deliberately the floor of the registry: a provider adapter that
        # recognises the same file is always the better answer, because it
        # also knows the file's balances, period and account.
        return 0.5 if has_date and has_amount else 0.0

    def parse(self, source: StatementSource) -> ParsedStatement:
        statement = ParsedStatement(
            detection=Detection(
                file_format=source.file_format,
                provider=self.provider,
                statement_type=self.statement_type,
                confidence=self.sniff(source),
                supported=True,
            ),
            account=StatementAccountHint(),
            period=StatementPeriod(),
        )
        transactions, failed_rows = import_service.parse_csv(source.content)

        for index, txn in enumerate(transactions, start=1):
            signed = txn.amount if txn.type == "credit" else -txn.amount
            statement.transactions.append(
                NormalizedTransaction(
                    date=txn.date,
                    description=txn.description,
                    amount=signed,
                    currency=txn.currency or "",
                    fx_rate=txn.fx_rate or Decimal("1"),
                    reference_id=txn.external_id,
                    source_provider=self.provider,
                    source_row=index,
                    payee=txn.payee_raw,
                    category_name=txn.category_name,
                    metadata={"notes": txn.notes} if txn.notes else {},
                )
            )

        for row in failed_rows:
            # Same codes the standard CSV import already reports, so the two
            # pages say the same thing about the same file.
            statement.warn(row.error_reason, row.raw_value, row=row.line_number)

        if statement.transactions:
            dates = [t.date for t in statement.transactions]
            statement.period = StatementPeriod(start=min(dates), end=max(dates))
        elif not failed_rows:
            statement.warn(WARN_EMPTY_STATEMENT, "No transactions in this file")
        return statement
