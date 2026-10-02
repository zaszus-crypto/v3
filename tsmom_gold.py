#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TSMOM GOLD v15.1 (Production-Ready)
Multi-Layer Confirmation + Graceful Degradation + Data Freshness Check.
"""
import os
import sys
import math
import time
import random
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

# ==============================================================================
# 1. KONFIGURASI
# ==============================================================================
class Config:
    VERSION         = "15.1"
    SYMBOL          = "GC=F"
    LOOKBACK        = 252
    REBALANCE       = 5
    ATR_P           = 20
    INIT_RISK_MULT  = 2.0
    TRAIL_MULT      = 4.0
    TARGET_VOL      = 0.12
    VOL_WINDOW      = 20
    SIZE_MIN        = 0.3
    SIZE_MAX        = 2.5
    MAX_HOLD        = 400
    
    # --- ADX FILTER ---
    ADX_PERIOD      = 14
    ADX_THRESHOLD   = 22
    
    # --- MULTI-TIMEFRAME ---
    MTF_ENABLED     = True
    MTF_TIMEFRAMES  = ["1d", "1wk"]
    MTF_LOOKBACKS   = [50, 20]
    
    # --- CORRELATION CHECK ---
    CORRELATION_ENABLED = True
    CORREL_SYMBOLS  = ["DX-Y.NYB", "^GSPC"]
    CORREL_LOOKBACK = 20
    
    # --- VOLATILITY REGIME ---
    VOL_REGIME_ENABLED = True
    VOL_LOW_THRESHOLD = 0.15
    VOL_HIGH_THRESHOLD = 0.25
    
    # --- CONFIRMATION FILTERS ---
    VOLUME_SPIKE_MULT = 1.5
    SMA_PROXIMITY_PCT = 2.0
    
    # --- PRODUKTION SAFETY ---
    FETCH_DELAY     = 1.5       # Delay antar fetch (hindari rate limit)
    FETCH_TIMEOUT   = 30        # Timeout per request
    MAX_TELEGRAM_LEN = 4000     # Telegram limit 4096, kita pakai 4000 untuk aman
    SKIP_WEEKEND    = True      # Skip jalankan di weekend
    DATA_YEARS      = 10
    MIN_BARS        = 400
    
    TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ==============================================================================
# 2. LOGGING & TELEGRAM
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("TSMOM")

def send_telegram(text: str):
    token = Config.TELEGRAM_TOKEN
    chat_id = Config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.info("Telegram tidak dikonfigurasi.")
        return
    
    # Truncate jika terlalu panjang
    if len(text) > Config.MAX_TELEGRAM_LEN:
        text = text[:Config.MAX_TELEGRAM_LEN - 100] + "\n\n... (truncated)"
    
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML"
        }, timeout=10)
        if r.status_code == 200:
            log.info("✅ Telegram terkirim.")
        else:
            log.error(f"Telegram gagal: {r.text}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ==============================================================================
# 3. DATA FETCHER (with graceful degradation)
# ==============================================================================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def fetch_data(symbol: str, interval: str = "1d", years: int = 10, 
               required: bool = True) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch data dengan graceful degradation.
    required=True → raise error jika gagal (untuk data utama)
    required=False → return None jika gagal (untuk data pendukung)
    """
    # Delay untuk hindari rate limit
    time.sleep(Config.FETCH_DELAY)
    
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={years}y"
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=Config.FETCH_TIMEOUT)
                if r.status_code == 429:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                r.raise_for_status()
                return _parse(r.json())
            except Exception as e:
                log.warning(f"Fetch {symbol} gagal (attempt {attempt+1}): {e}")
                time.sleep((2 ** attempt) + random.uniform(0, 1))
    
    if required:
        raise RuntimeError(f"Gagal ambil data WAJIB: {symbol}")
    else:
        log.warning(f"Data opsional {symbol} tidak tersedia, skip layer ini.")
        return None

