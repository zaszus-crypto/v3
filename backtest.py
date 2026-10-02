#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BACKTEST ENGINE v2.1 — 60 hari, walk-forward, diagnostic histogram.
"""
import os, sys, json, math, collections
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M
from datetime import timedelta

HOLD_MAX_BARS = 32
SL_MULT, RR   = 1.8, 2.5
BURN_IN       = 200

# ---------- Utilitas ----------
def resample_30m_to_1h(c30):
    out, bucket = [], []
    for c in c30:
        if bucket and (c["time"].hour != bucket[0]["time"].hour or
                       (c["time"] - bucket[0]["time"]).total_seconds() >= 3600):
            if len(bucket) >= 2: out.append(_agg(bucket))
            bucket = []
        bucket.append(c)
    if len(bucket) >= 2: out.append(_agg(bucket))
    return out

def _agg(bars):
    return {"time": bars[0]["time"], "open": bars[0]["open"],
            "high": max(b["high"] for b in bars),
            "low":  min(b["low"]  for b in bars),
            "close": bars[-1]["close"],
            "vol":  sum(b.get("vol", 0) for b in bars)}

def build_h1_map(c30, c60):
    mapping = [-1] * len(c30)
    j = -1
    for i in range(len(c30)):
        m30_end = c30[i]["time"] + timedelta(minutes=30)
        while j+1 < len(c60) and (c60[j+1]["time"] + timedelta(minutes=60)) <= m30_end:
            j += 1
        mapping[i] = j
    return mapping

def evaluate(c30, i, direction, sl_dist, tp_dist):
    if i+1 >= len(c30): return 0.0, "NO_ENTRY"
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
    last_c = c30[min(i+1+HOLD_MAX_BARS, len(c30))-1]["close"]
    r = (last_c - entry) / sl_dist * direction
    return r, "TIMEOUT"

# ---------- Run ----------
def run_backtest():
    log = M.log
    log("=== MULAI BACKTEST 60 HARI (v2.1) ===")
    c30_full = M.fetch_ohlc("30m", "60d")
    c60_full = resample_30m_to_1h(c30_full)
    h1_map   = build_h1_map(c30_full, c60_full)
    log(f"M30: {len(c30_full)} | H1: {len(c60_full)} | threshold={M.MIN_SCORE_WEIGHTED}")

    results = []
    skip_regime = skip_session = 0
    # Histogram skor (semua yang lolos regime+sesi, sebelum threshold)
    hist = collections.Counter()
    all_scores = []

    for i in range(BURN_IN, len(c30_full) - HOLD_MAX_BARS - 2):
        c30 = c30_full[:i+1]
        h1_idx = h1_map[i]
        if h1_idx < 55: continue
        c60 = c60_full[:h1_idx+1]

        if not M.regime_ok(c30): skip_regime += 1; continue
        hh = c30[-1]["time"].hour
        if not any(a <= hh < b for a, b in M.KILLZONES): skip_session += 1; continue

        votes = {
            "SMC_Sweep":    M.strat_smc_sweep(c30),
            "MSB_MultiTF":  M.strat_msb_mtf(c30, c60),
            "MeanReversion":M.strat_mean_reversion(c30, c60),
            "SqueezeBreak": M.strat_squeeze_breakout(c30),
        }
        score = sum(votes[k] * M.WEIGHTS[k] for k in votes)
        all_scores.append(abs(score))

        # Bucket histogram
        b = abs(score)
        if b < 0.5:    hist["0.0-0.5"] += 1
        elif b < 1.0:  hist["0.5-1.0"] += 1
        elif b < 1.5:  hist["1.0-1.5"] += 1
        elif b < 2.0:  hist["1.5-2.0"] += 1
        elif b < 2.5:  hist["2.0-2.5"] += 1
        elif b < 3.0:  hist["2.5-3.0"] += 1
        else:          hist["3.0+"] += 1

        if abs(score) < M.MIN_SCORE_WEIGHTED: continue

        direction = 1 if score > 0 else -1
        a = M.atr(c30, 14)
        if a <= 0: continue
        r, hasil = evaluate(c30_full, i, direction, SL_MULT*a, SL_MULT*a*RR)
        results.append({"i": i, "time": str(c30[-1]["time"]),
                        "dir": direction, "score": round(score, 2),
                        "R": r, "hasil": hasil, "votes": votes})

    # Cetak histogram (diagnostic)
    log("--- Distribusi |weighted score| (regime+sesi lolos) ---")
    total_checked = sum(hist.values())
    for k in ["0.0-0.5","0.5-1.0","1.0-1.5","1.5-2.0","2.0-2.5","2.5-3.0","3.0+"]:
        cnt = hist.get(k, 0)
        pct = (cnt/total_checked*100) if total_checked else 0
        log(f"  {k:8s}: {cnt:5d} ({pct:5.1f}%)")
    log(f"Total kandidat (regime+sesi lolos): {total_checked}")
    log(f"Threshold aktif: {M.MIN_SCORE_WEIGHTED} | sinyal lolos: {len(results)}")

    n = len(results)
    if n == 0:
        msg = (f"<b>BACKTEST</b>: 0 sinyal dgn threshold {M.MIN_SCORE_WEIGHTED}.\n"
               f"Kandidat diperiksa: {total_checked}\n"
               f"<b>Saran:</b> turunkan MIN_SCORE_WEIGHTED ke "
               f"{'1.5' if hist.get('1.5-2.0',0)>50 else '1.2'}")
        log(msg); M.send_telegram(msg); return

    wins   = [r for r in results if r["R"] > 0]
    losses = [r for r in results if r["R"] <= 0]
    wr = len(wins)/n*100
    gross_w = sum(r["R"] for r in wins)
    gross_l = abs(sum(r["R"] for r in losses))
    pf = gross_w/gross_l if gross_l > 0 else float("inf")
    exp_ = sum(r["R"] for r in results)/n
    eq = peak = mdd = 0
    for r in results:
        eq += r["R"]; peak = max(peak, eq); mdd = max(mdd, peak - eq)

    contrib = {}
    for r in results:
        for k, v in r["votes"].items():
            if v != 0 and v == r["dir"]:
                contrib.setdefault(k, [0, 0.0])
                contrib[k][0] += 1
                contrib[k][1] += r["R"]

    lines = [f"<b>📋 BACKTEST XAUUSD — 60 HARI (v2.1)</b>",
             "──────────────────",
             f"Sinyal: {n} (regime-skip {skip_regime}, sesi-skip {skip_session})",
             f"🏆 Win rate     : {wr:.1f}%  ({len(wins)}W / {len(losses)}L)",
             f"💵 Profit factor: {pf:.2f}",
             f"📈 Expectancy   : {exp_:+.3f} R",
             f"📉 Max DD       : {mdd:.1f} R",
             f"⏱ Timeout      : {sum(1 for r in results if r['hasil']=='TIMEOUT')}",
             "──────────────────", "<b>Kontribusi strategi</b>:"]
    for k, (c, t) in sorted(contrib.items(), key=lambda x: -x[1][1]):
        lines.append(f"• {k:14s}: {c:3d}x | {t:+.1f} R")
    verdict = ("✅ LAYAK forward test" if pf >= 1.4 and exp_ > 0.2
               else "⚠️ BELUM LAYAK — tuning ulang")
    lines += ["──────────────────", verdict,
              "<i>Belum termasuk spread/slippage (~0.1–0.2R per trade).</i>"]
    msg = "\n".join(lines)
    log(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    M.send_telegram(msg)

if __name__ == "__main__":
    try:
        run_backtest()
    except Exception as e:
        import traceback
        M.log(f"BACKTEST FATAL: {e}\n{traceback.format_exc()}")
