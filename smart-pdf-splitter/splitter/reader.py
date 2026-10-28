"""อ่านข้อความรายหน้า: ดึงข้อความตรง -> ตรวจคุณภาพ -> OCR เฉพาะหน้าที่จำเป็น (พร้อมเหตุผลรายหน้า)"""
import re
import threading
import time
import unicodedata

import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from pdfplumber.utils import extract_text

PDFIUM_LOCK = threading.RLock()  # pdfium ไม่ thread-safe: ใช้ร่วมกับเซิร์ฟเวอร์ตอนเรนเดอร์ภาพ


def render_page(pdoc, i, scale):
    with PDFIUM_LOCK:
        return pdoc[i].render(scale=scale).to_pil()


THAI = re.compile(r"[\u0E00-\u0E7F]")
BAD = re.compile(r"[\uE000-\uF8FF\uFFFD]|\(cid:\d+\)")


def _mark(ch):
    return unicodedata.category(ch) == "Mn" or ch == "\u0e33"


def clean_thai_spaces(t, aggressive=False):
    """PDF/OCR ภาษาไทยมักแทรกช่องว่างคั่นสระ/วรรณยุกต์ -> ลบช่องว่างระหว่างอักษรไทยที่ไม่ใช่ช่องว่างจริง"""
    t = t.replace("\u0e4d\u0e32", "\u0e33")  # นิคหิต+สระอา -> สระอำ
    out, n = [], len(t)
    for i, ch in enumerate(t):
        if ch == " " and 0 < i < n - 1:
            a, b = t[i - 1], t[i + 1]
            if THAI.match(a) and THAI.match(b) and (aggressive or _mark(a) or _mark(b)):
                continue
        out.append(ch)
    return "".join(out)


def direct_text(pg):
    """ดึงข้อความจากชั้นข้อความของ PDF
    วรรณยุกต์/สระบน-ล่างเป็นอักขระกว้าง 0 ซึ่งตัวเรียงตามแกน x ของไลบรารีมักเรียงสลับ
    -> ย้ายตำแหน่งไปกึ่งกลางพยัญชนะที่ถือ (ตามลำดับใน stream) ก่อนเรียงบรรทัด"""
    chars, base = [], None
    for c in pg.chars:
        c = dict(c)
        t = c["text"]
        if t and unicodedata.category(t[0]) == "Mn" and base is not None:
            c["x0"] = c["x1"] = (base["x0"] + base["x1"]) / 2
        else:
            base = c
        chars.append(c)
    return clean_thai_spaces(extract_text(chars) or ""), chars


def thai_orphan_ratio(t):
    marks = orphan = 0
    for i, ch in enumerate(t):
        if THAI.match(ch) and unicodedata.category(ch) == "Mn":
            marks += 1
            prev = t[i - 1] if i else ""
            if not prev or not THAI.match(prev) or prev == ch:
                orphan += 1
    return orphan / marks if marks else 0.0


# ---------------- OCR ----------------
_OCR = {}


def ocr_info(cfg=None):
    if "v" in _OCR:
        return _OCR["v"]
    try:
        import pytesseract
        if cfg and cfg.get("tesseract_cmd"):
            pytesseract.pytesseract.tesseract_cmd = cfg["tesseract_cmd"]
        pytesseract.get_tesseract_version()
        langs = set(pytesseract.get_languages())
        _OCR["v"] = {"ok": "eng" in langs, "thai": "tha" in langs, "langs": sorted(langs)}
    except Exception as e:  # noqa
        _OCR["v"] = {"ok": False, "thai": False, "langs": [], "err": str(e)[:200]}
    return _OCR["v"]


def _skew_angle(img):
    """หามุมเอียง (องศา) ด้วย projection profile บนภาพย่อ -- ใช้เฉพาะ PIL+numpy"""
    from PIL import Image
    small = img.copy()
    small.thumbnail((700, 700))
    a = np.asarray(small) < 150
    if a.sum() < 300:
        return 0.0
    base_img = Image.fromarray((a * 255).astype("uint8"))
    best, best_s, base = 0.0, -1.0, None
    for ang in np.arange(-5, 5.01, 0.5):
        r = base_img.rotate(float(ang), resample=Image.BILINEAR)
        s = float(np.var(np.asarray(r).sum(axis=1)))
        if abs(ang) < 1e-9:
            base = s
        if s > best_s:
            best, best_s = float(ang), s
    return best if base and best_s > base * 1.08 and abs(best) >= 1.0 else 0.0


