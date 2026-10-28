"""ตรวจจุดเริ่ม/จบของเอกสารจากหลายสัญญาณรวมกัน (ไม่พึ่งคำเดียว) พร้อมเหตุผลทุกจุด
คะแนน > 0 = หน้านี้เป็นหน้าแรกของเอกสารใหม่, < 0 = หน้าต่อเนื่องของเอกสารเดิม"""
from .fields import jaccard, norm_no

UNSURE = 2.5  # |คะแนน| ต่ำกว่านี้ = ไม่แน่ใจ ต้องให้คนตรวจ


def detect(F):
    """F = รายการ features ต่อหน้า (dict) คืนรายการผลต่อหน้า: start/score/conf/evidence"""
    out, last = [], None
    cur_no, pending, after_blank, cur_type = None, False, False, None
    for i, p in enumerate(F):
        if p["blank"]:
            out.append(dict(start=(i == 0), score=0, conf=1.0, blank=True,
                            evidence=[dict(t="หน้าว่าง (ถือเป็นตัวคั่น/หลังหน้า)", w=0)]))
            after_blank = after_blank or i > 0
            continue
        ev, s = [], 0.0

        def add(w, t):
            nonlocal s
            s += w
            ev.append(dict(t=t, w=w))

        if last is None:
            start = (i == 0)
            ev.append(dict(t="หน้าแรกของไฟล์" if i == 0 else "หน้าแรกที่มีเนื้อหา", w=0))
            s = 9.0 if start else -9.0
        else:
            q = F[last]
            if after_blank:
                add(2.5, "ถัดจากหน้าว่าง")
            # --- เลขที่เอกสาร ---
            if p["doc_no"] and cur_no:
                if norm_no(p["doc_no"]) == norm_no(cur_no):
                    add(-4, f"เลขที่เอกสารเดิม {cur_no}")
                else:
                    add(5, f"เลขที่เอกสารเปลี่ยน {cur_no} → {p['doc_no']}")
            elif p["doc_no"] and not cur_no:
                if pending:
                    add(-6, "หน้าก่อนหน้ามีช่อง “เลขที่เอกสาร” ว่าง และหน้านี้เป็นค่าที่เติม "
                            "(เอกสารกว้างถูกตัดคอลัมน์เป็น 2 หน้า)")
                else:
                    add(1, "พบเลขที่เอกสารในหน้านี้ ขณะที่ชุดก่อนหน้าไม่มีเลข")
            elif p["no_label"]:
                if cur_no:
                    add(3, "หน้านี้มีช่องเลขที่เอกสารแต่ค่าว่าง ขณะที่ชุดก่อนหน้าได้เลขแล้ว → น่าจะเป็นฟอร์มใหม่")
                elif pending:
                    add(2, "ชุดก่อนหน้ายังไม่พบเลข และหน้านี้ก็เป็นฟอร์มที่ช่องเลขว่างเช่นกัน")
            # --- ลำดับหน้า ---
            if p["px"] == 1:
                add(3, "ระบุ “หน้า 1” ของเอกสาร")
            elif q["px"] and p["px"] == q["px"] + 1 and (not p["py"] or p["py"] == q["py"]):
                add(-4, f"ลำดับหน้าต่อเนื่อง ({q['px']}→{p['px']})")
            if q["px"] and q["py"]:
                if q["px"] == q["py"] and p["px"] != q["px"] + 1:
                    add(2, f"หน้าก่อนหน้าเป็นหน้าสุดท้าย ({q['px']}/{q['py']})")
                elif q["px"] < q["py"] and p["px"] != 1:
                    add(-2, f"เอกสารก่อนหน้ายังไม่ถึงหน้าสุดท้าย ({q['px']}/{q['py']})")
            # --- ประเภท/ชื่อเอกสารที่หัวกระดาษ ---
            if p["title"]:
                if cur_type and p["title"] != cur_type:
                    add(3, f"ประเภทที่หัวหน้าเปลี่ยนเป็น “{p['title_label']}”")
                else:
                    add(0.5, "มีชื่อเอกสารที่หัวหน้า (อาจเป็นหัวซ้ำทุกหน้า)")
                    if q["total"]:
                        add(1, "หน้าก่อนหน้ามียอดรวมครบ แล้วหน้านี้เริ่มด้วยชื่อเอกสาร")
            elif cur_type:
                add(-1.5, "ไม่มีชื่อเอกสารที่หัวหน้า (เหมือนหน้าต่อ)")
            # --- หัวกระดาษ ---
            sim = jaccard(p["head"], q["head"])
            if sim < 0.3:
                add(1.5, f"หัวกระดาษต่างจากหน้าก่อนหน้ามาก (ความคล้าย {sim:.0%})")
            elif sim > 0.85:
                add(-0.5, f"หัวกระดาษเหมือนหน้าก่อนหน้า ({sim:.0%})")
            if not q["total"] and not p["doc_no"] and not p["title"] and not p["no_label"]:
                add(-1.5, "หน้าก่อนหน้ายังไม่มียอดรวม และหน้านี้ไม่มีหัวเรื่อง/เลขที่ใหม่")
            start = s > 0
        conf = 0.99 if abs(s) >= 9 else round(min(0.99, 0.5 + abs(s) / 10), 2)
        if abs(s) < UNSURE and last is not None:
            ev.append(dict(t="สัญญาณไม่ชัดเจน ต้องให้ผู้ใช้ตรวจ" + ("" if start else " (ค่าเริ่มต้น: ไม่ตัด)"), w=0))
        out.append(dict(start=start, score=round(s, 2), conf=conf, evidence=ev, blank=False))
        if start:
            cur_no, pending, cur_type = p["doc_no"], bool(p["no_label"] and not p["doc_no"]), p["title"]
        else:
            if p["doc_no"] and not cur_no:
                cur_no, pending = p["doc_no"], False
            cur_type = cur_type or p["title"]
        after_blank, last = False, i
    return out
