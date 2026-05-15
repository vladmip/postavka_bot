"""Извлечение текста из WB API PDF в txt-файлы. Запуск:
   python -X utf8 scripts/extract_wb_pdfs.py
"""
from pdfminer.high_level import extract_text
import glob, os, sys, re

src_dir = "файлы для показа клоду"
files = sorted(glob.glob(os.path.join(src_dir, "Документация - WB API*.pdf"))
               + glob.glob(os.path.join(src_dir, "Документация — WB API*.pdf")))

for f in files:
    base = os.path.basename(f).replace(".pdf", "")
    base = re.sub(r"Документация . WB API", "", base).strip(" -")
    name = base if base else "main"
    name_slug = re.sub(r"[^a-zа-яёA-ZА-ЯЁ0-9]+", "_", name, flags=re.IGNORECASE).strip("_").lower()
    out = f"wb_docs__{name_slug or 'main'}.txt"
    print("->", f, "=>", out)
    try:
        text = extract_text(f)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("   ", len(text), "chars")
    except Exception as e:
        print("   ERR:", e)