def _parse(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = data["chart"]["result"][0]
    ts = result.get("timestamp") or []
    q = result["indicators"]["quote"][0]
    v = result["indicators"]["quote"][0].get("volume", [])
    now = datetime.now(timezone.utc)
    out = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            vol = v[i] if i < len(v) else 0
        except (KeyError, IndexError, TypeError):
            continue
        if None in (o, h, l, c) or h < l or o <= 0 or c <= 0:
            continue
        dt = datetime.fromtimestamp(t, timezone.utc)
        if (dt + timedelta(days=1)) > now:
            continue
        out.append({"time": dt, "open": float(o), "high": float(h),
                    "low": float(l), "close": float(c), 
                    "volume": float(vol) if vol else 0})
    return out

# ==============================================================================
# 4. INDIKATOR
# ==============================================================================
def realized_vol(closes: List[float], w: int = 20) -> float:
    if len(closes) < w + 1:
        return 0.20
    rets = [math.log(closes[i] / closes[i-1]) for i in range(len(closes)-w, len(closes))]
    m = sum(rets) / w
    var = sum((x - m) ** 2 for x in rets) / w
    return math.sqrt(var) * math.sqrt(252)

def atr(data: List[Dict[str, Any]], n: int = 20) -> float:
    if len(data) < 2:
        return 0.0
    trs = []
    for i in range(1, len(data)):
        h, l, pc = data[i]["high"], data[i]["low"], data[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / min(n, len(trs))

def sma(closes: List[float], n: int = 200) -> float:
    if len(closes) < n:
        return 0.0
    return sum(closes[-n:]) / n

def calculate_adx(data: List[Dict[str, Any]], period: int = 14) -> float:
    if len(data) < period * 2:
        return 0.0
    
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(data)):
        h_diff = data[i]["high"] - data[i-1]["high"]
        l_diff = data[i-1]["low"] - data[i]["low"]
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
        h, l, pc = data[i]["high"], data[i]["low"], data[i-1]["close"]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
    
    def wilder_smooth(values, period):
        if len(values) < period:
            return []
        smoothed = [sum(values[:period])]
        for i in range(period, len(values)):
            smoothed.append(smoothed[-1] - smoothed[-1]/period + values[i])
        return smoothed
    
    plus_dm_smooth = wilder_smooth(plus_dm, period)
    minus_dm_smooth = wilder_smooth(minus_dm, period)
    tr_smooth = wilder_smooth(tr_list, period)
    
    dx_list = []
    for i in range(len(plus_dm_smooth)):
        if tr_smooth[i] == 0:
            dx_list.append(0)
            continue
        plus_di = 100 * plus_dm_smooth[i] / tr_smooth[i]
        minus_di = 100 * minus_dm_smooth[i] / tr_smooth[i]
        di_sum = plus_di + minus_di
        dx_list.append(100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0)
    
    if len(dx_list) < period:
        return 0.0
    return sum(dx_list[-period:]) / period

def avg_volume(data: List[Dict[str, Any]], n: int = 20) -> float:
    if len(data) < n:
        return 0.0
    vols = [d["volume"] for d in data[-n:] if d["volume"] > 0]
    return sum(vols) / len(vols) if vols else 0.0

def calculate_correlation(series1: List[float], series2: List[float], n: int = 20) -> float:
    if len(series1) < n or len(series2) < n:
        return 0.0
    s1, s2 = series1[-n:], series2[-n:]
    ret1 = [(s1[i] - s1[i-1]) / s1[i-1] for i in range(1, len(s1))]
    ret2 = [(s2[i] - s2[i-1]) / s2[i-1] for i in range(1, len(s2))]
    if len(ret1) != len(ret2) or len(ret1) == 0:
        return 0.0
    n = len(ret1)
    sum1, sum2 = sum(ret1), sum(ret2)
    sum1_sq, sum2_sq = sum(x**2 for x in ret1), sum(x**2 for x in ret2)
    p_sum = sum(ret1[i] * ret2[i] for i in range(n))
    num = p_sum - (sum1 * sum2 / n)
    den = math.sqrt((sum1_sq - sum1**2 / n) * (sum2_sq - sum2**2 / n))
    return num / den if den > 0 else 0.0

# ==============================================================================
# 5. DATA FRESHNESS CHECK
# ==============================================================================
def check_data_freshness(data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Cek apakah data masih fresh (tidak basi)."""
    if not data:
        return {"fresh": False, "reason": "Data kosong"}
    
    last_bar = data[-1]["time"]
    now = datetime.now(timezone.utc)
    age_hours = (now - last_bar).total_seconds() / 3600
    
    # Data daily harus < 48 jam (untuk antisipasi weekend)
    if age_hours > 48:
        return {
            "fresh": False, 
            "reason": f"Data basi ({age_hours:.1f} jam lalu). Terakhir: {last_bar.strftime('%d %b %Y')}"
        }
    
    return {"fresh": True, "last_update": last_bar, "age_hours": age_hours}

def is_weekend() -> bool:
    """Cek apakah sekarang weekend (Sabtu=5, Minggu=6)."""
    now = datetime.now(timezone.utc) + timedelta(hours=7)  # WIB
    return now.weekday() >= 5

# ==============================================================================
# 6. MULTI-LAYER ANALYSIS
# ==============================================================================
def analyze_multi_layer() -> Dict[str, Any]:
    """Analisis multi-layer dengan graceful degradation."""
    
    # 1. Cek weekend
    if Config.SKIP_WEEKEND and is_weekend():
        return {"signal": "SKIP", "reason": "Weekend — pasar tutup"}
    
    # 2. Fetch data Gold (WAJIB)
    log.info("Fetching Gold daily data...")
    gold_data = fetch_data(Config.SYMBOL, "1d", Config.DATA_YEARS, required=True)
    
    # 3. Cek freshness data
    freshness = check_data_freshness(gold_data)
    if not freshness["fresh"]:
        return {"signal": "SKIP", "reason": freshness["reason"]}
    
    if len(gold_data) < Config.LOOKBACK + 50:
        return {"signal": "NO_DATA", "reason": "Data Gold tidak cukup"}
    
    closes = [d["close"] for d in gold_data]
    current_price = closes[-1]
    current_adx = calculate_adx(gold_data, Config.ADX_PERIOD)
    current_sma = sma(closes, 200)
    current_vol_data = gold_data[-1]["volume"]
    avg_vol = avg_volume(gold_data, 20)
    
    # 4. ADX Filter
    if current_adx < Config.ADX_THRESHOLD:
        return {
            "signal": "NO_TRADE",
            "confidence": 0,
            "reason": f"ADX {current_adx:.1f} < {Config.ADX_THRESHOLD} (sideways)",
            "adx": current_adx,
            "price": current_price,
            "sma200": current_sma,
            "last_update": freshness["last_update"]
        }
    
    # 5. Base momentum
    momentum_dir = 1 if current_price > closes[-Config.LOOKBACK] else -1
    
    # 6. Confidence scoring
    confidence_score = 0
    confirmations = []
    warnings = []
    
    # LAYER 1: ADX Strength
    if current_adx >= 30:
        confidence_score += 2
        confirmations.append(f"ADX kuat ({current_adx:.1f})")
    elif current_adx >= 25:
        confidence_score += 1
        confirmations.append(f"ADX moderat ({current_adx:.1f})")
    
    # LAYER 2: Volume
    if avg_vol > 0 and current_vol_data > avg_vol * Config.VOLUME_SPIKE_MULT:
        confidence_score += 2
        confirmations.append(f"Volume spike ({current_vol_data/avg_vol:.1f}x)")
    
    # LAYER 3: SMA Proximity
    sma_distance_pct = abs(current_price - current_sma) / current_sma * 100
    if sma_distance_pct <= Config.SMA_PROXIMITY_PCT:
        confidence_score += 1
        confirmations.append(f"Dekat SMA 200 ({sma_distance_pct:.1f}%)")
    
    # LAYER 4: Multi-Timeframe (with graceful degradation)
    if Config.MTF_ENABLED:
        mtf_aligned = True
        mtf_signals = []
        for tf, lb in zip(Config.MTF_TIMEFRAMES, Config.MTF_LOOKBACKS):
            try:
                tf_data = gold_data if tf == "1d" else fetch_data(Config.SYMBOL, tf, 2, required=False)
                if tf_data and len(tf_data) > lb:
                    tf_closes = [d["close"] for d in tf_data]
                    tf_momentum = 1 if tf_closes[-1] > tf_closes[-lb] else -1
                    mtf_signals.append(f"{tf.upper()}: {'↑' if tf_momentum == 1 else '↓'}")
                    if tf_momentum != momentum_dir:
                        mtf_aligned = False
            except Exception as e:
                log.warning(f"MTF {tf} skip: {e}")
        
        if mtf_aligned and len(mtf_signals) > 0:
            confidence_score += 2
            confirmations.append(f"MTF aligned: {', '.join(mtf_signals)}")
        elif len(mtf_signals) > 0:
            warnings.append(f"MTF mixed: {', '.join(mtf_signals)}")
    
    # LAYER 5: Correlation (with graceful degradation)
    if Config.CORRELATION_ENABLED:
        try:
            log.info("Fetching correlation data...")
            dxy_data = fetch_data(Config.CORREL_SYMBOLS[0], "1d", 1, required=False)
            time.sleep(Config.FETCH_DELAY)
            spx_data = fetch_data(Config.CORREL_SYMBOLS[1], "1d", 1, required=False)
            
            if dxy_data:
                dxy_closes = [d["close"] for d in dxy_data]
                corr_dxy = calculate_correlation(closes, dxy_closes, Config.CORREL_LOOKBACK)
                if momentum_dir == 1 and corr_dxy < -0.3:
                    confidence_score += 1
                    confirmations.append(f"DXY negatif korelasi ({corr_dxy:.2f})")
                elif momentum_dir == -1 and corr_dxy > 0.3:
                    confidence_score += 1
                    confirmations.append(f"DXY positif korelasi ({corr_dxy:.2f})")
            
            if spx_data:
                spx_closes = [d["close"] for d in spx_data]
                corr_spx = calculate_correlation(closes, spx_closes, Config.CORREL_LOOKBACK)
                if abs(corr_spx) > 0.5:
                    confirmations.append(f"S&P500 korelasi ({corr_spx:.2f})")
        except Exception as e:
            log.warning(f"Correlation check skip: {e}")
    
    # LAYER 6: Volatility Regime
    if Config.VOL_REGIME_ENABLED:
        current_realized_vol = realized_vol(closes, Config.VOL_WINDOW)
        if current_realized_vol < Config.VOL_LOW_THRESHOLD:
            confidence_score += 1
            confirmations.append(f"Low vol regime ({current_realized_vol:.1%})")
        elif current_realized_vol > Config.VOL_HIGH_THRESHOLD:
            warnings.append(f"High vol regime ({current_realized_vol:.1%})")
    
    # Tentukan confidence level
    if confidence_score >= 6:
        confidence = "STRONG"
    elif confidence_score >= 4:
        confidence = "MEDIUM"
    else:
        confidence = "WEAK"
    
    return {
        "signal": "LONG" if momentum_dir == 1 else "SHORT",
        "confidence": confidence,
        "confidence_score": confidence_score,
        "confirmations": confirmations,
        "warnings": warnings,
        "adx": current_adx,
        "price": current_price,
        "sma200": current_sma,
        "momentum": momentum_dir,
        "last_update": freshness["last_update"]
    }

# ==============================================================================
# 7. MAIN RUN
# ==============================================================================
def run():
    log.info(f"=== 🚀 TSMOM GOLD v{Config.VERSION} (Production-Ready) ===")
    
    signal_data = analyze_multi_layer()
    
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%d %b %Y %H:%M UTC")
    
    # Handle SKIP (weekend/data basi)
    if signal_data.get("signal") == "SKIP":
        msg = (
            f"⏸️ <b>TSMOM GOLD v{Config.VERSION} — SKIP</b>\n"
            f"📅 {date_str}\n"
            f"──────────────────────\n"
            f"<b>Alasan:</b> {signal_data['reason']}\n"
            f"──────────────────────\n"
            f"<i>Script akan jalan lagi besok.</i>"
        )
        log.info("\n" + msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
        send_telegram(msg)
        return
    
    # Handle NO_DATA
    if signal_data.get("signal") == "NO_DATA":
        msg = f"⚠️ <b>TSMOM GOLD v{Config.VERSION}</b>\n{signal_data['reason']}"
        log.info("\n" + msg)
        send_telegram(msg)
        return
    
    # Handle NO_TRADE (sideways)
    if signal_data.get("signal") == "NO_TRADE":
        last_update = signal_data.get("last_update", now)
        msg = (
            f"🔍 <b>TSMOM GOLD v{Config.VERSION} — NO TRADE</b>\n"
            f"📅 {date_str}\n"
            f"💰 {Config.SYMBOL}: ${signal_data['price']:.2f}\n"
            f"📊 Data terakhir: {last_update.strftime('%d %b %Y')}\n"
            f"──────────────────────\n"
            f"<b>Status:</b> {signal_data['reason']}\n"
            f"📊 SMA 200: ${signal_data['sma200']:.2f}\n"
            f"──────────────────────\n"
            f"<i>⚠️ TUNGGU sinyal tren kuat sebelum entry.</i>"
        )
        log.info("\n" + msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
        send_telegram(msg)
        return
    
    # Handle SIGNAL (ada trade opportunity)
    conf_emoji = {"STRONG": "🟢", "MEDIUM": "🟡", "WEAK": "🟠"}
    conf_text = {"STRONG": "KUAT", "MEDIUM": "SEDANG", "WEAK": "LEMAH"}
    last_update = signal_data.get("last_update", now)
    
    msg = (
        f"{conf_emoji[signal_data['confidence']]} <b>TSMOM GOLD v{Config.VERSION} — MULTI-LAYER</b>\n"
        f"📅 {date_str}\n"
        f"💰 {Config.SYMBOL}: ${signal_data['price']:.2f}\n"
        f"📊 Data: {last_update.strftime('%d %b %Y')}\n"
        f"──────────────────────\n"
        f"<b>Sinyal:</b> {signal_data['signal']}\n"
        f"<b>Confidence:</b> {conf_emoji[signal_data['confidence']]} {conf_text[signal_data['confidence']]} "
        f"(Score: {signal_data['confidence_score']})\n"
        f"📊 ADX: {signal_data['adx']:.1f}\n"
        f"📊 SMA 200: ${signal_data['sma200']:.2f}\n"
        f"──────────────────────\n"
        f"<b>✅ Konfirmasi:</b>\n"
    )
    
    for conf in signal_data["confirmations"]:
        msg += f"  • {conf}\n"
    
    if signal_data["warnings"]:
        msg += f"<b>⚠️ Peringatan:</b>\n"
        for warn in signal_data["warnings"]:
            msg += f"  • {warn}\n"
    
    msg += (
        f"──────────────────────\n"
        f"<b>⚠️ PENTING:</b>\n"
        f"• BUKAN auto-trade signal\n"
        f"• Konfirmasi manual dengan chart\n"
        f"• STRONG: 1% | MEDIUM: 0.5% | WEAK: skip\n"
        f"• Stop loss: 2x ATR\n"
        f"──────────────────────\n"
        f"<i>Trading mengandung risiko tinggi.</i>"
    )
    
    log.info("\n" + msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    send_telegram(msg)

# ==============================================================================
# 8. ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        import traceback
        log.critical(f"FATAL: {e}\n{traceback.format_exc()}")
        send_telegram(f"🚨 <b>FATAL ERROR v{Config.VERSION}</b>\n<code>{e}</code>")
