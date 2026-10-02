#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAUUSD GOLD SIGNAL BOT — v3.2 STRICT
================================================================
Berdasarkan backtest v3.1:
  • PF 1.34, tapi DD 68.4R (fatal) → sinyal terlalu banyak
  • MSB n=506 (97% dominan) → filter terlalu longgar
  • MR n=13, PF 2.92 → kualitas bagus, frekuensi wajar

Perbaikan v3.2:
  1. MSB diperketat: H1 EMA50 vs EMA200 + M30 EMA stack + slope
  2. MSB hanya prime hours (12-17 UTC overlap London-NY)
  3. ADX_TREND 25 → 30
  4. Equity curve filter: stop entry setelah 4 loss beruntun
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

# Sesi (UTC)
KILLZONES     = [(0, 5), (6, 12), (12, 17)]
PRIME_HOURS   = set(range(12, 17))    # overlap London-NY
MSB_HOURS     = PRIME_HOURS            # MSB hanya di prime hours

# Regime thresholds (v3.2: ADX_TREND naik ke 30)
ADX_TREND = 30
ADX_RANGE = 20

# Risk
SL_MULT    = 1.8
RR_TARGET  = 2.5

# Anti-spam
COOLDOWN_MIN = 90

# Equity curve filter
MAX_CONSEC_LOSS = 4          # stop entry setelah N loss beruntun

# Spot verification
SPOT_OFFSET_DEFAULT = 15.0
MAX_SUDDEN_SHIFT    = 12.0
ABSOLUTE_MAX_SPREAD = 60.0
BOOTSTRAP_SAMPLES   = 3

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
    log("GAGAL kirim Telegram.")
    return False

def load_state():
    default = {"last_signal_time": "", "last_direction": "",
               "sent_ids": [], "spot_offset": SPOT_OFFSET_DEFAULT,
               "spot_samples": 0, "open_signals": [],
               "consec_losses": 0, "last_outcome": ""}
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

def _spans_weekend(t1, t2):
    d = t1.date()
    while d <= t2.date():
        if d.weekday() >= 5: return True
        d += timedelta(days=1)
    return False

# ========================== DATA ==========================
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
        if h < max(o, c) or l > min(o, c): continue
        dt = datetime.fromtimestamp(t, timezone.utc)
        if dt in seen: continue
        seen.add(dt)
        if drop_incomplete and (dt + timedelta(minutes=step_min)) > now:
            continue
        v = vols[i] if i < len(vols) and vols[i] is not None else 0
        candles.append({"time": dt, "open": float(o), "high": float(h),
                        "low": float(l), "close": float(c), "vol": float(v)})
    if len(candles) < 55:
        raise ValueError(f"Data {interval} terlalu sedikit: {len(candles)}")

    for i in range(1, len(candles)):
        gap_min = (candles[i]["time"] - candles[i-1]["time"]).total_seconds()/60
        if gap_min <= step_min * 4: continue
        if _spans_weekend(candles[i-1]["time"], candles[i]["time"]): continue
        log(f"WARNING gap {gap_min:.0f} menit pada {candles[i]['time']}")
    return candles

