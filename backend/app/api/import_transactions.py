import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.workspace_context import (
    WorkspaceContext,
    current_workspace,
    current_writable_workspace,
)
from app.core.config import get_settings
from app.schemas.statement_import import (
    StatementAccountMatch,
    StatementBalanceCheck,
    StatementImportPreview,
    StatementPeriodRead,
    StatementRow,
    StatementWarningRead,
)
from app.schemas.transaction import TransactionImportPreview, TransactionImportRequest
from app.services import account_service, import_service
from app.services.statement_import import service as statement_service
from app.services.statement_import.readers import StatementReadError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/transactions", tags=["import"])


@router.post("/import/preview", response_model=TransactionImportPreview)
async def preview_import(
    file: UploadFile = File(...),
    date_format: Optional[str] = Form(None),
    flip_amount: bool = Form(False),
    inflow_column: Optional[str] = Form(None),
    outflow_column: Optional[str] = Form(None),
    column_mapping: Optional[str] = Form(None),
    # Read-gated on purpose, and the exception is deliberate rather than an
    # oversight. This is a POST because it takes a file upload, not because
    # it changes anything: it parses the upload and returns what *would* be
    # imported. The only workspace data it touches is
    # `enrich_with_category_suggestions`, which SELECTs rules and categories
    # to label the preview. Nothing is persisted, so a read-only member
    # previewing a file is doing exactly what their role allows. The write
    # gate belongs on `POST /import` below, which is where the rows land.
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    content = await file.read()
    filename = file.filename or ""

    logger.info(
        "Import preview requested: filename=%s, size=%d bytes, content_type=%s",
        filename, len(content), file.content_type,
    )

    # column_mapping arrives as a JSON-encoded form field (Securo field -> CSV header)
    parsed_mapping: Optional[dict] = None
    if column_mapping:
        try:
            parsed_mapping = json.loads(column_mapping)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid column_mapping: must be a JSON object",
            )
        if not isinstance(parsed_mapping, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid column_mapping: must be a JSON object",
            )

    parse_error: Optional[str] = None
    failed_rows = []
    try:
        if filename.lower().endswith('.ofx') or filename.lower().endswith('.qfx'):
            transactions = import_service.parse_ofx(content)
            detected_format = "ofx"
        elif filename.lower().endswith('.qif'):
            transactions = import_service.parse_qif(content, date_format=date_format)
            detected_format = "qif"
        elif filename.lower().endswith('.xml') or filename.lower().endswith('.camt'):
            transactions = import_service.parse_camt(content)
            detected_format = "camt"
        elif filename.lower().endswith('.csv'):
            detected_format = "csv"
            try:
                transactions, failed_rows = import_service.parse_csv(
                    content,
                    date_format=date_format,
                    flip_amount=flip_amount,
                    inflow_column=inflow_column,
                    outflow_column=outflow_column,
                    column_mapping=parsed_mapping,
                )
            except ValueError as csv_err:
                # The CSV's columns couldn't be auto-mapped. As long as we can
                # still read its headers, return a soft failure so the UI can
                # show the column-mapping dropdowns instead of a hard error.
                if not import_service.detect_csv_columns(content):
                    raise
                transactions = []
                parse_error = str(csv_err)
        else:
            # Try to detect format
            try:
                transactions = import_service.parse_ofx(content)
                detected_format = "ofx"
            except Exception:
                try:
                    transactions = import_service.parse_qif(content, date_format=date_format)
                    detected_format = "qif"
                except Exception:
                    try:
                        transactions = import_service.parse_camt(content)
                        detected_format = "camt"
                    except Exception:
                        transactions, failed_rows = import_service.parse_csv(content)
                        detected_format = "csv"
    except Exception as e:
        logger.error(
            "Failed to parse import file: filename=%s, size=%d bytes, "
            "content_type=%s, first_100_bytes=%r, error=%s",
            filename, len(content), file.content_type,
            content[:100], e,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to parse file: {str(e)}",
        )

    logger.info(
        "Import preview parsed: filename=%s, format=%s, transactions=%d",
        filename, detected_format, len(transactions),
    )

    transactions = await import_service.enrich_with_category_suggestions(
        session, ctx.workspace.id, transactions,
    )

    # Expose CSV headers so the UI can offer accurate column-mapping dropdowns.
    csv_columns: list[str] = []
    if detected_format == "csv":
        try:
            csv_columns = import_service.detect_csv_columns(content)
        except Exception:
            csv_columns = []

    return TransactionImportPreview(
        transactions=transactions,
        detected_format=detected_format,
        csv_columns=csv_columns,
        parse_error=parse_error,
        failed_rows=failed_rows,
    )


