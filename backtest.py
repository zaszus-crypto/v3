#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BACKTEST DIAGNOSTIC v3.0 — uji tiap strategi terisolasi + pasangan + ensemble.
Tujuan: temukan kombinasi yang benar-benar punya edge.
"""
import os, sys, json, math, itertools
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M
from datetime import timedelta

HOLD_MAX_BARS = 32
SL_MULT, RR   = 1.8, 2.5
BURN_IN       = 200
STRATEGIES    = ["SMC_Sweep", "MSB_MultiTF", "MeanReversion", "SqueezeBreak"]

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
    if i+1 >= len(c30): return 0.0
    entry = c30[i+1]["open"]
    sl = entry - direction * sl_dist
    tp = entry + direction * tp_dist
    if direction == 1:
        if entry >= tp: return +RR
        if entry <= sl: return -1.0
    else:
        if entry <= tp: return +RR
        if entry >= sl: return -1.0
    for j in range(i+1, min(i+1+HOLD_MAX_BARS, len(c30))):
        h, l = c30[j]["high"], c30[j]["low"]
        if direction == 1:
            if l <= sl: return -1.0
            if h >= tp: return +RR
        else:
            if h >= sl: return -1.0
            if l <= tp: return +RR
    c = c30[min(i+1+HOLD_MAX_BARS, len(c30))-1]["close"]
    return (c - entry) / sl_dist * direction

def stats(rs):
    n = len(rs)
    if n == 0: return None
    wins = [r for r in rs if r > 0]
    loss = [r for r in rs if r <= 0]
    gw = sum(wins); gl = abs(sum(loss))
    pf = gw/gl if gl > 0 else float('inf')
    return {"n": n, "wr": len(wins)/n*100,
            "pf": pf, "exp": sum(rs)/n,
            "tot": sum(rs)}

# ---------- Run ----------
def run_backtest():
    log = M.log
    log("=== BACKTEST DIAGNOSTIC v3.0 ===")
    c30_full = M.fetch_ohlc("30m", "60d")
    c60_full = resample_30m_to_1h(c30_full)
    h1_map   = build_h1_map(c30_full, c60_full)
    log(f"M30: {len(c30_full)} | H1: {len(c60_full)}")

    per_strat = {s: [] for s in STRATEGIES}
    per_pair  = {f"{a}+{b}": [] for a, b in itertools.combinations(STRATEGIES, 2)}
    ensemble  = []

    for i in range(BURN_IN, len(c30_full) - HOLD_MAX_BARS - 2):
        c30 = c30_full[:i+1]
        h1_idx = h1_map[i]
        if h1_idx < 55: continue
        c60 = c60_full[:h1_idx+1]

        if not M.regime_ok(c30): continue
        hh = c30[-1]["time"].hour
        if not any(a <= hh < b for a, b in M.KILLZONES): continue

        a = M.atr(c30, 14)
        if a <= 0: continue
        sl_d = SL_MULT * a
        tp_d = SL_MULT * a * RR

        votes = {
            "SMC_Sweep":    M.strat_smc_sweep(c30),
            "MSB_MultiTF":  M.strat_msb_mtf(c30, c60),
            "MeanReversion":M.strat_mean_reversion(c30, c60),
            "SqueezeBreak": M.strat_squeeze_breakout(c30),
        }

        # (A) per strategi terisolasi
        for s, v in votes.items():
            if v != 0:
                per_strat[s].append(evaluate(c30_full, i, v, sl_d, tp_d))

        # (B) per pasangan (dua-duanya vote searah)
        for a_name, b_name in itertools.combinations(STRATEGIES, 2):
            va, vb = votes[a_name], votes[b_name]
            if va != 0 and va == vb:
                key = f"{a_name}+{b_name}"
                per_pair[key].append(evaluate(c30_full, i, va, sl_d, tp_d))

        # (C) ensemble seperti live
        score = sum(votes[k] * M.WEIGHTS[k] for k in votes)
        if abs(score) >= M.MIN_SCORE_WEIGHTED:
            d = 1 if score > 0 else -1
            ensemble.append(evaluate(c30_full, i, d, sl_d, tp_d))

    # ---- Laporkan ----
    def fmt(name, s):
        if s is None: return f"  {name:28s}: —"
        pf_s = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
        return (f"  {name:28s}: n={s['n']:4d} | WR={s['wr']:5.1f}% | "
                f"PF={pf_s:>5s} | Exp={s['exp']:+.3f}R | Tot={s['tot']:+.1f}R")

    log("\n--- (A) PER STRATEGI TERISOLASI ---")
    for s in STRATEGIES:
        log(fmt(s, stats(per_strat[s])))

    log("\n--- (B) PER PASANGAN (2 vote searah) ---")
    ranked = []
    for k, rs in per_pair.items():
        st = stats(rs)
        if st: ranked.append((k, st))
    for k, st in sorted(ranked, key=lambda x: -x[1]['exp']):
        log(fmt(k, st))

    log("\n--- (C) ENSEMBLE THRESHOLD 1.8 ---")
    log(fmt("ENSEMBLE", stats(ensemble)))

    # ---- Verdict ----
    best_single = max(((s, stats(per_strat[s])) for s in STRATEGIES
                       if stats(per_strat[s])),
                      key=lambda x: x[1]['exp'], default=(None, None))
    best_pair   = max(ranked, key=lambda x: x[1]['exp'], default=(None, None))

    lines = ["<b>🧪 DIAGNOSTIC BACKTEST — 60 HARI</b>", "──────────────────",
             "<b>(A) Per strategi terisolasi:</b>"]
    for s in STRATEGIES:
        st = stats(per_strat[s])
        if st:
            pf_s = f"{st['pf']:.2f}" if st['pf'] != float('inf') else "∞"
            lines.append(f"• {s:14s}: n={st['n']:3d} WR={st['wr']:.0f}% "
                         f"PF={pf_s} Exp={st['exp']:+.2f}R")
    lines.append("<b>(B) Best pairs:</b>")
    for k, st in sorted(ranked, key=lambda x: -x[1]['exp'])[:3]:
        pf_s = f"{st['pf']:.2f}" if st['pf'] != float('inf') else "∞"
        lines.append(f"• {k:32s}: n={st['n']:3d} PF={pf_s} Exp={st['exp']:+.2f}R")
    st_e = stats(ensemble)
    lines.append("<b>(C) Ensemble:</b>")
    if st_e:
        lines.append(f"• n={st_e['n']} PF={st_e['pf']:.2f} Exp={st_e['exp']:+.2f}R")
    lines.append("──────────────────")

    if best_single[0] and best_single[1]['pf'] >= 1.3 and best_single[1]['exp'] > 0.15:
        lines.append(f"✅ <b>Rekomendasi: pakai {best_single[0]} tunggal</b>")
        lines.append(f"   (PF={best_single[1]['pf']:.2f}, Exp={best_single[1]['exp']:+.2f}R)")
    elif best_pair[0] and best_pair[1]['pf'] >= 1.3 and best_pair[1]['exp'] > 0.15:
        lines.append(f"✅ <b>Rekomendasi: pasangan {best_pair[0]}</b>")
    else:
        lines.append("⚠️ <b>Tidak ada strategi dgn edge — perlu redesign</b>")

    msg = "\n".join(lines)
    log("\n" + msg.replace("<b>","").replace("</b>",""))
    M.send_telegram(msg)

if __name__ == "__main__":
    try:
        run_backtest()
    except Exception as e:
        import traceback
        M.log(f"FATAL: {e}\n{traceback.format_exc()}")
