"""Recon: extract text from every file in the dataset, identify by CONTENT not name."""
import sys, os, json, hashlib, pathlib

SRC = pathlib.Path(sys.argv[1])
OUT = pathlib.Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)

import fitz

index = []
for p in sorted(SRC.iterdir()):
    if not p.is_file():
        continue
    raw = p.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()[:12]
    kind, text, pages = "unknown", "", 0
    if raw[:5] == b"%PDF-":
        kind = "pdf"
        try:
            with fitz.open(stream=raw, filetype="pdf") as doc:
                pages = doc.page_count
                text = "\n".join(pg.get_text() for pg in doc)
        except Exception as e:
            kind, text = "pdf_broken", f"ERROR {e}"
    else:
        try:
            text = raw.decode("utf-8")
            kind = "text"
        except UnicodeDecodeError:
            kind = "binary"
            text = ""
    tp = OUT / f"{p.name}.txt"
    tp.write_text(text, encoding="utf-8")
    index.append({
        "file": p.name, "sha": sha, "kind": kind, "pages": pages,
        "bytes": len(raw), "chars": len(text),
    })

(OUT / "_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"extracted {len(index)} files")
for k in sorted({i['kind'] for i in index}):
    print(f"  {k}: {sum(1 for i in index if i['kind']==k)}")
