#!/usr/bin/env python3
"""Regenerate the binary statement fixtures next to this script.

    python tests/fixtures/statements/build_fixtures.py

The layouts here are reproductions of real statements from SBI, Axis Bank and
SuperMoney: the same column order, the same header and footer blocks, the same
hard-wrapped narration, the same quirks the parsers exist to cope with. The
identity data is invented. Nobody's account number, card number, name, address
or PAN belongs in a repository, and a fixture does not need one to prove a
parser reads a column correctly.

The generated files are committed, so running this is only necessary when a
fixture's *shape* needs to change. Checking them in also means a reportlab or
openpyxl upgrade cannot silently alter what the tests are reading.
"""
from __future__ import annotations

import io
from pathlib import Path

HERE = Path(__file__).parent

# ---------------------------------------------------------------- SBI (.xlsx)

#: (date, narration, ref, debit, credit, balance). The narration strings carry
#: the bank's own hard wrap: a newline followed by exactly one space, falling
#: mid-token, which is what `dewrap_cell` has to stitch back together.
SBI_ROWS = [
    ("01/09/2026", " WDL TFR   UPI/DR/624442236850/Indian C/HDFC/z\n erodha.ic/Merc   0097691162095 AT 16252 BABAIN", "", "2500.00", "", "34177.42"),
    ("01/09/2026", " WDL TFR   UPI/DR/624442245521/Indian C/HDFC/z\n erodha.ic/Merc   0097691162095 AT 16252 BABAIN", "", "2500.00", "", "31677.42"),
    ("03/09/2026", " WDL TFR   UPI/DR/624621728440/SAHIL/UTIB/so\n nisahil6/UPI   0097693162093 AT 16252 BABAIN", "", "15000.00", "", "16677.42"),
    ("03/09/2026", " DEP TFR   UPI/CR/661290123852/RAJESH K/PUNB/8\n 814934380/Paid   0097735162098 AT 16252 BABAIN", "", "", "15000.00", "31677.42"),
    ("04/09/2026", " POS ATM PURCH   OTHPG 624710895\n 434SBIUNIPAYDBCard       Mumbai", "", "8368.52", "", "23308.90"),
    ("04/09/2026", " DEP TFR   UPI/CR/500819400817/SAGAR RA/UBIN/8\n 864973891/Paym   0097736162097 AT 16252 BABAIN", "CHQ001234", "", "1.00", "23309.90"),
]

SBI_OPENING = "36,677.42CR"
SBI_CLOSING = "23,309.90CR"
SBI_TOTAL_DEBITS = "28,368.52"
SBI_TOTAL_CREDITS = "15,001.00"


def _sbi_sheet(rows, opening, closing, total_debits, total_credits, clear_balance):
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Statement"

    header_block = [
        ("Mr. TEST CUSTOMER  \ntest.customer@example.invalid", "State Bank of India  \nTEST BRANCH"),
        ("1 EXAMPLE ROAD,EXAMPLE TOWN,000000", "EXAMPLE BRANCH ADDRESS"),
        ("Date of Statement  :  14-09-2026", "Branch Code  :  00000"),
        (f"Clear Balance  :  {clear_balance}", "Branch Name  :  EXAMPLE"),
        ("Uncleared Amount  :  0.00", "Branch Email ID  :  branch@example.invalid"),
        ("MOD Bal   :  0.00", "Branch Phone   :  0000000000"),
        ("Lien  :  0.00", "CIF Number  :  00000000000"),
        ("Limit  :  0.00", "Account Number  :  00000004312"),
        ("Monthly Avg Balance  :  0.00", "Product  :  REGULAR SB CHQ-INDIVIDUALS"),
        ("Interest Rate  :  2.50 % p.a.", "IFSC Code  :  SBIN0000000"),
        ("Drawing Power  :  0.00", "Currency  :  INR"),
        ("\t\t\t", "Account Status  :  OPEN"),
        ("Account Open Date  :  16/08/2014", "Nominee  Name  :  XXXXXXXXXXX"),
        ("Statement From  :  01-09-2026  to  14-09-2026", None),
    ]
    sheet.append([])  # the export starts on row 2
    for left, right in header_block:
        sheet.append([left, right])

    sheet.append(["Date", "Details", "Ref No/Cheque No", "Debit", "Credit", "Balance"])
    for row in rows:
        sheet.append(list(row))

    sheet.append([])
    sheet.append(["Statement Summary : 01-09-2026  To  14-09-2026"])
    sheet.append([
        "Brought Forward (₹)", "Dr Count", "Cr Count",
        "Total Debits (₹)", "Total Credits (₹)", "Closing Balance (₹)",
    ])
    sheet.append([
        opening,
        str(sum(1 for r in rows if r[3])),
        str(sum(1 for r in rows if r[4])),
        total_debits,
        total_credits,
        closing,
    ])
    sheet.append([])
    sheet.append(["This is a computer generated statement and does not require a signature."])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_sbi() -> None:
    (HERE / "sbi_statement.xlsx").write_bytes(
        _sbi_sheet(SBI_ROWS, SBI_OPENING, SBI_CLOSING, SBI_TOTAL_DEBITS, SBI_TOTAL_CREDITS, SBI_CLOSING)
    )

    # Same statement with one running balance nudged, so the chain breaks at a
    # single identifiable row rather than everywhere after it.
    broken = [list(r) for r in SBI_ROWS]
    broken[2][5] = "16677.99"
    broken[3][5] = "31677.42"
    (HERE / "sbi_statement_unbalanced.xlsx").write_bytes(
        _sbi_sheet(
            [tuple(r) for r in broken],
            SBI_OPENING, SBI_CLOSING, SBI_TOTAL_DEBITS, SBI_TOTAL_CREDITS, SBI_CLOSING,
        )
    )


