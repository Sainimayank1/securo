import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

import { StatementImportPanel } from '@/components/statement-import-panel'
import { renderWithProviders, t } from '@/test/utils'
import type { StatementImportPreview } from '@/types'

const api = vi.hoisted(() => ({
  transactions: { previewStatementImport: vi.fn(), import: vi.fn() },
  accounts: { list: vi.fn() },
  categories: { list: vi.fn() },
  categoryGroups: { list: vi.fn() },
}))

vi.mock('@/lib/api', () => ({
  transactions: api.transactions,
  accounts: api.accounts,
  categories: api.categories,
  categoryGroups: api.categoryGroups,
}))

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }),
}))

vi.mock('@/contexts/workspace-context', () => ({
  useWorkspace: () => ({ canWrite: true }),
}))

const ACCOUNT_ID = '11111111-1111-1111-1111-111111111111'

function preview(overrides: Partial<StatementImportPreview> = {}): StatementImportPreview {
  return {
    supported: true,
    detected_format: 'xlsx',
    provider: 'sbi',
    statement_type: 'bank_account',
    confidence: 1,
    account: {
      masked_number: '4312',
      currency: 'INR',
      account_type: 'savings',
      institution: 'State Bank of India',
      suggested_account_id: ACCOUNT_ID,
      candidate_account_ids: [ACCOUNT_ID],
    },
    period: { start: '2026-09-01', end: '2026-09-14' },
    transactions: [
      { description: 'WDL TFR UPI/DR/624442236850', amount: 2500.00, date: '2026-09-01', type: 'debit', currency: 'INR' },
      { description: 'DEP TFR UPI/CR/661290123852', amount: 15000.00, date: '2026-09-03', type: 'credit', currency: 'INR' },
    ],
    rows: [
      { index: 0, warnings: [], duplicate: false, source_row: 19 },
      { index: 1, warnings: ['balance_mismatch'], duplicate: false, source_row: 22 },
    ],
    warnings: [],
    balance: {
      opening: '36677.42',
      total: '12500.00',
      expected_closing: '49177.42',
      statement_closing: '49177.42',
      difference: '0.00',
      matches: true,
    },
    duplicate_count: 0,
    ...overrides,
  }
}

async function dropFile(name = 'statement.xlsx') {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement
  const file = new File(['x'], name)
  Object.defineProperty(input, 'files', { value: [file], configurable: true })
  input.dispatchEvent(new Event('change', { bubbles: true }))
  return file
}

beforeEach(() => {
  vi.clearAllMocks()
  api.accounts.list.mockResolvedValue([
    { id: ACCOUNT_ID, name: 'SBI Savings', type: 'savings', currency: 'INR', balance: 0 },
  ])
  api.categories.list.mockResolvedValue([])
  api.categoryGroups.list.mockResolvedValue([])
})

