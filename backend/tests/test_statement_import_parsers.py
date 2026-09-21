"""Provider adapters, read against fixture statements.

The fixtures reproduce the layout of real SBI, Axis Bank and SuperMoney
exports — the same columns, the same header and footer blocks, the same
hard-wrapped narration — with invented identity data. See
`tests/fixtures/statements/build_fixtures.py`.
"""
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.statement_import import detector, normalizer, registry, validator
from app.services.statement_import.models import (
    WARN_AMBIGUOUS_DIRECTION,
    WARN_BALANCE_MISMATCH,
    WARN_MIXED_SOURCE_ACCOUNTS,
    WARN_NON_SUCCESS_SKIPPED,
)
from app.services.statement_import.parsers.base import dewrap_cell, last4, parse_statement_date

FIXTURES = Path(__file__).parent / "fixtures" / "statements"


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def parse(name: str, password: str | None = None):
    """Run a fixture through the pipeline the API uses."""
    detection, source = detector.detect(name, read(name), password)
    assert source is not None, f"{name} was not recognised: {detection.reason}"
    parser = registry.select_parser(source)
    assert parser is not None
    return validator.validate(parser.parse(source))


class TestSbiXlsx:
    def test_detects_the_bank_and_the_account(self):
        statement = parse("sbi_statement.xlsx")
        assert statement.detection.provider == "sbi"
        assert statement.detection.file_format == "xlsx"
        assert statement.detection.statement_type == "bank_account"
        assert statement.account.masked_number == "4312"
        assert statement.account.currency == "INR"
        assert statement.account.account_type == "savings"
        assert str(statement.period.start) == "2026-09-01"
        assert str(statement.period.end) == "2026-09-14"

    def test_debits_are_negative_and_credits_positive(self):
        statement = parse("sbi_statement.xlsx")
        assert len(statement.transactions) == 6
        assert statement.transactions[0].amount == Decimal("-2500.00")
        assert statement.transactions[3].amount == Decimal("15000.00")
        assert all(t.currency == "INR" for t in statement.transactions)
        assert all(t.fx_rate == Decimal("1") for t in statement.transactions)

    def test_multiline_descriptions_are_rejoined_without_inventing_a_space(self):
        """SBI wraps the narration mid-token and indents the continuation.

        Joining on a space would turn "…/z" + " erodha.ic" into "z erodha.ic"
        and a 10-digit phone number into two numbers.
        """
        statement = parse("sbi_statement.xlsx")
        assert "zerodha.ic" in statement.transactions[0].description
        assert "z erodha" not in statement.transactions[0].description
        assert "8814934380" in statement.transactions[3].description
        assert "624710895434SBIUNIPAYDBCard" in statement.transactions[4].description

    def test_running_balance_reconciles(self):
        statement = parse("sbi_statement.xlsx")
        assert statement.balance.opening == Decimal("36677.42")
        assert statement.balance.total == Decimal("-13367.52")
        assert statement.balance.expected_closing == Decimal("23309.90")
        assert statement.balance.statement_closing == Decimal("23309.90")
        assert statement.balance.difference == Decimal("0.00")
        assert statement.balance.matches is True
        assert not any(WARN_BALANCE_MISMATCH in t.warnings for t in statement.transactions)

    def test_a_broken_balance_chain_warns_on_the_rows_it_breaks_at(self):
        """The amount is never silently corrected to make the chain work."""
        statement = parse("sbi_statement_unbalanced.xlsx")
        flagged = [t for t in statement.transactions if WARN_BALANCE_MISMATCH in t.warnings]
        assert len(flagged) == 2
        # The amounts are still exactly what the bank printed.
        assert flagged[0].amount == Decimal("-15000.00")

    def test_reference_column_is_preserved_in_notes(self):
        statement = parse("sbi_statement.xlsx")
        imported = normalizer.to_transaction_imports(statement.transactions)
        assert imported[5].notes == "Ref CHQ001234"
        assert imported[0].notes is None

    def test_password_protected_workbook_is_named_not_just_refused(self):
        detection, source = detector.detect(
            "sbi_statement_encrypted.xlsx", read("sbi_statement_encrypted.xlsx")
        )
        assert detection.supported is False
        assert detection.reason == "password_required"
        assert source is None

    def test_password_protected_workbook_parses_with_the_password(self):
        statement = parse("sbi_statement_encrypted.xlsx", password="fixture-password")
        assert statement.detection.provider == "sbi"
        assert len(statement.transactions) == 6
        assert statement.balance.matches is True

    def test_wrong_password_is_reported_as_such(self):
        from app.services.statement_import.readers import StatementReadError

        with pytest.raises(StatementReadError) as excinfo:
            detector.detect(
                "sbi_statement_encrypted.xlsx",
                read("sbi_statement_encrypted.xlsx"),
                password="not-the-password",
            )
        assert excinfo.value.code == "wrong_password"