#: A second statement that overlaps the first: its opening three lines are
#: the closing three of `SBI_ROWS`, continuing the same balance chain. The
#: days the two files share must not be imported twice, and the days only
#: this one covers must not be skipped.
SBI_OVERLAP_ROWS = SBI_ROWS[3:] + [
    ("09/09/2026", " WDL TFR   UPI/DR/661816253073/EXAMPLE P/PUNB/e\n xample123/UPI   0097692162094 AT 16252 BABAIN", "", "5000.00", "", "18309.90"),
    ("10/09/2026", " DEP TFR   UPI/CR/661987747171/EXAMPLE C/UTIB/e\n xamplecus/UPI   0097735162098 AT 16252 BABAIN", "", "", "2000.00", "20309.90"),
    ("11/09/2026", " WDL TFR   UPI/DR/625425224476/EXAMPLE M/UTIB/e\n xamplemer/UPI   0097736162097 AT 16252 BABAIN", "", "1000.00", "", "19309.90"),
]


def build_sbi_overlap() -> None:
    (HERE / "sbi_statement_overlap.xlsx").write_bytes(
        _sbi_sheet(
            SBI_OVERLAP_ROWS,
            "16,677.42CR",
            "19,309.90CR",
            "14,368.52",
            "17,001.00",
            "19,309.90CR",
        )
    )


SBI_PASSWORD = "fixture-password"


def build_sbi_encrypted() -> None:
    """The same workbook, wrapped the way SBI mails it out."""
    from msoffcrypto.format.ooxml import OOXMLFile

    plain = io.BytesIO((HERE / "sbi_statement.xlsx").read_bytes())
    encrypted = io.BytesIO()
    office_file = OOXMLFile(plain)
    office_file.encrypt(SBI_PASSWORD, encrypted)
    (HERE / "sbi_statement_encrypted.xlsx").write_bytes(encrypted.getvalue())


# ----------------------------------------------------------------- PDF helper

#: The real statements are wide enough that their tables survive text
#: extraction as columns. Reproduce that rather than squeezing onto A4.
PAGE_WIDTH, PAGE_HEIGHT = 1700, 1200


def _pdf(pages: list[list[tuple[float, float, str, int]]]) -> bytes:
    """Draw (x, y, text, size) tuples, one list per page."""
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
    for page in pages:
        for x, y, text, size in page:
            pdf.setFont("Helvetica", size)
            pdf.drawString(x, PAGE_HEIGHT - y, text)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


# ------------------------------------------------------- Axis MyZone (.pdf)

#: (date, narration, merchant category, amount, Cr|Dr)
MYZONE_ROWS = [
    ("14/08/2026", "BBPS PAYMENT RECEIVED - SB4162260C707D3AM9CF", "", "3,793.03", "Cr"),
    ("16/08/2026", "EMI INTEREST - 16/18, REF# 56382477", "INSURANCE", "134.86", "Dr"),
    ("16/08/2026", "GST", "", "24.28", "Dr"),
    ("16/08/2026", "EMI PRINCIPAL - 16/18, REF# 56382477", "INSURANCE", "3,326.82", "Dr"),
    ("17/08/2026", "UPI/RAM DHAN DAIRY FARM/MAB.037215038290019@AXI", "FOOD PRODUCTS", "60.00", "Dr"),
    ("23/08/2026", "DISTRICT MOVIE TICKET R", "ENTERTAINMENT", "497.04", "Dr"),
]

