#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ORB BACKTEST — 60 hari data 15m (Yahoo limit)."""
import os, sys, math
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M
from datetime import datetime, timezone, timedelta

HOLD_MAX_BARS = 24      # 6 jam (24 candle 15m)

def evaluate(c15, i, direction, sl_price, tp_price):
    if i+1 >= len(c15): return 0.0, "NO"
    entry = c15[i+1]["open"]
    sl_dist = abs(entry - sl_price)
    if sl_dist <= 0: return 0.0, "NO"
    for j in range(i+1, min(i+1+HOLD_MAX_BARS, len(c15))):
        h, l = c15[j]["high"], c15[j]["low"]
        if direction == 1:
            if l <= sl_price: return -1.0, "SL"
            if h >= tp_price: return (tp_price-entry)/sl_dist, "TP"
        else:
            if h >= sl_price: return -1.0, "SL"
            if l <= tp_price: return (entry-tp_price)/sl_dist, "TP"
    c = c15[min(i+1+HOLD_MAX_BARS, len(c15))-1]["close"]
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

def run():
    log = M.log
    log("=== ORB BACKTEST (60d, 15m) ===")
    c15 = M.fetch_ohlc("15m", "60d")
    log(f"Data: {len(c15)} candle")

    # Group by session date
    by_date = {}
    for c in c15:
        d = c["time"].date()
        by_date.setdefault(d, []).append(c)

    results = []
    sessions_tested = 0
    skipped_width = skipped_weekday = skipped_breakout = 0

    for sess_date in sorted(by_date.keys()):
        # Skip weekend
        if sess_date.weekday() >= 5: skipped_weekday += 1; continue
        if sess_date.weekday() == 4: skipped_weekday += 1; continue  # Jumat

        day_candles = by_date[sess_date]
        or_data = M.find_opening_range(day_candles, sess_date)
        if not or_data: continue

        or_pct = or_data["width"] / or_data["high"] * 100
        if or_pct < M.OR_MIN_PCT or or_pct > M.OR_MAX_PCT:
            skipped_width += 1; continue

        sessions_tested += 1

        # Cari breakout di window
        # Loop candle setelah OR selesai
        post_or = [c for c in day_candles if c["time"] >= or_data["end"]
                   and c["time"].hour < M.TRADE_END_H]

        found = False
        for k in range(len(post_or)):
            # Slice sampai k+1 untuk pastikan tidak "lihat depan"
            hist = day_candles[:day_candles.index(post_or[k])+1]
            dir_, bd = M.orb_breakout(hist, or_data, post_or[k]["time"])
            if dir_ is None: continue

            entry = post_or[k]["close"]
            if dir_ == 1:
                sl = or_data["low"] - or_data["width"] * 0.05
                tp = entry + or_data["width"] * M.RR_TARGET
            else:
                sl = or_data["high"] + or_data["width"] * 0.05
                tp = entry - or_data["width"] * M.RR_TARGET

            # Index di c15_full untuk simulasi
            idx_in_full = c15.index(post_or[k])
            r, h = evaluate(c15, idx_in_full, dir_, sl, tp)
            results.append({"time": str(post_or[k]["time"]), "dir": dir_,
                            "R": r, "hasil": h, "sess": str(sess_date)})
            found = True
            break  # 1 sinyal per session

        if not found: skipped_breakout += 1

    n = len(results)
    log(f"Sessions dites: {sessions_tested} | sinyal: {n} | "
        f"skip_weekday={skipped_weekday} skip_width={skipped_width} "
        f"no_breakout={skipped_breakout}")

    if n == 0:
        M.send_telegram("<b>ORB BACKTEST</b>: 0 sinyal."); return

    st = stats(results)
    # Per segmen (3)
    dates = sorted(set(r["sess"] for r in results))
    if len(dates) >= 3:
        chunk = len(dates) // 3
        segs = [set(dates[:chunk]), set(dates[chunk:2*chunk]), set(dates[2*chunk:])]
    else:
        segs = [set(dates), set(), set()]

    seg_st = [stats([r for r in results if r["sess"] in s]) for s in segs]

    def line(name, s):
        if s is None: return f"  {name:15s}: n=0"
        pf_s = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
        return (f"  {name:15s}: n={s['n']:3d} WR={s['wr']:5.1f}% "
                f"PF={pf_s:>5s} Exp={s['exp']:+.3f}R DD={s['mdd']:.1f}R")

    log("\n--- OVERALL ---"); log(line("TOTAL", st))
    log("\n--- SEGMEN ---")
    for k in range(3): log(line(f"Segmen {k+1}", seg_st[k]))

    consistent = sum(1 for s in seg_st if s and s["pf"] >= 1.3)
    ok = st["n"] >= 15 and st["pf"] >= 1.4 and st["exp"] > 0.2 and consistent >= 2

    msg = (f"<b>📋 ORB BACKTEST (60d)</b>\n"
           f"──────────────────\n"
           f"Sessions dites: {sessions_tested}\n"
           f"Total sinyal  : {st['n']}\n"
           f"🏆 Win rate   : {st['wr']:.1f}%\n"
           f"💵 PF         : {st['pf']:.2f}\n"
           f"📈 Expectancy : {st['exp']:+.3f} R\n"
           f"📉 Max DD     : {st['mdd']:.1f} R\n"
           f"──────────────────\n"
           f"<b>Walk-forward:</b>\n")
    for k in range(3):
        s = seg_st[k]
        if s:
            pf_s = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
            msg += f"• S{k+1}: n={s['n']} PF={pf_s} Exp={s['exp']:+.2f}R\n"
        else:
            msg += f"• S{k+1}: n=0\n"
    msg += "──────────────────\n"
    msg += "✅ LAYAK forward test" if ok else "⚠️ BELUM — evaluasi ulang"
    log("\n" + msg.replace("<b>","").replace("</b>",""))
    M.send_telegram(msg)

if __name__ == "__main__":
    try: run()
    except Exception as e:
        import traceback; M.log(f"FATAL: {e}\n{traceback.format_exc()}")
