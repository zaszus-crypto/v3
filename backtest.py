#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TURTLE × ORB CONFLUENCE v10.0 — FINAL
Fondasi: Turtle Breakout (PF 1.22, n=831) + filter kualitas.
Target: PF ≥ 1.40 dengan n ≥ 150.
"""
import os, sys, math, time, random, requests
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M
from datetime import datetime, timezone, timedelta

# PARAMETER FINAL
DONCHIAN_N   = 20
EMA_FAST     = 50
EMA_SLOW     = 200
ATR_P        = 14
SL_ATR       = 2.0
RR_TARGET    = 1.5
HOLD_HOURS   = 48
BODY_MIN     = 0.55
ATR_SPIKE    = 2.5
SESSION_START = 7
SESSION_END   = 19
COOLDOWN_H    = 18

def fetch(symbol="GC=F", rng="2y"):
    for host in ("query1","query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?interval=1h&range={rng}")
        for a in range(3):
            try:
                r = requests.get(url, headers=M.HEADERS, timeout=30)
                if r.status_code == 429:
                    time.sleep((2**a)+random.uniform(0,1)); continue
                r.raise_for_status()
                return _p(r.json())
            except Exception:
                time.sleep((2**a)+random.uniform(0,1))
    raise RuntimeError(f"{symbol} fail")

def _p(data):
    d = data["chart"]["result"][0]
    ts = d.get("timestamp") or []
    q = d["indicators"]["quote"][0]
    now = datetime.now(timezone.utc)
    out = []
    for i,t in enumerate(ts):
        try: o,h,l,c = q["open"][i],q["high"][i],q["low"][i],q["close"][i]
        except (KeyError, IndexError): continue
        if None in (o,h,l,c) or h<l or o<=0 or c<=0: continue
        dt = datetime.fromtimestamp(t, timezone.utc)
        if (dt + timedelta(hours=1)) > now: continue
        out.append({"time":dt,"open":float(o),"high":float(h),
                    "low":float(l),"close":float(c)})
    return out

def ema(v,n):
    if not v: return 0.0
    if len(v)<n: return sum(v)/len(v)
    k=2/(n+1); e=v[-n]
    for x in v[-n+1:]: e=x*k+e*(1-k)
    return e

def sma(v,n):
    if not v: return 0.0
    n=min(n,len(v)); return sum(v[-n:])/n

def atr(c,n=14):
    if len(c)<2: return 0.0
    t=[]
    for i in range(1,len(c)):
        h,l,pc=c[i]["high"],c[i]["low"],c[i-1]["close"]
        t.append(max(h-l,abs(h-pc),abs(l-pc)))
    return sma(t,n)

def donchian(c,n=20):
    if len(c)<n+1: return None,None
    w=c[-n-1:-1]
    return max(x["high"] for x in w), min(x["low"] for x in w)

def sim(c,i,d,entry,sl,tp,dist):
    end=min(i+1+HOLD_HOURS,len(c))
    for j in range(i+1,end):
        h,l=c[j]["high"],c[j]["low"]
        if d==1:
            if l<=sl: return -1.0,"SL"
            if h>=tp: return RR_TARGET,"TP"
        else:
            if h>=sl: return -1.0,"SL"
            if l<=tp: return RR_TARGET,"TP"
    ex=c[end-1]["close"]
    return (ex-entry)/dist*d,"TIMEOUT"

def stats(rs):
    n=len(rs)
    if not n: return None
    w=[r for r in rs if r["R"]>0]; l=[r for r in rs if r["R"]<=0]
    gw=sum(r["R"] for r in w); gl=abs(sum(r["R"] for r in l))
    pf=gw/gl if gl>0 else float('inf')
    eq=peak=mdd=0
    for r in rs:
        eq+=r["R"]; peak=max(peak,eq); mdd=max(mdd,peak-eq)
    return {"n":n,"wr":len(w)/n*100,"pf":pf,
            "exp":sum(r["R"] for r in rs)/n,"mdd":mdd}

def run():
    log=M.log
    log("=== TURTLE × ORB CONFLUENCE v10.0 ===")
    c=fetch("GC=F","2y")
    log(f"Data: {len(c)} candle H1")
    if len(c)<500: M.send_telegram("Data kurang"); return

    results=[]; last=-999
    rea={"burn":0,"donchian":0,"trend":0,"body":0,"atr":0,
         "session":0,"cooldown":0,"signal":0}

    for i in range(EMA_SLOW+10, len(c)-HOLD_HOURS-1):
        cs=c[:i+1]
        closes=[x["close"] for x in cs]
        last_c=cs[-1]; prev=cs[-2]
        dc_up, dc_lo = donchian(cs, DONCHIAN_N)
        if dc_up is None: rea["donchian"]+=1; continue
        e50=ema(closes,EMA_FAST); e200=ema(closes,EMA_SLOW)
        a=atr(cs,ATR_P)
        if a<=0: continue

        # Trend filter
        if last_c["close"]>dc_up and last_c["close"]>e50 and e50>e200:
            d=+1
        elif last_c["close"]<dc_lo and last_c["close"]<e50 and e50<e200:
            d=-1
        else:
            rea["trend"]+=1; continue

        # ATR spike filter
        atr_series=[atr(cs[:k+1],ATR_P) for k in range(max(0,len(cs)-20),len(cs))]
        a_avg=sma(atr_series,20)
        if a>a_avg*ATR_SPIKE: rea["atr"]+=1; continue

        # Body filter
        body=abs(last_c["close"]-last_c["open"])
        rng=last_c["high"]-last_c["low"]
        body_pct=body/rng if rng>0 else 0
        if body_pct<BODY_MIN: rea["body"]+=1; continue

        # Session
        hh=last_c["time"].hour
        if not (SESSION_START<=hh<SESSION_END): rea["session"]+=1; continue

        # Cooldown
        if (i-last)<COOLDOWN_H: rea["cooldown"]+=1; continue

        entry=c[i+1]["open"]
        sl_dist=a*SL_ATR
        if d==1: sl=entry-sl_dist; tp=entry+sl_dist*RR_TARGET
        else: sl=entry+sl_dist; tp=entry-sl_dist*RR_TARGET

        r,h=sim(c,i,d,entry,sl,tp,sl_dist)
        results.append({"time":str(last_c["time"]),"dir":d,"R":r,
                        "hasil":h,"idx":i})
        rea["signal"]+=1; last=i

    n=len(results)
    log(f"Sinyal: {n} | {rea}")
    if not n:
        M.send_telegram("<b>Turtle×ORB v10.0</b>: 0 sinyal"); return

    total=len(c); third=total//3
    segs=[]
    for k in range(3):
        lo,hi=k*third,(k+1)*third
        segs.append(stats([r for r in results if lo<=r["idx"]<hi]))
    ov=stats(results)

    def L(name,s):
        if not s: return f"  {name:10s}: n=0"
        pf_s=f"{s['pf']:.2f}" if s['pf']!=float('inf') else "inf"
        return (f"  {name:10s}: n={s['n']:3d} WR={s['wr']:5.1f}% "
                f"PF={pf_s:>5s} Exp={s['exp']:+.3f}R DD={s['mdd']:.1f}R")

    log("\n--- OVERALL ---"); log(L("TOTAL",ov))
    log("\n--- SEGMEN ---")
    for k in range(3): log(L(f"S{k+1}",segs[k]))

    cons=sum(1 for s in segs if s and s["pf"]>=1.3)
    ok = ov["n"]>=100 and ov["pf"]>=1.40 and ov["exp"]>0.2 and cons>=2

    msg=(f"<b>📋 TURTLE × ORB CONFLUENCE v10.0</b>\n"
         f"──────────────────\n"
         f"Total sinyal  : {ov['n']}\n"
         f"🏆 Win rate   : {ov['wr']:.1f}%\n"
         f"💵 PF         : {ov['pf']:.2f}\n"
         f"📈 Expectancy : {ov['exp']:+.3f} R\n"
         f"📉 Max DD     : {ov['mdd']:.1f} R\n"
         f"──────────────────\n"
         f"<b>Walk-forward:</b>\n")
    for k in range(3):
        s=segs[k]
        if s:
            pf_s=f"{s['pf']:.2f}" if s['pf']!=float('inf') else "inf"
            msg+=f"• S{k+1}: n={s['n']} WR={s['wr']:.0f}% PF={pf_s} Exp={s['exp']:+.2f}R\n"
    msg+=f"──────────────────\nKonsistensi: {cons}/3\n"
    msg+="✅ LAYAK LIVE" if ok else "⚠️ BELUM"
    log("\n"+msg.replace("<b>","").replace("</b>",""))
    M.send_telegram(msg)

if __name__=="__main__":
    try: run()
    except Exception as e:
        import traceback; M.log(f"FATAL: {e}\n{traceback.format_exc()}")
