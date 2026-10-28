"""จำแนกประเภทเอกสารด้วยกฎที่ตรวจสอบได้ พร้อมหลักฐาน; ไม่แน่ใจ = other (Unknown) ไม่เดา"""
import re
from .fields import flat

GROUPS = {
    "sales": "เอกสารรายได้ (Sales)",
    "expense": "รายจ่ายและจัดซื้อ (Expense/Purchase)",
    "bank": "รายการเดินบัญชี (Bank Statement)",
    "payroll": "เงินเดือนและพนักงาน (Payroll/HR)",
    "tax": "ภาษีและรายงานประจำเดือน (Tax)",
    "other": "อื่นๆ / ไม่ทราบ (Unknown)",
}

# (group, subtype, ชื่อไทย, คีย์เวิร์ด(flat), น้ำหนัก)
RULES = [
    ("sales", "tax_invoice", "ใบกำกับภาษี", ["taxinvoice", "ใบกำกับภาษี"], 4),
    ("sales", "receipt", "ใบเสร็จรับเงิน", ["receipt", "ใบเสร็จรับเงิน", "ใบเสร็จ"], 3),
    ("sales", "credit_note", "ใบลดหนี้", ["creditnote", "ใบลดหนี้"], 4),
    ("sales", "debit_note", "ใบเพิ่มหนี้", ["debitnote", "ใบเพิ่มหนี้"], 4),
    ("sales", "sales_order", "ใบสั่งขาย (SO)", ["salesorder", "ใบสั่งขาย"], 5),
    ("sales", "invoice", "ใบแจ้งหนี้", ["invoice", "ใบแจ้งหนี้", "ใบวางบิล"], 3),
    ("expense", "po", "ใบสั่งซื้อ (PO)", ["purchaseorder", "ใบสั่งซื้อ"], 5),
    ("expense", "purchase_invoice", "ใบแจ้งหนี้ซื้อ", ["purchaseinvoice", "ใบแจ้งหนี้ซื้อ", "ใบกำกับภาษีซื้อ"], 5),
    ("expense", "quotation", "ใบเสนอราคา", ["quotation", "ใบเสนอราคา"], 4),
    ("expense", "expense_receipt", "ใบเสร็จค่าใช้จ่าย", ["expensereceipt", "expenseclaim", "ใบเสร็จค่าใช้จ่าย"], 4),
    ("bank", "bank_statement", "รายการเดินบัญชี",
     ["bankstatement", "statementofaccount", "accountstatement", "รายการเดินบัญชี", "รายการเคลื่อนไหวบัญชี"], 5),
    ("payroll", "payslip", "สลิปเงินเดือน", ["payslip", "salaryslip", "สลิปเงินเดือน", "ใบจ่ายเงินเดือน", "ใบแจ้งเงินเดือน"], 5),
    ("payroll", "payroll_report", "รายงานเงินเดือน", ["payrollreport", "payrollsummary", "รายงานเงินเดือน", "สรุปเงินเดือน"], 5),
    ("tax", "pp30", "ภ.พ.30", ["ภพ30", "vatreturn", "แบบแสดงรายการภาษีมูลค่าเพิ่ม"], 6),
    ("tax", "pnd", "ภ.ง.ด.", ["ภงด1", "ภงด3", "ภงด53", "ภงด54", "แบบยื่นรายการภาษีเงินได้หัก"], 6),
    ("tax", "tax_report", "รายงานภาษีซื้อ/ขาย",
     ["รายงานภาษีซื้อ", "รายงานภาษีขาย", "purchasetaxreport", "salestaxreport", "inputtaxreport", "outputtaxreport"], 5),
    ("tax", "wht_cert", "หนังสือรับรองหักภาษี ณ ที่จ่าย", ["50ทวิ", "หนังสือรับรองการหักภาษี", "withholdingtaxcertificate"], 5),
]
WEAK = [  # คำที่ช่วยยืนยัน (น้ำหนักเบา) ต้องเจอหลายคำจึงนับ
    ("bank", "bank_statement", ["withdrawal", "deposit", "ถอนเงิน", "ฝากเงิน", "ยอดคงเหลือ", "runningbalance"], 2),
    ("payroll", "payslip", ["เงินเดือน", "ประกันสังคม", "เงินได้สุทธิ", "netpay", "grosspay"], 2),
]
SUBLABEL = {r[1]: r[2] for r in RULES}
SALESLIKE = {"invoice", "tax_invoice", "receipt", "credit_note", "debit_note"}
SELLER_MARK = ["from:", "seller", "ผู้ขาย", "ผู้ออกใบ", "vendor"]
BUYER_MARK = ["billto", "bill to", "soldto", "ลูกค้า", "ผู้ซื้อ", "customer"]


