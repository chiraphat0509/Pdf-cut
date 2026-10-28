"""รวมทุกขั้น: อ่านหน้า -> สกัดฟิลด์ -> จำแนก -> ตรวจจุดตัด -> ประกอบเอกสาร + ส่งออก"""
import io
import json
import os
import re
import time
import zipfile

from pypdf import PdfReader, PdfWriter

from . import boundary, fields
from .classify import GROUPS, classify
from .reader import ocr_info, read_pages

CONF_OK = 0.75


def load_config(path=None):
    path = path or os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
    cfg = {"our_company_names": [], "ocr": "auto", "ocr_dpi": 300, "tesseract_cmd": "", "host": "127.0.0.1", "port": 8765}
    try:
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def _features(pg, cfg):
    t = pg["text"]
    blank = pg["method"] == "blank"
    doc_no, label = fields.find_doc_no(t)
    px, py = fields.find_page_no(t)
    c = classify(t, cfg) if not blank else None
    head_c = classify("\n".join([l for l in t.split("\n") if l.strip()][:8]), cfg) if not blank else None
    title = head_c["subtype"] if head_c and head_c["subtype"] != "unknown" and head_c["head_hit"] else None
    return dict(blank=blank, doc_no=doc_no, no_label=label, date=fields.find_date(t), px=px, py=py,
                total=fields.has_total(t), head=fields.head_sig(t), title=title,
                title_label=head_c["label"] if title else None, cls=c)


def analyze(path, cfg=None, progress=None):
    t0 = time.time()
    cfg = cfg or load_config()
    pages = read_pages(path, cfg, progress)
    if progress:
        progress(len(pages), len(pages), "วิเคราะห์ประเภทและจุดตัด")
    F = [_features(p, cfg) for p in pages]
    B = boundary.detect(F)
    groups = []
    for i, b in enumerate(B):
        if b["start"] or not groups:
            groups.append([])
        groups[-1].append(i)
    docs = []
    for k, idx in enumerate(groups):
        text = "\n".join(pages[i]["text"] for i in idx if not F[i]["blank"])
        real = [i for i in idx if not F[i]["blank"]]
        c = classify(text, cfg) if real else dict(group="other", subtype="blank", label="หน้าว่าง", conf=1.0, evidence=[])
        nos = sorted({fields.norm_no(F[i]["doc_no"]) for i in real if F[i]["doc_no"]})
        no = next((F[i]["doc_no"] for i in real if F[i]["doc_no"]), None)
        date = next((F[i]["date"] for i in real if F[i]["date"]), None)
        b0 = B[idx[0]]
        flags = []
        if c["conf"] < CONF_OK:
            flags.append(dict(level="warn", msg=f"ประเภทไม่แน่ใจ ({c['conf']:.0%}): " + "; ".join(c["evidence"])))
        if k > 0 and b0["conf"] < CONF_OK:
            flags.append(dict(level="warn", msg=f"จุดเริ่มเอกสารไม่แน่ใจ ({b0['conf']:.0%}) — ดูเหตุผลที่หน้า {idx[0] + 1}"))
        if k + 1 < len(groups) and B[groups[k + 1][0]]["conf"] < CONF_OK:
            flags.append(dict(level="warn", msg=f"จุดสิ้นสุดไม่แน่ใจ — ดูเหตุผลที่หน้า {groups[k + 1][0] + 1}"))
        if len(nos) > 1:
            flags.append(dict(level="error", msg="พบเลขที่เอกสารหลายค่าในชุดเดียว: " + ", ".join(nos) + " (อาจรวมผิดฉบับ)"))
        if real and not no and c["group"] in ("sales", "expense"):
            flags.append(dict(level="info", msg="ไม่พบเลขที่เอกสาร"))
        for i in real:
            if pages[i]["quality"] < 0.6:
                flags.append(dict(level="warn", msg=f"หน้า {i + 1}: คุณภาพข้อความต่ำ — " + "; ".join(x["msg"] for x in pages[i]["issues"]) or pages[i]["reason"]))
        docs.append(dict(id=k + 1, pages=[i + 1 for i in idx], group=c["group"], subtype=c["subtype"], label=c["label"],
                         conf=c["conf"], doc_no=no, date=date, type_evidence=c["evidence"],
                         start_conf=b0["conf"], flags=flags))
    seen = {}
    for d in docs:
        if d["doc_no"]:
            seen.setdefault(fields.norm_no(d["doc_no"]), []).append(d)
    for ds in seen.values():
        if len(ds) > 1:
            for d in ds:
                d["flags"].append(dict(level="warn", msg="เลขที่เอกสารซ้ำกับชุดอื่น (อาจถูกตัดผิดกลางเอกสาร หรือเป็นเอกสารซ้ำ)"))
    out_pages = []
    for i, p in enumerate(pages):
        out_pages.append(dict(n=p["n"], method=p["method"], quality=p["quality"], issues=p["issues"], reason=p["reason"],
                              doc_no=F[i]["doc_no"], blank=F[i]["blank"], boundary=B[i], ms=p["ms"],
                              ocr_conf=p.get("ocr_conf"), snippet=p["text"][:900]))
    return dict(n_pages=len(pages), pages=out_pages, docs=docs, groups=GROUPS,
                stats=dict(seconds=round(time.time() - t0, 2),
                           ocr_pages=sum(1 for p in pages if p["method"] == "ocr"),
                           review=sum(1 for d in docs if any(f["level"] in ("warn", "error") for f in d["flags"])),
                           ocr=ocr_info(cfg) if any(p["method"] == "ocr" for p in pages) else None))


def default_name(d, idx):
    base = d.get("doc_no") or f"{d.get('label') or 'doc'}_{idx:02d}"
    return safe_name(base)


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", s or "doc").strip(" ._")
    return (s or "doc")[:90]


def export_zip(src_pdf, docs, drop_pages=()):
    """docs = [{name, pages:[เลขหน้า 1-based]}]  คัดลอกหน้าต้นฉบับตรงๆ (ไม่เรนเดอร์ใหม่) คุณภาพเดิมครบ"""
    reader = PdfReader(src_pdf)
    if reader.is_encrypted:
        reader.decrypt("")
    drop = set(drop_pages)
    buf, used, manifest = io.BytesIO(), {}, []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, d in enumerate(docs, 1):
            pgs = [p for p in d["pages"] if p not in drop]
            if not pgs:
                continue
            w = PdfWriter()
            for p in pgs:
                w.add_page(reader.pages[p - 1])
            name = safe_name(d.get("name") or f"doc_{k:02d}")
            used[name] = used.get(name, 0) + 1
            if used[name] > 1:
                name += f"_{used[name]}"
            b = io.BytesIO()
            w.write(b)
            z.writestr(name + ".pdf", b.getvalue())
            manifest.append(dict(file=name + ".pdf", pages=pgs, type=d.get("type")))
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return buf.getvalue(), manifest


def export_pdf(src_pdf, pages, drop_pages=()):
    """คัดลอกหน้าที่เลือกลง PDF เดียว โดยไม่เรนเดอร์ใหม่"""
    reader = PdfReader(src_pdf)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("ไฟล์ PDF มีรหัสผ่านและไม่สามารถส่งออกได้")
    if any(not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= len(reader.pages)
           for page in pages):
        raise ValueError("เลขหน้าไม่ถูกต้อง")
    drop = set(drop_pages)
    selected = [page for page in pages if page not in drop]
    if not selected:
        raise ValueError("ไม่มีหน้าสำหรับส่งออก")
    writer = PdfWriter()
    for page in selected:
        writer.add_page(reader.pages[page - 1])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
