#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MULTI-STRATEGY BACKTEST ENGINE v9.0
Menguji 6 strategi terpisah pada XAUUSD (Yahoo GC=F, 1H, 2 tahun).
"""
import os, sys, math, time, random, requests, statistics
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M
from datetime import datetime, timezone, timedelta

# ============ KONFIGURASI UMUM ============
RANGE_DAYS = 730
ATR_PERIOD = 14
HOLD_HOURS = 48
SL_ATR_MULT = 2.0
RR_TARGET = 1.5

# ============ FUNGSI DATA (Yahoo Finance) ============
def fetch_yahoo(symbol="GC=F", rng="2y"):
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?interval=1h&range={rng}")
        for a in range(3):
            try:
                r = requests.get(url, headers=M.HEADERS, timeout=30)
                if r.status_code == 429: time.sleep((2**a)+random.uniform(0,1)); continue
                r.raise_for_status()
                return _parse(r.json())
            except Exception as e: pass
    raise RuntimeError(f"Yahoo fetch {symbol} gagal")

def _parse(data):
    d = data["chart"]["result"][0]
    ts = d.get("timestamp") or []
    q = d["indicators"]["quote"][0]
    now = datetime.now(timezone.utc)
    out = []
    for i, t in enumerate(ts):
        try: o,h,l,c = q["open"][i],q["high"][i],q["low"][i],q["close"][i]
        except (KeyError, IndexError): continue
        if None in (o,h,l,c) or h<l or o<=0 or c<=0: continue
        dt = datetime.fromtimestamp(t, timezone.utc)
        if (dt + timedelta(hours=1)) > now: continue
        out.append({"time": dt, "open": float(o), "high": float(h),
                    "low": float(l), "close": float(c), "vol": 0.0})
    return out

# ============ INDIKATOR ============
def sma(v, n):
    if not v: return 0.0
    n = min(n, len(v)); return sum(v[-n:])/n

def ema(v, n):
    if not v: return 0.0
    if len(v) < n: return sum(v)/len(v)
    k = 2/(n+1); e = v[-n]
    for x in v[-n+1:]: e = x*k + e*(1-k)
    return e

def atr(c, n=14):
    if len(c) < 2: return 0.0
    trs = []
    for i in range(1, len(c)):
        h,l,pc = c[i]["high"],c[i]["low"],c[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sma(trs, n)

def rsi(v, n=14):
    if len(v) < n+1: return 50.0
    g = l = 0.0
    for i in range(-n, 0):
        d = v[i] - v[i-1]
        if d > 0: g += d
        else: l -= d
    if l == 0: return 100.0
    return 100 - 100/(1+(g/n)/(l/n))

def donchian(c, n=20):
    if len(c) < n+1: return None, None
    w = c[-n-1:-1] # exclude candle terakhir (hari ini)
    return max(x["high"] for x in w), min(x["low"] for x in w)

def macd(v, fast=12, slow=26, signal=9):
    if len(v) < slow: return 0,0
    ef = ema(v, fast); es = ema(v, slow)
    macd_line = ef - es
    # untuk signal line, kita butuh seri, tapi sederhananya:
    return macd_line, 0 # placeholder

# ============ STRATEGI (6 BUAH) ============
# Setiap fungsi mengembalikan +1, -1, atau 0 (tidak ada sinyal)

def strat_turtle_breakout(c):
    """Turtle Donchian-20 breakout dengan filter trend."""
    closes = [x["close"] for x in c]
    dc_up, dc_lo = donchian(c, 20)
    if not dc_up: return 0
    e50 = ema(closes, 50); e200 = ema(closes, 200)
    px = closes[-1]
    if px > dc_up and px > e50 and e50 > e200: return +1
    if px < dc_lo and px < e50 and e50 < e200: return -1
    return 0

def strat_macd_cross(c):
    """MACD crossover sederhana (bisa dikembangkan)."""
    closes = [x["close"] for x in c]
    if len(closes) < 35: return 0
    macd_now, _ = macd(closes[-30:])
    macd_prev, _ = macd(closes[-31:-1])
    # (Implementasi MACD crossover penuh membutuhkan signal line, ini hanya placeholder logika)
    if macd_now > 0 and macd_prev <= 0: return +1 # Bullish cross
    if macd_now < 0 and macd_prev >= 0: return -1 # Bearish cross
    return 0

def strat_orb(c):
    """ORB sederhana pada H1 (NY Open 13:30 UTC)."""
    # Catatan: ORB asli butuh data M15. Ini versi H1 yang disederhanakan.
    # Menganggap jam 13:00-14:00 UTC sebagai 'opening range'.
    if len(c) < 5: return 0
    last = c[-1]
    if last["time"].hour not in (13, 14): # hanya trade saat/setelah opening
        return 0
    or_candles = [x for x in c if x["time"].hour == 13 and x["time"].date() == last["time"].date()]
    if not or_candles: return 0
    or_high = max(x["high"] for x in or_candles)
    or_low = min(x["low"] for x in or_candles)
    if last["close"] > or_high: return +1
    if last["close"] < or_low: return -1
    return 0

def strat_break_retest(c):
    """Break & Re-test level support/resistance sederhana."""
    # Placeholder: Cek apakah candle terakhir menembus high/low 20 candle lalu.
    if len(c) < 25: return 0
    prev_high = max(x["high"] for x in c[-21:-1])
    prev_low = min(x["low"] for x in c[-21:-1])
    last = c[-1]
    # Sinyal sederhana: breakout dan close di atas resistance.
    if last["close"] > prev_high and last["close"] > last["open"]: return +1
    if last["close"] < prev_low and last["close"] < last["open"]: return -1
    return 0

def strat_mean_reversion(c):
    """RSI + Bollinger Band mean reversion."""
    closes = [x["close"] for x in c]
    if len(closes) < 50: return 0
    r = rsi(closes)
    m = sma(closes, 20)
    sd = math.sqrt(sum((x-m)**2 for x in closes[-20:])/20)
    if sd == 0: return 0
    z = (closes[-1] - m)/sd
    if z <= -2.0 and r < 30: return +1
    if z >= 2.0 and r > 70: return -1
    return 0

def strat_intermarket(c, dxy):
    """Sinyal dari DXY z-score (butuh data DXY)."""
    if not dxy or len(dxy) < 50: return 0
    dc = [x["close"] for x in dxy]
    z = (dc[-1] - sma(dc, 50)) / (statistics.stdev(dc[-50:]) or 1)
    # DXY overbought -> BUY Gold; DXY oversold -> SELL Gold
    if z >= 2.0: return +1
    if z <= -2.0: return -1
    return 0

# ============ SIMULASI & EVALUASI ============
def simulate(c, i, direction, atr_val):
    """Simulasi exit: SL/TP fixed berdasarkan ATR."""
    if i+1 >= len(c): return 0.0
    entry = c[i+1]["open"]
    sl_dist = atr_val * SL_ATR_MULT
    tp_dist = sl_dist * RR_TARGET
    if direction == 1:
        sl = entry - sl_dist; tp = entry + tp_dist
    else:
        sl = entry + sl_dist; tp = entry - tp_dist
    for j in range(i+1, min(i+1+HOLD_HOURS, len(c))):
        h,l = c[j]["high"], c[j]["low"]
        if direction == 1:
            if l <= sl: return -1.0
            if h >= tp: return RR_TARGET
        else:
            if h >= sl: return -1.0
            if l <= tp: return RR_TARGET
    exit_c = c[min(i+1+HOLD_HOURS, len(c))-1]["close"]
    return (exit_c - entry)/sl_dist * direction

def calculate_stats(rs):
    n = len(rs)
    if n == 0: return None
    wins = [r for r in rs if r > 0]
    gw = sum(wins); gl = abs(sum(r for r in rs if r <= 0))
    pf = gw/gl if gl > 0 else float('inf')
    return {"n": n, "wr": len(wins)/n*100, "pf": pf,
            "exp": sum(rs)/n, "tot": sum(rs)}

# ============ ENGINE UTAMA ============
def run():
    log = M.log
    log("=" * 60)
    log("MULTI-STRATEGY BACKTEST v9.0 (XAUUSD, 1H, 2 tahun)")
    log("=" * 60)
    
    # 1. Fetch data
    gold = fetch_yahoo("GC=F", "2y")
    log(f"Gold: {len(gold)} candle")
    try:
        dxy = fetch_yahoo("DX-Y.NYB", "2y")
        # align by time
        gold_map = {x["time"]: x for x in gold}
        dxy_map  = {x["time"]: x for x in dxy}
        common = sorted(set(gold_map.keys()) & set(dxy_map.keys()))
        gold = [gold_map[t] for t in common]
        dxy  = [dxy_map[t] for t in common]
        log(f"Aligned with DXY: {len(gold)} pairs")
    except Exception as e:
        log(f"DXY fetch failed: {e}, skipping intermarket strategy")
        dxy = None

    # 2. Tentukan strategi
    strategies = {
        "Turtle_Breakout": strat_turtle_breakout,
        "MACD_Cross": strat_macd_cross,
        "ORB_NY": strat_orb,
        "Break_Retest": strat_break_retest,
        "Mean_Reversion": strat_mean_reversion,
    }
    if dxy:
        strategies["Intermarket_DXY"] = strat_intermarket

    # 3. Loop setiap strategi & backtest
    results_all = {}
    for name, strat_fn in strategies.items():
        log(f"Backtesting {name}...")
        trades = []
        for i in range(220, len(gold) - HOLD_HOURS - 2):
            c_slice = gold[:i+1]
            if name == "Intermarket_DXY":
                dxy_slice = dxy[:i+1]
                signal = strat_fn(c_slice, dxy_slice)
            else:
                signal = strat_fn(c_slice)
            
            if signal == 0: continue
            a = atr(c_slice, ATR_PERIOD)
            if a <= 0: continue
            r = simulate(gold, i, signal, a)
            trades.append(r)
        
        stats = calculate_stats(trades)
        results_all[name] = {"stats": stats, "trades": trades}

    # 4. Tampilkan hasil
    log("\n" + "=" * 60)
    log("HASIL PERBANDINGAN STRATEGI (2 tahun, 1H)")
    log("=" * 60)
    log(f"{'Strategi':<22} | {'n':>5} | {'WR%':>6} | {'PF':>6} | {'Exp(R)':>8} | {'Tot(R)':>8}")
    log("-" * 80)
    ranked = []
    for name, data in results_all.items():
        s = data["stats"]
        if s is None:
            log(f"{name:<22} | {'0':>5} | {'-':>6} | {'-':>6} | {'-':>8} | {'-':>8}")
            continue
        log(f"{name:<22} | {s['n']:>5} | {s['wr']:>6.1f} | {s['pf']:>6.2f} | {s['exp']:>+8.3f} | {s['tot']:>+8.1f}")
        ranked.append((name, s))
    
    log("-" * 80)
    
    # 5. Verdict & rekomendasi
    if ranked:
        ranked.sort(key=lambda x: x[1]["exp"], reverse=True)
        log("\nPERINGKAT BERDASARKAN EXPECTANCY:")
        for i, (name, s) in enumerate(ranked, 1):
            log(f"  {i}. {name:<20} Exp={s['exp']:+.3f}R | PF={s['pf']:.2f} | WR={s['wr']:.1f}% | n={s['n']}")
        
        best = ranked[0]
        log(f"\nREKOMENDASI: Fokus pada '{best[0]}'")
        if best[1]["pf"] >= 1.4 and best[1]["exp"] > 0.2:
            log(f"Status: LAYAK forward test (PF={best[1]['pf']:.2f})")
        else:
            log(f"Status: BELUM LAYAK (PF={best[1]['pf']:.2f} < 1.4)")
        
        # Kirim ringkasan ke Telegram
        msg = f"<b>📊 MULTI-STRATEGY BACKTEST — 2 Tahun</b>\n"
        msg += "──────────────────\n"
        msg += f"<b>Peringkat (by Expectancy):</b>\n"
        for i, (name, s) in enumerate(ranked[:4], 1):
            msg += f"{i}. {name}: PF={s['pf']:.2f} Exp={s['exp']:+.2f}R n={s['n']}\n"
        msg += "──────────────────\n"
        msg += f"<b>Rekomendasi:</b> {best[0]}\n"
        msg += "✅ LAYAK" if best[1]["pf"] >= 1.4 else "⚠️ BELUM"
        M.send_telegram(msg)
    else:
        log("Tidak ada strategi yang menghasilkan sinyal.")
        M.send_telegram("<b>Multi-Strategy Backtest</b>: 0 sinyal dari semua strategi.")

if __name__ == "__main__":
    try: run()
    except Exception as e:
        import traceback; M.log(f"FATAL: {e}\n{traceback.format_exc()}")
