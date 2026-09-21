import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, CheckCircle2, FileText, Lock, Upload, X } from 'lucide-react';
import { useCallback, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';

import { ImportReviewTable, type RowFindings } from '@/components/import-review-table';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useAuth } from '@/contexts/auth-context';
import { useWorkspace } from '@/contexts/workspace-context';
import { useDateLocale, useDisplayLocale } from '@/hooks/use-display-locale';
import { getAccountName, sortAccountsByDisplayName } from '@/lib/account-utils';
import {
  accounts as accountsApi,
  categories as categoriesApi,
  categoryGroups as categoryGroupsApi,
  transactions as transactionsApi,
} from '@/lib/api';
import { formatCurrency } from '@/lib/format';
import { invalidateFinancialQueries } from '@/lib/invalidate-queries';
import type {
  ImportPreviewTransaction,
  ImportReviewTransaction,
  StatementImportPreview,
} from '@/types';

const ACCEPTED = '.pdf,.csv,.xls,.xlsx';

const TYPE_LABELS: Record<string, string> = {
  checking: 'accounts.typeChecking',
  savings: 'accounts.typeSavings',
  credit_card: 'accounts.typeCreditCard',
  investment: 'accounts.typeInvestment',
};

/** Warning codes that mean "nothing was imported and here is why". */
const BLOCKING_CODES = new Set([
  'unsupported_format',
  'unknown_provider',
  'password_required',
  'ocr_required',
  'malformed_file',
]);

function toReviewTransactions(txns: ImportPreviewTransaction[]): ImportReviewTransaction[] {
  return txns.map((tx, i) => ({
    ...tx,
    _id: `stmt-${i}`,
    excluded: false,
    selected_category_id: undefined,
  }));
}

function formatMoney(
  value: string | number | null | undefined,
  currency: string,
  locale: string,
): string {
  if (value === null || value === undefined) return '—';
  return formatCurrency(Number(value), currency, locale);
}

function formatPeriod(
  period: StatementImportPreview['period'],
  dateLocale: string,
): string | null {
  if (!period.start && !period.end) return null;
  const render = (iso?: string | null) => {
    if (!iso) return '…';
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(dateLocale);
  };
  return `${render(period.start)} – ${render(period.end)}`;
}

