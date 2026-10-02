#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GOLDPULSE BACKTEST — 730 hari H1 (Yahoo max untuk 1h)."""
import os, sys, math
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M
from datetime import datetime, timezone, timedelta

HOLD_MAX = 24         # 24 jam max hold

def fetch_h1_long():
    """Fetch 730 hari H1 (Yahoo limit)."""
    last_err = None
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/GC=F"
               f"?interval=1h&range=2y")
        for attempt in range(3):
            try:
                r = M.requests.get(url, headers=M.HEADERS, timeout=30)
                if r.status_code == 429:
                    M.time.sleep((2**attempt) + M.random.uniform(0,1)); continue
                r.raise_for_status()
                return M._parse(r.json())
            except Exception as e:
                last_err = e
                M.time.sleep((2**attempt) + M.random.uniform(0,1))
    raise RuntimeError(f"Yahoo gagal: {last_err}")

def simulate(c, i, direction, entry, sl, tp, risk):
    """Simulasi dengan BE di +1R."""
    be_active = False
    for j in range(i+1, min(i+1+HOLD_MAX, len(c))):
        h, l = c[j]["high"], c[j]["low"]
        if direction == 1:
            if l <= sl: return -1.0, "SL"
            if be_active and l <= entry: return 0.0, "BE"
            if h >= tp: return RR_R, "TP"
            if not be_active and h >= entry + risk:
                be_active = True
        else:
            if h >= sl: return -1.0, "SL"
            if be_active and h >= entry: return 0.0, "BE"
            if l <= tp: return RR_R, "TP"
            if not be_active and l <= entry - risk:
                be_active = True
    # Timeout
    c_exit = c[min(i+1+HOLD_MAX, len(c))-1]["close"]
    return (c_exit - entry)/risk * direction, "TIMEOUT"

RR_R = M.RR_TARGET

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
    log("=== GOLDPULSE BACKTEST — 730 hari H1 ===")
    c = fetch_h1_long()
    log(f"Data: {len(c)} candle H1 ({len(c)/24:.0f} hari)")

    results = []
    reasons = {"bias": 0, "atr": 0, "pullback": 0, "trigger": 0,
               "body": 0, "vol": 0, "session": 0, "signal": 0}

    for i in range(220, len(c) - HOLD_MAX - 1):
        c_slice = c[:i+1]
        direction, info = M.check_signal(c_slice)
        if direction is None:
            for k in reasons:
                if k in str(info).lower(): reasons[k] += 1; break
            continue

        entry = c[i+1]["open"]     # eksekusi di open candle berikutnya
        a = info["atr"]
        sl, tp, risk = M.calc_sl_tp(direction, entry, info, a)
        if risk <= 0: continue

        r, hasil = simulate(c, i, direction, entry, sl, tp, risk)
        results.append({"time": str(c_slice[-1]["time"]), "dir": direction,
                        "R": r, "hasil": hasil, "idx": i})
        reasons["signal"] += 1

    n = len(results)
    log(f"Total sinyal: {n} | reasons: {reasons}")
    if n == 0:
        M.send_telegram("<b>GOLDPULSE BACKTEST</b>: 0 sinyal."); return

    # Walk-forward 3 segmen
    total = len(c)
    third = total // 3
    seg_stats = []
    for k in range(3):
        lo, hi = k*third, (k+1)*third
        rs = [r for r in results if lo <= r["idx"] < hi]
        seg_stats.append(stats(rs))

    overall = stats(results)

    def line(name, s):
        if s is None: return f"  {name:12s}: n=0"
        pf_s = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
        return (f"  {name:12s}: n={s['n']:3d} WR={s['wr']:5.1f}% "
                f"PF={pf_s:>5s} Exp={s['exp']:+.3f}R DD={s['mdd']:.1f}R")

    log("\n--- OVERALL ---"); log(line("TOTAL", overall))
    log("\n--- SEGMEN (walk-forward 3×) ---")
    for k in range(3): log(line(f"Segmen {k+1}", seg_stats[k]))

    consistent = sum(1 for s in seg_stats if s and s["pf"] >= 1.3)
    ok = (overall["n"] >= 50 and overall["pf"] >= 1.4
          and overall["exp"] > 0.2 and consistent >= 2)

    msg = (f"<b>📋 GOLDPULSE BACKTEST — 730d H1</b>\n"
           f"──────────────────\n"
           f"Total sinyal  : {overall['n']}\n"
           f"🏆 Win rate   : {overall['wr']:.1f}%\n"
           f"💵 PF         : {overall['pf']:.2f}\n"
           f"📈 Expectancy : {overall['exp']:+.3f} R\n"
           f"📉 Max DD     : {overall['mdd']:.1f} R\n"
           f"──────────────────\n"
           f"<b>Walk-forward:</b>\n")
    for k in range(3):
        s = seg_stats[k]
        if s:
            pf_s = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
            msg += f"• S{k+1}: n={s['n']} WR={s['wr']:.0f}% PF={pf_s} Exp={s['exp']:+.2f}R\n"
        else:
            msg += f"• S{k+1}: n=0\n"
    msg += "──────────────────\n"
    msg += f"Konsistensi: {consistent}/3 segmen PF≥1.3\n"
    msg += "✅ LAYAK forward test" if ok else "⚠️ BELUM"
    log("\n" + msg.replace("<b>","").replace("</b>",""))
    M.send_telegram(msg)

if __name__ == "__main__":
    try: run()
    except Exception as e:
        import traceback; M.log(f"FATAL: {e}\n{traceback.format_exc()}")
