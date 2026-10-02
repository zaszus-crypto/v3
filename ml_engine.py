#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ML ENGINE — Gradient Boosting (LightGBM-class) walk-forward.
Dilatih ulang tiap run pada 90 hari data riil, menjadi filter probabilitas sinyal.
Fallback aman: jika sklearn/data gagal, bot tetap jalan tanpa filter ML.
"""
import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M

FEATURES = ["ret1","ret4","atr_rel","rsi","z50","bb_width","ema_slope",
            "dist_swing_hi","dist_swing_lo","hour_sin","hour_cos","volume_rel"]
HORIZON = 16          # 8 jam (16 candle M30)
PROBA_MIN = 0.55      # ambang probabilitas minimal agar sinyal lolos

def _row(c30, i):
    c = c30[:i+1]
    px = [x["close"] for x in c]
    a = M.atr(c, 14); last = c[-1]
    highs, lows = M.swing_points(c, 5)
    hi = max(highs) if highs else last["high"]
    lo = min(lows) if lows else last["low"]
    m, up, lo_bb, sd = M.bollinger(px, 20)
    e20 = M.ema(px[-25:], 20) if len(px) >= 25 else px[-1]
    e_prev = M.ema(px[-45:-20], 20) if len(px) >= 45 else e20
    vols = [x.get("vol", 0) for x in c]
    vavg = M.sma(vols[-50:], 50) if len(vols) >= 50 else (vols[-1] or 1)
    t = last["time"].hour + last["time"].minute/60
    return [
        (px[-1]-px[-2])/px[-2], (px[-1]-px[-5])/px[-5],
        a/px[-1], M.rsi(px)/100,
        (px[-1]-M.sma(px,50))/(sd*5+1e-9), (up-lo_bb)/m,
        (e20-e_prev)/(px[-1]+1e-9),
        (hi-px[-1])/a, (px[-1]-lo)/a,
        math.sin(2*math.pi*t/24), math.cos(2*math.pi*t/24),
        (vols[-1]+1)/(vavg+1),
    ]

def _label(c30, i, a):
    """1 jika TP (1:2.5) tersentuh duluan sebelum SL, 0 jika SL duluan, None jika timeout."""
    direction = 1 if c30[i+1]["open"] >= c30[i]["close"] else -1  # proxy arah momentum
    entry = c30[i+1]["open"]; sl_d = 1.2*a; tp_d = sl_d*2.5
    sl = entry - direction*sl_d; tp = entry + direction*tp_d
    for j in range(i+1, min(i+1+HORIZON, len(c30))):
        h, l = c30[j]["high"], c30[j]["low"]
        if direction == 1:
            if l <= sl: return 0
            if h >= tp: return 1
        else:
            if h >= sl: return 0
            if l <= tp: return 1
    return None

def train_and_eval():
    """Walk-forward: latih 70% awal, uji 30% akhir. Kembalikan (model, stats)."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    c30 = M.fetch_ohlc("30m", "90d")
    if len(c30) < 400: raise ValueError("data tidak cukup")
    X, y = [], []
    for i in range(120, len(c30) - HORIZON - 1):
        a = M.atr(c30[:i+1], 14)
        lab = _label(c30, i, a)
        if lab is None: continue
        X.append(_row(c30, i)); y.append(lab)
    if len(X) < 150: raise ValueError("sampel tidak cukup")
    cut = int(len(X)*0.7)
    model = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.06,
                                           max_depth=4, random_state=42)
    model.fit(X[:cut], y[:cut])
    # evaluasi walk-forward
    from sklearn.metrics import accuracy_score, precision_score
    proba = model.predict_proba(X[cut:])[:, 1]
    acc = accuracy_score(y[cut:], (proba >= 0.5).astype(int))
    hi = proba >= 0.60
    prec = precision_score(y[cut:], (proba[hi] >= 0.5).astype(int)) if hi.sum() > 5 else float("nan")
    n_hi = int(hi.sum())
    M.log(f"ML walk-forward: n={len(y)} | acc={acc:.2f} | presisi(proba>=0.6)={prec:.2f} (n={n_hi})")
    # latih ulang pada SELURUH data untuk dipakai live
    model.fit(X, y)
    return model, {"acc": acc, "prec": prec, "n": n_hi}

_MODEL = None
def predict_proba(c30):
    """Probabilitas sinyal searah momentum terakhir lolos ke TP. None jika ML tak tersedia."""
    global _MODEL
    try:
        if _MODEL is None:
            _MODEL, _ = train_and_eval()
        return float(_MODEL.predict_proba([_row(c30, len(c30)-2)])[0][1])
    except Exception as e:
        M.log(f"ML nonaktif ({e}); lanjut tanpa filter ML.")
        return None
