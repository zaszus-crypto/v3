#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ORB GOLD BOT v1.0 — NY Open Range Breakout
================================================================
Strategi akademis (Zarattini & Aziz 2023) untuk XAUUSD.
Fokus: NY session open 13:30 UTC, breakout dari 30-min range.
"""
import os, json, time, math, random, requests
from datetime import datetime, timezone, timedelta

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_DIR        = os.environ.get("STATE_DIR", "state")
STATE_FILE       = os.path.join(STATE_DIR, "state.json")
LOG_FILE         = os.path.join(STATE_DIR, "signals.log")

os.makedirs(STATE_DIR, exist_ok=True)

# ---------- KONFIGURASI ----------
NY_OPEN_H, NY_OPEN_M = 13, 30          # NY open UTC (08:30 ET)
OR_MINUTES           = 30              # Opening Range 30 menit
TRADE_START_H, TRADE_START_M = 14, 0   # Mulai cari breakout
TRADE_END_H          = 19              # Stop entry baru setelah 19:00 UTC
SESSION_END_H        = 20              # Target close session

# Filter OR width
OR_MIN_PCT = 0.10                      # OR minimal 0.10% dari harga
OR_MAX_PCT = 1.20                      # OR maksimal 1.20%
BUFFER_MULT = 0.05                     # Breakout buffer = 5% OR width
VOL_MULT = 1.25                        # Volume konfirmasi = 1.25× rata2

# Risk
RR_TARGET = 1.5                        # 1 : 1.5

# Anti-spam
COOLDOWN_MIN = 240                     # 4 jam min antar sinyal searah
MAX_SIGNAL_PER_DAY = 2                 # Maks 2 sinyal per hari

# ---------- UTILITAS ----------
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"}

def log(msg):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
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
                log(f"Rate limit, tunggu {w}s"); time.sleep(w); continue
            log(f"Telegram HTTP {r.status_code}: {r.text[:150]}")
        except Exception as e:
            log(f"Telegram error {attempt+1}: {e}")
        time.sleep(3 * (attempt + 1) + random.uniform(0, 2))
    return False

def load_state():
    default = {"last_signal_time": "", "last_direction": "",
               "today_date": "", "today_signals": 0, "sent_ids": []}
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

# ---------- DATA ----------
def fetch_ohlc(interval="15m", rng="5d"):
    """Yahoo Finance GC=F, fallback query1→query2."""
    last_err = None
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/GC=F"
               f"?interval={interval}&range={rng}")
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=20)
                if r.status_code == 429:
                    time.sleep((2**attempt) + random.uniform(0,1)); continue
                r.raise_for_status()
                return _parse(r.json(), interval)
            except Exception as e:
                last_err = e
                time.sleep((2**attempt) + random.uniform(0,1))
    raise RuntimeError(f"Yahoo gagal: {last_err}")

def _parse(data, interval):
    d = data["chart"]["result"][0]
    ts = d.get("timestamp") or []
    q = d["indicators"]["quote"][0]
    vols = q.get("volume") or [0]*len(ts)
    step = {"5m":5,"15m":15,"30m":30,"1h":60}.get(interval, 15)
    now = datetime.now(timezone.utc)
    out, seen = [], set()
    for i, t in enumerate(ts):
        try: o,h,l,c = q["open"][i],q["high"][i],q["low"][i],q["close"][i]
        except (KeyError, IndexError): continue
        if None in (o,h,l,c) or h<l or o<=0 or c<=0: continue
        dt = datetime.fromtimestamp(t, timezone.utc)
        if dt in seen: continue
        seen.add(dt)
        if (dt + timedelta(minutes=step)) > now: continue  # skip in-progress
        v = vols[i] if i < len(vols) and vols[i] is not None else 0
        out.append({"time": dt, "open": float(o), "high": float(h),
                    "low": float(l), "close": float(c), "vol": float(v)})
    if len(out) < 30:
        raise ValueError(f"Data tidak cukup: {len(out)}")
    return out

# ---------- INDIKATOR ----------
def sma(vals, n):
    if not vals: return 0.0
    n = min(n, len(vals)); return sum(vals[-n:])/n

def atr(candles, n=14):
    if len(candles) < 2: return 0.0
    trs = []
    for i in range(1, len(candles)):
        h,l,pc = candles[i]["high"],candles[i]["low"],candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sma(trs, n)

# ---------- ORB CORE ----------
def get_session_date(dt):
    """Tanggal session trading (UTC)."""
    return dt.date()

def find_opening_range(candles, session_date):
    """Ambil OR dari 13:30–14:00 UTC pada session_date."""
    or_start = datetime.combine(session_date, datetime.min.time(),
                                tzinfo=timezone.utc) + timedelta(hours=NY_OPEN_H, minutes=NY_OPEN_M)
    or_end   = or_start + timedelta(minutes=OR_MINUTES)

    or_candles = [c for c in candles if or_start <= c["time"] < or_end]
    if len(or_candles) < 2:
        return None

    or_high = max(c["high"] for c in or_candles)
    or_low  = min(c["low"]  for c in or_candles)
    return {"high": or_high, "low": or_low,
            "width": or_high - or_low, "start": or_start, "end": or_end}

def in_trade_window(now):
    t_start = now.replace(hour=TRADE_START_H, minute=TRADE_START_M,
                          second=0, microsecond=0)
    t_end   = now.replace(hour=TRADE_END_H, minute=0, second=0, microsecond=0)
    return t_start <= now < t_end

def orb_breakout(candles, or_data, now):
    """Cek apakah ada breakout fresh di candle terakhir."""
    if not or_data: return None, None
    if len(candles) < 20: return None, None

    or_high, or_low = or_data["high"], or_data["low"]
    width = or_data["width"]
    buffer = width * BUFFER_MULT

    last = candles[-1]
    # Cek candle harus setelah OR selesai
    if last["time"] < or_data["end"]: return None, None

    # Volume confirmation
    vols = [c["vol"] for c in candles[-20:-1]]
    avg_vol = sma(vols, 20) if vols else 0
    vol_ok = (last["vol"] >= avg_vol * VOL_MULT) if avg_vol > 0 else True

    if last["close"] > or_high + buffer and vol_ok:
        return +1, {"or_high": or_high, "or_low": or_low, "width": width,
                    "buffer": buffer, "avg_vol": avg_vol, "vol": last["vol"]}
    if last["close"] < or_low - buffer and vol_ok:
        return -1, {"or_high": or_high, "or_low": or_low, "width": width,
                    "buffer": buffer, "avg_vol": avg_vol, "vol": last["vol"]}
    return None, None

# ---------- SIGNAL ----------
def calc_lot(entry, sl, risk_pct=1.0, equity=1000.0):
    risk_usd = equity * risk_pct / 100
    sl_dist = abs(entry - sl)
    if sl_dist <= 0: return 0.01
    return max(0.01, round(risk_usd / (sl_dist * 100), 2))

def build_signal(direction, entry, sl, tp, or_data, lot, session_date):
    side = "BUY" if direction == 1 else "SELL"
    emoji = "🟢" if direction == 1 else "🔴"
    rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
    return (
        f"<b>{emoji} XAUUSD — {side} (ORB NY Session)</b>\n"
        f"──────────────────\n"
        f"📅 Session  : {session_date} NY\n"
        f"📊 OR High  : <b>{or_data['or_high']:,.2f}</b>\n"
        f"📊 OR Low   : <b>{or_data['or_low']:,.2f}</b>\n"
        f"📏 OR Width : {or_data['width']:.2f} USD\n"
        f"──────────────────\n"
        f"💰 Entry    : <b>{entry:,.2f}</b>\n"
        f"🎯 Toleransi: ±1.50 USD\n"
        f"🛑 Stop Loss: <b>{sl:,.2f}</b>\n"
        f"🎯 Take Prof: <b>{tp:,.2f}</b>  (RR 1:{rr:.1f})\n"
        f"📦 Saran Lot: <b>{lot}</b> (risk 1% / equity $1000)\n"
        f"📊 Vol Ratio: {or_data['vol']/max(or_data['avg_vol'],1):.2f}×\n"
        f"⏰ {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n"
        f"⚠️ Eksekusi MANUAL di MT5."
    )

# ---------- MAIN ----------
def main():
    log("=== ORB GOLD BOT v1.0 ===")
    state = load_state()
    now = datetime.now(timezone.utc)

    # Reset counter harian
    today = str(get_session_date(now))
    if state.get("today_date") != today:
        state["today_date"] = today
        state["today_signals"] = 0

    # Skip weekend (Sabtu=5, Minggu=6)
    if now.weekday() >= 5:
        log("Weekend. Skip."); save_state(state); return

    # Skip Jumat (banyak false breakout)
    if now.weekday() == 4:
        log("Jumat — skip (avoid false breakout)."); save_state(state); return

    # Cek window
    if not in_trade_window(now):
        log(f"Jam {now.hour}:{now.minute:02d} UTC di luar window "
            f"{TRADE_START_H}:{TRADE_START_M:02d}–{TRADE_END_H}:00. Skip.")
        save_state(state); return

    # Batas sinyal harian
    if state.get("today_signals", 0) >= MAX_SIGNAL_PER_DAY:
        log(f"Batas sinyal harian tercapai. Skip.")
        save_state(state); return

    # Fetch data
    try:
        c15 = fetch_ohlc("15m", "5d")
    except Exception as e:
        log(f"Fetch gagal: {e}"); return
    log(f"Data 15m: {len(c15)} candle")

    # Identifikasi OR session hari ini
    session_date = get_session_date(now)
    or_data = find_opening_range(c15, session_date)
    if not or_data:
        log(f"OR belum lengkap untuk {session_date}. Tunggu.")
        save_state(state); return

    # Filter OR width
    or_pct = or_data["width"] / or_data["high"] * 100
    if or_pct < OR_MIN_PCT:
        log(f"OR terlalu sempit ({or_pct:.2f}% < {OR_MIN_PCT}%). Skip.")
        save_state(state); return
    if or_pct > OR_MAX_PCT:
        log(f"OR terlalu lebar ({or_pct:.2f}% > {OR_MAX_PCT}%). Skip.")
        save_state(state); return

    log(f"OR: H={or_data['high']:.2f} L={or_data['low']:.2f} "
        f"W={or_data['width']:.2f} ({or_pct:.2f}%)")

    # Cek breakout
    direction, bd = orb_breakout(c15, or_data, now)
    if direction is None:
        log("Belum ada breakout valid. Skip.")
        save_state(state); return

    # Cooldown searah
    last_t = state.get("last_signal_time")
    if last_t:
        try:
            mins = (now - datetime.fromisoformat(last_t)).total_seconds()/60
        except Exception: mins = 999
        dir_now = "BUY" if direction == 1 else "SELL"
        if state.get("last_direction") == dir_now and mins < COOLDOWN_MIN:
            log(f"Cooldown {mins:.0f}/{COOLDOWN_MIN}m. Skip.")
            save_state(state); return

    # Bangun sinyal
    entry = c15[-1]["close"]
    if direction == 1:
        sl = or_data["low"] - or_data["width"] * 0.05
        tp = entry + or_data["width"] * RR_TARGET
    else:
        sl = or_data["high"] + or_data["width"] * 0.05
        tp = entry - or_data["width"] * RR_TARGET

    lot = calc_lot(entry, sl)
    msg = build_signal(direction, entry, sl, tp, bd, lot, session_date)

    if send_telegram(msg):
        state["last_signal_time"] = now.isoformat()
        state["last_direction"] = "BUY" if direction == 1 else "SELL"
        state["today_signals"] = state.get("today_signals", 0) + 1
        save_state(state)
    else:
        save_state(state)

if __name__ == "__main__":
    try: main()
    except Exception as e:
        import traceback; log(f"FATAL: {e}\n{traceback.format_exc()}")
