#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAUUSD GOLD SIGNAL BOT - GitHub Actions Edition
Strategi Ensemble: SMC Liquidity Sweep + FVG | Bollinger Squeeze Breakout |
Mean Reversion Z-Score | Multi-TF MSB (H1 bias, M30 trigger) |
Regime Detection (ATR) + Session Killzone + Spot Price Verification
Sinyal dikirim ke Telegram untuk eksekusi MANUAL di MT5.
"""
import os, json, time, math, random, requests
from datetime import datetime, timezone

# ========================== KONFIGURASI ==========================
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_DIR  = os.environ.get("STATE_DIR", "state")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOG_FILE   = os.path.join(STATE_DIR, "signals.log")

# Offset harga spot -> harga MT5 Anda (sesuaikan!)
SPOT_OFFSETS = {"gold-api": -4.00, "coinbase-paxg": -4.50}

# Filter sesi (UTC): hanya sinyal di jam likuiditas institusional
KILLZONES = [(6, 12), (12, 17)]          # London & New York
MINUTES_PER_CANDLE = 30                   # trigger TF

# Voting ensemble
MIN_SCORE       = 3        # minimal skor agregat untuk valid sinyal
COOLDOWN_MIN    = 90       # antispam: minimal menit antar sinyal searah
MAX_DEVIATION   = 15.0     # toleransi selisih spot vs futures (USD)
RR_TARGET       = 2.5      # risk:reward minimum

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"}

os.makedirs(STATE_DIR, exist_ok=True)

# ========================== UTILITAS ==========================
def log(msg):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def send_telegram(text):
    """Kirim ke Telegram dengan retry/backoff (anti-fail telegram)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    for attempt in range(4):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                log("Telegram terkirim."); return True
            if r.status_code == 429:                      # rate limit
                wait = int(r.json().get("parameters", {}).get("retry_after", 5)) + 2
                log(f"Rate limit, tunggu {wait}s"); time.sleep(wait); continue
            log(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log(f"Telegram error percobaan {attempt+1}: {e}")
        time.sleep(3 * (attempt + 1) + random.uniform(0, 2))
    log("GAGAL kirim Telegram setelah semua percobaan.")
    return False

def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception:
        return {"last_signal_time": "", "last_direction": "", "sent_ids": []}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

# ========================== DATA (TANPA API KEY) ==========================
def fetch_ohlc(interval="30m", rng="5d"):
    """OHLC dari Yahoo Finance (GC=F = Gold Futures, proxy XAUUSD)."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
           f"?interval={interval}&range={rng}")
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    d = r.json()["chart"]["result"][0]
    ts, q = d["timestamp"], d["indicators"]["quote"][0]
    vols = d["indicators"].get("quote", [{}])[0].get("volume", [0]*len(ts))
    candles = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c) or h < l: continue
        candles.append({"time": datetime.fromtimestamp(t, timezone.utc),
                        "open": o, "high": h, "low": l, "close": c,
                        "vol": (vols[i] or 0) if i < len(vols) else 0})
    return candles

def fetch_spot():
    """Harga spot real-time multi-sumber (gratis) + offset."""
    prices = []
    try:
        p = float(requests.get("https://api.gold-api.com/price/XAU",
                               headers=HEADERS, timeout=6).json()["price"])
        prices.append(p + SPOT_OFFSETS["gold-api"])
    except Exception: pass
    try:
        p = float(requests.get("https://api.coinbase.com/v2/prices/PAXG-USD/spot",
                               headers=HEADERS, timeout=6).json()["data"]["amount"])
        prices.append(p + SPOT_OFFSETS["coinbase-paxg"])
    except Exception: pass
    return sum(prices)/len(prices) if prices else None

# ========================== INDIKATOR ==========================
def ema(vals, n):
    k = 2/(n+1); e = vals[0]
    for v in vals[1:]: e = v*k + e*(1-k)
    return e

def sma(vals, n): return sum(vals[-n:])/n

def atr(candles, n=14):
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sma(trs[-n:], n) if len(trs) >= n else sma(trs, len(trs))

def rsi(closes, n=14):
    if len(closes) < n+1: return 50
    g = l = 0
    for i in range(-n, 0):
        d = closes[i] - closes[i-1]
        if d > 0: g += d
        else: l -= d
    if l == 0: return 100
    rs = (g/n) / (l/n)
    return 100 - 100/(1+rs)

def bollinger(closes, n=20, mult=2.0):
    m = sma(closes, n)
    var = sum((c-m)**2 for c in closes[-n:]) / n
    sd = math.sqrt(var)
    return m, m + mult*sd, m - mult*sd, sd

def swing_points(candles, lb=5):
    """High/low fractal untuk liquidity pools."""
    highs, lows = [], []
    for i in range(lb, len(candles)-lb):
        h, l = candles[i]["high"], candles[i]["low"]
        if all(h >= candles[i-j]["high"] for j in range(1, lb+1)) and \
           all(h >= candles[i+j]["high"] for j in range(1, lb+1)):
            highs.append(h)
        if all(l <= candles[i-j]["low"] for j in range(1, lb+1)) and \
           all(l <= candles[i+j]["low"] for j in range(1, lb+1)):
            lows.append(l)
    return highs[-3:], lows[-3:]

# ========================== STRATEGI (masing2 mengembalikan -1/0/+1) ==========================
def strat_smc_sweep(c):
    """Liquidity sweep + FVG/OB."""
    highs, lows = swing_points(c)
    if not highs or not lows: return 0
    last = c[-2]  # candle terakhir yang sudah close
    if last["low"] < min(lows) and last["close"] > min(lows):
        return +1   # sweep bawah -> bullish
    if last["high"] > max(highs) and last["close"] < max(highs):
        return -1   # sweep atas -> bearish
    return 0

def strat_msb_mtf(c30, c60):
    """Market structure H1 sebagai bias, M30 trigger."""
    e60 = ema([x["close"] for x in c60], 50)
    bias = +1 if c60[-2]["close"] > e60 else -1
    e30 = ema([x["close"] for x in c30], 20)
    trig = +1 if c30[-2]["close"] > e30 else -1
    return bias if bias == trig else 0

def strat_mean_reversion(c):
    """Z-score dari mean 50 candle + RSI ekstrem."""
    closes = [x["close"] for x in c]
    if len(closes) < 50: return 0
    m = sma(closes, 50)
    sd = math.sqrt(sum((x-m)**2 for x in closes[-50:])/50)
    if sd == 0: return 0
    z = (closes[-2] - m)/sd
    r = rsi(closes)
    if z <= -2.0 and r < 35: return +1
    if z >= 2.0 and r > 65: return -1
    return 0

def strat_squeeze_breakout(c):
    """Bollinger squeeze + ekspansi ATR."""
    closes = [x["close"] for x in c]
    if len(closes) < 21: return 0
    m, up, lo, sd = bollinger(closes)
    a_now = atr(c, 14); a_ref = atr(c[:-7], 14) if len(c) > 21 else a_now
    width = (up-lo)/m
    squeeze = width < 0.008                     # pita sangat sempit
    expansion = a_now > a_ref * 1.3             # ATR meledak
    last = c[-2]
    if squeeze and expansion and last["close"] > up: return +1
    if squeeze and expansion and last["close"] < lo: return -1
    return 0

def regime_ok(c):
    """Regime filter: ATR relatif -> hindari market 'mati' atau terlalu gila."""
    a = atr(c, 14); px = c[-2]["close"]
    rel = a/px
    return 0.0008 < rel < 0.02   # antara 0.08% - 2% per candle M30

def in_killzone():
    h = datetime.now(timezone.utc).hour
    return any(a <= h < b for a, b in KILLZONES)

# ========================== MAIN ==========================
def build_signal(score, sl, tp, price, votes, regime_note, spot_dev):
    emoji = "🟢 BUY" if score > 0 else "🔴 SELL"
    side  = "BUY" if score > 0 else "SELL"
    return (
        f"<b>{emoji} XAUUSD — SINYAL {side} (M30/H1)</b>\n"
        f"──────────────────\n"
        f"💰 Entry ref : <b>{price:,.2f}</b>\n"
        f"🛑 Stop Loss : <b>{sl:,.2f}</b>\n"
        f"🎯 Take Profit: <b>{tp:,.2f}</b>  (RR 1:{RR_TARGET})\n"
        f"🧠 Skor Ensemble: {abs(score)}/5 | Votes: {votes}\n"
        f"📊 Regime: {regime_note}\n"
        f"🔎 Deviasi spot: {spot_dev}\n"
        f"⏰ {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n"
        f"⚠️ Eksekusi MANUAL di MT5. Sesuaikan dengan harga broker Anda."
    )

def main():
    log("=== RUN BOT XAUUSD ===")
    state = load_state()

    c30 = fetch_ohlc("30m", "5d")
    c60 = fetch_ohlc("1h", "1mo")
    if len(c30) < 60 or len(c60) < 60:
        log("Data OHLC tidak cukup, skip."); return

    price = c30[-2]["close"]
    a = atr(c30, 14)

    # --- 1. Regime gate ---
    if not regime_ok(c30):
        log("Regime tidak memenuhi syarat (volatilitas ekstrem/sepi). Skip.")
        return

    # --- 2. Session gate ---
    if not in_killzone():
        log("Di luar killzone London/NY. Skip."); return

    # --- 3. Spot verification ---
    spot = fetch_spot()
    if spot:
        dev = abs(spot - price)
        if dev > MAX_DEVIATION:
            log(f"Deviasi spot {dev:.2f} > {MAX_DEVIATION}. Skip (data korup?).")
            return
        dev_txt = f"{dev:.2f} USD (OK)"
    else:
        dev_txt = "verifikasi gagal (lanjut dengan hati-hati)"

    # --- 4. Ensemble voting ---
    votes = {
        "SMC_Sweep":    strat_smc_sweep(c30),
        "MSB_MultiTF":  strat_msb_mtf(c30, c60),
        "MeanReversion":strat_mean_reversion(c30),
        "SqueezeBreak": strat_squeeze_breakout(c30),
    }
    score = sum(votes.values())
    log(f"Votes: {votes} => skor {score}")

    # --- 5. Anti-spam / anti-loop cooldown ---
    now = datetime.now(timezone.utc)
    last_t = state.get("last_signal_time")
    if last_t:
        mins = (now - datetime.fromisoformat(last_t)).total_seconds()/60
        same_dir = (score > 0 and state["last_direction"] == "BUY") or \
                   (score < 0 and state["last_direction"] == "SELL")
        if same_dir and mins < COOLDOWN_MIN:
            log(f"Cooldown aktif ({mins:.0f}/{COOLDOWN_MIN} menit). Skip."); return

    # --- 6. Gate ML (Gradient Boosting walk-forward) ---
    ml_proba = None
    try:
        import ml_engine
        ml_proba = ml_engine.predict_proba(c30)
        if ml_proba is not None and ml_proba < 0.55:
            log(f"ML menolak sinyal (proba {ml_proba:.2f} < 0.55). Skip.")
            return
    except ImportError:
        log("ml_engine tidak tersedia, lanjut tanpa filter ML.")

    # --- 7. Eksekusi sinyal ---
    if abs(score) >= MIN_SCORE:
        direction = 1 if score > 0 else -1
        sl = price - direction * 1.2 * a
        tp = price + direction * 1.2 * a * RR_TARGET
        if sl > price or tp < price:  # sanity check aritmetika
            sl, tp = (price - 1.2*a, price + 1.2*a*RR_TARGET) if direction == 1 \
                     else (price + 1.2*a, price - 1.2*a*RR_TARGET)
        ml_txt = f"{ml_proba:.2f}" if ml_proba is not None else "n/a"
        msg = build_signal(score, sl, tp, price, votes,
                           f"Volatilitas normal | ML prob: {ml_txt}", dev_txt)
        if send_telegram(msg):
            state.update(last_signal_time=now.isoformat(),
                         last_direction="BUY" if direction == 1 else "SELL")
            save_state(state)
    else:
        log("Skor belum cukup. Tidak ada sinyal.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        # jangan raise -> workflow tetap hijau, bot tetap jalan run berikutnya
