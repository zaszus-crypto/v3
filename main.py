#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GOLDPULSE PRO v1.0 — XAUUSD Trend + Pullback + Momentum
H1 timeframe, Yahoo GC=F, Telegram alert.
"""
import os, json, time, math, random, requests
from datetime import datetime, timezone, timedelta

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_DIR        = os.environ.get("STATE_DIR", "state")
STATE_FILE       = os.path.join(STATE_DIR, "state.json")
LOG_FILE         = os.path.join(STATE_DIR, "signals.log")
os.makedirs(STATE_DIR, exist_ok=True)

# ============ STRATEGI PARAMETER (jangan tuning!) ============
EMA_FAST     = 50
EMA_SLOW     = 200
EMA_PULLBACK = 20
PULLBACK_LB  = 3         # cek pullback di 3 candle terakhir
BODY_MIN     = 0.50      # body > 50% range
VOL_MULT     = 1.0       # minimal volume normal
SL_ATR_MULT  = 0.3       # buffer SL dari swing
RR_TARGET    = 2.0       # 1:2
ATR_PERIOD   = 14
ATR_SPIKE    = 2.5       # tolak jika ATR > 2.5× rata2
SESSION_START_H = 7      # 07:00 UTC
SESSION_END_H   = 19     # 19:00 UTC
COOLDOWN_MIN    = 180    # 3 jam antar sinyal searah

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"}

# ============ UTIL ============
def log(msg):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(line + "\n")
    except Exception: pass

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
                w = int(r.json().get("parameters", {}).get("retry_after", 5)) + 2
                time.sleep(w); continue
        except Exception as e:
            log(f"Telegram err {attempt+1}: {e}")
        time.sleep(3 * (attempt + 1) + random.uniform(0, 2))
    return False

def load_state():
    default = {"last_signal_time": "", "last_direction": "", "sent_ids": []}
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        for k, v in default.items(): s.setdefault(k, v)
        return s
    except Exception:
        return default

def save_state(s):
    try:
        with open(STATE_FILE, "w") as f: json.dump(s, f, indent=2)
    except Exception: pass

# ============ DATA (H1, single request 730d) ============
def fetch_h1():
    last_err = None
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/GC=F"
               f"?interval=1h&range=1mo")
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=20)
                if r.status_code == 429:
                    time.sleep((2**attempt) + random.uniform(0, 1)); continue
                r.raise_for_status()
                return _parse(r.json())
            except Exception as e:
                last_err = e
                time.sleep((2**attempt) + random.uniform(0, 1))
    raise RuntimeError(f"Yahoo gagal: {last_err}")

def _parse(data):
    d = data["chart"]["result"][0]
    ts = d.get("timestamp") or []
    q = d["indicators"]["quote"][0]
    vols = q.get("volume") or [0]*len(ts)
    now = datetime.now(timezone.utc)
    out, seen = [], set()
    for i, t in enumerate(ts):
        try: o,h,l,c = q["open"][i],q["high"][i],q["low"][i],q["close"][i]
        except (KeyError, IndexError): continue
        if None in (o,h,l,c) or h<l or o<=0 or c<=0: continue
        dt = datetime.fromtimestamp(t, timezone.utc)
        if dt in seen: continue
        seen.add(dt)
        # skip candle yang belum close
        if (dt + timedelta(hours=1)) > now: continue
        v = vols[i] if i < len(vols) and vols[i] is not None else 0
        out.append({"time": dt, "open": float(o), "high": float(h),
                    "low": float(l), "close": float(c), "vol": float(v)})
    if len(out) < 220:
        raise ValueError(f"Data tidak cukup: {len(out)}")
    return out

# ============ INDIKATOR ============
def ema(vals, n):
    if not vals: return 0.0
    if len(vals) < n: return sum(vals)/len(vals)
    k = 2/(n+1); e = vals[-n]
    for v in vals[-n+1:]: e = v*k + e*(1-k)
    return e

def sma(vals, n):
    if not vals: return 0.0
    n = min(n, len(vals)); return sum(vals[-n:])/n

def atr(c, n=14):
    if len(c) < 2: return 0.0
    trs = []
    for i in range(1, len(c)):
        h,l,pc = c[i]["high"],c[i]["low"],c[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sma(trs, n)

# ============ STRATEGI ============
def check_signal(c):
    """
    Return (direction, info) atau (None, reason).
    direction: +1 BUY, -1 SELL
    """
    if len(c) < 220: return None, "data kurang"
    closes = [x["close"] for x in c]
    last = c[-1]
    prev = c[-2]

    e50  = ema(closes, EMA_FAST)
    e200 = ema(closes, EMA_SLOW)
    e20  = ema(closes, EMA_PULLBACK)

    # 1. Trend bias
    if closes[-1] > e200 and e50 > e200: bias = +1
    elif closes[-1] < e200 and e50 < e200: bias = -1
    else: return None, "bias tidak jelas"

    # 2. ATR spike filter
    a = atr(c, ATR_PERIOD)
    atr_series = [atr(c[:i+1], ATR_PERIOD) for i in range(max(0, len(c)-20), len(c))]
    a_avg = sma(atr_series, 20)
    if a > a_avg * ATR_SPIKE:
        return None, f"ATR spike ({a:.1f} > {a_avg*ATR_SPIKE:.1f})"

    # 3. Pullback detection (sentuh EMA20 dalam PULLBACK_LB candle terakhir)
    pullback = False
    for k in range(2, 2 + PULLBACK_LB):
        if k >= len(c): break
        ck = c[-k]
        if bias == 1 and ck["low"] <= e20: pullback = True; break
        if bias == -1 and ck["high"] >= e20: pullback = True; break
    if not pullback:
        return None, "tidak ada pullback"

    # 4. Momentum trigger
    body = abs(last["close"] - last["open"])
    rng = last["high"] - last["low"]
    body_pct = body / rng if rng > 0 else 0

    if bias == 1:
        if not (last["close"] > prev["high"] and last["close"] > e20):
            return None, "trigger BUY tidak terpenuhi"
        if body_pct < BODY_MIN or last["close"] <= last["open"]:
            return None, f"body lemah ({body_pct:.2f})"
    else:
        if not (last["close"] < prev["low"] and last["close"] < e20):
            return None, "trigger SELL tidak terpenuhi"
        if body_pct < BODY_MIN or last["close"] >= last["open"]:
            return None, f"body lemah ({body_pct:.2f})"

    # 5. Volume
    vols = [x["vol"] for x in c[-21:-1]]
    v_avg = sma(vols, 20)
    if v_avg > 0 and last["vol"] < v_avg * VOL_MULT:
        return None, "volume rendah"

    # 6. Session
    hh = last["time"].hour
    if not (SESSION_START_H <= hh < SESSION_END_H):
        return None, f"luar sesi ({hh} UTC)"

    return bias, {
        "e20": e20, "e50": e50, "e200": e200,
        "atr": a, "body_pct": body_pct,
        "vol_ratio": last["vol"]/v_avg if v_avg > 0 else 0,
        "swing_low": min(x["low"] for x in c[-5:]),
        "swing_high": max(x["high"] for x in c[-5:]),
    }

def calc_sl_tp(direction, entry, info, a):
    if direction == 1:
        sl = info["swing_low"] - a * SL_ATR_MULT
        risk = entry - sl
        tp = entry + risk * RR_TARGET
    else:
        sl = info["swing_high"] + a * SL_ATR_MULT
        risk = sl - entry
        tp = entry - risk * RR_TARGET
    return sl, tp, risk

def calc_lot(entry, sl, risk_pct=1.0, equity=1000.0):
    risk_usd = equity * risk_pct / 100
    sl_dist = abs(entry - sl)
    if sl_dist <= 0: return 0.01
    return max(0.01, round(risk_usd / (sl_dist * 100), 2))

def build_msg(direction, entry, sl, tp, info, lot):
    side = "BUY" if direction == 1 else "SELL"
    emoji = "🟢" if direction == 1 else "🔴"
    return (
        f"<b>{emoji} XAUUSD — {side} (GOLDPULSE)</b>\n"
        f"──────────────────\n"
        f"💰 Entry ref  : <b>{entry:,.2f}</b>\n"
        f"🎯 Toleransi  : ±1.50 USD\n"
        f"🛑 Stop Loss  : <b>{sl:,.2f}</b>  (risk {abs(entry-sl):.2f})\n"
        f"🎯 Take Profit: <b>{tp:,.2f}</b>  (RR 1:{RR_TARGET})\n"
        f"📦 Saran Lot  : <b>{lot}</b> (1% / $1000)\n"
        f"📊 Trend      : EMA50 {'>' if direction==1 else '<'} EMA200\n"
        f"📊 Body       : {info['body_pct']*100:.0f}%\n"
        f"📊 Vol Ratio  : {info['vol_ratio']:.2f}×\n"
        f"📊 ATR        : {info['atr']:.2f}\n"
        f"⏰ {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n"
        f"⚠️ Eksekusi MANUAL di MT5."
    )

# ============ MAIN ============
def main():
    log("=== GOLDPULSE PRO v1.0 ===")
    state = load_state()
    now = datetime.now(timezone.utc)

    if now.weekday() >= 5:
        log("Weekend. Skip."); return

    try:
        c = fetch_h1()
    except Exception as e:
        log(f"Fetch gagal: {e}"); return
    log(f"H1 data: {len(c)} candle")

    direction, info = check_signal(c)
    if direction is None:
        log(f"Tidak ada sinyal: {info}"); return

    entry = c[-1]["close"]
    a = info["atr"]
    sl, tp, risk = calc_sl_tp(direction, entry, info, a)

    # Cooldown searah
    last_t = state.get("last_signal_time")
    if last_t:
        try:
            mins = (now - datetime.fromisoformat(last_t)).total_seconds()/60
        except Exception: mins = 999
        dir_now = "BUY" if direction == 1 else "SELL"
        if state.get("last_direction") == dir_now and mins < COOLDOWN_MIN:
            log(f"Cooldown {mins:.0f}/{COOLDOWN_MIN}m. Skip."); return

    lot = calc_lot(entry, sl)
    msg = build_msg(direction, entry, sl, tp, info, lot)
    log(f"SINYAL: {'BUY' if direction==1 else 'SELL'} @ {entry:.2f} | "
        f"SL {sl:.2f} | TP {tp:.2f}")

    if send_telegram(msg):
        state["last_signal_time"] = now.isoformat()
        state["last_direction"] = "BUY" if direction == 1 else "SELL"
        save_state(state)

if __name__ == "__main__":
    try: main()
    except Exception as e:
        import traceback; log(f"FATAL: {e}\n{traceback.format_exc()}")
