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
    import worker as w
    from strategy53 import assess,simulate
    frames={k:load(Path(folder)/f'{symbol}_{k}.csv') for k in ('5m','15m','1h','4h')}
    context=pd.read_csv(Path(folder)/f'{symbol}_context.csv').sort_values('close_ms')
    base=frames['15m']; trades=[]; blocked={}
    for i in range(100,len(base)-96):
        t=int(base.iloc[i].close_ms); ctx=context[context.close_ms==t]
        if ctx.empty:continue
        x=ctx.iloc[-1]; slices={k:df[df.close_ms<=t].tail(220) for k,df in frames.items()}
        if min(map(len,slices.values()))<40:continue
        for r in assess(w,slices['15m'],slices['1h'],slices['4h'],slices['5m'],x.btc_direction,x.oi_change,x.funding_rate,symbol.split('-')[0] in w.EQUITY_SYMBOLS):
            for ex in ('fixed','breakeven','trailing'):
                if r['grade']!='強訊號' and ex!='fixed':continue
                key=(r['mode'],ex,r['grade'])
                if t<=blocked.get(key,-1):continue
                result=simulate(r,base.iloc[i+1:i+97],FEE_BPS+SLIPPAGE_BPS,ex)
                if not result or result.get('net_pct') is None:continue
                blocked[key]=result['exit_ms'];trades.append(dict(symbol=symbol,signal_ms=t,direction=r['direction'],score=r['score'],mode=r['mode'],exit_mode=ex,grade=r['grade'],**result))
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
        w=csv.DictWriter(f,fieldnames=['symbol','signal_ms','exit_ms','direction','score','entry','outcome','net_pct','mode','exit_mode','grade','r','funding_cost']);w.writeheader();w.writerows(rows)
    print(metrics(rows));print('Saved:',a.out)
