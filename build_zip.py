#!/usr/bin/env python3
"""
build_zip.py — Otomatis membuat folder project + ZIP berisi semua file.
Cara pakai:
  1. Copy semua file code block di atas ke path yang sesuai
     (main.py, backtest.py, ml_engine.py, requirements.txt, README.md,
      .github/workflows/bot.yml)
  2. Jalankan:  python build_zip.py
  3. File xauusd-bot.zip akan muncul di folder yang sama.
"""
import os, zipfile, shutil, sys

ROOT = "xauusd-bot"
FILES = {
    "main.py":            None,
    "backtest.py":        None,
    "ml_engine.py":       None,
    "requirements.txt":   None,
    "README.md":          None,
    ".github/workflows/bot.yml": None,
}

def collect_files():
    """Cari semua file di root project."""
    found = []
    for rel in FILES:
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            found.append(p)
        else:
            print(f"⚠️  Hilang: {p}")
    return found

def make_zip(paths, out="xauusd-bot.zip"):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, arcname=os.path.relpath(p, "."))
    print(f"✅ ZIP dibuat: {out} ({os.path.getsize(out)/1024:.1f} KB)")
    print(f"   Isi: {len(paths)} file")

if __name__ == "__main__":
    if not os.path.isdir(ROOT):
        print(f"❌ Folder '{ROOT}' tidak ditemukan.")
        print(f"   Buat folder '{ROOT}' lalu copy semua file ke dalamnya.")
        sys.exit(1)
    files = collect_files()
    if not files:
        print("❌ Tidak ada file ditemukan."); sys.exit(1)
    make_zip(files)
