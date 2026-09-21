"""The statement-import endpoint, and what happens to the rows afterwards.

The preview is new; everything past it is not. These tests exercise the whole
route a statement takes — upload, detect, preview, then `POST /import` — to
show that the rows land through Securo's existing importer, with its duplicate
detection, its categories and its import log, and not through anything new.
"""
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.import_log import ImportLog
from app.models.transaction import Transaction
from app.models.user import User

FIXTURES = Path(__file__).parent / "fixtures" / "statements"
PREVIEW_URL = "/api/transactions/import/statement/preview"
IMPORT_URL = "/api/transactions/import"


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def upload(name: str, content_type: str = "application/octet-stream"):
    return {"file": (name, read(name), content_type)}


@pytest.fixture
def sbi_upload():
    return upload("sbi_statement.xlsx")


async def _account(session: AsyncSession, user: User, **kwargs) -> Account:
    account = Account(
        id=uuid.uuid4(),
        user_id=user.id,
        name=kwargs.pop("name", "SBI Savings"),
        type=kwargs.pop("type", "savings"),
        balance=Decimal("0.00"),
        currency=kwargs.pop("currency", "INR"),
        **kwargs,
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


async def _preview(client: AsyncClient, headers, name: str, **form):
    return await client.post(PREVIEW_URL, headers=headers, files=upload(name), data=form)


async def _import(client: AsyncClient, headers, account_id, preview_body, name):
    return await client.post(
        IMPORT_URL,
        headers=headers,
        json={
            "account_id": str(account_id),
            "transactions": preview_body["transactions"],
            "filename": name,
            "detected_format": preview_body["detected_format"],
        },
    )


class TestPreview:
    @pytest.mark.asyncio
    async def test_reports_what_it_detected(self, client, auth_headers, test_user, session):
        await _account(session, test_user, masked_number="4312")
        response = await _preview(client, auth_headers, "sbi_statement.xlsx")

        assert response.status_code == 200
        body = response.json()
        assert body["supported"] is True
        assert body["provider"] == "sbi"
        assert body["detected_format"] == "xlsx"
        assert body["statement_type"] == "bank_account"
        assert body["account"]["masked_number"] == "4312"
        assert body["account"]["currency"] == "INR"
        assert body["period"] == {"start": "2026-09-01", "end": "2026-09-14"}
        assert len(body["transactions"]) == 6
        assert len(body["rows"]) == 6

    @pytest.mark.asyncio
    async def test_balance_check_is_reported(self, client, auth_headers, test_user, session):
        await _account(session, test_user, masked_number="4312")
        body = (await _preview(client, auth_headers, "sbi_statement.xlsx")).json()
        assert body["balance"] == {
            "opening": "36677.42",
            "total": "-13367.52",
            "expected_closing": "23309.90",
            "statement_closing": "23309.90",
            "difference": "0.00",
            "matches": True,
        }

    @pytest.mark.asyncio
    async def test_rows_carry_their_warnings_and_their_place_in_the_file(
        self, client, auth_headers, test_user, session
    ):
        await _account(session, test_user, masked_number="4312")
        body = (await _preview(client, auth_headers, "sbi_statement_unbalanced.xlsx")).json()
        flagged = [row for row in body["rows"] if "balance_mismatch" in row["warnings"]]
        assert len(flagged) == 2
        assert all(row["source_row"] for row in flagged)

    @pytest.mark.asyncio
    async def test_an_exactly_matching_account_is_preselected(
        self, client, auth_headers, test_user, session
    ):
        account = await _account(session, test_user, masked_number="4312")
        body = (await _preview(client, auth_headers, "sbi_statement.xlsx")).json()
        assert body["account"]["suggested_account_id"] == str(account.id)

    @pytest.mark.asyncio
    async def test_nothing_is_preselected_when_no_account_matches(
        self, client, auth_headers, test_user, session
    ):
        await _account(session, test_user, masked_number="9999")
        body = (await _preview(client, auth_headers, "sbi_statement.xlsx")).json()
        assert body["account"]["suggested_account_id"] is None
        assert "no_account_match" in {w["code"] for w in body["warnings"]}

    @pytest.mark.asyncio
    async def test_a_card_statement_does_not_preselect_a_savings_account(
        self, client, auth_headers, test_user, session
    ):
        """Same last four digits, wrong kind of account."""
        await _account(session, test_user, masked_number="3649", type="savings")
        card = await _account(
            session, test_user, masked_number="3649", type="credit_card", name="MyZone Card"
        )
        body = (await _preview(client, auth_headers, "axis_myzone_statement.pdf")).json()
        assert body["statement_type"] == "credit_card"
        assert body["account"]["suggested_account_id"] == str(card.id)

    @pytest.mark.asyncio
    async def test_an_ambiguous_match_leaves_the_choice_to_the_user(
        self, client, auth_headers, test_user, session
    ):
        await _account(session, test_user, masked_number="4312", name="One")
        await _account(session, test_user, masked_number="4312", name="Two")
        body = (await _preview(client, auth_headers, "sbi_statement.xlsx")).json()
        assert body["account"]["suggested_account_id"] is None
        assert len(body["account"]["candidate_account_ids"]) == 2


class TestRefusals:
    @pytest.mark.asyncio
    async def test_authentication_is_required(self, client):
        response = await client.post(PREVIEW_URL, files=upload("sbi_statement.xlsx"))
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_a_file_over_the_limit_is_refused_before_it_is_parsed(
        self, client, auth_headers
    ):
        from app.core.config import get_settings

        oversized = b"%PDF-1.4\n" + b"0" * (
            get_settings().statement_import_max_file_size_mb * 1024 * 1024
        )
        response = await client.post(
            PREVIEW_URL, headers=auth_headers, files={"file": ("big.pdf", oversized, "application/pdf")}
        )
        assert response.status_code == 413

    @pytest.mark.asyncio
    async def test_an_empty_upload_is_refused(self, client, auth_headers):
        response = await client.post(
            PREVIEW_URL, headers=auth_headers, files={"file": ("empty.csv", b"", "text/csv")}
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_an_unsupported_file_is_reported_not_crashed_on(self, client, auth_headers):
        response = await client.post(
            PREVIEW_URL,
            headers=auth_headers,
            files={"file": ("photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png")},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["supported"] is False
        assert body["transactions"] == []
        assert "unsupported_format" in {w["code"] for w in body["warnings"]}

    @pytest.mark.asyncio
    async def test_a_malformed_workbook_is_reported(self, client, auth_headers):
        response = await client.post(
            PREVIEW_URL,
            headers=auth_headers,
            files={"file": ("broken.xlsx", read("sbi_statement.xlsx")[:400], "application/vnd.ms-excel")},
        )
        assert response.status_code == 200
        assert response.json()["supported"] is False

    @pytest.mark.asyncio
    async def test_a_protected_workbook_asks_for_its_password(self, client, auth_headers):
        body = (await _preview(client, auth_headers, "sbi_statement_encrypted.xlsx")).json()
        assert body["supported"] is False
        assert "password_required" in {w["code"] for w in body["warnings"]}

    @pytest.mark.asyncio
    async def test_the_password_opens_it(self, client, auth_headers, test_user, session):
        await _account(session, test_user, masked_number="4312")
        response = await _preview(
            client, auth_headers, "sbi_statement_encrypted.xlsx", password="fixture-password"
        )
        assert response.status_code == 200
        assert len(response.json()["transactions"]) == 6

    @pytest.mark.asyncio
    async def test_a_wrong_password_says_so(self, client, auth_headers):
        response = await _preview(
            client, auth_headers, "sbi_statement_encrypted.xlsx", password="wrong"
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "wrong_password"

    @pytest.mark.asyncio
    async def test_an_account_outside_the_workspace_is_not_readable(self, client, auth_headers):
        """The duplicate report must never describe another tenant's rows."""
        response = await _preview(
            client, auth_headers, "sbi_statement.xlsx", account_id=str(uuid.uuid4())
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_a_read_only_member_may_preview_but_not_import(
        self, client, viewer_auth_headers, test_user, session
    ):
        account = await _account(session, test_user, masked_number="4312")
        preview = await _preview(client, viewer_auth_headers, "sbi_statement.xlsx")
        assert preview.status_code == 200

        imported = await _import(
            client, viewer_auth_headers, account.id, preview.json(), "sbi_statement.xlsx"
        )
        assert imported.status_code == 403


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_a_statement_lands_through_the_existing_importer(
        self, client, auth_headers, test_user, session
    ):
        account = await _account(session, test_user, masked_number="4312")
        preview = (await _preview(client, auth_headers, "sbi_statement.xlsx")).json()

        response = await _import(client, auth_headers, account.id, preview, "sbi_statement.xlsx")
        assert response.status_code == 201
        assert response.json()["imported"] == 6

        rows = (
            await session.execute(
                select(Transaction)
                .where(Transaction.account_id == account.id)
                .order_by(Transaction.date, Transaction.amount)
            )
        ).scalars().all()
        assert len(rows) == 6
        assert {r.currency for r in rows} == {"INR"}
        # Debits negative, credits positive, as the statement had them.
        debits = [r for r in rows if r.type == "debit"]
        credits = [r for r in rows if r.type == "credit"]
        assert sum(r.amount for r in credits) == Decimal("15001.00")
        assert sum(r.amount for r in debits) == Decimal("28368.52")
        # The importer's own bookkeeping ran: an import log exists and owns them.
        log = (await session.execute(select(ImportLog))).scalars().one()
        assert log.account_id == account.id
        assert log.transaction_count == 6
        assert {r.import_id for r in rows} == {log.id}
        assert {r.source for r in rows} == {"import"}

    @pytest.mark.asyncio
    async def test_a_credit_card_statement_lands_in_the_card_account(
        self, client, auth_headers, test_user, session
    ):
        card = await _account(
            session, test_user, masked_number="3649", type="credit_card", name="MyZone"
        )
        preview = (await _preview(client, auth_headers, "axis_myzone_statement.pdf")).json()
        assert preview["account"]["suggested_account_id"] == str(card.id)

        response = await _import(client, auth_headers, card.id, preview, "myzone.pdf")
        assert response.status_code == 201
        assert response.json()["imported"] == 6

        rows = (
            await session.execute(select(Transaction).where(Transaction.account_id == card.id))
        ).scalars().all()
        payment = next(r for r in rows if r.type == "credit")
        assert payment.amount == Decimal("3793.03")
        assert sum(r.amount for r in rows if r.type == "debit") == Decimal("4043.00")

    @pytest.mark.asyncio
    async def test_the_same_statement_twice_imports_nothing_the_second_time(
        self, client, auth_headers, test_user, session
    ):
        account = await _account(session, test_user, masked_number="4312")
        first = (await _preview(client, auth_headers, "sbi_statement.xlsx")).json()
        await _import(client, auth_headers, account.id, first, "sbi_statement.xlsx")

        second = (
            await _preview(
                client, auth_headers, "sbi_statement.xlsx", account_id=str(account.id)
            )
        ).json()
        # The preview says so before the user commits to it.
        assert second["duplicate_count"] == 6
        assert all(row["duplicate"] for row in second["rows"])

        response = await _import(client, auth_headers, account.id, second, "sbi_statement.xlsx")
        assert response.json() == {
            "imported": 0,
            "skipped": 6,
            "excluded": 0,
            "import_log_id": response.json()["import_log_id"],
        }
        rows = (
            await session.execute(select(Transaction).where(Transaction.account_id == account.id))
        ).scalars().all()
        assert len(rows) == 6

    @pytest.mark.asyncio
    async def test_overlapping_statements_import_only_the_new_days(
        self, client, auth_headers, test_user, session
    ):
        """01-14 Sep, then a statement starting mid-period.

        The three lines the two files share are already stored and must be
        skipped; the three only the second covers must not be.
        """
        account = await _account(session, test_user, masked_number="4312")
        first = (await _preview(client, auth_headers, "sbi_statement.xlsx")).json()
        await _import(client, auth_headers, account.id, first, "sbi_statement.xlsx")

        overlap = (
            await _preview(
                client, auth_headers, "sbi_statement_overlap.xlsx", account_id=str(account.id)
            )
        ).json()
        assert overlap["duplicate_count"] == 3
        assert [row["duplicate"] for row in overlap["rows"]] == [True, True, True, False, False, False]

        response = await _import(client, auth_headers, account.id, overlap, "overlap.xlsx")
        assert response.status_code == 201
        assert response.json()["imported"] == 3
        assert response.json()["skipped"] == 3

        rows = (
            await session.execute(select(Transaction).where(Transaction.account_id == account.id))
        ).scalars().all()
        assert len(rows) == 9

    @pytest.mark.asyncio
    async def test_duplicates_are_only_reported_against_the_chosen_account(
        self, client, auth_headers, test_user, session
    ):
        account = await _account(session, test_user, masked_number="4312")
        other = await _account(session, test_user, masked_number="7777", name="Other")
        first = (await _preview(client, auth_headers, "sbi_statement.xlsx")).json()
        await _import(client, auth_headers, account.id, first, "sbi_statement.xlsx")

        against_other = (
            await _preview(client, auth_headers, "sbi_statement.xlsx", account_id=str(other.id))
        ).json()
        assert against_other["duplicate_count"] == 0

    @pytest.mark.asyncio
    async def test_the_axis_csv_keeps_the_signs_the_balance_implies(
        self, client, auth_headers, test_user, session
    ):
        account = await _account(session, test_user, masked_number="3852", type="checking")
        preview = (await _preview(client, auth_headers, "axis_statement.csv")).json()
        await _import(client, auth_headers, account.id, preview, "axis.csv")

        rows = (
            await session.execute(select(Transaction).where(Transaction.account_id == account.id))
        ).scalars().all()
        salary = next(r for r in rows if "EXAMPLE EMPLOYER LLP" in r.description)
        merchant = next(r for r in rows if "EXAMPLE MERCHANT" in r.description)
        assert (salary.type, salary.amount) == ("credit", Decimal("101965.00"))
        assert (merchant.type, merchant.amount) == ("debit", Decimal("5000.00"))

    @pytest.mark.asyncio
    async def test_a_standard_securo_csv_behaves_the_same_on_both_pages(
        self, client, auth_headers, test_user, session
    ):
        """The statement tab delegates to the existing CSV importer."""
        await _account(session, test_user, masked_number="4312")
        statement = (await _preview(client, auth_headers, "securo_standard.csv")).json()
        standard = await client.post(
            "/api/transactions/import/preview",
            headers=auth_headers,
            files=upload("securo_standard.csv", "text/csv"),
        )
        assert standard.status_code == 200

        assert statement["provider"] == "generic"
        assert statement["detected_format"] == "csv"
        assert len(statement["transactions"]) == len(standard.json()["transactions"]) == 2
        for from_statement, from_standard in zip(
            statement["transactions"], standard.json()["transactions"]
        ):
            assert from_statement["description"] == from_standard["description"]
            assert from_statement["amount"] == from_standard["amount"]
            assert from_statement["type"] == from_standard["type"]
            assert from_statement["date"] == from_standard["date"]
