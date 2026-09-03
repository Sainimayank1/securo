import type { ElementType } from 'react'
import {
  Building2,
  CircleDashed,
  CreditCard,
  HandCoins,
  PiggyBank,
  TrendingUp,
  Wallet,
} from 'lucide-react'

/**
 * The account-type registry — the frontend half of `app/core/account_types.py`.
 *
 * Same shape and the same reason: the type used to be compared against the
 * literal `'credit_card'` at every site that cared (the create dialog, the icon
 * tile, the import mapper, the balance coloring, the account detail page), so
 * adding a type meant finding all of them. Here a type is one row, and a site
 * asks about the trait it actually depends on.
 *
 * `keys` MUST stay in sync with ACCOUNT_TYPE_KEYS on the backend: the API
 * validates writes against its own list, so a type offered here but missing
 * there fails with a 422 at save time.
 *
 * Traits mirror the backend's, minus the ones only the server acts on
 * (`countsPendingInBalance` has no frontend consumer, so it isn't modeled):
 *
 *   isLiability      Money owed rather than money held. Drives balance coloring
 *                    and the assets-vs-liabilities split in reports.
 *   hasBillingCycle  Bills in statement cycles. Gates the credit-limit and
 *                    close/due-day fields in the dialog, the "due in N days"
 *                    hint, the bills feed, and the cycle-shaped default date
 *                    range on the account detail page.
 */
export interface AccountTypeSpec {
  key: string
  /** i18n key for the display name. */
  labelKey: string
  icon: ElementType
  /** Tailwind text color for the type tile. */
  color: string
  /** Tailwind background for the type tile. */
  bg: string
  isLiability: boolean
  hasBillingCycle: boolean
  /**
   * i18n key for the balance field's label in the create/edit dialog. Debt
   * accounts ask for the amount owed rather than a signed balance, because the
   * API stores liabilities positive-for-debt.
   */
  balanceLabelKey: string
  /** i18n key for the hint under that field, when the type needs one. */
  balanceHintKey?: string
}

// Declaration order is the order the create/edit dialog offers these in:
// grouped by what the account is (cash, then debt, then holdings), not
// alphabetically.
export const ACCOUNT_TYPES: readonly AccountTypeSpec[] = [
  {
    key: 'checking',
    labelKey: 'accounts.typeChecking',
    icon: Building2,
    color: 'text-indigo-600',
    bg: 'bg-indigo-100',
    isLiability: false,
    hasBillingCycle: false,
    balanceLabelKey: 'accounts.balance',
  },
  {
    key: 'savings',
    labelKey: 'accounts.typeSavings',
    icon: PiggyBank,
    color: 'text-emerald-600',
    bg: 'bg-emerald-100',
    isLiability: false,
    hasBillingCycle: false,
    balanceLabelKey: 'accounts.balance',
  },
  {
    key: 'credit_card',
    labelKey: 'accounts.typeCreditCard',
    icon: CreditCard,
    color: 'text-violet-600',
    bg: 'bg-violet-100',
    isLiability: true,
    hasBillingCycle: true,
    balanceLabelKey: 'accounts.balanceCreditCard',
    balanceHintKey: 'accounts.balanceCreditCardHint',
  },
  {
    key: 'loan',
    labelKey: 'accounts.typeLoan',
    icon: HandCoins,
    color: 'text-orange-600',
    bg: 'bg-orange-100',
    isLiability: true,
    hasBillingCycle: false,
    balanceLabelKey: 'accounts.balanceCreditCard',
    balanceHintKey: 'accounts.balanceLoanHint',
  },
  {
    key: 'investment',
    labelKey: 'accounts.typeInvestment',
    icon: TrendingUp,
    color: 'text-amber-600',
    bg: 'bg-amber-100',
    isLiability: false,
    hasBillingCycle: false,
    balanceLabelKey: 'accounts.balance',
  },
  {
    key: 'wallet',
    labelKey: 'accounts.typeWallet',
    icon: Wallet,
    color: 'text-rose-600',
    bg: 'bg-rose-100',
    isLiability: false,
    hasBillingCycle: false,
    balanceLabelKey: 'accounts.balance',
  },
  {
    key: 'other',
    labelKey: 'accounts.typeOther',
    icon: CircleDashed,
    color: 'text-slate-600',
    bg: 'bg-slate-100',
    isLiability: false,
    hasBillingCycle: false,
    balanceLabelKey: 'accounts.balance',
  },
] as const

export const DEFAULT_ACCOUNT_TYPE = 'checking'

const BY_KEY = new Map(ACCOUNT_TYPES.map((s) => [s.key, s]))

/**
 * Resolve a spec, falling back to checking for a type this build doesn't know.
 *
 * The column is free-form server-side and older databases can hold values that
 * predate the registry, so an unknown type has to render as *something* rather
 * than crash the accounts list.
 */
export function getAccountTypeSpec(type: string | null | undefined): AccountTypeSpec {
  return (type ? BY_KEY.get(type) : undefined) ?? BY_KEY.get(DEFAULT_ACCOUNT_TYPE)!
}

export function isLiabilityType(type: string | null | undefined): boolean {
  return getAccountTypeSpec(type).isLiability
}

export function hasBillingCycle(type: string | null | undefined): boolean {
  return getAccountTypeSpec(type).hasBillingCycle
}

/** i18n key → display name, for any site that only needs the label. */
export const ACCOUNT_TYPE_LABEL_KEYS: Record<string, string> = Object.fromEntries(
  ACCOUNT_TYPES.map((s) => [s.key, s.labelKey]),
)
