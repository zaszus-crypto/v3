#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GOLDPULSE BACKTEST v2.0 — dengan exit management + entry filter.
Perbaikan dari v1.0:
  • Partial TP 50% di +1R
  • Breakeven stop di +0.5R
  • Filter EMA separation, RSI
"""
import os, sys, math
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M
from datetime import datetime, timezone, timedelta

HOLD_MAX = 24
RR_R = M.RR_TARGET
PARTIAL_AT_R = 1.0       # ambil 50% di +1R
BE_AT_R      = 0.5       # SL ke entry di +0.5R

# --- Filter baru ---
EMA_SEP_MIN  = 0.003     # EMA50-EMA200 minimal 0.3% dari harga
RSI_BUY_MAX  = 65
RSI_BUY_MIN  = 40
RSI_SELL_MAX = 60
RSI_SELL_MIN = 35

def rsi(closes, n=14):
    if len(closes) < n+1: return 50.0
    g = l = 0.0
    for i in range(-n, 0):
        d = closes[i] - closes[i-1]
        if d > 0: g += d
        else: l -= d
    if l == 0: return 100.0
    rs = (g/n) / (l/n)
    return 100 - 100/(1+rs)

def fetch_h1_long():
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

def simulate_v2(c, i, direction, entry, sl, tp, risk):
    """
    Simulasi dengan partial TP + breakeven.
    Return R (bukan hasil string), dengan partial 50% di +1R.
    """
    partial_done = False
    be_done = False
    r_total = 0.0

    for j in range(i+1, min(i+1+HOLD_MAX, len(c))):
        h, l = c[j]["high"], c[j]["low"]

        if direction == 1:
            # Cek SL dulu (worst case)
            if l <= sl:
                if partial_done:
                    # 50% sudah keluar di +1R, 50% kena SL
                    return 0.5 * PARTIAL_AT_R - 0.5 * 1.0 if be_done else \
                           0.5 * PARTIAL_AT_R - 0.5 * 1.0, "SL_partial"
                return -1.0, "SL"
            # Partial TP
            if not partial_done and h >= entry + risk * PARTIAL_AT_R:
                r_total += 0.5 * PARTIAL_AT_R
                partial_done = True
            # Breakeven: geser SL ke entry
            if partial_done and not be_done:
                sl = entry  # geser SL
                be_done = True
            # Full TP
            if h >= tp:
                if partial_done:
                    return r_total + 0.5 * RR_R, "TP_partial"
                return RR_R, "TP"
        else:
            if h >= sl:
                if partial_done:
                    return 0.5 * PARTIAL_AT_R - 0.5 * 1.0, "SL_partial"
                return -1.0, "SL"
            if not partial_done and l <= entry - risk * PARTIAL_AT_R:
                r_total += 0.5 * PARTIAL_AT_R
                partial_done = True
            if partial_done and not be_done:
                sl = entry
                be_done = True
            if l <= tp:
                if partial_done:
                    return r_total + 0.5 * RR_R, "TP_partial"
                return RR_R, "TP"

    # Timeout
    c_exit = c[min(i+1+HOLD_MAX, len(c))-1]["close"]
    if partial_done:
        r_remain = (c_exit - entry)/risk * direction
        return r_total + 0.5 * r_remain, "TIMEOUT_partial"
    return (c_exit - entry)/risk * direction, "TIMEOUT"

def check_signal_v2(c):
    """Wrapper dengan filter tambahan."""
    if len(c) < 220: return None, "data kurang"
    closes = [x["close"] for x in c]
    last = c[-1]; prev = c[-2]

    # Panggil filter dasar
    direction, info = M.check_signal(c)
    if direction is None: return None, info

    e50  = info["e50"]
    e200 = info["e200"]
    px   = last["close"]

    # Filter 1: EMA separation
    sep = abs(e50 - e200) / px
    if sep < EMA_SEP_MIN:
        return None, f"EMA terlalu dekat ({sep*100:.2f}%)"

    # Filter 2: RSI
    r = rsi(closes)
    if direction == 1:
        if not (RSI_BUY_MIN <= r <= RSI_BUY_MAX):
            return None, f"RSI BUY tidak ideal ({r:.0f})"
    else:
        if not (RSI_SELL_MIN <= r <= RSI_SELL_MAX):
            return None, f"RSI SELL tidak ideal ({r:.0f})"

    info["rsi"] = r
    info["ema_sep"] = sep
    return direction, info

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
    log("=== GOLDPULSE BACKTEST v2.0 (partial TP + BE + filter) ===")
    c = fetch_h1_long()
    log(f"Data: {len(c)} candle H1 ({len(c)/24:.0f} hari)")

    results = []
    reasons = {"bias": 0, "atr": 0, "pullback": 0, "trigger": 0,
               "body": 0, "vol": 0, "session": 0, "ema_sep": 0,
               "rsi": 0, "signal": 0}

    for i in range(220, len(c) - HOLD_MAX - 1):
        c_slice = c[:i+1]
        direction, info = check_signal_v2(c_slice)
        if direction is None:
            key = None
            s = str(info).lower()
            if "ema terlalu" in s: key = "ema_sep"
            elif "rsi" in s: key = "rsi"
            elif "bias" in s: key = "bias"
            elif "atr" in s: key = "atr"
            elif "pullback" in s: key = "pullback"
            elif "trigger" in s: key = "trigger"
            elif "body" in s: key = "body"
            elif "volume" in s: key = "vol"
            elif "sesi" in s or "session" in s: key = "session"
            if key: reasons[key] += 1
            continue

        entry = c[i+1]["open"]
        a = info["atr"]
        sl, tp, risk = M.calc_sl_tp(direction, entry, info, a)
        if risk <= 0: continue

        r, hasil = simulate_v2(c, i, direction, entry, sl, tp, risk)
        results.append({"time": str(c_slice[-1]["time"]), "dir": direction,
                        "R": r, "hasil": hasil, "idx": i})
        reasons["signal"] += 1

    n = len(results)
    log(f"Total sinyal: {n} | reasons: {reasons}")
    if n == 0:
        M.send_telegram("<b>GOLDPULSE v2.0</b>: 0 sinyal."); return

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
    log("\n--- SEGMEN ---")
    for k in range(3): log(line(f"Segmen {k+1}", seg_stats[k]))

    consistent = sum(1 for s in seg_stats if s and s["pf"] >= 1.3)
    ok = (overall["n"] >= 80 and overall["pf"] >= 1.35
          and overall["exp"] > 0.15 and consistent >= 2)

    msg = (f"<b>📋 GOLDPULSE v2.0 — 2y H1</b>\n"
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
    msg += "──────────────────\n"
    msg += f"Konsistensi: {consistent}/3 segmen PF≥1.3\n"
    msg += "✅ LAYAK forward test" if ok else "⚠️ BELUM"
    log("\n" + msg.replace("<b>","").replace("</b>",""))
    M.send_telegram(msg)

if __name__ == "__main__":
    try: run()
    except Exception as e:
        import traceback; M.log(f"FATAL: {e}\n{traceback.format_exc()}")
