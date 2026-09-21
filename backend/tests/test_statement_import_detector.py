"""Detection and the guards around it.

An uploaded statement is untrusted input that the app then expands: a
spreadsheet is a zip, a PDF is a tree of objects, and both can be built to
cost far more to read than to send. These tests pin the refusals.
"""
import io
import zipfile
from pathlib import Path

import pytest

from app.services.statement_import import detector, readers
from app.services.statement_import.models import (
    FORMAT_CSV,
    FORMAT_ENCRYPTED_OFFICE,
    FORMAT_HTML,
    FORMAT_PDF,
    FORMAT_UNKNOWN,
    FORMAT_XLSX,
)

FIXTURES = Path(__file__).parent / "fixtures" / "statements"


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class TestFormatSniffing:
    def test_formats_are_read_from_the_bytes(self):
        assert readers.sniff_format(read("sbi_statement.xlsx")) == FORMAT_XLSX
        assert readers.sniff_format(read("axis_statement.csv")) == FORMAT_CSV
        assert readers.sniff_format(read("axis_myzone_statement.pdf")) == FORMAT_PDF
        assert readers.sniff_format(read("sbi_statement_encrypted.xlsx")) == FORMAT_ENCRYPTED_OFFICE

    def test_the_filename_is_not_trusted(self):
        """A spreadsheet named .csv is still a spreadsheet.

        The name comes from the uploader. Dispatching on it would let a file
        be parsed by the wrong reader simply by being renamed.
        """
        detection, _ = detector.detect("totally-a-csv.csv", read("sbi_statement.xlsx"))
        assert detection.file_format == FORMAT_XLSX
        assert detection.provider == "sbi"

    def test_a_statement_with_no_extension_is_still_recognised(self):
        detection, _ = detector.detect("", read("axis_myzone_statement.pdf"))
        assert detection.provider == "axis_myzone"

    def test_html_saved_as_a_spreadsheet_is_named_not_mis_parsed(self):
        """Several banks mail an HTML table with an .xls name."""
        html = b"<html><body><table><tr><td>01/09/2026</td><td>2500.00</td></tr></table></body></html>"
        assert readers.sniff_format(html) == FORMAT_HTML
        detection, source = detector.detect("statement.xls", html)
        assert detection.supported is False
        assert detection.reason == "unsupported_format"
        assert source is None

    def test_an_image_is_refused(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        assert readers.sniff_format(png) == FORMAT_UNKNOWN
        detection, _ = detector.detect("statement.pdf", png)
        assert detection.supported is False
        assert detection.reason == "unsupported_format"

    def test_a_scanned_pdf_asks_for_ocr_rather_than_importing_nothing(self):
        detection, source = detector.detect("scan.pdf", read("scanned_statement.pdf"))
        assert detection.supported is False
        assert detection.reason == "ocr_required"
        assert source is None

    def test_an_unrecognised_bank_is_reported_as_such(self):
        csv = b"col_a;col_b;col_c\n1;2;3\n4;5;6\n"
        detection, _ = detector.detect("mystery.csv", csv)
        assert detection.supported is False
        assert detection.reason == "unknown_provider"


class TestExpansionGuards:
    def test_a_zip_that_is_not_a_workbook_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("notes.txt", "hello")
        assert readers.sniff_format(buffer.getvalue()) == FORMAT_UNKNOWN

    def test_a_truncated_workbook_is_refused_rather_than_raising(self):
        broken = read("sbi_statement.xlsx")[:400]
        detection, _ = detector.detect("broken.xlsx", broken)
        assert detection.supported is False

    def test_a_workbook_claiming_to_expand_to_gigabytes_is_refused(self):
        """The zip directory is inspected before anything is decompressed."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/workbook.xml", b"<workbook/>")
            archive.writestr("xl/worksheets/sheet1.xml", b"0" * (1024 * 1024))
        content = bytearray(buffer.getvalue())

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(readers, "MAX_ZIP_UNCOMPRESSED_BYTES", 1024)
            with pytest.raises(readers.StatementReadError) as excinfo:
                readers.read_xlsx_rows(bytes(content))
        assert excinfo.value.code == "malformed_file"

    def test_a_workbook_with_too_many_parts_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/workbook.xml", b"<workbook/>")
            for index in range(20):
                archive.writestr(f"xl/worksheets/sheet{index}.xml", b"<sheet/>")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(readers, "MAX_ZIP_ENTRIES", 5)
            with pytest.raises(readers.StatementReadError) as excinfo:
                readers.read_xlsx_rows(buffer.getvalue())
        assert excinfo.value.code == "malformed_file"

    def test_a_pdf_with_too_many_pages_is_refused(self):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(readers, "MAX_PDF_PAGES", 1)
            with pytest.raises(readers.StatementReadError) as excinfo:
                readers.read_pdf_pages(read("axis_myzone_statement.pdf"))
        assert excinfo.value.code == "malformed_file"

    def test_a_pdf_holding_more_text_than_a_statement_could_is_refused(self):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(readers, "MAX_PDF_TEXT_CHARS", 10)
            with pytest.raises(readers.StatementReadError) as excinfo:
                readers.read_pdf_pages(read("axis_myzone_statement.pdf"))
        assert excinfo.value.code == "malformed_file"

    def test_a_statement_longer_than_any_statement_is_truncated_with_a_warning(self):
        """One upload cannot turn into an unbounded number of lookups."""
        from app.services.statement_import import registry, validator
        from app.services.statement_import.models import WARN_ROW_LIMIT
        from app.services.statement_import.parsers.base import StatementSource
        from app.services.statement_import.service import MAX_TRANSACTIONS

        source = StatementSource("s.csv", read("axis_statement.csv"), FORMAT_CSV)
        parser = registry.select_parser(source)
        assert parser is not None
        statement = validator.validate(parser.parse(source))
        assert len(statement.transactions) <= MAX_TRANSACTIONS
        assert WARN_ROW_LIMIT not in {w.code for w in statement.warnings}


class TestMoneyParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1,09,654.52", "109654.52"),   # Indian lakh grouping
            ("41,630.90CR", "41630.90"),
            ("4,043.00 Dr", "-4043.00"),
            ("₹ 2,500.00", "2500.00"),
            ("Rs. 175", "175"),
            ("(1,200.00)", "-1200.00"),
            ("-5000.00", "-5000.00"),
            ("+150.00", "150.00"),
        ],
    )
    def test_statement_money_formats(self, raw, expected):
        from decimal import Decimal

        assert readers.parse_money(raw) == Decimal(expected)

    def test_no_number_is_none_rather_than_zero(self):
        """An empty Debit cell must not read as a zero-value transaction."""
        assert readers.parse_money(" ") is None
        assert readers.parse_money("") is None
        assert readers.parse_money(None) is None
        assert readers.parse_money("not money") is None
