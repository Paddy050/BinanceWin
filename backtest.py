"""Offline CSV backtest. Each CSV: open_ms,open,high,low,close,volume,quote_volume,close_ms.
Usage: python backtest.py --folder data --symbol ETH-USDT-SWAP --out results.csv
Only closed candles at the signal time. Next-bar open execution, stop-first ambiguous bars.
"""
import argparse
import csv
from pathlib import Path
import pandas as pd
from worker import evaluate, FEE_BPS, SLIPPAGE_BPS

def load(path):
    df=pd.read_csv(path)
    required={'open_ms','open','high','low','close','volume','quote_volume','close_ms'}
    if not required.issubset(df.columns):raise ValueError(f'{path}: missing {required-set(df.columns)}')
    df=df.sort_values('close_ms').drop_duplicates('open_ms')
    if df.isna().any().any() or (df[['open','high','low','close']]<=0).any().any():raise ValueError('Invalid OHLCV')
    return df.reset_index(drop=True)

def run(folder,symbol):
    frames={k:load(Path(folder)/f'{symbol}_{k}.csv') for k in ('5m','15m','1h','4h')}
    base=frames['15m']; trades=[]; blocked_until=-1
    for i in range(100,len(base)-1):
        bar=base.iloc[i];t=int(bar.close_ms)
        if t<=blocked_until:continue
        slices={k:df[df.close_ms<=t].tail(220 if k=='15m' else 110) for k,df in frames.items()}
        if any(len(df)<40 for df in slices.values()):continue
        result=evaluate(slices['15m'],slices['1h'],slices['4h'],slices['5m'],historical=True)
        if not result or result['grade']!='強訊號':continue
        nextbar=base.iloc[i+1];entry=float(nextbar.open)
        sign=1 if result['direction']=='偏多' else -1
        distance=abs(result['price']-result['stop'])
        stop=entry-sign*distance;target=entry+sign*distance*2
        if stop<=0:continue
        outcome='24H'; exit_price=None;exit_time=None
        future=base.iloc[i+1:]
        for candle in future.itertuples():
            if int(candle.close_ms)>t+24*3600000:break
            sl=candle.low<=stop if sign==1 else candle.high>=stop
            tp=candle.high>=target if sign==1 else candle.low<=target
            if sl:outcome='SL';exit_price=stop;exit_time=int(candle.close_ms);break
            if tp:outcome='TP';exit_price=target;exit_time=int(candle.close_ms);break
            exit_price=float(candle.close);exit_time=int(candle.close_ms)
        if exit_price is None:continue
        net=100*sign*(exit_price/entry-1)-(FEE_BPS+SLIPPAGE_BPS)/100
        trades.append(dict(symbol=symbol,signal_ms=t,exit_ms=exit_time,direction=result['direction'],score=result['score'],entry=entry,stop=stop,target=target,outcome=outcome,net_pct=round(net,5)))
        blocked_until=exit_time
    return trades

def metrics(trades):
    if not trades:return {'count':0,'note':'No qualifying completed trades'}
    vals=[t['net_pct'] for t in trades];equity=1.;peak=1.;max_dd=0.;loss_streak=0;max_streak=0
    for x in vals:
        equity*=max(0,1+x/100);peak=max(peak,equity);max_dd=max(max_dd,100*(1-equity/peak))
        loss_streak=loss_streak+1 if x<=0 else 0;max_streak=max(max_streak,loss_streak)
    wins=[v for v in vals if v>0];losses=[v for v in vals if v<0]
    return {'count':len(vals),'positive_net_rate':len(wins)/len(vals),'mean_net_pct':sum(vals)/len(vals),
            'profit_factor':sum(wins)/abs(sum(losses)) if losses else None,
            'max_drawdown_pct':max_dd,'max_consecutive_losses':max_streak,'compounded_return_pct':(equity-1)*100}

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--folder',required=True);ap.add_argument('--symbol',required=True);ap.add_argument('--out',default='backtest_results.csv');a=ap.parse_args()
    rows=run(a.folder,a.symbol)
    with open(a.out,'w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=['symbol','signal_ms','exit_ms','direction','score','entry','stop','target','outcome','net_pct']);w.writeheader();w.writerows(rows)
    print(metrics(rows));print('Saved:',a.out)
