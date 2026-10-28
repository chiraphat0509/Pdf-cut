import os
import re
import http.server
import socketserver

PORT = 8080
OUTPUT_ROOT = "extracted_documents"

# คลังข้อมูลคำค้นหาอัจฉริยะเพื่อจำแนกประเภทเอกสาร (อิงตามภาพที่คุณส่งมา)
# ระบบจะสแกนหาคำเหล่านี้เพื่อจัดกลุ่มว่าเป็นเอกสารอะไรอัตโนมัติ
DOCUMENT_CLASSIFIER = {
    "Quotation": [r"quotation", r"ใบเสนอราคา"],
    "Sales_Order_SO": [r"sales\s*order", r"ใบสั่งขาย", r"\(so\)"],
    "Purchase_Requisition_PR": [r"purchase\s*requisition", r"ใบขอซื้อ", r"\(pr\)"],
    "Purchase_Order_PO": [r"purchase\s*order", r"ใบสั่งซื้อ", r"\(po\)"],
    "Work_Order": [r"work\s*order", r"ใบสั่งงาน"],
    "Delivery_Note": [r"delivery\s*note", r"ใบส่งสินค้า", r"ใบส่งของ"],
    "Goods_Receipt_Note": [r"goods\s*receipt", r"ใบรับสินค้า"],
    "Asset_Acceptance": [r"asset\s*acceptance", r"ใบตรวจรับสินทรัพย์"],
    "Job_Completion": [r"job\s*completion", r"ใบส่งมอบงาน"],
    "Invoice": [r"invoice", r"ใบแจ้งหนี้", r"ใบกำกับภาษี"],
    "Billing_Note": [r"billing\s*note", r"ใบวางบิล"],
    "Bank_Statement": [r"bank\s*statement", r"รายการเดินบัญชี"],
    "Payroll": [r"payroll", r"สลิปเงินเดือน", r"ใบจ่ายเงินเดือน"]
}

# แพทเทิร์นการหาเลขที่เอกสารทั่วไปเพื่อนำมาตั้งชื่อไฟล์
DOC_NUMBER_PATTERNS = [
    r"[A-Z]{2,4}-\d{4}-\d{3,6}",  # เช่น INV-2026-001, PO-2026-0004
    r"[A-Z0-9]{3,6}-\d{5,8}",     # เช่น QT2026-00001
    r"No\.\s*:\s*([A-Z0-9\-]+)",   # จับกลุ่มคำหลังข้อความ No:
    r"เลขที่\s*:\s*([A-Z0-9\-]+)"   # จับกลุ่มคำหลังข้อความ เลขที่:
]

def classify_and_split_pdf(input_pdf_path, output_root):
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(input_pdf_path)
    
    current_writer = None
    current_doc_type = None
    current_doc_id = None
    success_files = []
    
    file_counter = 1

    for idx, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        text_lower = text.lower()
        
        detected_type = None
        detected_id = None
        
        # 1. ค้นหาประเภทเอกสารอัจฉริยะตาม Classifier Dictionary
        for doc_type, patterns in DOCUMENT_CLASSIFIER.items():
            for pt in patterns:
                if re.search(pt, text_lower):
                    detected_type = doc_type
                    break
            if detected_type:
                break
                
        # 2. ค้นหารหัส/เลขที่เอกสารเพื่อนำมาใช้เป็นชื่อไฟล์เฉพาะตัว
        for pt in DOC_NUMBER_PATTERNS:
            match = re.search(pt, text)
            if match:
                detected_id = match.group(0) if not match.groups() else match.group(1)
                # คลีนตัวอักษรแปลกปลอมออกให้ปลอดภัยต่อการเซฟไฟล์
                detected_id = "".join([c for c in detected_id if c.isalnum() or c in '-_']).strip()
                break

        # ตรวจสอบว่าเป็นจุดเริ่มต้นเอกสารชุดใหม่หรือไม่
        # เงื่อนไขคือ: เจอประเภทเอกสารใหม่ หรือเจอประเภทเดิมแต่เลขที่เอกสารเปลี่ยนไป
        is_new_document = False
        if detected_type:
            if detected_type != current_doc_type:
                is_new_document = True
            elif detected_id and detected_id != current_doc_id:
                is_new_document = True

        # 3. จัดการบันทึกไฟล์ชุดเก่าเมื่อขึ้นชุดใหม่
        if is_new_document:
            if current_writer and len(current_writer.pages) > 0:
                save_segmented_file(current_writer, output_root, current_doc_type, current_doc_id, file_counter, success_files)
                file_counter += 1
            
            current_writer = PdfWriter()
            current_doc_type = detected_type
            current_doc_id = detected_id if detected_id else f"Unknown_No_{file_counter}"

        # หากหน้าแรกสุดของไฟล์เปิดขึ้นมาไม่เจอคีย์เวิร์ด
        if current_writer is None:
            current_writer = PdfWriter()
            current_doc_type = detected_type if detected_type else "Unclassified"
            current_doc_id = detected_id if detected_id else f"Document_{file_counter}"

        current_writer.add_page(page)

    # บันทึกชุดสุดท้ายหลังจบลูป
    if current_writer and len(current_writer.pages) > 0:
        save_segmented_file(current_writer, output_root, current_doc_type, current_doc_id, file_counter, success_files)

    return success_files