def preprocess(img):
    """ปรับภาพเท่าที่จำเป็น: เพิ่มคอนทราสต์ถ้าจาง, แก้เอียงถ้าเอียงจริง"""
    from PIL import Image
    a = np.asarray(img)
    if a.std() < 45:
        lo, hi = np.percentile(a, 2), np.percentile(a, 98)
        if hi > lo:
            img = Image.fromarray(np.clip((a - lo) * 255.0 / (hi - lo), 0, 255).astype("uint8"))
    ang = _skew_angle(img)
    if ang:
        img = img.rotate(ang, resample=Image.BICUBIC, fillcolor=255)
    return img, ang


def run_ocr(img, cfg):
    import pytesseract
    info = ocr_info(cfg)
    lang = "tha+eng" if info["thai"] else "eng"
    img, ang = preprocess(img)

    def once(im):
        d = pytesseract.image_to_data(im, lang=lang, config="--psm 6", output_type=pytesseract.Output.DICT)
        lines, confs = {}, []
        for i, w in enumerate(d["text"]):
            if not w.strip() or float(d["conf"][i]) < 0:
                continue
            confs.append(float(d["conf"][i]))
            lines.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append(w)
        return "\n".join(" ".join(v) for v in lines.values()), (sum(confs) / len(confs) if confs else 0.0)

    text, conf = once(img)
    rotated = 0
    if conf < 50:  # อาจหมุนผิดทิศ -> ลองถาม OSD
        try:
            osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
            r = int(osd.get("rotate", 0))
            if r:
                t2, c2 = once(img.rotate(-r, expand=True, fillcolor=255))
                if c2 > conf:
                    text, conf, rotated = t2, c2, r
        except Exception:  # noqa
            pass
    return clean_thai_spaces(text, aggressive=True), conf, lang, ang, rotated


def _letters(t):
    return len(re.sub(r"[\W_]+", "", t))