COL_DATE, COL_DETAILS, COL_CATEGORY, COL_AMOUNT = 60, 200, 900, 1420


def build_myzone() -> None:
    page1: list[tuple[float, float, str, int]] = [
        (700, 40, "My Zone Rupay Credit Card Statement", 11),
        (60, 80, "TEST CUSTOMER", 9),
        (60, 100, "1 Example Road, Example Town 000000", 9),
        (760, 150, "PAYMENT SUMMARY", 10),
        # Deliberately present: these carry Dr markers and must never be read
        # as transactions. Only the absence of a leading date keeps them out.
        (60, 180, "Total Payment Due", 9),
        (400, 180, "Minimum Payment Due", 9),
        (760, 180, "Statement Period", 9),
        (1100, 180, "Payment Due Date", 9),
        (60, 200, "4,043.00 Dr", 9),
        (400, 200, "3,498.00 Dr", 9),
        (760, 200, "14/08/2026 - 12/09/2026", 9),
        (1100, 200, "02/10/2026", 9),
        (60, 240, "Credit Card Number", 9),
        (400, 240, "Credit Limit", 9),
        (60, 260, "653046******3649", 9),
        (400, 260, "470,000.00", 9),
        (60, 300,
         "Previous Balance - Payments - Credits + Purchase + Cash Advance + Other Debit&Charges =Total Payment Due", 9),
        (60, 320, "3,793.03  Dr", 9),
        (360, 320, "3,793.03", 9),
        (560, 320, "0.00", 9),
        (720, 320, "3,883.86", 9),
        (920, 320, "0.00", 9),
        (1100, 320, "159.14", 9),
        (1300, 320, "4,043.00  Dr", 9),
        (760, 360, "Account Summary", 10),
        (COL_DATE, 385, "DATE", 9),
        (COL_DETAILS, 385, "TRANSACTION DETAILS", 9),
        (COL_CATEGORY, 385, "MERCHANT CATEGORY", 9),
        (COL_AMOUNT, 385, "AMOUNT (Rs.)", 9),
        (COL_DETAILS, 405, "Card No: 653046******3649", 9),
        (760, 405, "Name TEST CUSTOMER", 9),
    ]
    y = 430
    for row_date, details, category, amount, marker in MYZONE_ROWS:
        page1.append((COL_DATE, y, row_date, 9))
        page1.append((COL_DETAILS, y, details, 9))
        if category:
            page1.append((COL_CATEGORY, y, category, 9))
        page1.append((COL_AMOUNT, y, f"{amount} {marker}", 9))
        y += 20

    page1 += [
        (760, y + 15, "EMI BALANCES", 9),
        # No Cr/Dr marker, so the amount guard rejects it even before the
        # section guard does.
        (COL_DETAILS, y + 35, "POLICYBAZAARINSURANCE", 9),
        (COL_CATEGORY, y + 35, "56382477", 9),
        (COL_AMOUNT, y + 35, "6,923.39", 9),
        (700, y + 60, "**** End of Statement ****", 9),
        # Dated lines after the table closed. Nothing here may be imported.
        (COL_DATE, y + 90, "25/08/2026", 9),
        (COL_DETAILS, y + 90, "FOOTER LINE THAT LOOKS LIKE A TRANSACTION", 9),
        (COL_AMOUNT, y + 90, "999.99 Dr", 9),
        (60, y + 130, "IMPORTANT MESSAGE", 8),
        (60, y + 150, "Axis Bank GST registration no.: 00AAACU0000K0ZD.", 8),
        (1500, y + 200, "Page : 1 of 2", 8),
    ]

    page2: list[tuple[float, float, str, int]] = [
        (700, 40, "My Zone Rupay Credit Card Statement", 11),
        (60, 80, "Finance Charge calculation", 10),
        (900, 80, "Schedule of charges", 10),
        (60, 110, "For example, assume that you have purchased household goods for Rs. 25000.00 on 12th June", 8),
        (60, 130, "and withdrawn cash from ATM for Rs. 5000.00 on 15th June.", 8),
        (60, 170, "Interest on cash withdrawal of Rs 5000 @ 3.75% from 15th June to 20th June (6 days)", 8),
        (900, 170, "36.99", 8),
        (60, 190, "Interest on purchase of Rs 25000 @ 3.75% from 12th June to 10th July (29 days)", 8),
        (900, 190, "893.84", 8),
        (60, 210, "Total Interest charged on 20th July", 8),
        (900, 210, "1265.11", 8),
        (900, 250, "Duplicate Statement Fee", 8),
        (1200, 250, "Waived", 8),
        (900, 270, "Fee for Cash Payment at branches", 8),
        (1200, 270, "Rs. 175", 8),
        (60, 320, "Minimum Amount Due Calculation", 10),
        (60, 350, "Txn Date", 8),
        (250, 350, "Type", 8),
        (600, 350, "Cr/Db", 8),
        (760, 350, "MAD Contribution", 8),
        (1000, 350, "Amount", 8),
        (60, 370, "25th Sep", 8),
        (250, 370, "Purchase", 8),
        (600, 370, "Db", 8),
        (760, 370, "2%", 8),
        (1000, 370, "5000", 8),
        (60, 390, "1st Oct", 8),
        (250, 390, "Joining Fees", 8),
        (600, 390, "Db", 8),
        (760, 390, "100%", 8),
        (1000, 390, "1000", 8),
        (760, 430, "Total Amount Due", 8),
        (1000, 430, "8813.65", 8),
        (1500, 1100, "Page : 2 of 2", 8),
    ]
    (HERE / "axis_myzone_statement.pdf").write_bytes(_pdf([page1, page2]))