def fetch_spot():
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
    if not prices: return None
    return sorted(prices)[len(prices)//2]

# ========================== INDIKATOR ==========================
def ema(vals, n):
    if not vals: return 0.0
    if len(vals) < n: return sum(vals)/len(vals)
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

def adx(candles, n=14):
    if len(candles) < 2*n + 2: return 20.0
    trs, pdm, mdm = [], [], []
    for i in range(1, len(candles)):
        up = candles[i]["high"] - candles[i-1]["high"]
        dn = candles[i-1]["low"] - candles[i]["low"]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    def wilder(vals, p):
        if len(vals) < p: return []
        s = sum(vals[:p]); out = [s]
        for v in vals[p:]:
            s = s - s/p + v
            out.append(s)
        return out
    atr_w = wilder(trs, n); pdm_w = wilder(pdm, n); mdm_w = wilder(mdm, n)
    if not atr_w: return 20.0
    pdi = [100 * a/(b+1e-9) for a, b in zip(pdm_w, atr_w)]
    mdi = [100 * a/(b+1e-9) for a, b in zip(mdm_w, atr_w)]
    dx = [100 * abs(a-b)/(a+b+1e-9) for a, b in zip(pdi, mdi)]
    if len(dx) < n: return sum(dx)/len(dx) if dx else 20.0
    s = sum(dx[:n])
    for v in dx[n:]: s = s - s/n + v
    return s/n

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

# ========================== STRATEGI (v3.2) ==========================
def strat_msb_strict(c30, c60):
    """
    MSB v3.2 — stricter:
      H1 bias: EMA50 vs EMA200
      M30 trigger: EMA20 vs EMA50 stack + slope + price position
      Quality >= 3 dari 3 kondisi
    """
    if len(c30) < 55 or len(c60) < 60: return 0
    closes30 = [x["close"] for x in c30]
    closes60 = [x["close"] for x in c60]

    # H1 bias
    e50_60  = ema(closes60, 50)
    e200_60 = ema(closes60, min(200, len(closes60)))
    if e50_60 > e200_60:   bias = +1
    elif e50_60 < e200_60: bias = -1
    else: return 0

    # M30 stack
    e20_30 = ema(closes30, 20)
    e50_30 = ema(closes30, 50)
    e20_30_prev = ema(closes30[:-5], 20)

    # Quality score (0-3)
    q = 0
    if bias == 1:
        if e20_30 > e50_30:                q += 1   # stack aligned
        if closes30[-1] > e20_30:          q += 1   # price above EMA20
        if e20_30 > e20_30_prev:           q += 1   # EMA20 rising
    else:
        if e20_30 < e50_30:                q += 1
        if closes30[-1] < e20_30:          q += 1
        if e20_30 < e20_30_prev:           q += 1

    return bias if q >= 3 else 0                    # SEMUA kondisi harus terpenuhi

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

def regime_ok(c):
    if len(c) < 20: return False
    a = atr(c, 14); px = c[-1]["close"]
    if px <= 0: return False
    return 0.0008 < a/px < 0.02

def in_killzone(hour=None):
    h = hour if hour is not None else datetime.now(timezone.utc).hour
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
    return max(0.01, round(risk_usd / (sl_dist * 100.0), 2))

# ========================== SIGNAL BUILDER ==========================
def build_signal(score, sl, tp, price, strategy_name, regime, ml_txt,
                 spot_txt, lot):
    emoji = "🟢 BUY" if score > 0 else "🔴 SELL"
    side  = "BUY" if score > 0 else "SELL"
    return (
        f"<b>{emoji} XAUUSD — {side} ({strategy_name})</b>\n"
        f"──────────────────\n"
        f"💰 Entry ref   : <b>{price:,.2f}</b>\n"
        f"🎯 Toleransi   : ±2.00 USD (skip jika di luar)\n"
        f"🛑 Stop Loss   : <b>{sl:,.2f}</b>\n"
        f"🎯 Take Profit : <b>{tp:,.2f}</b>  (RR 1:{RR_TARGET})\n"
        f"📦 Saran Lot   : <b>{lot}</b> (risk 1% / equity $1000)\n"
        f"📊 Regime      : {regime}\n"
        f"🧠 ML proba    : {ml_txt}\n"
        f"🔎 Spot        : {spot_txt}\n"
        f"⏰ {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n"
        f"⚠️ Eksekusi MANUAL di MT5."
    )

# ========================== MAIN ==========================
def main():
    log("=== RUN BOT XAUUSD v3.2 (STRICT) ===")
    state = load_state()

    # 1. Fetch
    try:
        c30 = fetch_ohlc("30m", "5d")
        c60 = fetch_ohlc("1h", "1mo")
    except Exception as e:
        log(f"Fetch gagal: {e}"); return
    if len(c30) < 60 or len(c60) < 55:
        log(f"Data tidak cukup"); return

    price = c30[-1]["close"]
    a     = atr(c30, 14)
    if a <= 0: log("ATR=0"); return

    # 2. Regime volatilitas
    if not regime_ok(c30):
        log("Volatilitas ekstrem/sepi. Skip."); return

    # 3. Killzone
    hh = datetime.now(timezone.utc).hour
    if not in_killzone(hh):
        log(f"Jam {hh} UTC di luar killzone. Skip."); return
    prime = hh in PRIME_HOURS

    # 4. ADX regime
    adx_val = adx(c30, 14)
    if adx_val >= ADX_TREND:
        regime = f"TRENDING (ADX {adx_val:.1f})"
        # MSB HANYA di prime hours
        if hh not in MSB_HOURS:
            log(f"ADX trending tapi jam {hh} bukan prime. Skip MSB."); return
        strategy = "MSB_Strict"
        score = strat_msb_strict(c30, c60) * 1.5
    elif adx_val <= ADX_RANGE:
        regime = f"RANGING (ADX {adx_val:.1f})"
        strategy = "MeanReversion"
        score = strat_mean_reversion(c30, c60) * 2.0
    else:
        log(f"ADX {adx_val:.1f} zona transisi. Skip."); return
    log(f"{regime} | {strategy} | score={score:.2f}")

    # 5. Spot
    spot_raw = fetch_spot()
    if spot_raw is not None:
        raw_spread = price - spot_raw
        prev_offset = float(state.get("spot_offset", raw_spread))
        samples = int(state.get("spot_samples", 0))
        if abs(raw_spread) > ABSOLUTE_MAX_SPREAD:
            log(f"⚠️ Spread {raw_spread:.2f} > max. Skip."); save_state(state); return
        dev = abs(raw_spread - prev_offset)
        gate = samples >= BOOTSTRAP_SAMPLES
        if gate and dev > MAX_SUDDEN_SHIFT:
            log(f"⚠️ Spot jump {dev:.2f}. Skip."); save_state(state); return
        new_off = 0.85 * prev_offset + 0.15 * raw_spread
        state["spot_offset"] = round(new_off, 2)
        state["spot_samples"] = samples + 1
        spot_txt = f"spread {raw_spread:+.2f} | EMA {new_off:+.2f}"
    else:
        spot_txt = "gagal verifikasi"

    # 6. Equity curve filter
    consec = int(state.get("consec_losses", 0))
    if consec >= MAX_CONSEC_LOSS:
        log(f"Equity filter: {consec} loss beruntun. Skip sampai reset.")
        save_state(state); return

    # 7. Cooldown
    now = datetime.now(timezone.utc)
    last_t = state.get("last_signal_time")
    if last_t and abs(score) > 0:
        try:
            mins = (now - datetime.fromisoformat(last_t)).total_seconds()/60
        except Exception:
            mins = 999
        dir_now = "BUY" if score > 0 else "SELL"
        if state.get("last_direction") == dir_now and mins < COOLDOWN_MIN:
            log(f"Cooldown ({mins:.0f}/{COOLDOWN_MIN}m). Skip.")
            save_state(state); return

    # 8. ML
    proba = ml_proba(c30)
    ml_txt = "n/a"
    if proba is not None:
        ml_txt = f"{proba:.2f}"
        if proba < 0.55:
            log(f"ML tolak ({proba:.2f}). Skip."); save_state(state); return

    # 9. Emit
    if abs(score) > 0:
        direction = 1 if score > 0 else -1
        sl = price - direction * SL_MULT * a
        tp = price + direction * SL_MULT * a * RR_TARGET
        lot = calc_lot(price, sl)
        prime_note = " | PRIME" if prime else ""
        msg = build_signal(score, sl, tp, price, strategy,
                           regime + prime_note, ml_txt, spot_txt, lot)
        if send_telegram(msg):
            state["last_signal_time"] = now.isoformat()
            state["last_direction"]   = "BUY" if direction == 1 else "SELL"
            state.setdefault("open_signals", []).append({
                "time": now.isoformat(), "dir": state["last_direction"],
                "strategy": strategy, "entry": price, "sl": sl, "tp": tp,
                "status": "PENDING"
            })
            state["open_signals"] = state["open_signals"][-50:]
            save_state(state)
    else:
        log(f"{strategy} tidak vote. Tidak ada sinyal.")
    save_state(state)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback; log(f"FATAL: {e}\n{traceback.format_exc()}")
