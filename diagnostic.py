#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostic: hitung berapa banyak sinyal yang dibuang tiap tahap filter per segmen."""
import os, sys
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M

RANGE_DAYS = "180d"
BURN_IN    = 100

def run():
    log = M.log
    log(f"=== DIAGNOSTIC ({RANGE_DAYS}) ===")
    c60_full = M.fetch_ohlc("1h", RANGE_DAYS)
    total_len = len(c60_full)
    log(f"H1 candles: {total_len}")

    segment_size = total_len // 3
    seg = lambda i: min(2, i // segment_size)

    # Counters per segment
    stats = [{"total":0, "regime":0, "killzone":0, "adx_high":0,
              "prime_ok":0, "msb_vote":0, "mr_vote":0, "signal":0}
             for _ in range(3)]

    # ADX distribution
    adx_vals = []
    msb_cond_counts = {"e50>e200":0, "e20>e50":0, "close>e20":0, "e20_rising":0,
                       "all_3_bull":0, "all_3_bear":0}

    for i in range(BURN_IN, total_len - 20):
        c = c60_full[:i+1]
        s = seg(i)
        stats[s]["total"] += 1

        if not M.regime_ok(c): continue
        stats[s]["regime"] += 1

        hh = c[-1]["time"].hour
        if not M.in_killzone(hh): continue
        stats[s]["killzone"] += 1

        adx_val = M.adx(c, 14)
        adx_vals.append((s, adx_val, hh))

        if adx_val >= M.ADX_TREND:
            stats[s]["adx_high"] += 1
            if hh not in M.MSB_HOURS: continue
            stats[s]["prime_ok"] += 1
            v = M.strat_msb_strict(c, c)
            if v != 0: stats[s]["msb_vote"] += 1
            if abs(v) > 0: stats[s]["signal"] += 1

            # Breakdown MSB condition
            closes = [x["close"] for x in c]
            e50  = M.ema(closes, 50)
            e200 = M.ema(closes, min(200, len(closes)))
            e20  = M.ema(closes, 20)
            e20p = M.ema(closes[:-5], 20)
            if e50 > e200: msb_cond_counts["e50>e200"] += 1
            if e20 > e50:  msb_cond_counts["e20>e50"] += 1
            if closes[-1] > e20: msb_cond_counts["close>e20"] += 1
            if e20 > e20p: msb_cond_counts["e20_rising"] += 1
            if v == +1: msb_cond_counts["all_3_bull"] += 1
            if v == -1: msb_cond_counts["all_3_bear"] += 1
        elif adx_val <= M.ADX_RANGE:
            v = M.strat_mean_reversion(c, c)
            if v != 0:
                stats[s]["mr_vote"] += 1
                stats[s]["signal"] += 1

    # Report
    log("\n=== PER SEGMEN ===")
    log(f"{'Seg':4s} {'Total':>7s} {'Regime':>7s} {'Killz':>7s} {'ADX≥30':>7s} "
        f"{'Prime':>7s} {'MSBvote':>8s} {'MRvote':>7s} {'SIGNAL':>7s}")
    for k in range(3):
        st = stats[k]
        log(f"S{k+1:<3d} {st['total']:>7d} {st['regime']:>7d} {st['killzone']:>7d} "
            f"{st['adx_high']:>7d} {st['prime_ok']:>7d} {st['msb_vote']:>8d} "
            f"{st['mr_vote']:>7d} {st['signal']:>7d}")

    # ADX distribution
    log("\n=== ADX DISTRIBUTION (per segmen) ===")
    import statistics
    for k in range(3):
        vals = [v for (s, v, h) in adx_vals if s == k]
        if not vals: continue
        log(f"S{k+1}: n={len(vals)} min={min(vals):.1f} "
            f"med={statistics.median(vals):.1f} max={max(vals):.1f} "
            f">=30: {sum(1 for v in vals if v>=30)} "
            f"<=20: {sum(1 for v in vals if v<=20)}")

    # MSB conditions
    log("\n=== MSB CONDITION COUNT (setelah ADX≥30 + prime) ===")
    for k, v in msb_cond_counts.items():
        log(f"  {k:15s}: {v}")

    msg = (f"<b>🔬 DIAGNOSTIC — {RANGE_DAYS}</b>\n"
           f"──────────────────\n"
           f"Per segmen (Total→Regime→Killz→ADX≥30→Prime→Vote→Signal):\n")
    for k in range(3):
        st = stats[k]
        msg += (f"• S{k+1}: {st['total']}→{st['regime']}→{st['killzone']}→"
                f"{st['adx_high']}→{st['prime_ok']}→"
                f"{st['msb_vote']+st['mr_vote']}→{st['signal']}\n")
    M.send_telegram(msg)
    log("\n" + msg.replace("<b>","").replace("</b>",""))

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        import traceback
        M.log(f"FATAL: {e}\n{traceback.format_exc()}")
