"""The account-type registry, and `loan` as the first type that exercises it.

Two kinds of test live here. The pure ones pin the registry's contract — what
each trait means, that unknown types degrade instead of raising, that the SQL
key sets stay deterministic. The service ones prove the call sites actually
route through the traits rather than through `== "credit_card"`: each one is a
case where a loan must behave like a card on one axis and unlike it on another,
so a site still hardwired to the card would fail it.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import account_types as at
from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.transaction import Transaction
from app.providers.enable_banking import _map_cash_account_type
from app.schemas.account import AccountCreate, AccountUpdate
from app.services.account_service import (
    _opening_balance_values,
    _simplefin_to_internal_balance,
    create_account,
    get_accounts,
    serialize_account,
    update_account,
)
from app.services.credit_card_service import apply_effective_date
from app.services.report_service import _net_worth_at


# ---------------------------------------------------------------------------
# Registry contract
# ---------------------------------------------------------------------------


def test_every_key_resolves_to_its_own_spec():
    for key in at.ACCOUNT_TYPE_KEYS:
        assert at.spec(key).key == key


def test_traits_split_apart_on_loan():
    """The reason the registry exists: loan shares the liability axis with a
    card and nothing else. If these ever collapse back into one flag, `loan`
    silently inherits statement-cycle behavior it has no business having."""
    assert at.is_liability("credit_card") and at.is_liability("loan")
    assert at.has_billing_cycle("credit_card") and not at.has_billing_cycle("loan")
    assert at.counts_pending_in_balance("credit_card")
    assert not at.counts_pending_in_balance("loan")


def test_sql_key_sets_are_sorted_tuples():
    """Rendered `IN (...)` lists must be byte-stable across processes."""
    for keys in (at.LIABILITY_TYPES, at.PENDING_IN_BALANCE_TYPES, at.BILLING_CYCLE_TYPES):
        assert isinstance(keys, tuple)
        assert list(keys) == sorted(keys)
    assert at.LIABILITY_TYPES == ("credit_card", "loan")
    assert at.BILLING_CYCLE_TYPES == ("credit_card",)


def test_unknown_type_degrades_to_plain_asset():
    """A stale row from an older build must render, not 500."""
    assert at.spec("mystery").key == "__unknown__"
    assert not at.is_liability("mystery")
    assert at.balance_sign("mystery") == 1
    assert at.metadata_fields("mystery") == frozenset()
    assert at.spec(None).key == "__unknown__"


def test_spec_accepts_anything_with_a_type_attribute():
    assert at.is_liability(SimpleNamespace(type="loan"))
    assert at.is_liability(AccountCreate(name="x", type="loan"))
    assert at.balance_sign(SimpleNamespace(type="checking")) == 1


def test_clear_unowned_metadata_strips_card_columns_from_a_loan():
    acc = SimpleNamespace(
        type="loan", credit_limit=Decimal("1"), statement_close_day=5,
        payment_due_day=15, minimum_payment=Decimal("1"), card_brand="x", card_level="y",
    )
    at.clear_unowned_metadata(acc)
    assert all(getattr(acc, f) is None for f in at.ALL_METADATA_FIELDS)


def test_clear_unowned_metadata_keeps_card_columns_on_a_card():
    acc = SimpleNamespace(
        type="credit_card", credit_limit=Decimal("1"), statement_close_day=5,
        payment_due_day=15, minimum_payment=Decimal("1"), card_brand="x", card_level="y",
    )
    at.clear_unowned_metadata(acc)
    assert acc.credit_limit == Decimal("1") and acc.card_brand == "x"


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_schema_accepts_every_registered_type():
    for key in at.ACCOUNT_TYPE_KEYS:
        assert AccountCreate(name="x", type=key).type == key
        assert AccountUpdate(type=key).type == key


def test_schema_rejects_unregistered_type():
    with pytest.raises(ValidationError):
        AccountCreate(name="x", type="lonn")
    with pytest.raises(ValidationError):
        AccountUpdate(type="lonn")


def test_schema_update_without_type_is_fine():
    assert AccountUpdate(name="renamed").type is None


# ---------------------------------------------------------------------------
# Pure helpers that used to branch on the literal
# ---------------------------------------------------------------------------


def test_enable_banking_maps_loan_to_loan():
    assert _map_cash_account_type("LOAN") == "loan"
    assert _map_cash_account_type("CARD") == "credit_card"
    assert _map_cash_account_type("CACC") == "checking"


def test_simplefin_normalization_flips_every_liability():
    assert _simplefin_to_internal_balance("simplefin", "loan", Decimal("-800")) == Decimal("800")
    assert _simplefin_to_internal_balance("simplefin", "credit_card", Decimal("-800")) == Decimal("800")
    assert _simplefin_to_internal_balance("simplefin", "checking", Decimal("-800")) == Decimal("-800")
    assert _simplefin_to_internal_balance("pluggy", "loan", Decimal("800")) == Decimal("800")


def test_opening_balance_for_a_loan_is_a_debit():
    """A positive "amount owed" on a liability opens the ledger with a debit,
    so the transaction sum trends negative like a card's does."""
    assert _opening_balance_values("loan", Decimal("5000")) == (Decimal("5000"), "debit")
    assert _opening_balance_values("credit_card", Decimal("5000")) == (Decimal("5000"), "debit")
    assert _opening_balance_values("checking", Decimal("5000")) == (Decimal("5000"), "credit")


