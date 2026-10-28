"""สร้าง PDF ทดสอบสังเคราะห์ (ภาษาอังกฤษ) ที่รู้คำตอบ: เอกสารยาวไม่เท่ากัน ไม่มีหน้าว่างคั่น ฯลฯ"""
import io

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


def _page(c, lines, y0=740):
    y = y0
    for size, text in lines:
        c.setFont("Helvetica-Bold" if size > 12 else "Helvetica", size)
        c.drawString(60, y, text)
        y -= size + 8
    c.showPage()


def invoice_pages(no, n, style="A", title="INVOICE", seller="ABC Trading Co., Ltd.", buyer="Customer Co."):
    pages = []
    for k in range(1, n + 1):
        L = []
        if style == "A":     # หัวเต็มทุกหน้า + Page k of n
            L += [(20, title), (11, f"From: {seller}"), (11, f"{title.title()} No: {no}"), (11, "Date: 2026-10-27"),
                  (11, f"Bill To: {buyer}"), (11, f"Page {k} of {n}")]
        elif style == "B":   # เลขที่ซ้ำทุกหน้า ไม่มี Page x of y
            L += [(20, title), (11, f"{title.title()} No: {no}"), (11, "Date: 2026-10-27")] if k == 1 else \
                 [(11, f"{title.title()} No: {no} (continued)")]
        elif style == "C":   # หน้าต่อมีแต่ตาราง ไม่มีหัว/เลข/หน้า
            L += [(20, title), (11, f"{title.title()} No: {no}"), (11, "Date: 2026-10-27"), (11, f"Bill To: {buyer}")] if k == 1 else []
        for r in range(8):
            L.append((11, f"Item {no}-{k}-{r + 1}  Widget model {r + k}00   qty {r + 1}   price {10 + r}.00   amount {(10 + r) * (r + 1)}.00"))
        if k == n:
            L += [(11, "Subtotal: 1,000.00"), (11, "VAT 7%: 70.00"), (12, "Grand Total: 1,070.00")]
        pages.append(L)
    return pages


def make_pdf(docs):
    """docs = list ของ list-หน้า (แต่ละหน้า = list ของ (size,text)) ; None = หน้าว่าง"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    truth = []
    page_no = 0
    for d in docs:
        truth.append([])
        for pg in d:
            page_no += 1
            truth[-1].append(page_no)
            if pg is None:
                c.showPage()
            else:
                _page(c, pg)
    c.save()
    return buf.getvalue(), truth


def case_mixed():
    payslip = [[(20, "PAYSLIP"), (11, "Employee: Somchai Jaidee"), (11, "Month: 10/2026"), (11, "Gross pay 30,000.00"),
                (11, "Net pay 28,500.00"), (11, "Social security 750.00")]]
    bank = [[(18, "BANK STATEMENT"), (11, "Statement of Account 001-2-34567-8"), (11, f"Page {k} of 2"),
             (11, "Date  Description  Withdrawal  Deposit  Balance")] +
            [(11, f"2026-10-{r + 1:02d}  Transfer {r}  500.00  0.00  {10000 - r * 500}.00") for r in range(10)] for k in (1, 2)]
    po = invoice_pages("PO-7001", 2, "A", title="PURCHASE ORDER", seller="Rungruang Trading", buyer="Neo Printing")
    return [invoice_pages("INV-100", 1), invoice_pages("INV-101", 3), invoice_pages("INV-102", 2), po, payslip, bank]


def case_repeated_number():
    return [invoice_pages("INV-300", 2, "B"), invoice_pages("INV-301", 1, "B"), invoice_pages("INV-302", 3, "B")]


def case_table_continuation():
    return [invoice_pages("INV-400", 3, "C"), invoice_pages("INV-401", 1, "C"), invoice_pages("INV-402", 2, "C")]


def case_blank_separated():
    return [invoice_pages("INV-500", 2, "C") + [None], invoice_pages("INV-501", 1, "C") + [None], invoice_pages("INV-502", 2, "C")]
