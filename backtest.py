#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BACKTEST v3.1 — validate regime-switching (MSB/MR + ADX)."""
import os, sys, math
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M
from datetime import timedelta

HOLD_MAX_BARS = 32
SL_MULT, RR   = 1.8, 2.5
BURN_IN       = 200

def resample_30m_to_1h(c30):
    out, bucket = [], []
    for c in c30:
        if bucket and (c["time"].hour != bucket[0]["time"].hour or
                       (c["time"] - bucket[0]["time"]).total_seconds() >= 3600):
            if len(bucket) >= 2:
                out.append({"time": bucket[0]["time"], "open": bucket[0]["open"],
                            "high": max(b["high"] for b in bucket),
                            "low":  min(b["low"]  for b in bucket),
                            "close": bucket[-1]["close"],
                            "vol":  sum(b.get("vol",0) for b in bucket)})
            bucket = []
        bucket.append(c)
    if len(bucket) >= 2:
        out.append({"time": bucket[0]["time"], "open": bucket[0]["open"],
                    "high": max(b["high"] for b in bucket),
                    "low":  min(b["low"]  for b in bucket),
                    "close": bucket[-1]["close"],
                    "vol":  sum(b.get("vol",0) for b in bucket)})
    return out

def build_h1_map(c30, c60):
    mapping = [-1] * len(c30); j = -1
    for i in range(len(c30)):
        m30_end = c30[i]["time"] + timedelta(minutes=30)
        while j+1 < len(c60) and (c60[j+1]["time"] + timedelta(minutes=60)) <= m30_end:
            j += 1
        mapping[i] = j
    return mapping

def evaluate(c30, i, direction, sl_dist, tp_dist):
    if i+1 >= len(c30): return 0.0, "NO"
    entry = c30[i+1]["open"]
    sl = entry - direction * sl_dist
    tp = entry + direction * tp_dist
    if direction == 1:
        if entry >= tp: return +RR, "TP_GAP"
        if entry <= sl: return -1.0, "SL_GAP"
    else:
        if entry <= tp: return +RR, "TP_GAP"
        if entry >= sl: return -1.0, "SL_GAP"
    for j in range(i+1, min(i+1+HOLD_MAX_BARS, len(c30))):
        h, l = c30[j]["high"], c30[j]["low"]
        if direction == 1:
            if l <= sl: return -1.0, "SL"
            if h >= tp: return +RR, "TP"
        else:
            if h >= sl: return -1.0, "SL"
            if l <= tp: return +RR, "TP"
    c = c30[min(i+1+HOLD_MAX_BARS, len(c30))-1]["close"]
    return (c - entry)/sl_dist*direction, "TIMEOUT"

def run_backtest():
    log = M.log
    log("=== BACKTEST v3.1 (REGIME-SWITCHING) ===")
    c30_full = M.fetch_ohlc("30m", "60d")
    c60_full = resample_30m_to_1h(c30_full)
    h1_map = build_h1_map(c30_full, c60_full)
    log(f"M30: {len(c30_full)} | H1: {len(c60_full)}")

    results = []
    sk_regime = sk_session = sk_trans = 0
    msb_n = mr_n = 0

    for i in range(BURN_IN, len(c30_full) - HOLD_MAX_BARS - 2):
        c30 = c30_full[:i+1]
        h1_idx = h1_map[i]
        if h1_idx < 55: continue
        c60 = c60_full[:h1_idx+1]
        if not M.regime_ok(c30): sk_regime += 1; continue
        hh = c30[-1]["time"].hour
        if not any(a <= hh < b for a, b in M.KILLZONES): sk_session += 1; continue

        adx_val = M.adx(c30, 14)
        if adx_val >= M.ADX_TREND:
            v = M.strat_msb_mtf(c30, c60); regime = "TREND"; msb_n += 1
        elif adx_val <= M.ADX_RANGE:
            v = M.strat_mean_reversion(c30, c60); regime = "RANGE"; mr_n += 1
        else:
            sk_trans += 1; continue
        if v == 0: continue

        a = M.atr(c30, 14)
        if a <= 0: continue
        r, h = evaluate(c30_full, i, v, SL_MULT*a, SL_MULT*a*RR)
        results.append({"time": str(c30[-1]["time"]), "dir": v, "R": r,
                        "hasil": h, "regime": regime, "adx": round(adx_val,1)})

    n = len(results)
    log(f"Kandidat: MSB={msb_n} MR={mr_n} | skip_regime={sk_regime} "
        f"skip_sesi={sk_session} skip_trans={sk_trans} | sinyal={n}")
    if n == 0:
        M.send_telegram("<b>BACKTEST v3.1</b>: tidak ada sinyal."); return

    wins = [r for r in results if r["R"] > 0]
    loss = [r for r in results if r["R"] <= 0]
    wr = len(wins)/n*100
    gw = sum(r["R"] for r in wins); gl = abs(sum(r["R"] for r in loss))
    pf = gw/gl if gl > 0 else float("inf")
    exp_ = sum(r["R"] for r in results)/n
    eq = peak = mdd = 0
    for r in results:
        eq += r["R"]; peak = max(peak, eq); mdd = max(mdd, peak - eq)

    # Split by regime
    msb_res = [r for r in results if r["regime"] == "TREND"]
    mr_res  = [r for r in results if r["regime"] == "RANGE"]
    def sub(rs):
        if not rs: return "n=0"
        w = [r for r in rs if r["R"] > 0]; l = [r for r in rs if r["R"] <= 0]
        gw = sum(r["R"] for r in w); gl = abs(sum(r["R"] for r in l))
        pf = gw/gl if gl > 0 else float("inf")
        return (f"n={len(rs)} WR={len(w)/len(rs)*100:.0f}% "
                f"PF={pf:.2f} Exp={sum(r['R'] for r in rs)/len(rs):+.2f}R")

    msg = (f"<b>📋 BACKTEST v3.1 REGIME-SWITCHING</b>\n"
           f"──────────────────\n"
           f"Total sinyal    : {n}\n"
           f"🏆 Win rate     : {wr:.1f}% ({len(wins)}W/{len(loss)}L)\n"
           f"💵 Profit factor: {pf:.2f}\n"
           f"📈 Expectancy   : {exp_:+.3f} R\n"
           f"📉 Max DD       : {mdd:.1f} R\n"
           f"──────────────────\n"
           f"<b>Trending (MSB):</b> {sub(msb_res)}\n"
           f"<b>Ranging (MR):</b> {sub(mr_res)}\n"
           f"──────────────────\n"
           f"{'✅ LAYAK forward test' if pf >= 1.4 and exp_ > 0.2 else '⚠️ Perlu tuning'}")
    log(msg.replace("<b>","").replace("</b>",""))
    M.send_telegram(msg)

if __name__ == "__main__":
    try:
        run_backtest()
    except Exception as e:
        import traceback
        M.log(f"FATAL: {e}\n{traceback.format_exc()}")