@router.post("/import/statement/preview", response_model=StatementImportPreview)
async def preview_statement_import(
    file: UploadFile = File(...),
    #: Only for password-protected workbooks. Used in memory to decrypt, then
    #: dropped: never stored, never logged, never echoed back.
    password: Optional[str] = Form(None),
    #: The account the user has selected, if any. Duplicate detection needs a
    #: target to compare against; without one the preview simply reports none.
    account_id: Optional[str] = Form(None),
    # Read-gated for the same reason `preview_import` above is: this is a POST
    # because it carries a file, not because it changes anything. It parses the
    # upload and reports what *would* be imported. The write gate lives on
    # `POST /import`, which is the endpoint this preview's rows are posted to.
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    settings = get_settings()
    content = await file.read()
    max_bytes = settings.statement_import_max_file_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"File too large. Maximum size is "
                f"{settings.statement_import_max_file_size_mb} MB."
            ),
        )
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="The uploaded file is empty"
        )

    target_account_id = None
    if account_id:
        try:
            target_account_id = uuid.UUID(account_id)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid account_id"
            )
        # Resolved against the workspace before it is used, so the duplicate
        # report can never describe another tenant's transactions.
        if not await account_service.get_account(
            session, target_account_id, ctx.workspace.id
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Account not found"
            )

    try:
        result = await statement_service.preview(
            session,
            ctx.workspace.id,
            file.filename or "",
            content,
            password=password,
            account_id=target_account_id,
            default_currency=settings.default_currency,
        )
    except StatementReadError as exc:
        # A refusal the user can act on (wrong password, malformed archive),
        # reported by code so the UI can translate it.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.code
        )
    except Exception:
        # Nothing about the file itself is logged: a bank statement's bytes,
        # its name and its contents are all account-identifying.
        logger.exception("Statement import preview failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to parse file"
        )

    statement = result.statement
    logger.info(
        "Statement preview parsed: provider=%s, format=%s, transactions=%d, duplicates=%d",
        statement.detection.provider,
        statement.detection.file_format,
        len(result.transactions),
        len(result.duplicate_indexes),
    )

    return StatementImportPreview(
        supported=statement.detection.supported,
        detected_format=statement.detection.file_format,
        provider=statement.detection.provider,
        statement_type=statement.detection.statement_type,
        confidence=statement.detection.confidence,
        account=StatementAccountMatch(
            masked_number=statement.account.masked_number,
            currency=statement.account.currency,
            account_type=statement.account.account_type,
            institution=statement.account.institution,
            suggested_account_id=result.suggested_account_id,
            candidate_account_ids=result.candidate_account_ids,
        ),
        period=StatementPeriodRead(
            start=statement.period.start, end=statement.period.end
        ),
        transactions=result.transactions,
        rows=[
            StatementRow(
                index=index,
                warnings=row.warnings,
                duplicate=index in result.duplicate_indexes,
                source_row=row.source_row,
                source_page=row.source_page,
                running_balance=row.running_balance,
            )
            for index, row in enumerate(statement.transactions)
        ],
        warnings=[
            StatementWarningRead(code=w.code, row=w.row, detail=w.detail)
            for w in statement.warnings
        ],
        balance=StatementBalanceCheck(
            opening=statement.balance.opening,
            total=statement.balance.total,
            expected_closing=statement.balance.expected_closing,
            statement_closing=statement.balance.statement_closing,
            difference=statement.balance.difference,
            matches=statement.balance.matches,
        ),
        duplicate_count=len(result.duplicate_indexes),
    )


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_transactions(
    data: TransactionImportRequest,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    # Verify the target account lives in this workspace BEFORE doing any
    # writes — otherwise a hand-rolled request could import into an
    # account owned by another tenant.
    account = await account_service.get_account(session, data.account_id, ctx.workspace.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    imported, skipped, excluded, import_log_id = await import_service.import_transactions(
        session, ctx.workspace.id, ctx.user_id, data.account_id, data.transactions, "import",
        filename=data.filename, detected_format=data.detected_format,
        detect_duplicates=data.detect_duplicates,
    )

    return {
        "imported": imported,
        "skipped": skipped,
        "excluded": excluded,
        "import_log_id": str(import_log_id),
    }
