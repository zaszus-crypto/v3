#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BACKTEST ENGINE — menilai strategi bot pada 60 hari data historis riil.
Jalankan manual di GitHub Actions (workflow_dispatch: job=backtest) atau otomatis tiap Minggu.
Hasil: win rate, profit factor, expectancy (R), max drawdown -> dikirim ke Telegram.
"""
import os, sys, json, math
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")      # agar import main aman
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M
import requests

HOLD_MAX_BARS = 16          # 8 jam maksimal pegang posisi (16 candle M30)
SL_MULT, RR = 1.2, 2.5

def resample_30m_to_1h(c30):
    """Gabungkan 2 candle M30 -> 1 candle H1 (cukup untuk EMA50 H1)."""
    out = []
    for i in range(0, len(c30) - 1, 2):
        a, b = c30[i], c30[i+1]
        out.append({"time": a["time"], "open": a["open"], "high": max(a["high"], b["high"]),
                    "low": min(a["low"], b["low"]), "close": b["close"]})
    return out

def evaluate(c30, i, direction, sl_dist, tp_dist):
    """Simulasi hasil posisi: +R jika TP tersentuh duluan, -1R jika SL, exit timeout di close."""
    entry = c30[i+1]["open"]                      # eksekusi di open candle berikutnya
    sl = entry - direction * sl_dist
    tp = entry + direction * tp_dist
    for j in range(i+1, min(i+1+HOLD_MAX_BARS, len(c30))):
        h, l, c = c30[j]["high"], c30[j]["low"], c30[j]["close"]
        if direction == 1:
            if l <= sl: return -1.0, "SL"
            if h >= tp: return +RR, "TP"
        else:
            if h >= sl: return -1.0, "SL"
            if l <= tp: return +RR, "TP"
    r = (c - entry) / sl_dist * direction          # timeout: untung/rugi dalam satuan R
    return r, "TIMEOUT"

def run_backtest():
    log = M.log
    log("=== MULAI BACKTEST 60 HARI ===")
    c30_full = M.fetch_ohlc("30m", "60d")
    c60_full = resample_30m_to_1h(c30_full)
    log(f"Data M30: {len(c30_full)} candle | H1: {len(c60_full)} candle")

    results, skip_regime, skip_session, low_score = [], 0, 0, 0
    START = 120                                       # burn-in indikator
    for i in range(START, len(c30_full) - HOLD_MAX_BARS - 1):
        c30 = c30_full[:i+1]
        # H1 ter-align perkiraan: indeks i//2 - offset kecil
        h1_idx = max(60, (i // 2) - 1)
        c60 = c60_full[:h1_idx+1]
        if len(c60) < 55: continue

        # Terapkan SEMUA filter persis seperti live bot
        if not M.regime_ok(c30): skip_regime += 1; continue
        hh = c30[-1]["time"].hour
        if not any(a <= hh < b for a, b in M.KILLZONES): skip_session += 1; continue

        votes = {"SMC": M.strat_smc_sweep(c30),
                 "MSB": M.strat_msb_mtf(c30, c60),
                 "MR":  M.strat_mean_reversion(c30),
                 "SQ":  M.strat_squeeze_breakout(c30)}
        score = sum(votes.values())
        if abs(score) < M.MIN_SCORE: low_score += 1; continue

        direction = 1 if score > 0 else -1
        a = M.atr(c30, 14)
        r, hasil = evaluate(c30_full, i, direction, SL_MULT*a, SL_MULT*a*RR)
        results.append({"i": i, "time": str(c30[-1]["time"]), "dir": direction,
                        "score": score, "R": r, "hasil": hasil, "votes": votes})

    n = len(results)
    if n == 0:
        msg = "<b>BACKTEST</b>: tidak ada sinyal dalam 60 hari. Cek parameter."
        log(msg); M.send_telegram(msg); return

    wins  = [r for r in results if r["R"] > 0]
    losses = [r for r in results if r["R"] <= 0]
    win_rate = len(wins)/n*100
    gross_w = sum(r["R"] for r in wins); gross_l = abs(sum(r["R"] for r in losses))
    pf = gross_w/gross_l if gross_l > 0 else float("inf")
    expectancy = sum(r["R"] for r in results)/n
    # max drawdown dalam R
    eq, peak, mdd = 0, 0, 0
    for r in results:
        eq += r["R"]; peak = max(peak, eq); mdd = max(mdd, peak - eq)
    # statistik per strategi (kontribusi)
    contrib = {}
    for r in results:
        for k, v in r["votes"].items():
            if v != 0 and v == r["dir"]:
                contrib.setdefault(k, [0, 0]); contrib[k][0] += 1
                contrib[k][1] += r["R"]

    lines = [f"<b>📋 BACKTEST XAUUSD — 60 HARI DATA RIIL</b>", "──────────────────",
             f"Sinyal total    : {n}  (regime skip {skip_regime}, sesi skip {skip_session}, skor-rendah {low_score})",
             f"🏆 Win rate      : {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)",
             f"💵 Profit factor : {pf:.2f}",
             f"📈 Expectancy    : {expectancy:+.3f} R per trade",
             f"📉 Max drawdown  : {mdd:.1f} R",
             f"Hasil timeout   : {sum(1 for r in results if r['hasil']=='TIMEOUT')}",
             "──────────────────", "<b>Kontribusi strategi</b> (ikut vote searah):"]
    for k, (cnt, tot) in sorted(contrib.items(), key=lambda x: -x[1][1]):
        lines.append(f"• {k:4s}: {cnt:3d} kali | total {tot:+.1f} R")
    verdict = ("✅ LAYAK forward test" if pf >= 1.3 and expectancy > 0.15
               else "⚠️ BELUM LAYAK — perlu tuning (naikkan MIN_SCORE / sesuaikan filter)")
    lines += ["──────────────────", verdict,
              "<i>Catatan: belum termasuk spread/slippage. Kurangi ~0.1-0.2 R per trade untuk estimasi konservatif.</i>"]
    msg = "\n".join(lines)
    log("\n" + msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    M.send_telegram(msg)

if __name__ == "__main__":
    try:
        run_backtest()
    except Exception as e:
        M.log(f"BACKTEST FATAL: {e}")
