#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BACKTEST v3.5 — 180 hari H1, tanpa equity filter (measure raw edge).
Fix: equity filter menyebabkan deadlock di backtest (permanen stuck
setelah 4 loss karena tidak ada trade baru yg dievaluasi).
"""
import os, sys, math
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M
from datetime import timedelta

HOLD_MAX_BARS = 16
SL_MULT, RR   = 1.8, 2.5
BURN_IN       = 100
RANGE_DAYS    = "180d"

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

def stats(rs):
    n = len(rs)
    if n == 0: return None
    wins = [r for r in rs if r["R"] > 0]
    loss = [r for r in rs if r["R"] <= 0]
    gw = sum(r["R"] for r in wins); gl = abs(sum(r["R"] for r in loss))
    pf = gw/gl if gl > 0 else float("inf")
    eq = peak = mdd = 0
    for r in rs:
        eq += r["R"]; peak = max(peak, eq); mdd = max(mdd, peak - eq)
    return {"n": n, "wr": len(wins)/n*100, "pf": pf,
            "exp": sum(r["R"] for r in rs)/n, "mdd": mdd,
            "tot": sum(r["R"] for r in rs)}

def run_backtest():
    log = M.log
    log(f"=== BACKTEST v3.5 ({RANGE_DAYS} H1, TANPA equity filter) ===")
    c60_full = M.fetch_ohlc("1h", RANGE_DAYS)
    log(f"H1: {len(c60_full)} candle")

    total_len = len(c60_full)
    segment_size = total_len // 3
    results = []

    for i in range(BURN_IN, total_len - HOLD_MAX_BARS - 2):
        c30 = c60_full[:i+1]
        c60 = c30

        if not M.regime_ok(c30): continue
        hh = c30[-1]["time"].hour
        if not M.in_killzone(hh): continue

        adx_val = M.adx(c30, 14)
        if adx_val >= M.ADX_TREND:
            if hh not in M.MSB_HOURS: continue
            v = M.strat_msb_strict(c30, c60); regime = "TREND"
        elif adx_val <= M.ADX_RANGE:
            v = M.strat_mean_reversion(c30, c60); regime = "RANGE"
        else:
            continue
        if v == 0: continue

        a = M.atr(c30, 14)
        if a <= 0: continue
        r, h = evaluate(c60_full, i, v, SL_MULT*a, SL_MULT*a*RR)
        seg = min(2, i // segment_size)
        results.append({"time": str(c30[-1]["time"]), "dir": v, "R": r,
                        "hasil": h, "regime": regime, "seg": seg,
                        "adx": round(adx_val, 1)})

    n = len(results)
    log(f"Total sinyal: {n}")
    if n == 0:
        M.send_telegram(f"<b>BACKTEST v3.5</b>: 0 sinyal."); return

    overall = stats(results)
    seg_stats = [stats([r for r in results if r["seg"] == k]) for k in range(3)]
    msb_s = stats([r for r in results if r["regime"] == "TREND"])
    mr_s  = stats([r for r in results if r["regime"] == "RANGE"])

    def line(name, s):
        if s is None: return f"  {name:20s}: n=0"
        pf_s = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
        return (f"  {name:20s}: n={s['n']:3d} WR={s['wr']:5.1f}% "
                f"PF={pf_s:>5s} Exp={s['exp']:+.3f}R DD={s['mdd']:.1f}R")

    log("\n--- OVERALL ---")
    log(line("TOTAL", overall))
    log("\n--- PER SEGMEN (walk-forward) ---")
    for k in range(3):
        log(line(f"Segmen {k+1}", seg_stats[k]))
    log("\n--- PER REGIME ---")
    log(line("Trending MSB", msb_s))
    log(line("Ranging MR", mr_s))

    consistent = sum(1 for s in seg_stats if s and s["pf"] >= 1.4)
    verdict_ok = (overall["n"] >= 50 and overall["pf"] >= 1.4
                  and overall["exp"] > 0.2 and consistent >= 2)

    lines = [f"<b>📋 BACKTEST v3.5 — {RANGE_DAYS} H1</b>", "──────────────────"]
    if overall:
        pf_s = f"{overall['pf']:.2f}" if overall['pf'] != float('inf') else "inf"
        lines += [f"Total sinyal    : {overall['n']}",
                  f"🏆 Win rate     : {overall['wr']:.1f}%",
                  f"💵 Profit factor: {pf_s}",
                  f"📈 Expectancy   : {overall['exp']:+.3f} R",
                  f"📉 Max DD       : {overall['mdd']:.1f} R"]
    lines.append("──────────────────")
    lines.append("<b>Walk-forward 3 segmen:</b>")
    for k in range(3):
        s = seg_stats[k]
        if s:
            pf_s = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
            lines.append(f"• S{k+1}: n={s['n']} WR={s['wr']:.0f}% "
                         f"PF={pf_s} Exp={s['exp']:+.2f}R")
        else:
            lines.append(f"• S{k+1}: n=0")
    lines.append("──────────────────")
    lines.append("<b>Per regime:</b>")
    if msb_s:
        pf_s = f"{msb_s['pf']:.2f}" if msb_s['pf'] != float('inf') else "inf"
        lines.append(f"• Trend MSB: n={msb_s['n']} WR={msb_s['wr']:.0f}% "
                     f"PF={pf_s} Exp={msb_s['exp']:+.2f}R")
    if mr_s:
        pf_s = f"{mr_s['pf']:.2f}" if mr_s['pf'] != float('inf') else "inf"
        lines.append(f"• Range MR : n={mr_s['n']} WR={mr_s['wr']:.0f}% "
                     f"PF={pf_s} Exp={mr_s['exp']:+.2f}R")
    lines.append("──────────────────")
    lines.append(f"<b>Konsistensi: {consistent}/3 segmen PF≥1.4</b>")
    lines.append("✅ LAYAK forward test" if verdict_ok
                 else "⚠️ BELUM — evaluasi ulang")
    msg = "\n".join(lines)
    log("\n" + msg.replace("<b>","").replace("</b>",""))
    M.send_telegram(msg)

if __name__ == "__main__":
    try:
        run_backtest()
    except Exception as e:
        import traceback
        M.log(f"FATAL: {e}\n{traceback.format_exc()}")
