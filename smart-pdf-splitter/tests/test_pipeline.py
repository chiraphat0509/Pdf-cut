"""ทดสอบกับไฟล์ตัวอย่างจริงของผู้ใช้ + เคสสังเคราะห์ที่รู้เฉลย  รัน:  python -m pytest -q"""
import io
import json
import os
import threading
import urllib.parse
import urllib.request
import zipfile
from http.server import HTTPServer
from uuid import uuid4

import pytest
from pypdf import PdfReader, PdfWriter

from splitter.classify import classify
from splitter.pipeline import analyze, export_pdf, export_zip, load_config
from splitter import server as server_module
from splitter.server import deduplicate_staged_uploads, file_sha256
from tests import make_cases as mc

HERE = os.path.dirname(__file__)
S = os.path.join(os.path.dirname(HERE), "samples")
CFG = dict(load_config(), ocr="off")


def groups(path, cfg=CFG):
    r = analyze(path, cfg)
    return r, [d["pages"] for d in r["docs"]]


def test_invoices_three_single_pages():
    r, g = groups(os.path.join(S, "invoices_3x1p.pdf"))
    assert g == [[1], [2], [3]]
    assert [d["doc_no"] for d in r["docs"]] == ["INV-2026-001", "INV-2026-002", "INV-2026-003"]


def test_po_english_three_single_pages():
    r, g = groups(os.path.join(S, "po_en_3x1p.pdf"))
    assert g == [[1], [2], [3]]
    assert all(d["group"] == "expense" and d["subtype"] == "po" for d in r["docs"])


def test_thai_po_split_by_excel_is_one_document():
    """ไฟล์ Excel ที่คอลัมน์ล้นไปหน้า 2: ต้องรวมเป็นเอกสารเดียว และอ่านไทยได้ถูกต้อง"""
    r, g = groups(os.path.join(S, "po_th_excel_split_2p.pdf"))
    assert g == [[1, 2]]
    d = r["docs"][0]
    assert d["doc_no"] == "PO-20261028-01" and d["group"] == "expense" and d["subtype"] == "po"
    assert "บริษัทสยามนวัตกรรมจำกัด" in r["pages"][0]["snippet"].replace(" ", "")


def test_all_samples_merged_into_one_pdf():
    from pypdf import PdfWriter
    w = PdfWriter()
    for f in ("invoices_3x1p", "po_en_3x1p", "po_th_excel_split_2p"):
        for p in PdfReader(os.path.join(S, f + ".pdf")).pages:
            w.add_page(p)
    b = io.BytesIO(); w.write(b)
    path = os.path.join(os.path.dirname(HERE), "data", "_merged_test.pdf")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(b.getvalue())
    _, g = groups(path)
    assert g == [[1], [2], [3], [4], [5], [6], [7, 8]]


@pytest.mark.parametrize("case", ["case_mixed", "case_repeated_number", "case_table_continuation", "case_blank_separated"])
def test_variable_length_documents(case, tmp_path):
    data, truth = mc.make_pdf(getattr(mc, case)())
    p = tmp_path / "t.pdf"; p.write_bytes(data)
    _, g = groups(str(p))
    assert g == truth


def test_types_in_mixed_case(tmp_path):
    data, truth = mc.make_pdf(mc.case_mixed())
    p = tmp_path / "t.pdf"; p.write_bytes(data)
    r, _ = groups(str(p))
    assert [d["group"] for d in r["docs"]] == ["sales", "sales", "sales", "expense", "payroll", "bank"]


def test_our_company_decides_invoice_direction(tmp_path):
    data, _ = mc.make_pdf(mc.case_mixed())
    p = tmp_path / "t.pdf"; p.write_bytes(data)
    buyer = analyze(str(p), dict(CFG, our_company_names=["Customer Co."]))
    assert [d["group"] for d in buyer["docs"][:3]] == ["expense"] * 3          # เราเป็นผู้ซื้อ -> ใบแจ้งหนี้ซื้อ
    seller = analyze(str(p), dict(CFG, our_company_names=["ABC Trading Co., Ltd."]))
    assert [d["group"] for d in seller["docs"][:3]] == ["sales"] * 3
    assert all(d["conf"] >= 0.75 for d in seller["docs"][:3])                  # รู้ทิศทางแล้วไม่ต้องเตือน


def test_export_keeps_every_page_and_content(tmp_path):
    src = os.path.join(S, "po_th_excel_split_2p.pdf")
    data, man = export_zip(src, [dict(name="a/b:c", pages=[1, 2])])
    z = zipfile.ZipFile(io.BytesIO(data))
    assert "a_b_c.pdf" in z.namelist()
    out = PdfReader(io.BytesIO(z.read("a_b_c.pdf")))
    assert len(out.pages) == 2
    assert out.pages[1].extract_text() == PdfReader(src).pages[1].extract_text()


def test_export_pdf_keeps_selected_pages_and_drops_blank_pages():
    src = os.path.join(S, "po_th_excel_split_2p.pdf")
    data = export_pdf(src, [1, 2], drop_pages=[1])
    output = PdfReader(io.BytesIO(data))
    assert len(output.pages) == 1
    assert output.pages[0].extract_text() == PdfReader(src).pages[1].extract_text()


