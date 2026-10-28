"""ใช้จากบรรทัดคำสั่ง:  python -m splitter.cli ไฟล์.pdf [--export out.zip] [--json]"""
import argparse
import json
import sys

from .pipeline import analyze, default_name, export_zip, load_config


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--export", help="เขียน ZIP ของไฟล์ที่แยกแล้ว")
    ap.add_argument("--json", action="store_true", help="พิมพ์ผลเต็มเป็น JSON")
    a = ap.parse_args(argv)
    r = analyze(a.pdf, load_config())
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        for p in r["pages"]:
            b = p["boundary"]
            print(f"หน้า {p['n']:>3} [{p['method']:<5}] เลข={p['doc_no'] or '-':<18} จุดตัด={'ตัด' if b['start'] else 'ต่อ'} มั่นใจ={b['conf']:.0%}")
        for d in r["docs"]:
            print(f"ชุด {d['id']}: หน้า {d['pages']} | {d['label']} ({d['conf']:.0%}) | เลข {d['doc_no']} | ต้องตรวจ={len(d['flags'])}")
            for f in d["flags"]:
                print("   -", f["level"], f["msg"])
        print("สถิติ:", r["stats"])
    if a.export:
        docs = [dict(name=default_name(d, d["id"]), pages=d["pages"], type=d["group"]) for d in r["docs"]]
        drop = [p["n"] for p in r["pages"] if p["blank"]]
        data, _ = export_zip(a.pdf, docs, drop)
        with open(a.export, "wb") as f:
            f.write(data)
        print("เขียน", a.export)


if __name__ == "__main__":
    sys.exit(main())