def test_effective_date_on_a_loan_is_the_transaction_date():
    """No statement cycle → no bucketing, even if cycle days are somehow set."""
    tx = SimpleNamespace(date=date(2026, 3, 10), effective_bill_date=None, effective_date=None)
    loan = SimpleNamespace(type="loan", statement_close_day=5, payment_due_day=15)
    apply_effective_date(tx, loan)
    assert tx.effective_date == date(2026, 3, 10)

    card = SimpleNamespace(type="credit_card", statement_close_day=5, payment_due_day=15)
    apply_effective_date(tx, card)
    assert tx.effective_date == date(2026, 4, 15)


# ---------------------------------------------------------------------------
# Service paths
# ---------------------------------------------------------------------------


async def _connection(session: AsyncSession, user_id: uuid.UUID, provider: str) -> BankConnection:
    conn = BankConnection(
        id=uuid.uuid4(), user_id=user_id, provider=provider,
        external_id=f"ext-{provider}-{uuid.uuid4().hex[:8]}",
        institution_name=f"{provider} bank", credentials={"token": "fake"},
        status="active", last_sync_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()
    await session.refresh(conn)
    return conn


@pytest.mark.asyncio
async def test_manual_loan_serializes_negative_and_lands_in_liabilities(
    session: AsyncSession, test_user, test_workspace,
):
    """The cheap path: a manual loan needs no sign special-casing at all, because
    its opening debit already makes the transaction sum negative."""
    account = await create_account(
        session, test_workspace.id, test_user.id,
        AccountCreate(name="Car loan", type="loan", balance=Decimal("12000"), currency="BRL"),
    )
    opening = (await session.execute(
        select(Transaction).where(Transaction.account_id == account.id)
    )).scalar_one()
    assert opening.type == "debit" and opening.amount == Decimal("12000")

    rows = await get_accounts(session, test_workspace.id)
    row = next(r for r in rows if r["id"] == account.id)
    assert row["current_balance"] == pytest.approx(-12000.0)
    assert row["available_credit"] is None and row["next_due_date"] is None

    dp = await _net_worth_at(session, test_workspace.id, date.today(), "BRL")
    assert dp.breakdowns["liabilities"] == 12000.0
    assert dp.breakdowns["accounts"] == 0.0


@pytest.mark.asyncio
async def test_connected_loan_is_negated_and_counted_as_liability(
    session: AsyncSession, test_user, test_workspace,
):
    """The decisive case. A connected loan stores debt positive like a card; a
    site still keyed on `credit_card` would add +12000 to assets and net worth
    would be off by twice the loan."""
    conn = await _connection(session, test_user.id, "pluggy")
    loan = Account(
        id=uuid.uuid4(), user_id=test_user.id, connection_id=conn.id,
        name="Mortgage", type="loan", balance=Decimal("12000"), currency="BRL",
    )
    session.add(loan)
    await session.commit()
    await session.refresh(loan)

    assert serialize_account(loan, None, None)["current_balance"] == pytest.approx(-12000.0)

    rows = await get_accounts(session, test_workspace.id)
    assert next(r for r in rows if r["id"] == loan.id)["current_balance"] == pytest.approx(-12000.0)

    dp = await _net_worth_at(session, test_workspace.id, date.today(), "BRL")
    assert dp.breakdowns["liabilities"] == 12000.0
    assert dp.breakdowns["accounts"] == 0.0
    assert dp.value == -12000.0


@pytest.mark.asyncio
async def test_simplefin_checking_to_loan_flips_stored_balance(
    session: AsyncSession, test_user, test_workspace,
):
    """Crossing the liability boundary flips the raw SimpleFIN sign, exactly
    as checking → credit_card does."""
    conn = await _connection(session, test_user.id, "simplefin")
    account = Account(
        id=uuid.uuid4(), user_id=test_user.id, connection_id=conn.id,
        name="SF Loan", type="checking", balance=Decimal("-900"), currency="BRL",
        external_id="sf-loan",
    )
    session.add(account)
    await session.commit()

    updated = await update_account(session, account.id, test_workspace.id, AccountUpdate(type="loan"))
    assert updated is not None
    assert updated.balance == Decimal("900")
    assert serialize_account(updated, None, None)["current_balance"] == pytest.approx(-900.0)


@pytest.mark.asyncio
async def test_simplefin_card_to_loan_does_not_flip(
    session: AsyncSession, test_user, test_workspace,
):
    """Both sides of the edit are liabilities, so the stored sign is already
    right. The old `"credit_card" in (old, new)` test would have flipped it."""
    conn = await _connection(session, test_user.id, "simplefin")
    account = Account(
        id=uuid.uuid4(), user_id=test_user.id, connection_id=conn.id,
        name="SF Card", type="credit_card", balance=Decimal("900"), currency="BRL",
        external_id="sf-card", credit_limit=Decimal("5000"), statement_close_day=5,
    )
    session.add(account)
    await session.commit()

    updated = await update_account(session, account.id, test_workspace.id, AccountUpdate(type="loan"))
    assert updated is not None
    assert updated.balance == Decimal("900")
    # And the card-only columns are gone, because a loan doesn't own them.
    assert updated.credit_limit is None and updated.statement_close_day is None


@pytest.mark.asyncio
async def test_card_metadata_is_refused_on_a_loan(
    session: AsyncSession, test_user, test_workspace,
):
    conn = await _connection(session, test_user.id, "pluggy")
    account = Account(
        id=uuid.uuid4(), user_id=test_user.id, connection_id=conn.id,
        name="Loan", type="loan", balance=Decimal("100"), currency="BRL", external_id="pl-loan",
    )
    session.add(account)
    await session.commit()

    with pytest.raises(ValueError, match="only be set on credit card"):
        await update_account(
            session, account.id, test_workspace.id, AccountUpdate(credit_limit=Decimal("1000"))
        )


@pytest.mark.asyncio
async def test_manual_create_drops_card_metadata_for_a_loan(
    session: AsyncSession, test_user, test_workspace,
):
    """A payload can send card fields for any type; the registry decides what
    the row keeps, so the account never ends up half credit-card."""
    account = await create_account(
        session, test_workspace.id, test_user.id,
        AccountCreate(
            name="Loan", type="loan", balance=Decimal("0"),
            credit_limit=Decimal("5000"), statement_close_day=5, payment_due_day=15,
        ),
    )
    assert account.credit_limit is None
    assert account.statement_close_day is None and account.payment_due_day is None
