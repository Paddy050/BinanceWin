"""Shared causal research engine; no order endpoints."""
import numpy as np
import pandas as pd
MODES=('balanced','strict','retest')
def adx(d,n=14):
    up=d.high.diff(); down=-d.low.diff()
    tr=pd.concat([d.high-d.low,(d.high-d.close.shift()).abs(),(d.low-d.close.shift()).abs()],axis=1).max(axis=1)
    smooth=lambda x:x.ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    p=100*smooth(up.where((up>down)&(up>0),0))/smooth(tr); m=100*smooth(down.where((down>up)&(down>0),0))/smooth(tr)
    return float(smooth(100*(p-m).abs()/(p+m).replace(0,np.nan)).iloc[-1])
def assess(w,d,h,f,m,btc,oi,funding,equity=False):
    r=w.evaluate(d,h,f,m,historical=True)
    if not r:return []
    s=1 if r['direction']=='偏多' else -1; a=r['atr']; p=r['price']; t=int(d.iloc[-1].close_ms)
    strength=adx(h[h.close_ms<=t]); checks=dict(r['checks']); checks['ADX']=bool(strength>=25)
    checks['BTC方向']=equity or (btc is not None and btc==s)
    checks['OI']=oi is not None and oi>=0
    checks['資金費率']=funding is not None and s*funding<=0.0005
    checks['盈虧空間']=r['room_r'] is None or r['room_r']>=w.RISK_REWARD
    retest=False
    for i in range(2,5):
        b=d.iloc[-i]; past=d.iloc[-i-20:-i]; level=float(past.high.max() if s==1 else past.low.min()); last=d.iloc[-1]
        if s*(b.close-level)>=.1*a and s*(p-level)>0 and abs((last.low if s==1 else last.high)-level)<=.3*a and all(s*(x-level)>=-.2*a for x in d.iloc[-i+1:].close):retest=True;break
    score=round(100*sum(checks.values())/len(checks)); out=[]
    for mode in MODES:
        passed=(r['grade']=='強訊號' and all(checks.values())) if mode=='strict' else (all(checks[k] for k in ('4H趨勢','1H動能','避免追價','BTC方向','資金費率','盈虧空間')) and strength>=20 and (retest if mode=='retest' else r['grade']!='觀察訊號'))
        grade='強訊號' if passed else ('候選訊號' if score>=75 else '觀察訊號')
        out.append(dict(r,mode=mode,grade=grade,score=score,adx=strength,oi=oi,funding=funding,retest=retest,checks=checks,t=t,lo=p-.25*a,hi=p+.25*a))
    return out

def simulate(r,bars,fee_bps=16,exit_mode='fixed',funding_events=()):
    """Next open, no chasing, gap stop, stop-first, trailing updates next bar. Missing bars invalidate."""
    t=r['t']; future=bars[(bars.open_ms>t)&(bars.open_ms<=t+86400000)].sort_values('open_ms'); s=1 if r['direction']=='偏多' else -1
    if future.empty:return None
    if int(future.iloc[0].open_ms)!=t+1:return dict(outcome='GAP',net_pct=None)
    entry=float(future.iloc[0].open); risk=abs(r['price']-r['stop']); stop=entry-s*risk; target=entry+s*risk*2
    if not r['lo']<=entry<=r['hi'] or stop<=0 or target<=0:return dict(outcome='NO_FILL',net_pct=None)
    prev=t; mfe=mae=0
    for b in future.itertuples():
        if int(b.open_ms)!=prev+1:return dict(outcome='GAP',net_pct=None)
        prev=int(b.close_ms); outcome=None
        if s*(b.open-stop)<=0:outcome='SL'; price=b.open
        elif (b.low<=stop if s==1 else b.high>=stop):outcome='SL';price=stop
        elif (b.high>=target if s==1 else b.low<=target):outcome='TP';price=target
        elif prev>=t+86400000:outcome='24H';price=b.close
        if outcome:
            cost=sum(s*rate*100 for ts,rate in funding_events if t<ts<=prev)
            net=100*s*(price/entry-1)-fee_bps/100-cost
            return dict(outcome=outcome,net_pct=net,r=net/(100*risk/entry),exit_ms=prev,entry=entry,funding_cost=cost)
        favorable=(b.high-entry if s==1 else entry-b.low)
        if favorable>=risk and exit_mode!='fixed':
            new=entry if exit_mode=='breakeven' else entry+s*max(0,favorable-risk)
            stop=max(stop,new) if s==1 else min(stop,new)
    return None
