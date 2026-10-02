#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ML ENGINE v2.0 — label berdasarkan arah ENSEMBLE (bukan look-ahead).
Model di-cache ke disk; retrain bila file > 3 hari.
"""
import os, sys, math, pickle, json
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M

FEATURES = ["ret1","ret4","atr_rel","rsi","z50","bb_width","ema_slope",
            "dist_swing_hi","dist_swing_lo","hour_sin","hour_cos","volume_rel"]
HORIZON  = 32
PROBA_MIN = 0.55
RETRAIN_DAYS = 3
BURN_IN = 200

def make_features(c30):
    """Feature vektor dari slice candle closed."""
    if len(c30) < 60: return None
    px = [x["close"] for x in c30]
    a = M.atr(c30, 14)
    if a <= 0: return None
    last = c30[-1]
    highs, lows = M.swing_points(c30, 5)
    hi = max(highs) if highs else last["high"]
    lo = min(lows)  if lows  else last["low"]
    m, up, lo_bb, sd = M.bollinger(px, 20)
    if m <= 0: return None
    e20    = M.ema(px[-25:], 20) if len(px) >= 25 else px[-1]
    e_prev = M.ema(px[-45:-20], 20) if len(px) >= 45 else e20
    vols = [x.get("vol", 0) for x in c30]
    vavg = M.sma(vols[-50:], 50) if len(vols) >= 50 else (vols[-1] or 1)
    t = last["time"].hour + last["time"].minute/60
    return [
        (px[-1]-px[-2])/px[-2],
        (px[-1]-px[-5])/px[-5] if len(px) >= 5 else 0,
        a/px[-1],
        M.rsi(px)/100,
        (px[-1]-M.sma(px, 50))/(sd*5 + 1e-9),
        (up-lo_bb)/m,
        (e20-e_prev)/(px[-1] + 1e-9),
        (hi-px[-1])/a,
        (px[-1]-lo)/a,
        math.sin(2*math.pi*t/24),
        math.cos(2*math.pi*t/24),
        (vols[-1]+1)/(vavg+1),
    ]

# alias untuk kompatibilitas main.py
_row = make_features

def _label(c30, i, sl_dist, tp_dist, direction):
    """1 = TP duluan, 0 = SL duluan, None = timeout / gap tidak jelas."""
    if i+1 >= len(c30): return None
    entry = c30[i+1]["open"]
    sl = entry - direction * sl_dist
    tp = entry + direction * tp_dist
    if direction == 1:
        if entry >= tp: return 1
        if entry <= sl: return 0
    else:
        if entry <= tp: return 1
        if entry >= sl: return 0
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
    from sklearn.ensemble import HistGradientBoostingClassifier
    c30 = M.fetch_ohlc("30m", "90d")
    if len(c30) < 500: raise ValueError("data < 500")

    X, y = [], []
    for i in range(BURN_IN, len(c30) - HORIZON - 2):
        c_slice = c30[:i+1]
        if not M.regime_ok(c_slice): continue
        hh = c_slice[-1]["time"].hour
        if not any(a <= hh < b for a, b in M.KILLZONES): continue

        votes = {
            "SMC_Sweep":    M.strat_smc_sweep(c_slice),
            "MSB_MultiTF":  M.strat_msb_mtf(c_slice, c_slice),   # proxy single-TF
            "MeanReversion":M.strat_mean_reversion(c_slice, c_slice),
            "SqueezeBreak": M.strat_squeeze_breakout(c_slice),
        }
        score = sum(votes[k] * M.WEIGHTS[k] for k in votes)
        if abs(score) < M.MIN_SCORE_WEIGHTED: continue

        direction = 1 if score > 0 else -1
        a = M.atr(c_slice, 14)
        if a <= 0: continue
        lab = _label(c30, i, M.SL_MULT*a, M.SL_MULT*a*M.RR_TARGET, direction)
        if lab is None: continue
        row = make_features(c_slice)
        if row is None: continue
        X.append(row); y.append(lab)

    if len(X) < 150: raise ValueError(f"sampel < 150 ({len(X)})")

    cut = int(len(X)*0.7)
    model = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.06,
                                            max_depth=4, random_state=42)
    model.fit(X[:cut], y[:cut])

    from sklearn.metrics import accuracy_score, precision_score
    proba = model.predict_proba(X[cut:])[:, 1]
    acc = accuracy_score(y[cut:], (proba >= 0.5).astype(int))
    hi = proba >= 0.60
    prec = (precision_score(y[cut:], (proba[hi] >= 0.5).astype(int))
            if hi.sum() > 5 else float("nan"))
    M.log(f"ML WF: n={len(y)} acc={acc:.2f} prec(>=.6)={prec:.2f} (n={int(hi.sum())})")

    # Train ulang pada seluruh data untuk live
    model.fit(X, y)
    return model, {"acc": acc, "prec": prec, "n_train": len(X)}

def _is_fresh():
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(M.MODEL_FILE), timezone.utc)
        return (datetime.now(timezone.utc) - mtime) < timedelta(days=RETRAIN_DAYS)
    except Exception:
        return False

def ensure_model(force=False):
    if not force and _is_fresh() and os.path.exists(M.MODEL_FILE):
        return True
    try:
        model, stats = train_and_eval()
        with open(M.MODEL_FILE, "wb") as f:
            pickle.dump(model, f)
        with open(M.MODEL_META_FILE, "w") as f:
            json.dump(stats, f)
        M.log(f"Model disimpan: {stats}")
        return True
    except Exception as e:
        M.log(f"ML training gagal: {e}")
        return False

def predict_proba(c30):
    """Untuk main.py live (load model dari disk)."""
    return None   # main.py pakai _load_ml sendiri

if __name__ == "__main__":
    ensure_model(force=True)