# ---------------- ต่อหน้า ----------------
def read_pages(path, cfg=None, progress=None):
    cfg = cfg or {}
    mode = cfg.get("ocr", "auto")
    dpi = int(cfg.get("ocr_dpi", 300))
    oi = ocr_info(cfg) if mode != "off" else {"ok": False, "thai": False, "err": "ปิดใช้งานใน config.json"}
    pdoc = pdfium.PdfDocument(path)
    pages = []
    with pdfplumber.open(path) as pl:
        n = len(pl.pages)
        for i, pg in enumerate(pl.pages):
            t0 = time.time()
            if progress:
                progress(i, n, f"อ่านหน้า {i + 1}/{n}")
            text, chars = direct_text(pg)
            area = float(pg.width * pg.height) or 1.0
            tarea = sum(max(c["x1"] - c["x0"], 0) * max(c["bottom"] - c["top"], 0) for c in chars) / area
            arr = np.asarray(render_page(pdoc, i, 1.5).convert("L"))
            mh, mw = int(arr.shape[0] * 0.04), int(arr.shape[1] * 0.04)
            ink = float((arr[mh:arr.shape[0] - mh, mw:arr.shape[1] - mw] < 200).mean())  # ตัดขอบกันเงาสแกน
            cover = 0.0
            for im in pg.images:
                cover += max(0, min(im["x1"], pg.width) - max(im["x0"], 0)) * max(0, min(im["bottom"], pg.height) - max(im["top"], 0))
            cover = min(cover / area, 1.0)
            nchar = _letters(text)
            issues, need = [], "none"
            if nchar < 15 and ink < 0.0004:  # ขาวสนิท: ว่างแน่นอน
                pages.append(dict(n=i + 1, text="", method="blank", quality=1.0, issues=[], chars=0, ink=round(ink, 5),
                                  reason="หน้าว่าง: ไม่มีข้อความและไม่มีหมึกเลย", ms=int((time.time() - t0) * 1000)))
                continue
            if nchar < 15:
                issues.append(("no_text", "ไม่พบข้อความ แต่หน้ามีเนื้อหา (น่าจะเป็นภาพสแกน)"))
                need = "must"
            elif cover > 0.6 and nchar < 80:
                issues.append(("scanned", "หน้าเป็นภาพเต็มหน้า มีข้อความแฝงน้อยมาก"))
                need = "must"
            if BAD.search(text) and len(BAD.findall(text)) > 0.01 * max(len(text), 1):
                issues.append(("bad_chars", "พบอักขระผิดรูป/(cid:) มาก ข้อความตรงไม่น่าเชื่อถือ"))
                need = "must"
            if thai_orphan_ratio(text) > 0.04:
                issues.append(("thai_marks", "วรรณยุกต์/สระไทยลอยผิดตำแหน่ง ข้อความตรงเพี้ยน"))
                need = "must"
            if need == "none" and ink > 0.03 and tarea < 0.3 * ink and nchar < 400:
                issues.append(("ink_mismatch", "หมึกบนหน้ามากกว่าที่ข้อความอธิบายได้ (อาจมีภาพข้อความ)"))
                need = "check"
            method, reason, quality, extra = "text", "ข้อความตรงจาก PDF มีปริมาณและรูปแบบปกติ จึงไม่ใช้ OCR", 1.0, {}
            if mode == "always":
                need = "must"
            if need in ("must", "check") and oi["ok"]:
                img = render_page(pdoc, i, dpi / 72).convert("L")
                otext, conf, lang, ang, rot = run_ocr(img, cfg)
                if nchar < 15 and _letters(otext) < 15 and ink < 0.01:
                    pages.append(dict(n=i + 1, text="", method="blank", quality=0.9, issues=[], chars=0, ink=round(ink, 5),
                                      reason="หน้าว่าง: ไม่มีข้อความทั้งจากชั้นข้อความและ OCR และหมึกน้อยมาก (ยืนยันด้วย OCR)",
                                      ms=int((time.time() - t0) * 1000)))
                    continue
                extra = dict(ocr_conf=round(conf, 1), ocr_lang=lang, deskew=ang, rotated=rot)
                if need == "must" or _letters(otext) > 1.5 * nchar + 20:
                    text, method = otext, "ocr"
                    reason = "ใช้ OCR (%s): %s" % (lang, "; ".join(m for _, m in issues) or "ตั้งค่าให้ OCR ทุกหน้า")
                    quality = min(1.0, conf / 100.0 + 0.1)
                    if conf < 60:
                        issues.append(("ocr_low", f"OCR มั่นใจต่ำ ({conf:.0f}%) ควรตรวจด้วยตา"))
                    if not oi["thai"]:
                        issues.append(("no_tha", "ไม่ได้ติดตั้งภาษาไทยของ Tesseract (tha) จึง OCR ได้เฉพาะอังกฤษ"))
                        quality = min(quality, 0.7)
                else:
                    reason = "ตรวจเทียบกับ OCR แล้ว ข้อความตรงครบถ้วนพอ จึงใช้ข้อความตรง"
                    issues = []
            elif need == "must":
                quality = 0.2
                reason = "ควรใช้ OCR แต่ยังไม่ได้ติดตั้ง/เปิดใช้ Tesseract ข้อความหน้านี้จึงไม่น่าเชื่อถือ"
                issues.append(("ocr_unavailable", "ไม่มี OCR ใช้งาน: " + oi.get("err", "ปิดอยู่")))
            elif need == "check":
                quality = 0.8
                reason = "ข้อความตรงใช้ได้ แต่มีข้อสงสัยเรื่องภาพ ควรเปิด OCR เพื่อตรวจเทียบ"
            pages.append(dict(n=i + 1, text=text, method=method, quality=round(quality, 2),
                              issues=[dict(code=c, msg=m) for c, m in issues], chars=nchar, ink=round(ink, 4),
                              reason=reason, ms=int((time.time() - t0) * 1000), **extra))
    return pages
