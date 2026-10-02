#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TSMOM GOLD v12.0 — Time-Series Momentum institutional.
Basis: Moskowitz-Ooi-Pedersen (2012). State-machine fixed.
"""
import os, sys, math, time, random, requests
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M
from datetime import datetime, timezone, timedelta

LOOKBACK   = 252
REBALANCE  = 5
ATR_P      = 20
INIT_RISK  = 2.0
TRAIL_MULT = 4.0
TARGET_VOL = 0.15
VOL_WINDOW = 20
SIZE_MIN   = 0.3
SIZE_MAX   = 3.0
MAX_HOLD   = 500

def fetch_daily(years=10):
    for host in ("query1","query2"):
        url=(f"https://{host}.finance.yahoo.com/v8/finance/chart/GC=F"
             f"?interval=1d&range={years}y")
        for a in range(3):
            try:
                r=requests.get(url,headers=M.HEADERS,timeout=30)
                if r.status_code==429:
                    time.sleep((2**a)+random.uniform(0,1)); continue
                r.raise_for_status()
                return _p(r.json())
            except Exception:
                time.sleep((2**a)+random.uniform(0,1))
    raise RuntimeError("Yahoo fail")

def _p(data):
    d=data["chart"]["result"][0]
    ts=d.get("timestamp") or []
    q=d["indicators"]["quote"][0]
    now=datetime.now(timezone.utc)
    out=[]
    for i,t in enumerate(ts):
        try: o,h,l,c=q["open"][i],q["high"][i],q["low"][i],q["close"][i]
        except (KeyError, IndexError): continue
        if None in (o,h,l,c) or h<l or o<=0 or c<=0: continue
        dt=datetime.fromtimestamp(t,timezone.utc)
        if (dt+timedelta(days=1))>now: continue
        out.append({"time":dt,"open":float(o),"high":float(h),
                    "low":float(l),"close":float(c)})
    return out

def sma(v,n):
    if not v: return 0.0
    n=min(n,len(v)); return sum(v[-n:])/n

def atr(c,n=20):
    if len(c)<2: return 0.0
    t=[]
    for i in range(1,len(c)):
        h,l,pc=c[i]["high"],c[i]["low"],c[i-1]["close"]
        t.append(max(h-l,abs(h-pc),abs(l-pc)))
    return sma(t,n)

def realized_vol(closes,w=20):
    if len(closes)<w+1: return 0.20
    rets=[(closes[i]-closes[i-1])/closes[i-1] for i in range(-w,0)]
    m=sum(rets)/w
    sd=math.sqrt(sum((x-m)**2 for x in rets)/w)
    return sd*math.sqrt(252)

def stats(rs):
    n=len(rs)
    if n==0: return None
    w=[r for r in rs if r["R"]>0]; l=[r for r in rs if r["R"]<=0]
    gw=sum(r["R"] for r in w); gl=abs(sum(r["R"] for r in l))
    pf=gw/gl if gl>0 else float('inf')
    eq=peak=mdd=0
    for r in rs:
        eq+=r["R"]; peak=max(peak,eq); mdd=max(mdd,peak-eq)
    return {"n":n,"wr":len(w)/n*100,"pf":pf,
            "exp":sum(r["R"] for r in rs)/n,"mdd":mdd,
            "tot":sum(r["R"] for r in rs)}

def run():
    log=M.log
    log("=== TSMOM GOLD v12.0 ===")
    c=fetch_daily(10)
    log(f"Daily: {len(c)} ({len(c)/252:.1f} tahun)")
    if len(c)<400:
        M.send_telegram("Data kurang"); return

    closes=[x["close"] for x in c]
    results=[]
    pos=0; entry_price=0.0; entry_idx=0
    peak_price=0.0; init_risk=0.0; trail_dist=0.0; vol_size=1.0
    last_rebal=-REBALANCE

    for i in range(LOOKBACK+30, len(c)):
        a=atr(c[:i+1], ATR_P)
        if a<=0: continue

        # 1. Trailing / exit
        if pos != 0:
            hold=i-entry_idx
            exited=False
            if pos==1:
                peak_price=max(peak_price, c[i]["high"])
                sl_now=max(entry_price-init_risk, peak_price-trail_dist)
                if c[i]["low"] <= sl_now:
                    r=(sl_now-entry_price)/init_risk*vol_size
                    results.append({"time":str(c[i]["time"]),"dir":pos,"R":r,
                                    "idx":entry_idx,"hold":hold})
                    pos=0; exited=True
            else:
                peak_price=min(peak_price, c[i]["low"])
                sl_now=min(entry_price+init_risk, peak_price+trail_dist)
                if c[i]["high"] >= sl_now:
                    r=(entry_price-sl_now)/init_risk*vol_size
                    results.append({"time":str(c[i]["time"]),"dir":pos,"R":r,
                                    "idx":entry_idx,"hold":hold})
                    pos=0; exited=True
            if not exited and hold>=MAX_HOLD:
                r=(c[i]["close"]-entry_price)/init_risk*vol_size if pos==1 else \
                  (entry_price-c[i]["close"])/init_risk*vol_size
                results.append({"time":str(c[i]["time"]),"dir":pos,"R":r,
                                "idx":entry_idx,"hold":hold})
                pos=0

        # 2. TSMOM signal (weekly rebalance)
        if (i-last_rebal) < REBALANCE: continue
        last_rebal=i

        past=closes[i-LOOKBACK]; curr=closes[i]
        new_dir = +1 if curr>past else -1
        if new_dir==pos: continue

        # Close posisi lama kalau ada
        if pos!=0:
            r=(curr-entry_price)/init_risk*vol_size if pos==1 else \
              (entry_price-curr)/init_risk*vol_size
            results.append({"time":str(c[i]["time"]),"dir":pos,"R":r,
                            "idx":entry_idx,"hold":i-entry_idx})

        # Buka posisi baru
        vol=realized_vol(closes[:i+1], VOL_WINDOW)
        if vol<=0: continue
        vol_size=max(SIZE_MIN, min(SIZE_MAX, TARGET_VOL/vol))
        pos=new_dir; entry_price=curr; entry_idx=i
        init_risk=a*INIT_RISK; trail_dist=a*TRAIL_MULT
        peak_price=c[i]["high"] if pos==1 else c[i]["low"]

    n=len(results)
    log(f"Total trades: {n}")
    if n==0:
        M.send_telegram("<b>TSMOM v12.0</b>: 0 trade"); return

    total=len(c); fourth=total//4
    segs=[]
    for k in range(4):
        lo,hi=k*fourth,(k+1)*fourth
        segs.append(stats([r for r in results if lo<=r["idx"]<hi]))
    ov=stats(results)

    def L(name,s):
        if not s: return f"  {name:10s}: n=0"
        pf_s=f"{s['pf']:.2f}" if s['pf']!=float('inf') else "inf"
        return (f"  {name:10s}: n={s['n']:3d} WR={s['wr']:5.1f}% "
                f"PF={pf_s:>5s} Exp={s['exp']:+.3f}R DD={s['mdd']:.1f}R")

    log("\n--- OVERALL ---"); log(L("TOTAL",ov))
    log("\n--- 4 SEGMEN ---")
    for k in range(4): log(L(f"S{k+1}",segs[k]))

    holds=[r["hold"] for r in results]
    avg_hold=sum(holds)/len(holds) if holds else 0
    log(f"Avg hold: {avg_hold:.1f} hari")

    cons=sum(1 for s in segs if s and s["pf"]>=1.3)
    ok = ov["n"]>=20 and ov["pf"]>=1.4 and ov["exp"]>0.3 and cons>=3

    msg=(f"<b>📋 TSMOM GOLD v12.0</b>\n"
         f"──────────────────\n"
         f"Total trades  : {ov['n']}\n"
         f"🏆 Win rate   : {ov['wr']:.1f}%\n"
         f"💵 PF         : {ov['pf']:.2f}\n"
         f"📈 Expectancy : {ov['exp']:+.3f} R\n"
         f"📉 Max DD     : {ov['mdd']:.1f} R\n"
         f"⏱ Avg hold    : {avg_hold:.0f} hari\n"
         f"──────────────────\n"
         f"<b>Walk-forward:</b>\n")
    for k in range(4):
        s=segs[k]
        if s:
            pf_s=f"{s['pf']:.2f}" if s['pf']!=float('inf') else "inf"
            msg+=f"• S{k+1}: n={s['n']} WR={s['wr']:.0f}% PF={pf_s} Exp={s['exp']:+.2f}R\n"
    msg+=f"──────────────────\nKonsistensi: {cons}/4\n"
    msg+="✅ LAYAK LIVE" if ok else "⚠️ BELUM"
    log("\n"+msg.replace("<b>","").replace("</b>",""))
    M.send_telegram(msg)

if __name__=="__main__":
    try: run()
    except Exception as e:
        import traceback; M.log(f"FATAL: {e}\n{traceback.format_exc()}")