def save_segmented_file(writer, root_dir, doc_type, doc_id, counter, success_list):
    # แยกเก็บลงโฟลเดอร์ตามประเภทเอกสารย่อยโดยอัตโนมัติ เพื่อความเรียบร้อย
    target_folder = os.path.join(root_dir, doc_type)
    if not os.path.exists(target_folder):
        os.makedirs(target_folder)
        
    filename = f"{doc_id}.pdf"
    output_path = os.path.join(target_folder, filename)
    
    # หากชื่อซ้ำ ป้องกันการเขียนทับด้วยการใส่ตัวเลขต่อท้าย
    if os.path.exists(output_path):
        filename = f"{doc_id}_{counter}.pdf"
        output_path = os.path.join(target_folder, filename)
        
    with open(output_path, "wb") as f:
        writer.write(f)
        
    success_list.append(f"[{doc_type}] {filename} ({len(writer.pages)} หน้า)")

class SmartSplitterHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            
            html_content = """
            <!DOCTYPE html>
            <html lang="th">
            <head>
                <meta charset="UTF-8">
                <title>PDF Multi-Type Smart Splitter</title>
                <style>
                    body { font-family: 'Segoe UI', sans-serif; background-color: #f4f6f9; color: #333; margin: 40px auto; max-width: 850px; padding: 20px; }
                    .card { background: white; padding: 30px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin-bottom: 20px; }
                    h1 { color: #2c3e50; text-align: center; margin-top:0; }
                    .btn { background-color: #3498db; color: white; border: none; padding: 14px 24px; font-size: 16px; border-radius: 6px; cursor: pointer; width: 100%; font-weight: bold; }
                    .btn:hover { background-color: #2980b9; }
                    .file-input { margin-bottom: 20px; padding: 20px; width: 95%; border: 2px dashed #bdc3c7; border-radius: 6px; background: #fafafa; text-align: center; }
                    .info-tag { background-color: #e8f4fd; border-left: 4px solid #3498db; padding: 12px; font-size: 14px; margin-bottom: 20px; border-radius: 4px; line-height:1.5; }
                    iframe { width: 100%; height: 350px; border: 1px solid #ddd; border-radius: 6px; margin-top: 10px; display:none; }
                    ul { padding-left: 20px; color: #555; }
                </style>
            </head>
            <body>
                <div class="card">
                    <h1>📑 PDF Multi-Type Smart Splitter</h1>
                    <div class="info-tag">
                        🎯 <b>ระบบจำแนกและแยกชุดเอกสารอัตโนมัติ:</b><br>
                        รองรับการแยกประเภทกลุ่มงาน เช่น Quotation, PO, PR, SO, Invoice, Delivery Note, Billing Note และจัดหมวดหมู่ลงโฟลเดอร์แยกกันให้อัตโนมัติด้วยความแม่นยำสูง
                    </div>
                    
                    <form method="POST" enctype="multipart/form-data" action="/upload">
                        <label style="font-weight:bold; display:block; margin-bottom:10px;">เลือกไฟล์ PDF ที่รวมเอกสารหลายชุดเข้ามา:</label>
                        <input type="file" name="pdf_file" accept=".pdf" class="file-input" id="pdf-file-input" required><br>
                        <iframe id="pdf-preview"></iframe>
                        <br>
                        <button type="submit" class="btn">⚡ สแกน จำแนกประเภท และแยกไฟล์ทันที</button>
                    </form>
                </div>

                <script>
                    document.getElementById('pdf-file-input').addEventListener('change', function(e) {
                        const file = e.target.files[0];
                        if (file) {
                            const fileURL = URL.createObjectURL(file);
                            const iframe = document.getElementById('pdf-preview');
                            iframe.src = fileURL;
                            iframe.style.display = 'block';
                        }
                    });
                </script>
            </body>
            </html>
            """
            self.wfile.write(html_content.encode('utf-8'))
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == '/upload':
            content_length = int(self.headers['Content-Length'])
            raw_data = self.rfile.read(content_length)
            
            try:
                boundary = self.headers['Content-Type'].split("=")[1].encode()
                parts = raw_data.split(b"--" + boundary)
                
                pdf_bytes = b""
                for part in parts:
                    if b'name="pdf_file"' in part and b"application/pdf" in part:
                        head, body = part.split(b"\r\n\r\n", 1)
                        pdf_bytes = body.rsplit(b"\r\n", 1)[0]

                if pdf_bytes:
                    temp_input_path = "temp_uploaded.pdf"
                    with open(temp_input_path, 'wb') as f:
                        f.write(pdf_bytes)
                    
                    results = classify_and_split_pdf(temp_input_path, OUTPUT_ROOT)
                    
                    if os.path.exists(temp_input_path):
                        os.remove(temp_input_path)
                else:
                    results = ["ไม่พบไฟล์ที่อัปโหลดเข้ามา"]
            except Exception as e:
                results = [f"เกิดข้อผิดพลาดในการรันระบบ: {str(e)}"]

            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            
            result_list = "".join([f"<li style='margin-bottom:6px;'>✅ {item}</li>" for item in results])
            abs_path = os.path.abspath(OUTPUT_ROOT)
            response_html = f"""
<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <title>ผลการแยกเอกสาร</title>
    <style>
        body {{ font-family: sans-serif; max-width: 650px; margin: 50px auto; padding: 20px; line-height: 1.6; background-color: #f4f6f9; }}
        .success-card {{ background: white; padding: 30px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1); }}
        .folder-path {{ background: #eef2f7; padding: 12px; border-radius: 6px; font-family: monospace; word-break: break-all; color: #2c3e50; font-weight: bold; }}
        .btn {{ display: inline-block; background: #3498db; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; margin-top: 20px; font-weight: bold; }}
        ul {{ background: #fdfefe; border: 1px solid #e1e8ed; padding: 15px 15px 15px 35px; border-radius: 6px; max-height: 300px; overflow-y: auto; }}
    </style>
</head>
<body>
    <div class="success-card">
        <h1>🎉 ประมวลผลและแยกเอกสารสำเร็จ!</h1>
        <p>ระบบตรวจสอบคีย์เวิร์ดของเอกสารย่อยแล้วแยกเก็บไว้ที่โฟลเดอร์ปลายทางหลัก:</p>
        <p class="folder-path">{abs_path}</p>
        <p>รายการไฟล์ที่ตรวจพบและดึงออกมาได้:</p>
        <ul>{result_list}</ul>
        <a class="btn" href="/">⬅️ กลับไปประมวลผลไฟล์เพิ่ม</a>
    </div>
</body>
</html>
"""
            self.wfile.write(response_html.encode("utf-8"))


with socketserver.TCPServer(("", PORT), SmartSplitterHandler) as httpd:
    print(f"🌍 ระบบ Smart Classifier พร้อมใช้งานแล้ว! -> http://localhost:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 ปิดการทำงาน")