class TestAxisCsv:
    def test_detects_the_bank_and_the_account(self):
        statement = parse("axis_statement.csv")
        assert statement.detection.provider == "axis"
        assert statement.account.masked_number == "3852"
        assert statement.account.currency == "INR"
        assert str(statement.period.start) == "2026-09-01"
        assert str(statement.period.end) == "2026-09-04"

    def test_direction_comes_from_the_balance_not_the_column_heading(self):
        """This export's DR column carries money *in*.

        Reading the headings literally would invert the sign of every row in
        the file, so the running balance decides. The salary credit sits under
        DR and has to come out positive; the merchant payment sits under CR
        and has to come out negative.
        """
        statement = parse("axis_statement.csv")
        salary, merchant = statement.transactions[0], statement.transactions[1]
        assert salary.amount == Decimal("101965.00")
        assert "EXAMPLE EMPLOYER" in salary.description
        assert merchant.amount == Decimal("-5000.00")
        assert "EXAMPLE MERCHANT" in merchant.description

    def test_a_conventional_export_is_read_the_conventional_way(self):
        """The orientation is detected per file, not hard-coded per bank."""
        statement = parse("axis_statement_conventional.csv")
        assert statement.transactions[0].amount == Decimal("-2500.00")
        assert statement.transactions[1].amount == Decimal("15000.00")
        assert not any(
            WARN_AMBIGUOUS_DIRECTION in t.warnings for t in statement.transactions
        )

    def test_an_unresolvable_direction_is_flagged_rather_than_guessed(self):
        statement = parse("axis_statement_ambiguous.csv")
        assert any(w.code == WARN_AMBIGUOUS_DIRECTION for w in statement.warnings)
        assert all(WARN_AMBIGUOUS_DIRECTION in t.warnings for t in statement.transactions)

    def test_dates_are_normalized_and_the_footer_is_not_imported(self):
        statement = parse("axis_statement.csv")
        assert len(statement.transactions) == 5
        assert str(statement.transactions[0].date) == "2026-09-03"
        assert str(statement.transactions[-1].date) == "2026-09-04"
        assert not any("REGISTERED OFFICE" in t.description for t in statement.transactions)
        assert not any("Legends" in t.description for t in statement.transactions)

    def test_balance_check_derives_the_opening_the_bank_omits(self):
        statement = parse("axis_statement.csv")
        assert statement.balance.opening == Decimal("5809.02")
        assert statement.balance.statement_closing == Decimal("26855.69")
        assert statement.balance.matches is True


class TestAxisMyZonePdf:
    def test_detects_the_card(self):
        statement = parse("axis_myzone_statement.pdf")
        assert statement.detection.provider == "axis_myzone"
        assert statement.detection.statement_type == "credit_card"
        assert statement.account.masked_number == "3649"
        assert statement.account.account_type == "credit_card"
        assert str(statement.period.start) == "2026-08-14"
        assert str(statement.period.end) == "2026-09-12"

    def test_credits_are_positive_and_debits_negative(self):
        statement = parse("axis_myzone_statement.pdf")
        by_description = {t.description: t.amount for t in statement.transactions}
        assert by_description["BBPS PAYMENT RECEIVED - SB4162260C707D3AM9CF"] == Decimal("3793.03")
        assert by_description["EMI INTEREST - 16/18, REF# 56382477"] == Decimal("-134.86")
        assert by_description["GST"] == Decimal("-24.28")
        assert by_description["DISTRICT MOVIE TICKET R"] == Decimal("-497.04")

    def test_only_the_transaction_table_is_imported(self):
        """Everything else on those pages that carries a number stays out."""
        statement = parse("axis_myzone_statement.pdf")
        assert len(statement.transactions) == 6
        amounts = {abs(t.amount) for t in statement.transactions}

        # Payment summary
        assert Decimal("4043.00") not in amounts   # Total Payment Due
        assert Decimal("3498.00") not in amounts   # Minimum Payment Due
        assert Decimal("3883.86") not in amounts   # Purchase total
        assert Decimal("159.14") not in amounts    # Other Debit & Charges
        assert Decimal("470000.00") not in amounts  # Credit limit
        # Previous Balance shares its value with the real payment row, so
        # check it was not read twice rather than that it is absent.
        assert sum(1 for t in statement.transactions if t.amount == Decimal("3793.03")) == 1

        # EMI balances, finance-charge worked example, schedule of charges,
        # minimum-amount-due example, and a dated footer line after the table.
        for excluded in ("6923.39", "36.99", "893.84", "1265.11", "175", "5000", "8813.65", "999.99"):
            assert Decimal(excluded) not in amounts

    def test_reads_both_pages_without_importing_the_second(self):
        statement = parse("axis_myzone_statement.pdf")
        assert {t.source_page for t in statement.transactions} == {1}

    def test_merchant_category_is_kept_out_of_the_description(self):
        statement = parse("axis_myzone_statement.pdf")
        food = next(t for t in statement.transactions if t.amount == Decimal("-60.00"))
        assert food.description == "UPI/RAM DHAN DAIRY FARM/MAB.037215038290019@AXI"
        assert food.metadata["merchant_category"] == "FOOD PRODUCTS"

    def test_payment_summary_is_used_as_an_integrity_check(self):
        """The bank's own totals confirm the right rows were extracted."""
        statement = parse("axis_myzone_statement.pdf")
        assert statement.balance.opening == Decimal("3793.03")
        assert statement.balance.total == Decimal("249.97")
        assert statement.balance.statement_closing == Decimal("4043.00")
        assert statement.balance.matches is True
        assert statement.warnings == []


