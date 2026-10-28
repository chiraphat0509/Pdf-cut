"""เซิร์ฟเวอร์ในเครื่อง (stdlib ล้วน ไม่ต้องติดตั้ง framework) -- ผูกที่ 127.0.0.1 เท่านั้น เอกสารไม่ออกนอกเครื่อง"""
import json
import hashlib
import os
import re
import shutil
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pypdf import PdfReader, PdfWriter
import pypdfium2 as pdfium

from .pipeline import analyze, export_pdf, export_zip, load_config, safe_name
from .reader import PDFIUM_LOCK

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
WEB = os.path.join(ROOT, "web")
MAX_BYTES = 800 * 1024 * 1024
JOBS = {}
CFG = load_config()
ID_RE = re.compile(r"^[0-9a-f]{32}$")


def job_dir(jid):
    return os.path.join(DATA, jid)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deduplicate_staged_uploads(staged):
    seen_hashes, unique_staged, duplicate_ids = set(), [], []
    for source_id, entry in staged:
        if entry["sha256"] in seen_hashes:
            duplicate_ids.append(source_id)
        else:
            seen_hashes.add(entry["sha256"])
            unique_staged.append((source_id, entry))
    return unique_staged, duplicate_ids


def run_job(jid):
    j = JOBS[jid]

    def prog(i, n, msg):
        j.update(progress=round(i / max(n, 1), 3), msg=msg)
    try:
        r = analyze(os.path.join(job_dir(jid), "source.pdf"), CFG, prog)
        with open(os.path.join(job_dir(jid), "result.json"), "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False)
        j.update(status="done", progress=1.0, result=r, msg="เสร็จแล้ว")
    except Exception as e:  # noqa
        traceback.print_exc()
        j.update(status="error", error=f"{type(e).__name__}: {e}", msg="ผิดพลาด")


def get_job(jid):
    if not ID_RE.match(jid):
        return None
    if jid not in JOBS:
        rp = os.path.join(job_dir(jid), "result.json")
        if os.path.exists(rp):
            with open(rp, encoding="utf-8") as f:
                JOBS[jid] = dict(status="done", progress=1.0, msg="โหลดผลเดิม", result=json.load(f),
                                 name=open(os.path.join(job_dir(jid), "name.txt"), encoding="utf-8").read()
                                 if os.path.exists(os.path.join(job_dir(jid), "name.txt")) else "document.pdf")
            metadata = os.path.join(job_dir(jid), "combine.json")
            if os.path.isfile(metadata):
                with open(metadata, encoding="utf-8") as f:
                    JOBS[jid]["files"] = json.load(f).get("files", [])
    return JOBS.get(jid)


def get_staged_upload(jid):
    if not ID_RE.match(jid):
        return None
    j = JOBS.get(jid)
    if j and j.get("deferred"):
        j.setdefault("sha256", file_sha256(os.path.join(job_dir(jid), "source.pdf")))
        return j
    metadata = os.path.join(job_dir(jid), "pending.json")
    if not os.path.isfile(metadata):
        return None
    with open(metadata, encoding="utf-8") as f:
        j = json.load(f)
    j["deferred"] = True
    j.setdefault("sha256", file_sha256(os.path.join(job_dir(jid), "source.pdf")))
    JOBS[jid] = j
    return j


class H(BaseHTTPRequestHandler):
    server_version = "SmartPDFSplitter/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            with open(os.path.join(WEB, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        m = re.match(r"^/api/jobs/([0-9a-f]{32})$", u.path)
        if m:
            j = get_job(m.group(1)) or get_staged_upload(m.group(1))
            if not j:
                return self._send(404, {"error": "ไม่พบงาน"})
            return self._send(200, {k: j.get(k) for k in (
                "status", "progress", "msg", "error", "result", "name", "files", "sha256"
            )})
        m = re.match(r"^/api/jobs/([0-9a-f]{32})/page/(\d+)\.png$", u.path)
        if m:
            return self._page_png(m.group(1), int(m.group(2)), int(q.get("w", ["200"])[0]))
        self._send(404, {"error": "not found"})

    def _page_png(self, jid, n, w):
        j = get_job(jid)
        if not j:
            return self._send(404, {"error": "ไม่พบงาน"})
        w = max(60, min(w, 1800))
        cache = os.path.join(job_dir(jid), "cache")
        os.makedirs(cache, exist_ok=True)
        fp = os.path.join(cache, f"{n}_{w}.png")
        if not os.path.exists(fp):
            with PDFIUM_LOCK:
                pdf = pdfium.PdfDocument(os.path.join(job_dir(jid), "source.pdf"))
                if not 1 <= n <= len(pdf):
                    return self._send(404, {"error": "ไม่มีหน้านี้"})
                pg = pdf[n - 1]
                img = pg.render(scale=w / pg.get_width()).to_pil().convert("RGB")
                img.save(fp)
        with open(fp, "rb") as f:
            self._send(200, f.read(), "image/png", {"Cache-Control": "max-age=3600"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        ln = int(self.headers.get("Content-Length", 0))
        if u.path == "/api/upload":
            if ln <= 0 or ln > MAX_BYTES:
                return self._send(400, {"error": "ขนาดไฟล์ไม่ถูกต้อง"})
            jid = uuid.uuid4().hex
            os.makedirs(job_dir(jid), exist_ok=True)
            head, left = b"", ln
            with open(os.path.join(job_dir(jid), "source.pdf"), "wb") as f:
                while left:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    if not head:
                        head = chunk[:5]
                    f.write(chunk)
                    left -= len(chunk)
            if left:
                shutil.rmtree(job_dir(jid), ignore_errors=True)
                return self._send(400, {"error": "อัปโหลดไฟล์ไม่ครบ"})
            if head != b"%PDF-":
                shutil.rmtree(job_dir(jid), ignore_errors=True)
                return self._send(400, {"error": "ไม่ใช่ไฟล์ PDF"})
            name = os.path.basename(q.get("name", ["document.pdf"])[0].replace("\\", "/")) or "document.pdf"
            sha256 = file_sha256(os.path.join(job_dir(jid), "source.pdf"))
            with open(os.path.join(job_dir(jid), "name.txt"), "w", encoding="utf-8") as f:
                f.write(name)
            if q.get("defer", ["0"])[0] == "1":
                staged = dict(status="uploaded", progress=0.0, msg="อัปโหลดแล้ว", name=name,
                              sha256=sha256, deferred=True)
                with open(os.path.join(job_dir(jid), "pending.json"), "w", encoding="utf-8") as f:
                    json.dump(staged, f, ensure_ascii=False)
                JOBS[jid] = staged
                return self._send(200, {"job": jid, "name": name, "sha256": sha256})
            JOBS[jid] = dict(status="running", progress=0.0, msg="เริ่มวิเคราะห์", name=name)
            threading.Thread(target=run_job, args=(jid,), daemon=True).start()
            return self._send(200, {"job": jid})
        if u.path == "/api/jobs/combine":
            if ln <= 0 or ln > 1 << 20:
                return self._send(400, {"error": "รายการไฟล์ไม่ถูกต้อง"})
            try:
                body = json.loads(self.rfile.read(ln))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._send(400, {"error": "อ่านรายการไฟล์ไม่สำเร็จ"})
            ids = body.get("jobs") if isinstance(body, dict) else None
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
                    or any(not isinstance(jid, str) or not ID_RE.fullmatch(jid) for jid in ids)
                    or len(set(ids)) != len(ids)):
                return self._send(400, {"error": "รายการไฟล์ไม่ถูกต้อง"})
            target_id = body.get("merge_id")
            if not isinstance(target_id, str) or not ID_RE.fullmatch(target_id) or target_id in ids:
                return self._send(400, {"error": "รหัสงานรวมไฟล์ไม่ถูกต้อง"})
            staged = [(jid, get_staged_upload(jid)) for jid in ids]
            if any(not entry or not os.path.isfile(os.path.join(job_dir(jid), "source.pdf"))
                   for jid, entry in staged):
                return self._send(400, {"error": "ไม่พบไฟล์ที่อัปโหลด กรุณาเลือกไฟล์อีกครั้ง"})
            staged, duplicate_ids = deduplicate_staged_uploads(staged)
            ids = [source_id for source_id, _ in staged]
            merge_metadata = os.path.join(job_dir(target_id), "combine.json")
            if os.path.isfile(merge_metadata):
                with open(merge_metadata, encoding="utf-8") as f:
                    prior = json.load(f)
                previous = get_job(target_id)
                if prior.get("jobs") == ids:
                    if not previous:
                        name_path = os.path.join(job_dir(target_id), "name.txt")
                        with open(name_path, encoding="utf-8") as f:
                            name = f.read()
                        previous = dict(status="running", progress=0.0, msg="เริ่มวิเคราะห์ไฟล์ที่รวมแล้ว", name=name)
                        JOBS[target_id] = previous
                        threading.Thread(target=run_job, args=(target_id,), daemon=True).start()
                    return self._send(200, {"job": target_id, "name": previous.get("name"),
                                            "files": previous.get("files", prior.get("files", [])),
                                            "duplicates": duplicate_ids})
                if previous and previous.get("status") == "running":
                    return self._send(409, {"error": "งานกำลังวิเคราะห์อยู่"})
            if os.path.exists(job_dir(target_id)):
                if not os.path.isfile(merge_metadata):
                    return self._send(409, {"error": "รหัสงานนี้ถูกใช้ไปแล้ว"})
            total_bytes = sum(os.path.getsize(os.path.join(job_dir(jid), "source.pdf")) for jid, _ in staged)
            if total_bytes > MAX_BYTES:
                return self._send(400, {"error": "ขนาดไฟล์รวมเกินขีดจำกัด"})

            names = [entry["name"] for _, entry in staged]
            name = f"{os.path.splitext(names[0])[0]}_combined_{len(names)}_files.pdf"
            jid = target_id
            os.makedirs(job_dir(jid), exist_ok=True)
            try:
                writer = PdfWriter()
                for source_id, _ in staged:
                    reader = PdfReader(os.path.join(job_dir(source_id), "source.pdf"))
                    if reader.is_encrypted and not reader.decrypt(""):
                        raise ValueError("ไฟล์ PDF มีรหัสผ่านและไม่สามารถรวมได้")
                    writer.append(reader)
                with open(os.path.join(job_dir(jid), "source.pdf"), "wb") as f:
                    writer.write(f)
                with open(os.path.join(job_dir(jid), "name.txt"), "w", encoding="utf-8") as f:
                    f.write(name)
                cache_dir = os.path.join(job_dir(jid), "cache")
                if os.path.isdir(cache_dir):
                    shutil.rmtree(cache_dir)
                result_path = os.path.join(job_dir(jid), "result.json")
                if os.path.isfile(result_path):
                    os.remove(result_path)
                files = [{"job": sid, "name": entry["name"], "sha256": entry["sha256"]}
                         for sid, entry in staged]
                with open(merge_metadata, "w", encoding="utf-8") as f:
                    json.dump({"jobs": ids, "files": files}, f)
            except Exception as e:
                return self._send(400, {"error": f"รวมไฟล์ PDF ไม่สำเร็จ: {type(e).__name__}: {e}"})

            JOBS[jid] = dict(status="running", progress=0.0, msg="เริ่มวิเคราะห์ไฟล์ที่รวมแล้ว",
                             name=name, files=files)
            threading.Thread(target=run_job, args=(jid,), daemon=True).start()
            return self._send(200, {"job": jid, "name": name, "files": files, "duplicates": duplicate_ids})
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/export/pdf", u.path)
        if m:
            jid = m.group(1)
            j = get_job(jid)
            if not j or j["status"] != "done":
                return self._send(404, {"error": "งานยังไม่พร้อม"})
            if ln <= 0 or ln > 1 << 20:
                return self._send(400, {"error": "ข้อมูลส่งออกไม่ถูกต้อง"})
            try:
                body = json.loads(self.rfile.read(ln))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._send(400, {"error": "อ่านข้อมูลส่งออกไม่สำเร็จ"})
            pages = body.get("pages") if isinstance(body, dict) else None
            name = body.get("name") if isinstance(body, dict) else None
            drop_blank = body.get("drop_blank", True) if isinstance(body, dict) else True
            if (not isinstance(pages, list) or not pages
                    or any(not isinstance(page, int) or isinstance(page, bool) for page in pages)
                    or len(set(pages)) != len(pages)
                    or any(not 1 <= page <= j["result"]["n_pages"] for page in pages)
                    or (name is not None and not isinstance(name, str))
                    or not isinstance(drop_blank, bool)):
                return self._send(400, {"error": "ข้อมูลส่งออกไม่ถูกต้อง"})
            blank = [page["n"] for page in j["result"]["pages"] if page["blank"]] if drop_blank else []
            try:
                data = export_pdf(os.path.join(job_dir(jid), "source.pdf"), pages, blank)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": f"สร้าง PDF ไม่สำเร็จ: {type(e).__name__}: {e}"})
            filename = safe_name(name or "document") + ".pdf"
            self._log_edits(jid, j["result"], [{"name": name or "document", "pages": pages}])
            return self._send(200, data, "application/pdf", {
                "Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(filename),
                "X-Download-Filename": urllib.parse.quote(filename),
            })
        m = re.match(r"^/api/jobs/([0-9a-f]{32})/export$", u.path)
        if m:
            j = get_job(m.group(1))
            if not j or j["status"] != "done":
                return self._send(404, {"error": "งานยังไม่พร้อม"})
            body = json.loads(self.rfile.read(ln) or b"{}")
            r = j["result"]
            blank = [p["n"] for p in r["pages"] if p["blank"]] if body.get("drop_blank", True) else []
            docs = body.get("docs", [])
            used = {p for d in docs for p in d["pages"]}
            if any(not 1 <= p <= r["n_pages"] for p in used):
                return self._send(400, {"error": "เลขหน้าไม่ถูกต้อง"})
            data, manifest = export_zip(os.path.join(job_dir(m.group(1)), "source.pdf"), docs, blank)
            self._log_edits(m.group(1), r, docs)
            missing = sorted(set(range(1, r["n_pages"] + 1)) - used - set(blank))
            nm = re.sub(r"\.pdf$", "", j.get("name", "document"), flags=re.I) + "_split.zip"
            return self._send(200, data, "application/zip", {
                "Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(nm),
                "X-Pages-Not-Exported": ",".join(map(str, missing))})
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{32})", urllib.parse.urlparse(self.path).path)
        if not m:
            return self._send(404, {"error": "not found"})
        jid = m.group(1)
        staged = get_staged_upload(jid)
        j = get_job(jid)
        has_pdf_job = j and j.get("status") != "running" and os.path.isfile(
            os.path.join(job_dir(jid), "source.pdf"))
        if not staged and not has_pdf_job:
            return self._send(404, {"error": "ไม่พบไฟล์ที่ลบได้"})
        if j and j.get("status") == "running":
            return self._send(409, {"error": "ไม่สามารถลบงานที่กำลังวิเคราะห์"})
        try:
            shutil.rmtree(job_dir(jid))
        except OSError as e:
            return self._send(500, {"error": f"ลบไฟล์ไม่สำเร็จ: {e}"})
        JOBS.pop(jid, None)
        return self._send(200, {"deleted": jid})

    def _log_edits(self, jid, r, docs):
        """เก็บสถิติการแก้ (ไม่เก็บเนื้อหาเอกสาร) ไว้วัดผล: จำนวนจุดที่ผู้ใช้ต้องแก้"""
        a = {d["pages"][0] for d in r["docs"]}
        b = {min(d["pages"]) for d in docs if d["pages"]}
        line = dict(ts=int(time.time()), pages=r["n_pages"], auto_docs=len(a), final_docs=len(b),
                    boundary_changes=len(a ^ b), seconds=r["stats"]["seconds"], ocr_pages=r["stats"]["ocr_pages"])
        with open(os.path.join(DATA, "corrections.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")


def serve(open_browser=True):
    os.makedirs(DATA, exist_ok=True)
    host, port = CFG.get("host", "127.0.0.1"), int(CFG.get("port", 8765))
    srv = ThreadingHTTPServer((host, port), H)
    url = f"http://{host}:{port}/"
    print("Smart PDF Splitter พร้อมใช้งานที่", url, "(กด Ctrl+C เพื่อหยุด)")
    if open_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
