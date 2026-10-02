#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAUUSD GOLD SIGNAL BOT — GitHub Actions Edition v2.0 (FIXED)
================================================================
Perbaikan dari v1.0:
  • Data validation (gap, duplikat, None, harga invalid)
  • Candle-close enforcement (tidak memakai candle in-progress)
  • Dynamic spot-offset calibration
  • Weighted voting + prime-hour boost
  • Position sizing calculator
  • Open-signal tracking untuk monitoring performa
  • Retry/backoff Yahoo (query1 → query2 fallback)
  • ATR/indikator aman saat data minim
Sinyal dikirim ke Telegram untuk eksekusi MANUAL di MT5.
"""
import os, json, time, math, random, pickle, requests
from datetime import datetime, timezone, timedelta

# ========================== KONFIGURASI ==========================
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_DIR        = os.environ.get("STATE_DIR", "state")
STATE_FILE       = os.path.join(STATE_DIR, "state.json")
LOG_FILE         = os.path.join(STATE_DIR, "signals.log")
MODEL_FILE       = os.path.join(STATE_DIR, "ml_model.pkl")
MODEL_META_FILE  = os.path.join(STATE_DIR, "ml_meta.json")

os.makedirs(STATE_DIR, exist_ok=True)

# Sesi (UTC). Gold bergerak 24 jam; prioritaskan overlap London–NY.
KILLZONES     = [(0, 5), (6, 12), (12, 17)]     # Asia, London, NY-overlap
PRIME_HOURS   = set(range(12, 17))               # overlap = likuiditas tertinggi

# Voting
MIN_SCORE_WEIGHTED = 2.6
WEIGHTS = {"SMC_Sweep": 1.25, "MSB_MultiTF": 1.15,
           "MeanReversion": 0.80, "SqueezeBreak": 1.00}

# Risk model
SL_MULT    = 1.8
RR_TARGET  = 2.5
ATR_PERIOD = 14

# Anti-spam
COOLDOWN_MIN = 90

# Spot verification
MAX_DEVIATION      = 15.0
SPOT_OFFSET_DEFAULT = -4.0        # fallback awal; akan dikalibrasi dinamis

# ========================== UTILITAS ==========================
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
           "Accept": "application/json,text/plain,*/*"}

def log(msg):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    for attempt in range(5):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                log("Telegram terkirim."); return True
            if r.status_code == 429:
                wait = int(r.json().get("parameters", {}).get("retry_after", 5)) + 2
                log(f"Rate limit, tunggu {wait}s"); time.sleep(wait); continue
            log(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log(f"Telegram error attempt {attempt+1}: {e}")
        time.sleep(3 * (attempt + 1) + random.uniform(0, 2))
    log("GAGAL kirim Telegram setelah semua percobaan.")
    return False

def load_state():
    default = {"last_signal_time": "", "last_direction": "",
               "sent_ids": [], "spot_offset": SPOT_OFFSET_DEFAULT,
               "open_signals": []}
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        for k, v in default.items():
            s.setdefault(k, v)
        return s
    except Exception:
        return default

def save_state(s):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f, indent=2)
    except Exception as e:
        log(f"save_state error: {e}")

# ========================== DATA (Yahoo, fallback ganda) ==========================
_INTERVAL_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60}

def _yahoo_chart(symbol, interval, rng, host="query1"):
    url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval={interval}&range={rng}")
    last_err = None
    for attempt in range(4):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 429:
                wait = (2 ** attempt) + random.uniform(0, 1.5)
                log(f"Yahoo 429 ({host}), tunggu {wait:.1f}s")
                time.sleep(wait); continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep((2 ** attempt) + random.uniform(0, 1))
    raise RuntimeError(f"Yahoo fetch gagal: {last_err}")

def fetch_ohlc(interval="30m", rng="5d", drop_incomplete=True):
    """OHLC dari Yahoo Finance (GC=F) dengan validasi ketat."""
    data = None
    for host in ("query1", "query2"):
        try:
            data = _yahoo_chart("GC=F", interval, rng, host=host)
            break
        except Exception as e:
            log(f"Host {host} gagal: {e}")
    if data is None:
        raise RuntimeError("Semua host Yahoo gagal")

    d = data["chart"]["result"][0]
    ts = d.get("timestamp") or []
    q = d["indicators"]["quote"][0]
    vols = (d["indicators"].get("quote") or [{}])[0].get("volume") or [0]*len(ts)
    step_min = _INTERVAL_MIN.get(interval, 30)
    now = datetime.now(timezone.utc)

    candles, seen = [], set()
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c): continue
        if h < l or o <= 0 or c <= 0 or h <= 0 or l <= 0: continue
        if h < max(o, c) or l > min(o, c): continue          # OHLC tidak konsisten
        dt = datetime.fromtimestamp(t, timezone.utc)
        if dt in seen: continue
        seen.add(dt)
        if drop_incomplete and (dt + timedelta(minutes=step_min)) > now:
            continue                                          # candle belum close
        v = vols[i] if i < len(vols) and vols[i] is not None else 0
        candles.append({"time": dt, "open": float(o), "high": float(h),
                        "low": float(l), "close": float(c), "vol": float(v)})
    if len(candles) < 55:
        raise ValueError(f"Data {interval} terlalu sedikit: {len(candles)}")
    # Deteksi gap besar
    for i in range(1, len(candles)):
        gap_min = (candles[i]["time"] - candles[i-1]["time"]).total_seconds()/60
        if gap_min > step_min * 4:                            # gap >4 bar
            log(f"WARNING gap {gap_min:.0f} menit pada {candles[i]['time']}")
    return candles

def fetch_spot(state):
    """Multi-source spot; update offset dinamis via EMA."""
    prices = []
    try:
        p = float(requests.get("https://api.gold-api.com/price/XAU",
                               headers=HEADERS, timeout=6).json()["price"])
        prices.append(p)
    except Exception: pass
    try:
        p = float(requests.get("https://api.coinbase.com/v2/prices/PAXG-USD/spot",
                               headers=HEADERS, timeout=6).json()["data"]["amount"])
        prices.append(p)
    except Exception: pass
    if not prices: return None, state.get("spot_offset", SPOT_OFFSET_DEFAULT)
    raw = sorted(prices)[len(prices)//2]
    return raw, state.get("spot_offset", SPOT_OFFSET_DEFAULT)

# ========================== INDIKATOR ==========================
def ema(vals, n):
    if not vals: return 0.0
    if len(vals) < n:
        return sum(vals)/len(vals)
    k = 2/(n+1); e = vals[-n]
    for v in vals[-n+1:]: e = v*k + e*(1-k)
    return e

def sma(vals, n):
    if not vals: return 0.0
    n = min(n, len(vals))
    return sum(vals[-n:])/n

def atr(candles, n=14):
    if len(candles) < 2: return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    if not trs: return 0.0
    return sma(trs, n)

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

def bollinger(closes, n=20, mult=2.0):
    if len(closes) < n:
        m = sma(closes, len(closes)); return m, m, m, 0.0
    m = sma(closes, n)
    var = sum((c-m)**2 for c in closes[-n:]) / n
    sd = math.sqrt(var)
    return m, m + mult*sd, m - mult*sd, sd

def swing_points(candles, lb=5):
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

# ========================== STRATEGI ==========================
def strat_smc_sweep(c):
    highs, lows = swing_points(c)
    if not highs or not lows or len(c) < 3: return 0
    last = c[-1]
    if last["low"] < min(lows) and last["close"] > min(lows): return +1
    if last["high"] > max(highs) and last["close"] < max(highs): return -1
    return 0

def strat_msb_mtf(c30, c60):
    if len(c60) < 55 or len(c30) < 25: return 0
    e60 = ema([x["close"] for x in c60], 50)
    bias = +1 if c60[-1]["close"] > e60 else -1
    e30 = ema([x["close"] for x in c30], 20)
    trig = +1 if c30[-1]["close"] > e30 else -1
    return bias if bias == trig else 0

def strat_mean_reversion(c, c60=None):
    closes = [x["close"] for x in c]
    if len(closes) < 50: return 0
    m = sma(closes, 50)
    sd = math.sqrt(sum((x-m)**2 for x in closes[-50:])/50)
    if sd == 0: return 0
    z = (closes[-1] - m)/sd
    r = rsi(closes)
    trend_up = trend_down = False
    if c60 and len(c60) >= 60:
        e200 = ema([x["close"] for x in c60], min(200, len(c60)))
        trend_up = c60[-1]["close"] > e200
        trend_down = c60[-1]["close"] < e200
    if z <= -2.0 and r < 35 and not trend_down: return +1
    if z >= 2.0 and r > 65 and not trend_up: return -1
    return 0

def strat_squeeze_breakout(c):
    closes = [x["close"] for x in c]
    if len(closes) < 25: return 0
    m, up, lo, sd = bollinger(closes)
    if m <= 0: return 0
    a_now = atr(c, 14)
    a_ref = atr(c[:-7], 14) if len(c) > 21 else a_now
    width = (up-lo)/m
    squeeze = width < 0.008
    expansion = a_now > a_ref * 1.3
    vols = [x.get("vol", 0) for x in c]
    v_avg = sma(vols[-20:], 20) if len(vols) >= 20 else 1
    v_spike = vols[-1] > v_avg * 1.4 if v_avg > 0 else False
    last = c[-1]
    if squeeze and expansion and v_spike and last["close"] > up: return +1
    if squeeze and expansion and v_spike and last["close"] < lo: return -1
    return 0

def regime_ok(c):
    if len(c) < 20: return False
    a = atr(c, 14); px = c[-1]["close"]
    if px <= 0: return False
    rel = a/px
    return 0.0008 < rel < 0.02

def in_killzone():
    h = datetime.now(timezone.utc).hour
    return any(a <= h < b for a, b in KILLZONES)

# ========================== ML FILTER ==========================
_MODEL_CACHE = None
def _load_ml():
    global _MODEL_CACHE
    if _MODEL_CACHE is not None: return _MODEL_CACHE
    try:
        with open(MODEL_FILE, "rb") as f:
            _MODEL_CACHE = pickle.load(f)
        return _MODEL_CACHE
    except Exception:
        return None

def ml_proba(c30):
    model = _load_ml()
    if model is None: return None
    try:
        import ml_engine
        row = ml_engine.make_features(c30)
        if row is None: return None
        return float(model.predict_proba([row])[0][1])
    except Exception as e:
        log(f"ML predict error: {e}")
        return None

# ========================== POSITION SIZING ==========================
def calc_lot(entry, sl, risk_pct=1.0, equity=1000.0):
    risk_usd = equity * risk_pct / 100.0
    sl_dist  = abs(entry - sl)
    if sl_dist <= 0: return 0.01
    lot = risk_usd / (sl_dist * 100.0)     # XAUUSD: 1 lot = $100 per $1 move
    return max(0.01, round(lot, 2))

# ========================== SIGNAL BUILDER ==========================
def build_signal(score, sl, tp, price, votes, regime_note, spot_txt, lot):
    emoji = "🟢 BUY" if score > 0 else "🔴 SELL"
    side  = "BUY" if score > 0 else "SELL"
    return (
        f"<b>{emoji} XAUUSD — {side} (M30/H1)</b>\n"
        f"──────────────────\n"
        f"💰 Entry ref   : <b>{price:,.2f}</b>\n"
        f"🎯 Toleransi   : ±2.00 USD (skip jika di luar)\n"
        f"🛑 Stop Loss   : <b>{sl:,.2f}</b>\n"
        f"🎯 Take Profit : <b>{tp:,.2f}</b>  (RR 1:{RR_TARGET})\n"
        f"📦 Saran Lot   : <b>{lot}</b> (risk 1% / equity $1000)\n"
        f"🧠 Skor        : {abs(score):.2f} | {votes}\n"
        f"📊 Regime      : {regime_note}\n"
        f"🔎 Spot dev    : {spot_txt}\n"
        f"⏰ {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n"
        f"⚠️ Eksekusi MANUAL di MT5."
    )

# ========================== MAIN ==========================
def main():
    log("=== RUN BOT XAUUSD v2.0 ===")
    state = load_state()

    # --- 1. Fetch data ---
    try:
        c30 = fetch_ohlc("30m", "5d")
        c60 = fetch_ohlc("1h", "1mo")
    except Exception as e:
        log(f"Fetch gagal: {e}"); return
    if len(c30) < 60 or len(c60) < 55:
        log(f"Data tidak cukup: M30={len(c30)} H1={len(c60)}"); return

    price = c30[-1]["close"]
    a     = atr(c30, 14)
    if a <= 0:
        log("ATR = 0, skip."); return

    # --- 2. Regime gate ---
    if not regime_ok(c30):
        log("Regime volatilitas ekstrem/sepi. Skip."); return

    # --- 3. Session gate ---
    if not in_killzone():
        log("Di luar killzone. Skip."); return
    hh = datetime.now(timezone.utc).hour
    prime = hh in PRIME_HOURS

    # --- 4. Spot verification + dynamic offset ---
    spot_raw, offset = fetch_spot(state)
    if spot_raw is not None:
        spot_cal = spot_raw + offset
        dev = abs(spot_cal - price)
        if dev > MAX_DEVIATION:
            log(f"Deviasi spot {dev:.2f} > {MAX_DEVIATION}. Skip."); return
        # kalibrasi offset dinamis (EMA α=0.15)
        new_off = 0.85 * offset + 0.15 * (price - spot_raw)
        state["spot_offset"] = round(new_off, 2)
        spot_txt = f"{dev:.2f} USD (offset {new_off:+.2f})"
    else:
        spot_txt = "gagal verifikasi (lanjut hati-hati)"

    # --- 5. Ensemble voting (weighted) ---
    votes = {
        "SMC_Sweep":    strat_smc_sweep(c30),
        "MSB_MultiTF":  strat_msb_mtf(c30, c60),
        "MeanReversion":strat_mean_reversion(c30, c60),
        "SqueezeBreak": strat_squeeze_breakout(c30),
    }
    score = sum(votes[k] * WEIGHTS[k] for k in votes)
    # bonus prime-hour untuk sinyal dengan conviction kuat
    if prime and abs(score) >= MIN_SCORE_WEIGHTED:
        score *= 1.05
    log(f"Votes: {votes} => weighted score {score:.2f}")

    # --- 6. Cooldown ---
    now = datetime.now(timezone.utc)
    last_t = state.get("last_signal_time")
    if last_t:
        try:
            mins = (now - datetime.fromisoformat(last_t)).total_seconds()/60
        except Exception:
            mins = 999
        same_dir = ((score > 0 and state.get("last_direction") == "BUY") or
                    (score < 0 and state.get("last_direction") == "SELL"))
        if same_dir and mins < COOLDOWN_MIN:
            log(f"Cooldown aktif ({mins:.0f}/{COOLDOWN_MIN}m). Skip."); return

    # --- 7. ML gate ---
    proba = ml_proba(c30)
    ml_txt = "n/a"
    if proba is not None:
        ml_txt = f"{proba:.2f}"
        if proba < 0.55:
            log(f"ML menolak (proba {proba:.2f}). Skip."); return

    # --- 8. Emit sinyal ---
    if abs(score) >= MIN_SCORE_WEIGHTED:
        direction = 1 if score > 0 else -1
        sl = price - direction * SL_MULT * a
        tp = price + direction * SL_MULT * a * RR_TARGET
        lot = calc_lot(price, sl)
        msg = build_signal(score, sl, tp, price, votes,
                           f"Volatilitas normal{' | PRIME' if prime else ''} | ML: {ml_txt}",
                           spot_txt, lot)
        if send_telegram(msg):
            state["last_signal_time"] = now.isoformat()
            state["last_direction"]   = "BUY" if direction == 1 else "SELL"
            state.setdefault("open_signals", []).append({
                "time": now.isoformat(), "dir": state["last_direction"],
                "entry": price, "sl": sl, "tp": tp, "status": "PENDING"
            })
            # simpan max 50 sinyal terakhir
            state["open_signals"] = state["open_signals"][-50:]
            save_state(state)
    else:
        log("Skor belum cukup. Tidak ada sinyal.")
    save_state(state)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback; log(f"FATAL: {e}\n{traceback.format_exc()}")
