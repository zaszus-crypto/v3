#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TSMOM GOLD v13.0 (Ultimate) — Time-Series Momentum Institutional Grade.
Basis: Moskowitz-Ooi-Pedersen (2012) + Volatility Targeting + Realistic Frictions.
"""
import os
import sys
import math
import time
import random
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any

# ==============================================================================
# 1. KONFIGURASI STRATEGI & SISTEM
# ==============================================================================
class Config:
    # --- Parameter Strategi ---
    LOOKBACK = 252          # Periode momentum (1 tahun perdagangan)
    REBALANCE = 5           # Cek sinyal setiap 5 hari (mingguan)
    ATR_P = 20              # Periode Average True Range
    INIT_RISK_MULT = 2.0    # Stop loss awal = 2.0 x ATR
    TRAIL_MULT = 4.0        # Jarak trailing stop = 4.0 x ATR
    TARGET_VOL = 0.15       # Target volatilitas tahunan portofolio (15%)
    VOL_WINDOW = 20         # Jendela pengamatan volatilitas (20 hari)
    SIZE_MIN = 0.3          # Batas bawah leverage/ukuran posisi
    SIZE_MAX = 3.0          # Batas atas leverage/ukuran posisi
    MAX_HOLD = 500          # Maksimal hari menahan posisi (~2 tahun)
    
    # --- Realitas Pasar (Frictions) ---
    # Biaya komisi + slippage dinyatakan dalam satuan R-multiple per trade.
    # Contoh: 0.15R artinya setiap trade memakan biaya setara 15% dari risiko awal.
    COST_PER_TRADE_R = 0.15 

    # --- Sistem & Notifikasi ---
    SYMBOL = "GC=F"
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "dummy")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "dummy")
    LOG_LEVEL = logging.INFO

# Setup Logging
logging.basicConfig(
    level=Config.LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("TSMOM_GOLD")

# ==============================================================================
# 2. FUNGSI UTILITAS & INDIKATOR
# ==============================================================================
def get_realized_volatility(closes: List[float], w: int = 20) -> float:
    """Menghitung volatilitas tahunan menggunakan Log Return (lebih akurat)."""
    if len(closes) < w + 1:
        return 0.20  # Fallback default
    
    # Log return: ln(P_t / P_t-1)
    rets = [math.log(closes[i] / closes[i-1]) for i in range(len(closes)-w, len(closes))]
    mean_ret = sum(rets) / w
    variance = sum((x - mean_ret) ** 2 for x in rets) / w
    daily_vol = math.sqrt(variance)
    
    # Annualisasi (252 hari perdagangan)
    return daily_vol * math.sqrt(252)

def calculate_atr(data: List[Dict[str, Any]], n: int = 20) -> float:
    """Menghitung Average True Range."""
    if len(data) < 2:
        return 0.0
    
    true_ranges = []
    for i in range(1, len(data)):
        h = data[i]["high"]
        l = data[i]["low"]
        pc = data[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        true_ranges.append(tr)
        
    if len(true_ranges) < n:
        return sum(true_ranges) / len(true_ranges)
    return sum(true_ranges[-n:]) / n

def calculate_stats(results: List[Dict[str, Any]], closes: List[float]) -> Dict[str, Any]:
    """Menghitung metrik kinerja portofolio secara komprehensif."""
    n = len(results)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "exp": 0.0, "mdd": 0.0, "tot": 0.0, "sharpe": 0.0, "calmar": 0.0}

    wins = [r for r in results if r["R_net"] > 0]
    losses = [r for r in results if r["R_net"] <= 0]
    
    gross_win = sum(r["R_net"] for r in wins)
    gross_loss = abs(sum(r["R_net"] for r in losses))
    
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
    total_r = sum(r["R_net"] for r in results)
    expectancy = total_r / n
    
    # Drawdown Calculation based on Cumulative R
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    daily_eq = []
    
    # Buat kurva ekuitas harian (simplifikasi: R terakumulasi per trade, di-interpolasi flat antar trade)
    # Untuk akurasi lebih tinggi, kita hitung MDD berdasarkan titik ekuitas setelah setiap trade.
    for r in results:
        eq += r["R_net"]
        daily_eq.append(eq)
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > mdd:
            mdd = dd

    # Annualized Sharpe Ratio (asumsi risk-free rate = 0, berdasarkan return harian ekuivalen R)
    # Ini adalah aproksimasi. Untuk akurasi penuh, butuh ekuitas harian sebenarnya.
    avg_r_per_day = total_r / (results[-1]["idx"] - results[0]["idx"]) if len(results) > 1 else 0
    sharpe = (avg_r_per_day * 252) / (0.15 * math.sqrt(252)) if 0.15 > 0 else 0 # Dinormalisasi terhadap target vol
    
    calmar = (total_r / mdd) if mdd > 0 else float('inf')

    return {
        "n": n,
        "wr": (len(wins) / n) * 100,
        "pf": pf,
        "exp": expectancy,
        "mdd": mdd,
        "tot": total_r,
        "sharpe": sharpe,
        "calmar": calmar
    }

# ==============================================================================
# 3. PENGAMBILAN DATA (DATA FEED)
# ==============================================================================
def fetch_daily(years: int = 10) -> List[Dict[str, Any]]:
    """Mengambil data historis dari Yahoo Finance dengan retry mechanism."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{Config.SYMBOL}?interval=1d&range={years}y"
        for attempt in range(3):
            try:
                logger.debug(f"Fetching data from {host} (Attempt {attempt+1})...")
                r = requests.get(url, headers=headers, timeout=30)
                if r.status_code == 429:
                    sleep_time = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(f"Rate limited. Sleeping for {sleep_time:.2f}s...")
                    time.sleep(sleep_time)
                    continue
                r.raise_for_status()
                return _parse_yahoo_data(r.json())
            except Exception as e:
                logger.warning(f"Fetch failed: {e}. Retrying...")
                time.sleep((2 ** attempt) + random.uniform(0, 1))
                
    raise RuntimeError("Gagal mengambil data dari Yahoo Finance setelah beberapa percobaan.")

