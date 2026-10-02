#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live Monitor untuk TSMOM GOLD.
Dijalankan harian (misal: jam 16:00 WIB setelah pasar AS tutup) untuk cek sinyal.
"""
import os
import sys
import main as M
import requests
from datetime import datetime, timezone

# Import fungsi dari skrip utama (pastikan tsmom_gold.py ada di folder yang sama)
# Atau salin fungsi fetch_daily, calculate_atr, get_realized_volatility ke sini.
# Untuk kesederhanaan, kita asumsikan kita memanggil logika dasar di sini.

def get_latest_signal():
    M.log("🔍 Mengecek sinyal TSMOM GOLD hari ini...")
    
    # 1. Ambil data 1 tahun terakhir
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1d&range=1y"
    try:
        r = requests.get(url, headers=M.HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()["chart"]["result"][0]
        closes = data["indicators"]["quote"][0]["close"]
        timestamps = data["timestamp"]
    except Exception as e:
        M.log(f"❌ Gagal ambil data: {e}")
        return

    # 2. Pastikan data cukup
    if len(closes) < 252:
        M.log("⚠️ Data tidak cukup (kurang dari 252 hari).")
        return

    current_price = closes[-1]
    past_price = closes[-252] # Harga 252 hari lalu
    
    # 3. Hitung Sinyal Sederhana (TSMOM 12-month)
    if current_price > past_price:
        signal = "🟢 LONG (BULLISH)"
        color = "🟢"
    else:
        signal = "🔴 SHORT (BEARISH)"
        color = "🔴"

    # 4. Kirim Notifikasi
    msg = (
        f"{color} <b>SINYAL HARIAN TSMOM GOLD</b>\n"
        f"📅 Tanggal: {datetime.now(timezone.utc).strftime('%d %b %Y')}\n"
        f"💰 Instrumen: Gold Futures (GC=F)\n"
        f"📊 Harga Saat Ini: ${current_price:.2f}\n"
        f"📈 Harga 252 Hari Lalu: ${past_price:.2f}\n"
        f"──────────────────\n"
        f"<b>Status: {signal}</b>\n"
        f"<i>Catatan: Ini adalah sinyal mentah. Selalu konfirmasi dengan manajemen risiko (ATR & Volatility Targeting) sebelum eksekusi.</i>"
    )
    
    M.log(f"Sinyal terdeteksi: {signal}")
    M.send_telegram(msg)

if __name__ == "__main__":
    get_latest_signal()