describe('StatementImportPanel', () => {
  it('advertises the formats a bank actually hands you', () => {
    renderWithProviders(<StatementImportPanel />)
    expect(screen.getByText(t('statementImport.acceptedFormats'))).toBeInTheDocument()
    const input = document.querySelector('input[type="file"]')
    expect(input).toHaveAttribute('accept', '.pdf,.csv,.xls,.xlsx')
  })

  it('shows what the file was detected as', async () => {
    api.transactions.previewStatementImport.mockResolvedValue(preview())
    renderWithProviders(<StatementImportPanel />)
    await dropFile()

    await waitFor(() => {
      expect(screen.getByText('State Bank of India')).toBeInTheDocument()
    })
    expect(screen.getByText(t('statementImport.types.bank_account'))).toBeInTheDocument()
    expect(screen.getByText('•••• 4312')).toBeInTheDocument()
    expect(screen.getByText('INR')).toBeInTheDocument()
  })

  it('reports the balance check', async () => {
    api.transactions.previewStatementImport.mockResolvedValue(preview())
    renderWithProviders(<StatementImportPanel />)
    await dropFile()

    await waitFor(() => {
      expect(screen.getByText(t('statementImport.balanceMatches'))).toBeInTheDocument()
    })
    expect(screen.getByText(t('statementImport.openingBalance'))).toBeInTheDocument()
    expect(screen.getByText(t('statementImport.difference'))).toBeInTheDocument()
  })

  it('says so when the statement does not add up', async () => {
    api.transactions.previewStatementImport.mockResolvedValue(
      preview({
        balance: {
          opening: '36677.42',
          total: '12500.00',
          expected_closing: '49177.42',
          statement_closing: '49177.99',
          difference: '0.57',
          matches: false,
        },
      }),
    )
    renderWithProviders(<StatementImportPanel />)
    await dropFile()

    await waitFor(() => {
      expect(screen.getByText(t('statementImport.balanceDiffers'))).toBeInTheDocument()
    })
  })

  it('surfaces a row warning rather than hiding it', async () => {
    api.transactions.previewStatementImport.mockResolvedValue(preview())
    renderWithProviders(<StatementImportPanel />)
    await dropFile()

    await waitFor(() => {
      expect(
        screen.getByText(t('statementImport.warnings.balance_mismatch')),
      ).toBeInTheDocument()
    })
  })

  it('marks rows the import will skip as duplicates', async () => {
    api.transactions.previewStatementImport.mockResolvedValue(
      preview({
        duplicate_count: 1,
        rows: [
          { index: 0, warnings: [], duplicate: true, source_row: 19 },
          { index: 1, warnings: [], duplicate: false, source_row: 22 },
        ],
      }),
    )
    renderWithProviders(<StatementImportPanel />)
    await dropFile()

    await waitFor(() => {
      expect(screen.getByText(t('statementImport.warnings.duplicate'))).toBeInTheDocument()
    })
  })

  it('preselects the account the statement matched', async () => {
    api.transactions.previewStatementImport.mockResolvedValue(preview())
    renderWithProviders(<StatementImportPanel />)
    await dropFile()

    await waitFor(() => {
      expect(screen.getByText(t('import.importTo'))).toBeInTheDocument()
    })
    const select = document.querySelector('select') as HTMLSelectElement
    expect(select.value).toBe(ACCOUNT_ID)
  })

  it('never preselects an account when the match is not certain', async () => {
    api.transactions.previewStatementImport.mockResolvedValue(
      preview({
        account: {
          masked_number: '4312',
          currency: 'INR',
          account_type: 'savings',
          institution: 'State Bank of India',
          suggested_account_id: null,
          candidate_account_ids: [],
        },
        warnings: [{ code: 'no_account_match', row: null, detail: '' }],
      }),
    )
    renderWithProviders(<StatementImportPanel />)
    await dropFile()

    await waitFor(() => {
      expect(
        screen.getByText(t('statementImport.warnings.no_account_match')),
      ).toBeInTheDocument()
    })
    const select = document.querySelector('select') as HTMLSelectElement
    expect(select.value).toBe('')
    expect(screen.getByText(t('import.selectAccountWarning'))).toBeInTheDocument()
  })

  it('explains an unreadable file instead of showing an empty preview', async () => {
    api.transactions.previewStatementImport.mockResolvedValue(
      preview({
        supported: false,
        provider: 'unknown',
        transactions: [],
        rows: [],
        warnings: [{ code: 'ocr_required', row: null, detail: '' }],
      }),
    )
    renderWithProviders(<StatementImportPanel />)
    await dropFile('scan.pdf')

    await waitFor(() => {
      expect(screen.getByText(t('statementImport.warnings.ocr_required'))).toBeInTheDocument()
    })
    expect(screen.getByText(t('statementImport.hints.ocr_required'))).toBeInTheDocument()
    expect(screen.queryByText(t('statementImport.balanceCheck'))).not.toBeInTheDocument()
  })

  it('asks for a password and re-parses with it, without keeping it', async () => {
    api.transactions.previewStatementImport.mockResolvedValueOnce(
      preview({
        supported: false,
        provider: 'unknown',
        transactions: [],
        rows: [],
        warnings: [{ code: 'password_required', row: null, detail: '' }],
      }),
    )
    const { user } = renderWithProviders(<StatementImportPanel />)
    await dropFile()

    const field = await screen.findByLabelText(t('statementImport.passwordPlaceholder'))
    api.transactions.previewStatementImport.mockResolvedValueOnce(preview())
    await user.type(field, 'hunter2')
    await user.click(screen.getByRole('button', { name: t('statementImport.unlock') }))

    await waitFor(() => {
      expect(api.transactions.previewStatementImport).toHaveBeenLastCalledWith(
        expect.any(File),
        expect.objectContaining({ password: 'hunter2' }),
      )
    })
  })

  it('posts the parsed rows to the existing import endpoint', async () => {
    api.transactions.previewStatementImport.mockResolvedValue(preview())
    api.transactions.import.mockResolvedValue({
      imported: 2,
      skipped: 0,
      excluded: 0,
      import_log_id: 'log-1',
    })
    const { user } = renderWithProviders(<StatementImportPanel />)
    await dropFile()

    const button = await screen.findByRole('button', {
      name: t('import.importButton', { count: 2 }),
    })
    await user.click(button)

    await waitFor(() => {
      expect(api.transactions.import).toHaveBeenCalledWith(
        ACCOUNT_ID,
        expect.arrayContaining([
          expect.objectContaining({ description: 'WDL TFR UPI/DR/624442236850', type: 'debit' }),
        ]),
        'statement.xlsx',
        'xlsx',
      )
    })
  })

  it('will not import until an account is chosen', async () => {
    api.transactions.previewStatementImport.mockResolvedValue(
      preview({
        account: {
          masked_number: null,
          currency: 'INR',
          account_type: null,
          institution: null,
          suggested_account_id: null,
          candidate_account_ids: [],
        },
      }),
    )
    renderWithProviders(<StatementImportPanel />)
    await dropFile()

    const button = await screen.findByRole('button', {
      name: t('import.importButton', { count: 2 }),
    })
    expect(button).toBeDisabled()
  })
})