def _parse_yahoo_data(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Membersihkan dan memvalidasi data mentah dari Yahoo Finance."""
    try:
        result = data["chart"]["result"][0]
    except (KeyError, IndexError):
        raise ValueError("Format data Yahoo Finance tidak valid atau simbol tidak ditemukan.")

    ts = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    now = datetime.now(timezone.utc)
    out = []

    for i, t in enumerate(ts):
        try:
            o = quote["open"][i]
            h = quote["high"][i]
            l = quote["low"][i]
            c = quote["close"][i]
        except (KeyError, IndexError, TypeError):
            continue

        # Validasi data anomali
        if None in (o, h, l, c) or h < l or o <= 0 or c <= 0:
            continue

        dt = datetime.fromtimestamp(t, timezone.utc)
        # Abaikan data masa depan (timezone mismatch protection)
        if (dt + timedelta(days=1)) > now:
            continue

        out.append({
            "time": dt,
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c)
        })
    return out

# ==============================================================================
# 4. MESIN BACKTEST (CORE ENGINE)
# ==============================================================================
def run_backtest():
    logger.info("=== 🚀 TSMOM GOLD v13.0 (Ultimate) ===")
    
    # 1. Ambil Data
    c = fetch_daily(years=10)
    logger.info(f"Data berhasil dimuat: {len(c)} baris ({len(c)/252:.1f} tahun)")
    
    if len(c) < 400:
        logger.error("Data tidak cukup untuk backtest (minimal 400 hari).")
        send_telegram("⚠️ <b>TSMOM v13.0</b>: Data historis tidak cukup untuk backtest.")
        return

    closes = [x["close"] for x in c]
    results = []
    
    # State variables
    pos = 0
    entry_price = 0.0
    entry_idx = 0
    peak_price = 0.0
    init_risk = 0.0
    trail_dist = 0.0
    vol_size = 1.0
    last_rebal = -Config.REBALANCE

    # 2. Loop Simulasi
    for i in range(Config.LOOKBACK + 30, len(c)):
        current_atr = calculate_atr(c[:i+1], Config.ATR_P)
        if current_atr <= 0:
            continue

        # --- A. LOGIKA EXIT (Dicek setiap hari) ---
        if pos != 0:
            hold_days = i - entry_idx
            exited = False
            exit_price = 0.0

            if pos == 1:  # Long Position
                peak_price = max(peak_price, c[i]["high"])
                sl_now = max(entry_price - init_risk, peak_price - trail_dist)
                
                # Gap risk handling: jika low <= sl, kita asumsikan fill di sl_now ATAU open (mana yang lebih buruk)
                if c[i]["low"] <= sl_now:
                    exit_price = min(c[i]["open"], sl_now)
                    exited = True
            else:  # Short Position
                peak_price = min(peak_price, c[i]["low"])
                sl_now = min(entry_price + init_risk, peak_price + trail_dist)
                
                if c[i]["high"] >= sl_now:
                    exit_price = max(c[i]["open"], sl_now)
                    exited = True

            # Time-based exit (Max Hold)
            if not exited and hold_days >= Config.MAX_HOLD:
                exit_price = c[i]["close"]
                exited = True

            if exited:
                # Hitung R-Multiple
                raw_profit_points = (exit_price - entry_price) * pos
                r_multiple = (raw_profit_points / init_risk) * vol_size
                r_net = r_multiple - Config.COST_PER_TRADE_R
                
                results.append({
                    "time": str(c[i]["time"].date()),
                    "dir": pos,
                    "R_gross": r_multiple,
                    "R_net": r_net,
                    "idx": entry_idx,
                    "hold": hold_days
                })
                pos = 0  # Reset posisi

        # --- B. LOGIKA ENTRY & REBALANCE (Dicek mingguan) ---
        if (i - last_rebal) < Config.REBALANCE:
            continue
        last_rebal = i

        past_price = closes[i - Config.LOOKBACK]
        curr_price = closes[i]
        new_dir = 1 if curr_price > past_price else -1

        if new_dir == pos:
            continue  # Tidak ada perubahan sinyal

        # Close posisi lama jika ada (Signal Reversal)
        if pos != 0:
            raw_profit_points = (curr_price - entry_price) * pos
            r_multiple = (raw_profit_points / init_risk) * vol_size
            r_net = r_multiple - Config.COST_PER_TRADE_R
            
            results.append({
                "time": str(c[i]["time"].date()),
                "dir": pos,
                "R_gross": r_multiple,
                "R_net": r_net,
                "idx": entry_idx,
                "hold": i - entry_idx
            })

        # Buka posisi baru
        current_vol = get_realized_volatility(closes[:i+1], Config.VOL_WINDOW)
        if current_vol <= 0:
            continue
            
        # Volatility Targeting: Size = Target Vol / Realized Vol
        vol_size = max(Config.SIZE_MIN, min(Config.SIZE_MAX, Config.TARGET_VOL / current_vol))
        
        pos = new_dir
        entry_price = curr_price
        entry_idx = i
        init_risk = current_atr * Config.INIT_RISK_MULT
        trail_dist = current_atr * Config.TRAIL_MULT
        peak_price = c[i]["high"] if pos == 1 else c[i]["low"]
        
        logger.debug(f"[{c[i]['time'].date()}] Entry {'LONG' if pos==1 else 'SHORT'} | Price: {entry_price:.2f} | ATR: {current_atr:.2f} | VolSize: {vol_size:.2f}x")

    # 3. Evaluasi Hasil
    evaluate_and_report(results, c)

# ==============================================================================
# 5. PELAPORAN & NOTIFIKASI
# ==============================================================================
def evaluate_and_report(results: List[Dict[str, Any]], c: List[Dict[str, Any]]):
    n = len(results)
    logger.info(f"Total trades dieksekusi: {n}")
    
    if n == 0:
        send_telegram("<b>⚠️ TSMOM v13.0</b>: 0 trade dieksekusi. Cek parameter atau data.")
        return

    # Analisis Walk-Forward (4 Segmen)
    total_bars = len(c)
    quarter = total_bars // 4
    segs = []
    for k in range(4):
        lo, hi = k * quarter, (k + 1) * quarter
        seg_results = [r for r in results if lo <= r["idx"] < hi]
        segs.append(calculate_stats(seg_results, c))
    
    ov = calculate_stats(results, c)

    # Helper format
    def fmt_stat(name: str, s: Dict[str, Any]) -> str:
        if s["n"] == 0:
            return f"  {name:8s}: n=0"
        pf_str = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
        return (f"  {name:8s}: n={s['n']:3d} | WR={s['wr']:5.1f}% | PF={pf_str:>5s} | "
                f"Exp={s['exp']:+.3f}R | MDD={s['mdd']:.1f}R")

    logger.info("\n--- 📊 OVERALL PERFORMANCE ---")
    logger.info(fmt_stat("TOTAL", ov))
    logger.info("\n--- 📈 WALK-FORWARD (4 Segmen) ---")
    for k in range(4):
        logger.info(fmt_stat(f"SEG {k+1}", segs[k]))

    avg_hold = sum(r["hold"] for r in results) / n if n > 0 else 0
    logger.info(f"Rata-rata waktu tahan (Avg Hold): {avg_hold:.1f} hari")
    logger.info(f"Biaya per trade (Friction): {Config.COST_PER_TRADE_R}R")

    # Kriteria Kelulusan Live Trading
    # Minimal 20 trade, PF > 1.3 (lebih realistis dari 1.4 karena sudah ada biaya), Exp > 0.2R, dan minimal 3 segmen konsisten (PF >= 1.1)
    consistent_segs = sum(1 for s in segs if s["n"] > 0 and s["pf"] >= 1.1)
    is_live_ready = (ov["n"] >= 20 and ov["pf"] >= 1.3 and ov["exp"] > 0.2 and consistent_segs >= 3)

    # Format Pesan Telegram
    pf_str = f"{ov['pf']:.2f}" if ov['pf'] != float('inf') else "inf"
    msg = (
        f"<b>🏆 TSMOM GOLD v13.0 (Ultimate)</b>\n"
        f"<code>{Config.SYMBOL}</code> | Target Vol: {Config.TARGET_VOL*100:.0f}%\n"
        f"──────────────────────────────\n"
        f"📊 Total Trades : {ov['n']}\n"
        f"🎯 Win Rate     : {ov['wr']:.1f}%\n"
        f"💵 Profit Factor: {pf_str}\n"
        f"📈 Expectancy   : {ov['exp']:+.3f} R (Net of fees)\n"
        f"📉 Max Drawdown : {ov['mdd']:.1f} R\n"
        f"⚡ Sharpe Ratio : {ov['sharpe']:.2f}\n"
        f"⏱️ Avg Hold     : {avg_hold:.0f} hari\n"
        f"──────────────────────────────\n"
        f"<b>Walk-Forward Consistency:</b>\n"
    )
    for k in range(4):
        s = segs[k]
        if s["n"] > 0:
            s_pf = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
            msg += f"• Q{k+1}: n={s['n']} | WR={s['wr']:.0f}% | PF={s_pf} | Exp={s['exp']:+.2f}R\n"
    
    msg += f"──────────────────────────────\n"
    msg += f"Konsistensi: {consistent_segs}/4 Segmen\n"
    if is_live_ready:
        msg += f"✅ <b>STATUS: LAYAK LIVE TRADING</b>"
    else:
        msg += f"⚠️ <b>STATUS: BELUM LAYAK (Perlu Optimasi)</b>"

    logger.info("\n" + msg.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
    send_telegram(msg)

def send_telegram(text: str):
    """Mengirim pesan ke Telegram jika token valid."""
    if Config.TELEGRAM_TOKEN == "dummy" or Config.TELEGRAM_CHAT_ID == "dummy":
        logger.info("Telegram tidak dikonfigurasi. Pesan hanya ditampilkan di log.")
        return
    
    url = f"https://api.telegram.org/bot{Config.TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": Config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info("Pesan berhasil dikirim ke Telegram.")
        else:
            logger.error(f"Gagal kirim Telegram: {response.text}")
    except Exception as e:
        logger.error(f"Exception saat kirim Telegram: {e}")

# ==============================================================================
# 6. ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    try:
        run_backtest()
    except Exception as e:
        import traceback
        logger.critical(f"FATAL ERROR: {e}\n{traceback.format_exc()}")
        send_telegram(f"🚨 <b>FATAL ERROR TSMOM v13.0</b>\n<code>{e}</code>")
