"""ดึงสัญญาณจากข้อความของแต่ละหน้า: เลขที่เอกสาร วันที่ ลำดับหน้า ยอดรวม ลายเซ็นหัวกระดาษ"""
import re


def flat(s):
    """ตัดช่องว่าง/เครื่องหมายทิ้งทั้งหมด เพื่อจับคำไทยที่ถูกแทรกช่องว่างและคำย่อมีจุดอย่าง ภ.พ.30"""
    return re.sub(r"[\s.\-_,:;()\[\]/\\|\"'“”#]+", "", s).lower()


def _kw(w):
    return r"\s*".join(re.escape(c) for c in w if not c.isspace())


DOC_LABELS = sorted([
    "Invoice Number", "Invoice No", "Invoice #", "Inv No", "Tax Invoice No", "Purchase Order Number",
    "Purchase Order No", "PO Number", "P.O. Number", "P.O. No", "PO No", "PO #", "Document Number",
    "Document No", "Doc No", "Receipt Number", "Receipt No", "Credit Note No", "Debit Note No",
    "Quotation No", "Quote No", "Order Number", "Order No",
    "เลขที่เอกสาร", "เลขที่ใบกำกับภาษี", "เลขที่ใบแจ้งหนี้", "เลขที่ใบสั่งซื้อ", "เลขที่ใบเสร็จรับเงิน",
    "เลขที่ใบเสร็จ", "เลขที่ใบเสนอราคา", "เลขที่ใบลดหนี้", "เลขที่ใบเพิ่มหนี้", "เลขที่ใบสำคัญจ่าย",
], key=len, reverse=True)
LAB = re.compile(r"(?<![A-Za-z0-9])(?:%s)" % "|".join(_kw(x) for x in DOC_LABELS), re.I)
SEP = re.compile(r"[\s:：.#/|\-]*")
VAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/.]{2,}")


def find_doc_no(text):
    """คืน (เลขที่เอกสาร|None, มีช่อง 'เลขที่เอกสาร' อยู่ในหน้าหรือไม่)
    ช่องมีแต่ค่าว่าง = สัญญาณว่าหน้านี้อาจเป็นครึ่งซ้ายของเอกสารที่ถูกตัดคอลัมน์ไปหน้าถัดไป"""
    present = False
    for line in text.split("\n"):
        m = LAB.search(line)
        if not m:
            continue
        present = True
        pos = m.end()
        while True:  # ป้ายสองภาษาติดกัน เช่น "เลขที่เอกสาร / PO No.:"
            pos = SEP.match(line, pos).end()
            m2 = LAB.match(line, pos)
            if not m2:
                break
            pos = m2.end()
        v = VAL.match(line[pos:])
        if v and re.search(r"\d", v.group(0)):
            return v.group(0).strip(".-/_"), True
    return None, present


D1 = re.compile(r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b|\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b")
D2 = re.compile(r"\b\d{1,2}\s*[ก-๙A-Za-z.]{3,14}\s*\d{4}\b")


def find_date(text):
    for line in text.split("\n"):
        f = flat(line)
        if ("date" in f or "วันที่" in f) and not re.search(r"due|delivery|กำหนด|ครบ|ส่งมอบ", f):
            m = D1.search(line) or D2.search(line)
            if m:
                return m.group(0).strip()
    return None


PXY = re.compile(r"(?:page|หน้า(?:ที่)?)\s*[:.]?\s*(\d{1,4})\s*(?:of|/|จาก(?:ทั้งหมด)?)\s*(\d{1,4})", re.I)
PX = re.compile(r"^\s*(?:page|หน้า(?:ที่)?)\s*[:.]?\s*(\d{1,3})\s*$", re.I)


def find_page_no(text):
    m = PXY.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    lines = [l for l in text.split("\n") if l.strip()]
    for l in lines[:3] + lines[-3:]:
        m = PX.match(l)
        if m:
            return int(m.group(1)), None
    return None, None


TOTAL_KW = ["grandtotal", "totaldue", "totalamount", "amountdue", "balancedue", "รวมทั้งสิ้น", "รวมเงินทั้งสิ้น",
            "จำนวนเงินรวม", "ยอดรวมสุทธิ", "ยอดชำระ"]
AMOUNT = re.compile(r"\d[\d,]*\.\d{2}")


def has_total(text):
    """มีบรรทัด 'ยอดรวม' ที่มีตัวเลขเงินกำกับจริง (ป้ายเปล่าไม่นับ)"""
    for line in text.split("\n"):
        f = flat(line)
        if any(k in f for k in TOTAL_KW) and AMOUNT.search(line):
            return True
    return False


def head_sig(text, n=5):
    lines = [l for l in text.split("\n") if l.strip()][:n]
    s = re.sub(r"\d+", "", flat(" ".join(lines)))
    return {s[i:i + 3] for i in range(max(len(s) - 2, 0))}


def jaccard(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / max(len(a | b), 1)


def norm_no(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())