def _direction(text, cfg):
    """ใบแจ้งหนี้/ใบกำกับภาษี เป็นฝั่งขายหรือซื้อ ขึ้นกับว่าบริษัทเราเป็นผู้ออกหรือผู้รับ"""
    names = [flat(x) for x in (cfg or {}).get("our_company_names", []) if x.strip()]
    if not names:
        return None, "ไม่ทราบชื่อบริษัทของเรา (ตั้ง our_company_names ใน config.json) จึงสมมติว่าเป็นเอกสารที่เราออก (ขาย)"
    low = text.lower()
    s = min([low.find(m) for m in SELLER_MARK if m in low] or [-1])
    b = min([low.find(m) for m in BUYER_MARK if m in low] or [-1])
    seg_s = flat(low[s:b] if 0 <= s < b else (low[s:s + 150] if s >= 0 else ""))
    seg_b = flat(low[b:b + 200] if b >= 0 else "")
    in_s = any(n in seg_s for n in names)
    in_b = any(n in seg_b for n in names)
    if in_s and not in_b:
        return "sales", "ชื่อบริษัทเราอยู่ฝั่งผู้ขาย/ผู้ออกเอกสาร"
    if in_b and not in_s:
        return "expense", "ชื่อบริษัทเราอยู่ฝั่งผู้ซื้อ/ผู้รับเอกสาร"
    return None, "ระบุทิศทางซื้อ/ขายจากชื่อบริษัทไม่ได้ชัดเจน"


def classify(text, cfg=None):
    f_all = flat(text)
    head = flat(" ".join([l for l in text.split("\n") if l.strip()][:8]))
    scores, ev = {}, {}
    for g, st, th, kws, w in RULES:
        h = sum(head.count(k) for k in kws)
        b = sum(f_all.count(k) for k in kws)
        if st == "invoice":  # 'invoice' เป็นส่วนหนึ่งของ taxinvoice/purchaseinvoice
            sub = sum(f_all.count(k) for k in ("taxinvoice", "purchaseinvoice", "salesinvoice"))
            subh = sum(head.count(k) for k in ("taxinvoice", "purchaseinvoice", "salesinvoice"))
            b, h = max(b - sub, 0), max(h - subh, 0)
        s = 2 * w * min(h, 2) + 0.5 * w * min(max(b - h, 0), 2)
        if s:
            scores[(g, st)] = s
            ev[(g, st)] = (f"พบ “{th}” ที่หัวเอกสาร" if h else f"พบ “{th}” ในเนื้อหา", h > 0)
    for g, st, kws, w in WEAK:
        hits = [k for k in kws if k in f_all]
        if len(hits) >= 2:
            scores[(g, st)] = scores.get((g, st), 0) + w * len(hits)
            ev.setdefault((g, st), ("พบคำประกอบ " + ", ".join(hits[:4]), False))
    if not scores:
        return dict(group="other", subtype="unknown", label="ไม่ทราบประเภท", conf=0.2, head_hit=False,
                    evidence=["ไม่พบคำที่บ่งชี้ประเภทเอกสาร"])
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (g, st), s1 = ranked[0]
    s2 = next((s for (g2, _), s in ranked[1:] if g2 != g), 0)
    evidence = [ev[(g, st)][0]]
    if s1 < 3:
        return dict(group="other", subtype="unknown", label="ไม่ทราบประเภท", conf=0.3, head_hit=False,
                    evidence=evidence + ["หลักฐานน้อยเกินไป จึงไม่เดาประเภท"])
    conf = min(0.97, 0.55 + 0.42 * (1 - s2 / s1))
    if not ev[(g, st)][1]:
        conf = min(conf, 0.7)
    if st in SALESLIKE:
        d, why = _direction(text, cfg)
        evidence.append(why)
        if d == "expense":
            g = "expense"
        elif d is None:
            conf = min(conf, 0.6)
    if s2 and s2 > 0.6 * s1:
        evidence.append("มีหลักฐานของอีกประเภทใกล้เคียงกัน")
        conf = min(conf, 0.6)
    label = SUBLABEL.get(st, st) + (" (ซื้อ)" if g == "expense" and st in SALESLIKE else "")
    return dict(group=g, subtype=st, label=label, conf=round(conf, 2), head_hit=ev[(ranked[0][0])][1], evidence=evidence)