# --------------------------------------------------------- SuperMoney (.pdf)

SUPERMONEY_ROWS = [
    ("GAGANBHUTANI", "Axis 3852", "-27.00", "3 September 2026", "SUCCESS"),
    ("DHANU KUNWAR", "Axis 3852", "-40.00", "3 September 2026", "SUCCESS"),
    ("EXAMPLE MERCHANT", "Axis 3852", "-5000.00", "3 September 2026", "SUCCESS"),
    ("REFUND SOURCE", "Axis 3852", "150.00", "4 September 2026", "SUCCESS"),
    ("FAILED PAYEE", "Axis 3852", "-99.00", "4 September 2026", "FAILED"),
    ("PENDING PAYEE", "Axis 3852", "-12.50", "4 September 2026", "PENDING"),
]

#: SuperMoney is an aggregator: its Bank column names the funding account per
#: *row*, so one export can span several. An import lands in one Securo
#: account, which makes this case something to refuse rather than guess at.
SUPERMONEY_MIXED_ROWS = [
    ("GAGANBHUTANI", "Utkarsh XX20", "-27.00", "3 September 2026", "SUCCESS"),
    ("EXAMPLE MERCHANT", "Axis 3852", "-5000.00", "3 September 2026", "SUCCESS"),
]


def _supermoney_page(rows) -> list[tuple[float, float, str, int]]:
    page: list[tuple[float, float, str, int]] = [
        (60, 60, "Transaction History", 12),
        (60, 90, "1 September 2026 to 4 September 2026", 9),
        (60, 140, "Name", 9),
        (400, 140, "Bank", 9),
        (700, 140, "Amount", 9),
        (950, 140, "Date", 9),
        (1300, 140, "Status", 9),
    ]
    y = 170
    for name, bank, amount, row_date, status in rows:
        page += [
            (60, y, name, 9),
            (400, y, bank, 9),
            (700, y, amount, 9),
            (950, y, row_date, 9),
            (1300, y, status, 9),
        ]
        y += 25
    return page


def build_supermoney() -> None:
    (HERE / "supermoney_history.pdf").write_bytes(
        _pdf([_supermoney_page(SUPERMONEY_ROWS)])
    )
    (HERE / "supermoney_history_mixed.pdf").write_bytes(
        _pdf([_supermoney_page(SUPERMONEY_MIXED_ROWS)])
    )


def build_scanned_pdf() -> None:
    """A PDF with no extractable text, standing in for a scan."""
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
    pdf.rect(100, 100, 400, 200, fill=0)
    pdf.showPage()
    pdf.save()
    (HERE / "scanned_statement.pdf").write_bytes(buffer.getvalue())


def main() -> None:
    build_sbi()
    build_sbi_overlap()
    build_sbi_encrypted()
    build_myzone()
    build_supermoney()
    build_scanned_pdf()
    for path in sorted(HERE.glob("*")):
        if path.name != Path(__file__).name:
            print(f"{path.name}: {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