def test_unknown_type_is_not_guessed(tmp_path):
    data, _ = mc.make_pdf([[[(14, "Meeting notes"), (11, "Agenda: lunch plans and parking")]]])
    p = tmp_path / "t.pdf"; p.write_bytes(data)
    r = analyze(str(p), CFG)
    assert r["docs"][0]["group"] == "other"


def test_sales_order_is_classified_as_sales():
    result = classify("SALES ORDER\nSales Order No: SO-2026-001")
    assert result["group"] == "sales"
    assert result["subtype"] == "sales_order"
    assert result["label"] == "ใบสั่งขาย (SO)"


def test_upload_hash_uses_file_contents(tmp_path):
    first = tmp_path / "first.pdf"
    renamed_duplicate = tmp_path / "renamed.pdf"
    different = tmp_path / "different.pdf"
    first.write_bytes(b"same PDF content")
    renamed_duplicate.write_bytes(b"same PDF content")
    different.write_bytes(b"different PDF content")

    assert file_sha256(str(first)) == file_sha256(str(renamed_duplicate))
    assert file_sha256(str(first)) != file_sha256(str(different))


def test_duplicate_uploads_keep_first_occurrence_and_order():
    staged = [
        ("one", {"name": "1.pdf", "sha256": "same"}),
        ("two", {"name": "2.pdf", "sha256": "unique"}),
        ("three", {"name": "3.pdf", "sha256": "same"}),
        ("four", {"name": "4.pdf", "sha256": "another"}),
    ]

    unique, duplicates = deduplicate_staged_uploads(staged)

    assert [job_id for job_id, _ in unique] == ["one", "two", "four"]
    assert [entry["name"] for _, entry in unique] == ["1.pdf", "2.pdf", "4.pdf"]
    assert duplicates == ["three"]


def test_upload_and_combine_api_skip_duplicate_content(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "DATA", str(tmp_path))
    monkeypatch.setattr(server_module, "JOBS", {})
    monkeypatch.setattr(server_module, "run_job", lambda job_id: None)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    pdf = io.BytesIO()
    writer.write(pdf)
    duplicate_content = pdf.getvalue()
    unique_writer = PdfWriter()
    unique_writer.add_blank_page(width=144, height=144)
    unique_pdf = io.BytesIO()
    unique_writer.write(unique_pdf)

    httpd = HTTPServer(("127.0.0.1", 0), server_module.H)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_port}"

    def upload(name, content):
        request = urllib.request.Request(
            f"{base_url}/api/upload?defer=1&name={name}",
            data=content,
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())

    try:
        first = upload("1.pdf", duplicate_content)
        duplicate = upload("3.pdf", duplicate_content)
        second = upload("2.pdf", unique_pdf.getvalue())
        merge_id = uuid4().hex
        request = urllib.request.Request(
            f"{base_url}/api/jobs/combine",
            data=json.dumps({
                "jobs": [first["job"], duplicate["job"], second["job"]],
                "merge_id": merge_id,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            result = json.loads(response.read())

        assert result["duplicates"] == [duplicate["job"]]
        assert [item["job"] for item in result["files"]] == [first["job"], second["job"]]
        assert [item["name"] for item in result["files"]] == ["1.pdf", "2.pdf"]
        assert len(PdfReader(os.path.join(tmp_path, merge_id, "source.pdf")).pages) == 2
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_export_pdf_api_returns_single_pdf_download(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "DATA", str(tmp_path))
    monkeypatch.setattr(server_module, "JOBS", {})
    job_id = uuid4().hex
    os.makedirs(os.path.join(tmp_path, job_id))
    source = os.path.join(S, "po_th_excel_split_2p.pdf")
    with open(source, "rb") as src, open(os.path.join(tmp_path, job_id, "source.pdf"), "wb") as dst:
        dst.write(src.read())
    server_module.JOBS[job_id] = {
        "status": "done",
        "name": "merged.pdf",
        "result": {
            "n_pages": 2,
            "pages": [{"n": 1, "blank": False}, {"n": 2, "blank": False}],
            "docs": [{"pages": [1]}],
            "stats": {"seconds": 1, "ocr_pages": 0},
        },
    }
    httpd = HTTPServer(("127.0.0.1", 0), server_module.H)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    request = urllib.request.Request(
        f"http://127.0.0.1:{httpd.server_port}/api/jobs/{job_id}/export/pdf",
        data=json.dumps({"name": "ใบแจ้งหนี้", "pages": [2]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            data = response.read()
            assert response.headers["Content-Type"] == "application/pdf"
            assert urllib.parse.unquote(response.headers["X-Download-Filename"]) == "ใบแจ้งหนี้.pdf"
        output = PdfReader(io.BytesIO(data))
        original = PdfReader(source)
        assert len(output.pages) == 1
        assert output.pages[0].extract_text() == original.pages[1].extract_text()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
