"""The account-type registry: one row per type, one place to add the next.

`Account.type` is a free-form string column, and for most of the product it is
only a label — an icon, a color, a translated name. A few types carry actual
*behavior*, and each behavior used to be spelled as `account.type ==
"credit_card"` at every site that cared: nine separate sites for the sign
convention alone. Adding a second debt-shaped type meant finding all nine, and
missing one produced a quietly wrong number rather than an exception.

So the behaviors are named here as traits, and the call sites ask about the
trait instead of the type. Adding a type is a row in `ACCOUNT_TYPES`; adding a
behavior is a field on `AccountTypeSpec` plus the sites that honor it.

The traits are deliberately independent. `credit_card` happens to set all three
today, but they answer different questions, and the first type to split them
apart is `loan`:

  is_liability
      Does this account represent money owed? Drives the sign convention —
      providers report debt as a positive number and every display site negates
      it — and the assets-vs-liabilities split in net worth.

  counts_pending_in_balance
      Is an authorized-but-uncleared row already real? True for a card, where a
      pending purchase is already owed and the bill will include it. False for a
      loan, whose balance moves only when a payment actually settles.

  has_billing_cycle
      Does this account bill in statement cycles? Drives `effective_date`
      bucketing, bill-vs-P&L totals, and the available-credit / cycle-date
      fields. A loan has a due date but no cycle: nothing accrues to a
      statement, so none of that machinery applies to it.

`metadata_fields` names the columns a type owns. Changing an account's type
clears the columns the new type does not own, so an account is never left half
credit-card. A future type that brings its own columns (loan terms — principal,
rate, maturity — being the obvious next one) lists them here and inherits that
behavior without touching the type-change path.
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AccountTypeSpec:
    key: str
    is_liability: bool = False
    counts_pending_in_balance: bool = False
    has_billing_cycle: bool = False
    metadata_fields: frozenset[str] = frozenset()


# Columns that only make sense on an account with a statement cycle. Kept as a
# name so the registry row reads as a statement about the type rather than a
# six-line literal.
_CARD_FIELDS = frozenset({
    "credit_limit",
    "statement_close_day",
    "payment_due_day",
    "minimum_payment",
    "card_brand",
    "card_level",
})

# Declaration order is the order the UI offers these in, so it is grouped by
# what the account *is* (cash, then debt, then holdings) rather than
# alphabetically.
ACCOUNT_TYPES: tuple[AccountTypeSpec, ...] = (
    AccountTypeSpec("checking"),
    AccountTypeSpec("savings"),
    AccountTypeSpec(
        "credit_card",
        is_liability=True,
        counts_pending_in_balance=True,
        has_billing_cycle=True,
        metadata_fields=_CARD_FIELDS,
    ),
    AccountTypeSpec("loan", is_liability=True),
    AccountTypeSpec("investment"),
    AccountTypeSpec("wallet"),
    # The catch-all. Present so a type this build doesn't recognize has a real
    # home rather than only the silent `_FALLBACK`, and because the agent-facing
    # MCP contract has advertised it as a filterable value since before the
    # registry existed (mcp_server/tools/transactions.py).
    AccountTypeSpec("other"),
)

_SPECS: dict[str, AccountTypeSpec] = {s.key: s for s in ACCOUNT_TYPES}

# Unknown types resolve to the plainest possible reading: an asset-shaped
# account with no special handling. `type` is a free-form column that predates
# this registry and is written by provider mappers, so a database can hold a
# value this build has never heard of. A KeyError here would turn one stale row
# into a 500 on the whole accounts list.
_FALLBACK = AccountTypeSpec("__unknown__")

ACCOUNT_TYPE_KEYS: tuple[str, ...] = tuple(s.key for s in ACCOUNT_TYPES)
DEFAULT_ACCOUNT_TYPE = "checking"

# Key sets for SQL `IN` clauses, where the filter runs inside the database and
# cannot call a Python predicate per row. Sorted tuples rather than sets so the
# rendered SQL is byte-stable across processes — set iteration order is not, and
# an unstable literal list defeats statement caching and makes query plans
# harder to compare between runs.
LIABILITY_TYPES: tuple[str, ...] = tuple(
    sorted(s.key for s in ACCOUNT_TYPES if s.is_liability)
)
PENDING_IN_BALANCE_TYPES: tuple[str, ...] = tuple(
    sorted(s.key for s in ACCOUNT_TYPES if s.counts_pending_in_balance)
)
BILLING_CYCLE_TYPES: tuple[str, ...] = tuple(
    sorted(s.key for s in ACCOUNT_TYPES if s.has_billing_cycle)
)

# Every column owned by some type — the set `clear_unowned_metadata` clears from.
ALL_METADATA_FIELDS: frozenset[str] = frozenset().union(
    *(s.metadata_fields for s in ACCOUNT_TYPES)
)


def spec(account_or_type: Any) -> AccountTypeSpec:
    """Resolve a spec from an account-like object or a bare type string.

    Accepts either so call sites can pass whatever they already hold — an
    `Account`, an `AccountCreate`, a provider's `AccountData`, or the string
    itself — without an attribute dance at each one.
    """
    key = getattr(account_or_type, "type", account_or_type)
    if not isinstance(key, str):
        return _FALLBACK
    return _SPECS.get(key, _FALLBACK)


def is_liability(account_or_type: Any) -> bool:
    """True when the account represents money owed rather than money held."""
    return spec(account_or_type).is_liability


def counts_pending_in_balance(account_or_type: Any) -> bool:
    """True when authorized-but-uncleared rows already belong in the balance."""
    return spec(account_or_type).counts_pending_in_balance


def has_billing_cycle(account_or_type: Any) -> bool:
    """True when the account bills in statement cycles (bucketing, bills, limit)."""
    return spec(account_or_type).has_billing_cycle


def balance_sign(account_or_type: Any) -> int:
    """The multiplier taking a stored balance to its displayed value.

    Liabilities are stored positive-for-debt (the convention every provider
    reports and `_simplefin_to_internal_balance` normalizes SimpleFIN into) and
    displayed negative. Everything else stores and displays the same number.
    """
    return -1 if is_liability(account_or_type) else 1


def metadata_fields(account_or_type: Any) -> frozenset[str]:
    """The type-owned columns this type is allowed to carry."""
    return spec(account_or_type).metadata_fields


def clear_unowned_metadata(account: Any) -> None:
    """Null every type-owned column the account's current type does not own.

    Safe to call unconditionally: it only clears fields outside the type's own
    set, so an account keeps exactly the metadata its type declares.
    """
    for name in ALL_METADATA_FIELDS - metadata_fields(account):
        setattr(account, name, None)