export function StatementImportPanel() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const { canWrite } = useWorkspace();
  const locale = useDisplayLocale();
  const dateLocale = useDateLocale();
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [preview, setPreview] = useState<StatementImportPreview | null>(null);
  const [reviewTransactions, setReviewTransactions] = useState<ImportReviewTransaction[]>([]);
  const [currentFile, setCurrentFile] = useState<File | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [selectedAccount, setSelectedAccount] = useState('');
  const [dragOver, setDragOver] = useState(false);
  const [password, setPassword] = useState('');
  const [passwordPrompt, setPasswordPrompt] = useState(false);

  const [searchQuery, setSearchQuery] = useState('');
  const [filterCategoryIds, setFilterCategoryIds] = useState<string[]>([]);
  const [filterUncategorized, setFilterUncategorized] = useState(false);
  const [statusFilter, setStatusFilter] = useState<'all' | 'included' | 'excluded'>('all');
  const [currentPage, setCurrentPage] = useState(1);

  const { data: accountsList } = useQuery({ queryKey: ['accounts'], queryFn: () => accountsApi.list() });
  const { data: categoriesList = [] } = useQuery({ queryKey: ['categories'], queryFn: categoriesApi.list });
  const { data: categoryGroupsList = [] } = useQuery({
    queryKey: ['category-groups'],
    queryFn: categoryGroupsApi.list,
  });

  const userCurrency = user?.preferences?.currency_display ?? 'USD';
  // A statement's own figures are in the statement's currency, not the
  // viewer's display currency: converting them would stop them matching the
  // paper the person is holding.
  const statementCurrency = preview?.account.currency ?? userCurrency;

  function reset() {
    setPreview(null);
    setReviewTransactions([]);
    setCurrentFile(null);
    setFileName(null);
    setSelectedAccount('');
    setPassword('');
    setSearchQuery('');
    setFilterCategoryIds([]);
    setFilterUncategorized(false);
    setStatusFilter('all');
    setCurrentPage(1);
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  const previewMutation = useMutation({
    mutationFn: ({
      file,
      options,
    }: {
      file: File;
      options?: { password?: string; account_id?: string; };
    }) => transactionsApi.previewStatementImport(file, options),
    onSuccess: (data) => {
      setPreview(data);
      setReviewTransactions(toReviewTransactions(data.transactions));
      setCurrentPage(1);
      const needsPassword = data.warnings.some((w) => w.code === 'password_required');
      setPasswordPrompt(needsPassword);
      if (data.account.suggested_account_id) {
        setSelectedAccount(data.account.suggested_account_id);
      }
    },
    onError: (error: unknown) => {
      const detail = (error as { response?: { data?: { detail?: string; }; }; })?.response?.data?.detail;
      const status = (error as { response?: { status?: number; }; })?.response?.status;
      if (detail === 'wrong_password') {
        setPasswordPrompt(true);
        toast.error(t('statementImport.warnings.wrong_password'));
        return;
      }
      if (status === 413) {
        toast.error(t('statementImport.tooLarge'));
        return;
      }
      toast.error(detail || t('import.processError'));
    },
  });

  const importMutation = useMutation({
    mutationFn: () => {
      const txns = reviewTransactions.map((rt) => ({
        description: rt.description,
        amount: rt.amount,
        date: rt.date,
        type: rt.type,
        currency: rt.currency ?? undefined,
        fx_rate: rt.fx_rate ?? undefined,
        payee_raw: rt.payee_raw ?? undefined,
        notes: rt.notes ?? undefined,
        category_name: rt.category_name ?? undefined,
        excluded: rt.excluded,
        category_id:
          rt.selected_category_id !== undefined
            ? (rt.selected_category_id ?? undefined)
            : (rt.suggested_category_id ?? undefined),
        force_uncategorized: rt.selected_category_id === null,
      }));
      // The same endpoint the standard import posts to: duplicate detection,
      // rules, payees, recurring matching and the import log all run there.
      return transactionsApi.import(
        selectedAccount,
        txns,
        fileName ?? '',
        preview?.detected_format ?? '',
      );
    },
    onSuccess: (data) => {
      invalidateFinancialQueries(queryClient);
      queryClient.invalidateQueries({ queryKey: ['import-logs'] });
      queryClient.invalidateQueries({ queryKey: ['payees'] });
      queryClient.invalidateQueries({ queryKey: ['categories'] });
      const skippedOrExcluded = (data.skipped ?? 0) > 0 || (data.excluded ?? 0) > 0;
      toast.success(
        skippedOrExcluded
          ? t('import.importedWithExcluded', {
            imported: data.imported,
            skipped: data.skipped ?? 0,
            excluded: data.excluded ?? 0,
          })
          : `${data.imported} ${t('import.transactionsImported')}`,
      );
      reset();
    },
    onError: (error: unknown) => {
      const detail = (error as { response?: { data?: { detail?: string; }; }; })?.response?.data?.detail;
      toast.error(detail || t('import.importError'));
    },
  });

  function processFile(file: File) {
    setFileName(file.name);
    setCurrentFile(file);
    setPassword('');
    setSelectedAccount('');
    previewMutation.mutate({ file });
  }

  /** Re-parse with a password, or against a newly chosen account.
   *  Duplicate detection needs a target account to compare against, so
   *  picking one asks the server again rather than guessing client-side. */
  const rePreview = useCallback(
    (options: { password?: string; account_id?: string; }) => {
      if (!currentFile) return;
      previewMutation.mutate({
        file: currentFile,
        options: {
          password: options.password ?? password ?? undefined,
          account_id: options.account_id ?? selectedAccount ?? undefined,
        },
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [currentFile, password, selectedAccount],
  );

  const handleToggleExcluded = useCallback((id: string) => {
    setReviewTransactions((prev) =>
      prev.map((tx) => (tx._id === id ? { ...tx, excluded: !tx.excluded } : tx)),
    );
  }, []);

  const handleChangeCategory = useCallback((id: string, categoryId: string | null) => {
    setReviewTransactions((prev) =>
      prev.map((tx) => (tx._id === id ? { ...tx, selected_category_id: categoryId } : tx)),
    );
  }, []);

  const rowFindings: RowFindings = useMemo(() => {
    const findings: RowFindings = {};
    for (const row of preview?.rows ?? []) {
      findings[`stmt-${row.index}`] = { warnings: row.warnings, duplicate: row.duplicate };
    }
    return findings;
  }, [preview]);

  const warnedCount = (preview?.rows ?? []).filter((r) => r.warnings.length > 0).length;
  const includedCount = reviewTransactions.filter((tx) => !tx.excluded).length;
  const blocking = (preview?.warnings ?? []).filter((w) => BLOCKING_CODES.has(w.code));
  const fileWarnings = (preview?.warnings ?? []).filter((w) => !BLOCKING_CODES.has(w.code));
  const period = preview ? formatPeriod(preview.period, dateLocale) : null;

  return (
    <div className="space-y-6">
      {canWrite && (
        <div
          className={`bg-card rounded-xl border-2 border-dashed transition-all cursor-pointer ${dragOver ? 'border-primary bg-primary/5' : 'border-border hover:border-border'
            }`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            const file = e.dataTransfer.files?.[0];
            if (file) processFile(file);
          }}
          onClick={() => !previewMutation.isPending && fileInputRef.current?.click()}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept={ACCEPTED}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) processFile(file);
            }}
            className="hidden"
          />
          <div className="flex flex-col items-center justify-center py-12 px-6 text-center">
            {previewMutation.isPending ? (
              <>
                <div className="w-12 h-12 rounded-full bg-primary/10 flex items-center justify-center mb-4 animate-pulse">
                  <FileText size={22} className="text-primary" />
                </div>
                <p className="text-sm font-semibold text-foreground">{t('import.processing')}</p>
                <p className="text-xs text-muted-foreground mt-1">{fileName}</p>
              </>
            ) : fileName && preview?.supported ? (
              <>
                <div className="w-12 h-12 rounded-full bg-emerald-100 flex items-center justify-center mb-4">
                  <CheckCircle2 size={22} className="text-emerald-500" />
                </div>
                <p className="text-sm font-semibold text-foreground">{fileName}</p>
                <p className="text-xs text-muted-foreground mt-1">
                  {t('import.previewInfo', {
                    count: preview.transactions.length,
                    format: preview.detected_format.toUpperCase(),
                  })}
                </p>
                <button
                  className="mt-3 text-xs text-muted-foreground hover:text-rose-500 transition-colors flex items-center gap-1"
                  onClick={(e) => {
                    e.stopPropagation();
                    reset();
                  }}
                >
                  <X size={12} /> {t('import.removeFile')}
                </button>
              </>
            ) : (
              <>
                <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center mb-4">
                  <Upload size={22} className="text-muted-foreground" />
                </div>
                <p className="text-sm font-semibold text-foreground mb-1">
                  {t('import.dragOrClick')}
                </p>
                <p className="text-xs text-muted-foreground">
                  {t('statementImport.acceptedFormats')}
                </p>
                <p className="text-xs text-muted-foreground mt-2 max-w-md">
                  {t('statementImport.noConversionNeeded')}
                </p>
              </>
            )}
          </div>
        </div>
      )}

      {/* A file we could not read at all: say which of the reasons it was. */}
      {blocking.length > 0 && (
        <div className="bg-card rounded-xl border border-amber-200 shadow-sm p-5 space-y-3">
          {blocking.map((warning) => (
            <div key={warning.code} className="flex items-start gap-2 text-sm text-amber-700">
              <AlertCircle size={16} className="shrink-0 mt-0.5" />
              <div>
                <p className="font-medium">{t(`statementImport.warnings.${warning.code}`)}</p>
                <p className="text-xs text-amber-600 mt-0.5">
                  {t(`statementImport.hints.${warning.code}`, { defaultValue: '' })}
                </p>
              </div>
            </div>
          ))}
          {blocking.some((w) => w.code === 'password_required') && (
            <Button size="sm" variant="outline" className="gap-2" onClick={() => setPasswordPrompt(true)}>
              <Lock size={14} />
              {t('statementImport.enterPassword')}
            </Button>
          )}
        </div>
      )}

      {preview?.supported && (
        <div className="bg-card rounded-xl border border-border shadow-sm overflow-hidden">
          {/* What we recognised */}
          <div className="px-5 py-4 border-b border-border grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
            <Detail label={t('statementImport.detectedProvider')}>
              {preview.account.institution ?? t(`statementImport.providers.${preview.provider}`)}
            </Detail>
            <Detail label={t('statementImport.statementType')}>
              {t(`statementImport.types.${preview.statement_type}`)}
            </Detail>
            <Detail label={t('statementImport.period')}>{period ?? '—'}</Detail>
            <Detail label={t('statementImport.account')}>
              {preview.account.masked_number ? `•••• ${preview.account.masked_number}` : '—'}
            </Detail>
            <Detail label={t('statementImport.currency')}>{preview.account.currency ?? '—'}</Detail>
          </div>

          {/* Parsing summary */}
          <div className="px-5 py-4 border-b border-border flex flex-wrap items-center gap-x-6 gap-y-2 text-xs">
            <Stat
              label={t('statementImport.transactionsFound')}
              value={preview.transactions.length}
            />
            <Stat
              label={t('statementImport.valid')}
              value={preview.transactions.length - warnedCount}
              tone="text-emerald-600"
            />
            <Stat label={t('statementImport.withWarnings')} value={warnedCount} tone="text-amber-600" />
            <Stat
              label={t('statementImport.duplicates')}
              value={preview.duplicate_count}
              tone="text-sky-600"
            />
          </div>

          {/* Balance check. Deliberately not called "reconciliation": that word
              already means invoice matching elsewhere in Securo. */}
          {preview.balance.matches !== null && preview.balance.matches !== undefined && (
            <div className="px-5 py-4 border-b border-border">
              <div className="flex items-center gap-2 mb-3">
                <p className="text-xs font-medium text-muted-foreground">
                  {t('statementImport.balanceCheck')}
                </p>
                <span
                  className={`text-xs px-2 py-0.5 rounded ${preview.balance.matches
                      ? 'bg-emerald-50 text-emerald-700'
                      : 'bg-amber-50 text-amber-700'
                    }`}
                >
                  {preview.balance.matches
                    ? t('statementImport.balanceMatches')
                    : t('statementImport.balanceDiffers')}
                </span>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
                <Detail label={t('statementImport.openingBalance')}>
                  {formatMoney(preview.balance.opening, statementCurrency, locale)}
                </Detail>
                <Detail label={t('statementImport.movement')}>
                  {formatMoney(preview.balance.total, statementCurrency, locale)}
                </Detail>
                <Detail label={t('statementImport.expectedClosing')}>
                  {formatMoney(preview.balance.expected_closing, statementCurrency, locale)}
                </Detail>
                <Detail label={t('statementImport.statementClosing')}>
                  {formatMoney(preview.balance.statement_closing, statementCurrency, locale)}
                </Detail>
                <Detail label={t('statementImport.difference')}>
                  {formatMoney(preview.balance.difference, statementCurrency, locale)}
                </Detail>
              </div>
            </div>
          )}

          {/* File-level warnings that do not stop the import */}
          {fileWarnings.length > 0 && (
            <div className="px-5 py-4 border-b border-border space-y-2">
              {fileWarnings.map((warning, index) => (
                <div
                  key={`${warning.code}-${index}`}
                  className="flex items-start gap-2 text-xs text-amber-700 bg-amber-50 border border-amber-200 px-3 py-2 rounded-lg"
                >
                  <AlertCircle size={14} className="shrink-0 mt-0.5" />
                  <span>
                    {t(`statementImport.warnings.${warning.code}`)}
                    {warning.row ? ` · ${t('import.lineNumber')} ${warning.row}` : ''}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* Account */}
          <div className="px-5 py-4 border-b border-border">
            <div className="flex flex-wrap items-center gap-3">
              <Label className="text-sm text-muted-foreground">{t('import.importTo')}</Label>
              <select
                className="flex-1 min-w-[200px] border border-border rounded-lg px-3 py-1.5 text-sm bg-card text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                value={selectedAccount}
                onChange={(e) => {
                  setSelectedAccount(e.target.value);
                  if (e.target.value) rePreview({ account_id: e.target.value });
                }}
                disabled={!canWrite}
              >
                <option value="">{t('import.selectAccount')}</option>
                {sortAccountsByDisplayName(accountsList ?? []).map((acc) => (
                  <option key={acc.id} value={acc.id}>
                    {getAccountName(acc)} ({t(TYPE_LABELS[acc.type] || acc.type)})
                  </option>
                ))}
              </select>
              {!selectedAccount && (
                <div className="flex items-center gap-1.5 text-xs text-amber-600 bg-amber-50 border border-amber-100 px-2.5 py-1.5 rounded-lg shrink-0">
                  <AlertCircle size={12} />
                  {t('import.selectAccountWarning')}
                </div>
              )}
            </div>
            {preview.account.suggested_account_id === selectedAccount && selectedAccount && (
              <p className="text-xs text-muted-foreground mt-2">
                {t('statementImport.accountMatched', {
                  masked: preview.account.masked_number ?? '',
                })}
              </p>
            )}
          </div>

          <ImportReviewTable
            transactions={reviewTransactions}
            rowFindings={rowFindings}
            categories={categoriesList}
            groups={categoryGroupsList}
            userCurrency={statementCurrency}
            locale={locale}
            dateLocale={dateLocale}
            searchQuery={searchQuery}
            filterCategoryIds={filterCategoryIds}
            filterUncategorized={filterUncategorized}
            statusFilter={statusFilter}
            currentPage={currentPage}
            onToggleExcluded={handleToggleExcluded}
            onChangeCategory={handleChangeCategory}
            onSearchChange={setSearchQuery}
            onCategoryIdsChange={setFilterCategoryIds}
            onUncategorizedChange={setFilterUncategorized}
            onStatusFilterChange={setStatusFilter}
            onPageChange={setCurrentPage}
          />

          <div className="px-4 sm:px-5 py-4 border-t border-border flex items-center justify-between">
            <button
              className="text-sm text-muted-foreground hover:text-foreground transition-colors"
              onClick={reset}
            >
              {t('common.cancel')}
            </button>
            <Button
              onClick={() => importMutation.mutate()}
              disabled={
                !canWrite || !selectedAccount || importMutation.isPending || includedCount === 0
              }
              className="gap-2"
            >
              <Upload size={14} />
              {importMutation.isPending
                ? t('common.loading')
                : t('import.importButton', { count: includedCount })}
            </Button>
          </div>
        </div>
      )}

      <Dialog open={passwordPrompt} onOpenChange={setPasswordPrompt}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="text-base font-semibold">
              {t('statementImport.passwordTitle')}
            </DialogTitle>
            <DialogDescription className="text-xs text-muted-foreground mt-1">
              {t('statementImport.passwordDesc')}
            </DialogDescription>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setPasswordPrompt(false);
              rePreview({ password });
            }}
          >
            <Input
              type="password"
              autoFocus
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={t('statementImport.passwordPlaceholder')}
              aria-label={t('statementImport.passwordPlaceholder')}
            />
            <DialogFooter className="mt-4">
              <Button type="submit" size="sm" disabled={!password}>
                {t('statementImport.unlock')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Detail({ label, children }: { label: string; children: React.ReactNode; }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="text-sm font-medium text-foreground mt-0.5 tabular-nums">{children}</p>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: string; }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`font-semibold tabular-nums ${tone ?? 'text-foreground'}`}>{value}</span>
      <span className="text-muted-foreground">{label}</span>
    </span>
  );
}
