#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TSMOM GOLD v13.2 (ADX Filter) — Only trade when trend is strong.
Basis: Moskowitz-Ooi-Pedersen (2012) + ADX Regime Filter.
"""
import os
import sys
import math
import time
import random
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

# ==============================================================================
# 1. KONFIGURASI
# ==============================================================================
class Config:
    SYMBOL          = "GC=F"
    LOOKBACK        = 252       # Kembali ke 252 (lebih stabil)
    REBALANCE       = 5
    ATR_P           = 20
    INIT_RISK_MULT  = 2.0       # Kembali ke 2.0 (beri ruang)
    TRAIL_MULT      = 4.0       # Kembali ke 4.0
    TARGET_VOL      = 0.12      # Tengah-tengah (dari 0.15 dan 0.10)
    VOL_WINDOW      = 20
    SIZE_MIN        = 0.3
    SIZE_MAX        = 2.5
    MAX_HOLD        = 400
    
    # --- ADX FILTER (KUNCI UTAMA) ---
    ADX_PERIOD      = 14        # Periode ADX
    ADX_THRESHOLD   = 25        # Minimum ADX untuk trade (25 = tren kuat)
    
    COST_PER_TRADE_R = 0.15
    DATA_YEARS      = 10
    MIN_BARS        = 400
    MIN_TRADES      = 20
    MIN_PF          = 1.3
    MIN_EXP         = 0.2
    MIN_CONSISTENT  = 3
    SEG_MIN_PF      = 1.1
    NUM_SEGMENTS    = 4
    
    TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ==============================================================================
# 2. LOGGING & TELEGRAM
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("TSMOM")

def send_telegram(text: str):
    token = Config.TELEGRAM_TOKEN
    chat_id = Config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.info("Telegram tidak dikonfigurasi.")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML"
        }, timeout=10)
        if r.status_code == 200:
            log.info("✅ Telegram terkirim.")
        else:
            log.error(f"Telegram gagal: {r.text}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ==============================================================================
# 3. DATA FETCHER
# ==============================================================================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def fetch_daily(years: int = 10) -> List[Dict[str, Any]]:
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{Config.SYMBOL}?interval=1d&range={years}y"
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=30)
                if r.status_code == 429:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                r.raise_for_status()
                return _parse(r.json())
            except Exception as e:
                log.warning(f"Fetch gagal ({e}), retry...")
                time.sleep((2 ** attempt) + random.uniform(0, 1))
    raise RuntimeError("Gagal ambil data Yahoo Finance.")

def _parse(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = data["chart"]["result"][0]
    ts = result.get("timestamp") or []
    q = result["indicators"]["quote"][0]
    now = datetime.now(timezone.utc)
    out = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        except (KeyError, IndexError, TypeError):
            continue
        if None in (o, h, l, c) or h < l or o <= 0 or c <= 0:
            continue
        dt = datetime.fromtimestamp(t, timezone.utc)
        if (dt + timedelta(days=1)) > now:
            continue
        out.append({"time": dt, "open": float(o), "high": float(h),
                    "low": float(l), "close": float(c)})
    return out

# ==============================================================================
# 4. INDIKATOR
# ==============================================================================
def realized_vol(closes: List[float], w: int = 20) -> float:
    if len(closes) < w + 1:
        return 0.20
    rets = [math.log(closes[i] / closes[i-1]) for i in range(len(closes)-w, len(closes))]
    m = sum(rets) / w
    var = sum((x - m) ** 2 for x in rets) / w
    return math.sqrt(var) * math.sqrt(252)

def atr(data: List[Dict[str, Any]], n: int = 20) -> float:
    if len(data) < 2:
        return 0.0
    trs = []
    for i in range(1, len(data)):
        h, l, pc = data[i]["high"], data[i]["low"], data[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / min(n, len(trs))

def calculate_adx(data: List[Dict[str, Any]], period: int = 14) -> float:
    """
    Average Directional Index (ADX) — mengukur kekuatan tren.
    ADX > 25 = tren kuat, ADX < 20 = sideways.
    """
    if len(data) < period * 2:
        return 0.0
    
    # Hitung +DM, -DM, TR
    plus_dm = []
    minus_dm = []
    tr_list = []
    
    for i in range(1, len(data)):
        h_diff = data[i]["high"] - data[i-1]["high"]
        l_diff = data[i-1]["low"] - data[i]["low"]
        
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
        
        h, l, pc = data[i]["high"], data[i]["low"], data[i-1]["close"]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
    
    # Wilder's smoothing (EMA-like)
    def wilder_smooth(values, period):
        if len(values) < period:
            return []
        smoothed = [sum(values[:period])]
        for i in range(period, len(values)):
            smoothed.append(smoothed[-1] - smoothed[-1]/period + values[i])
        return smoothed
    
    plus_dm_smooth = wilder_smooth(plus_dm, period)
    minus_dm_smooth = wilder_smooth(minus_dm, period)
    tr_smooth = wilder_smooth(tr_list, period)
    
    # Hitung DX
    dx_list = []
    for i in range(len(plus_dm_smooth)):
        if tr_smooth[i] == 0:
            dx_list.append(0)
            continue
        plus_di = 100 * plus_dm_smooth[i] / tr_smooth[i]
        minus_di = 100 * minus_dm_smooth[i] / tr_smooth[i]
        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_list.append(0)
        else:
            dx_list.append(100 * abs(plus_di - minus_di) / di_sum)
    
    # ADX = rata-rata DX terakhir
    if len(dx_list) < period:
        return 0.0
    return sum(dx_list[-period:]) / period

def calc_stats(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(results)
    empty = {"n": 0, "wr": 0.0, "pf": 0.0, "exp": 0.0, "mdd": 0.0, "tot": 0.0}
    if n == 0:
        return empty
    
    wins = [r for r in results if r["R_net"] > 0]
    losses = [r for r in results if r["R_net"] <= 0]
    gw = sum(r["R_net"] for r in wins)
    gl = abs(sum(r["R_net"] for r in losses))
    pf = gw / gl if gl > 0 else float('inf')
    tot = sum(r["R_net"] for r in results)
    
    eq = peak = mdd = 0.0
    for r in results:
        eq += r["R_net"]
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    
    return {"n": n, "wr": len(wins)/n*100, "pf": pf,
            "exp": tot/n, "mdd": mdd, "tot": tot}

# ==============================================================================
# 5. BACKTEST ENGINE
# ==============================================================================
def run():
    log.info(f"=== 🚀 TSMOM GOLD v13.2 (ADX Filter) ===")
    c = fetch_daily(Config.DATA_YEARS)
    log.info(f"Data: {len(c)} bar ({len(c)/252:.1f} tahun)")
    
    if len(c) < Config.MIN_BARS:
        send_telegram("⚠️ Data tidak cukup untuk backtest.")
        return

    closes = [x["close"] for x in c]
    results = []
    pos = 0; entry_price = 0.0; entry_idx = 0
    peak_price = 0.0; init_risk = 0.0; trail_dist = 0.0; vol_size = 1.0
    last_rebal = -Config.REBALANCE
    skipped_by_adx = 0

    for i in range(Config.LOOKBACK + 50, len(c)):
        a = atr(c[:i+1], Config.ATR_P)
        if a <= 0:
            continue

        # --- EXIT ---
        if pos != 0:
            hold = i - entry_idx
            exited = False
            exit_price = 0.0
            
            if pos == 1:
                peak_price = max(peak_price, c[i]["high"])
                sl = max(entry_price - init_risk, peak_price - trail_dist)
                if c[i]["low"] <= sl:
                    exit_price = min(c[i]["open"], sl)
                    exited = True
            else:
                peak_price = min(peak_price, c[i]["low"])
                sl = min(entry_price + init_risk, peak_price + trail_dist)
                if c[i]["high"] >= sl:
                    exit_price = max(c[i]["open"], sl)
                    exited = True

            if not exited and hold >= Config.MAX_HOLD:
                exit_price = c[i]["close"]
                exited = True

            if exited:
                raw = (exit_price - entry_price) * pos
                r_gross = (raw / init_risk) * vol_size
                results.append({
                    "time": str(c[i]["time"].date()), "dir": pos,
                    "R_gross": r_gross, "R_net": r_gross - Config.COST_PER_TRADE_R,
                    "idx": entry_idx, "hold": hold
                })
                pos = 0

        # --- ENTRY (mingguan) + ADX FILTER ---
        if (i - last_rebal) < Config.REBALANCE:
            continue
        last_rebal = i

        # 1. Hitung ADX
        current_adx = calculate_adx(c[:i+1], Config.ADX_PERIOD)
        
        # 2. ADX FILTER: Hanya trade jika ADX > threshold (tren kuat)
        if current_adx < Config.ADX_THRESHOLD:
            skipped_by_adx += 1
            continue
        
        # 3. Sinyal momentum dasar
        new_dir = 1 if closes[i] > closes[i - Config.LOOKBACK] else -1
        
        if new_dir == pos:
            continue

        # Close posisi lama jika ada
        if pos != 0:
            raw = (closes[i] - entry_price) * pos
            r_gross = (raw / init_risk) * vol_size
            results.append({
                "time": str(c[i]["time"].date()), "dir": pos,
                "R_gross": r_gross, "R_net": r_gross - Config.COST_PER_TRADE_R,
                "idx": entry_idx, "hold": i - entry_idx
            })

        # Buka posisi baru
        vol = realized_vol(closes[:i+1], Config.VOL_WINDOW)
        if vol <= 0:
            continue
        vol_size = max(Config.SIZE_MIN, min(Config.SIZE_MAX, Config.TARGET_VOL / vol))
        pos = new_dir
        entry_price = closes[i]
        entry_idx = i
        init_risk = a * Config.INIT_RISK_MULT
        trail_dist = a * Config.TRAIL_MULT
        peak_price = c[i]["high"] if pos == 1 else c[i]["low"]

    # --- EVALUASI ---
    n = len(results)
    log.info(f"Total trades: {n} (Difilter oleh ADX: {skipped_by_adx})")
    if n == 0:
        send_telegram("⚠️ <b>TSMOM v13.2</b>: 0 trade setelah filter ADX.")
        return

    quarter = len(c) // 4
    segs = []
    for k in range(Config.NUM_SEGMENTS):
        lo, hi = k * quarter, (k + 1) * quarter
        segs.append(calc_stats([r for r in results if lo <= r["idx"] < hi]))
    ov = calc_stats(results)

    avg_hold = sum(r["hold"] for r in results) / n
    consistent = sum(1 for s in segs if s["n"] > 0 and s["pf"] >= Config.SEG_MIN_PF)
    ok = (ov["n"] >= Config.MIN_TRADES and ov["pf"] >= Config.MIN_PF and
          ov["exp"] > Config.MIN_EXP and consistent >= Config.MIN_CONSISTENT)

    def fmt(name, s):
        if s["n"] == 0: return f"  {name:6s}: n=0"
        pf_s = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
        return f"  {name:6s}: n={s['n']:3d} WR={s['wr']:5.1f}% PF={pf_s:>5s} Exp={s['exp']:+.3f}R DD={s['mdd']:.1f}R"

    log.info("\n--- OVERALL ---")
    log.info(fmt("TOTAL", ov))
    log.info("\n--- 4 SEGMEN ---")
    for k in range(Config.NUM_SEGMENTS):
        log.info(fmt(f"S{k+1}", segs[k]))
    log.info(f"Avg hold: {avg_hold:.1f} hari")
    log.info(f"Biaya per trade: {Config.COST_PER_TRADE_R}R")
    log.info(f"Sinyal difilter (ADX < {Config.ADX_THRESHOLD}): {skipped_by_adx}")

    pf_s = f"{ov['pf']:.2f}" if ov['pf'] != float('inf') else "inf"
    msg = (
        f"<b>🏆 TSMOM GOLD v13.2 (ADX Filter)</b>\n"
        f"<code>{Config.SYMBOL}</code> | Vol Target: {Config.TARGET_VOL*100:.0f}%\n"
        f"🔍 Filter: ADX > {Config.ADX_THRESHOLD} (tren kuat saja)\n"
        f"──────────────────────\n"
        f"📊 Trades     : {ov['n']}\n"
        f"🎯 Win Rate   : {ov['wr']:.1f}%\n"
        f"💵 PF         : {pf_s}\n"
        f"📈 Expectancy : {ov['exp']:+.3f} R (net)\n"
        f"📉 Max DD     : {ov['mdd']:.1f} R\n"
        f"⏱️ Avg Hold   : {avg_hold:.0f} hari\n"
        f"🚫 Filtered   : {skipped_by_adx} sinyal\n"
        f"──────────────────────\n"
        f"<b>Walk-Forward:</b>\n"
    )
    for k in range(Config.NUM_SEGMENTS):
        s = segs[k]
        if s["n"] > 0:
            s_pf = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
            msg += f"• S{k+1}: n={s['n']} WR={s['wr']:.0f}% PF={s_pf} Exp={s['exp']:+.2f}R\n"
    msg += f"──────────────────────\n"
    msg += f"Konsistensi: {consistent}/{Config.NUM_SEGMENTS}\n"
    msg += "✅ <b>LAYAK LIVE</b>" if ok else "⚠️ <b>BELUM LAYAK</b>"

    log.info("\n" + msg.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
    send_telegram(msg)

# ==============================================================================
# 6. ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        import traceback
        log.critical(f"FATAL: {e}\n{traceback.format_exc()}")
        send_telegram(f"🚨 <b>FATAL ERROR</b>\n<code>{e}</code>")