class TestSuperMoneyPdf:
    def test_imports_only_successful_rows(self):
        statement = parse("supermoney_history.pdf")
        assert len(statement.transactions) == 4
        assert not any("FAILED" in t.description for t in statement.transactions)
        assert not any("PENDING" in t.description for t in statement.transactions)
        assert any(w.code == WARN_NON_SUCCESS_SKIPPED for w in statement.warnings)

    def test_signed_amounts_are_preserved(self):
        statement = parse("supermoney_history.pdf")
        assert statement.transactions[0].amount == Decimal("-27.00")
        assert statement.transactions[2].amount == Decimal("-5000.00")
        assert statement.transactions[3].amount == Decimal("150.00")

    def test_dates_are_normalized_from_their_long_form(self):
        statement = parse("supermoney_history.pdf")
        assert str(statement.transactions[0].date) == "2026-09-03"
        assert str(statement.transactions[3].date) == "2026-09-04"
        assert str(statement.period.start) == "2026-09-01"
        assert str(statement.period.end) == "2026-09-04"

    def test_currency_is_inr_at_par(self):
        statement = parse("supermoney_history.pdf")
        assert all(t.currency == "INR" and t.fx_rate == Decimal("1") for t in statement.transactions)

    def test_a_single_funding_account_is_offered_for_matching(self):
        statement = parse("supermoney_history.pdf")
        assert statement.account.masked_number == "3852"
        assert not any(w.code == WARN_MIXED_SOURCE_ACCOUNTS for w in statement.warnings)

    def test_a_file_spanning_two_funding_accounts_is_flagged_and_matches_nothing(self):
        statement = parse("supermoney_history_mixed.pdf")
        assert len(statement.transactions) == 2
        assert any(w.code == WARN_MIXED_SOURCE_ACCOUNTS for w in statement.warnings)
        # No account is suggested: every row would otherwise land in one.
        assert statement.account.masked_number is None


class TestGenericCsv:
    def test_a_standard_securo_csv_still_behaves_as_it_always_did(self):
        """The statement page delegates to the existing CSV importer."""
        from app.services import import_service

        statement = parse("securo_standard.csv")
        assert statement.detection.provider == "generic"
        direct, failed = import_service.parse_csv(read("securo_standard.csv"))

        assert len(statement.transactions) == len(direct) == 2
        for parsed, expected in zip(statement.transactions, direct):
            assert parsed.description == expected.description
            assert abs(parsed.amount) == expected.amount
            assert parsed.transaction_type == expected.type
            assert parsed.date == expected.date

    def test_unparsable_rows_are_reported_with_the_existing_codes(self):
        statement = parse("securo_standard.csv")
        assert [w.code for w in statement.warnings] == ["invalid_date"]


class TestHelpers:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("01/09/2026", "2026-09-01"),
            ("03-09-2026", "2026-09-03"),
            ("2026-09-03", "2026-09-03"),
            ("3 September 2026", "2026-09-03"),
            ("3 Sep 2026", "2026-09-03"),
        ],
    )
    def test_dates_normalize_to_iso(self, raw, expected):
        assert str(parse_statement_date(raw)) == expected

    def test_an_unreadable_date_is_none_rather_than_a_guess(self):
        assert parse_statement_date("not a date") is None
        assert parse_statement_date("") is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("653046******3649", "3649"),
            ("XXXXXX4023", "4023"),
            ("00000004312", "4312"),
            ("12", None),
        ],
    )
    def test_only_the_last_four_characters_survive(self, raw, expected):
        assert last4(raw) == expected

    def test_dewrap_joins_an_indented_continuation_without_a_separator(self):
        assert dewrap_cell(" ABC/z\n erodha.ic/DEF") == "ABC/zerodha.ic/DEF"

    def test_dewrap_keeps_a_space_between_genuinely_separate_lines(self):
        assert dewrap_cell("FIRST LINE\nSECOND LINE") == "FIRST LINE SECOND LINE"
