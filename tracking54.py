"""Independent forward paper tracking, using closed one-minute bars; no orders."""
import json, sqlite3, time, sys

def w():
    return sys.modules['__main__'] if hasattr(sys.modules['__main__'],'DB') else sys.modules['worker']
def init_tracking():
    with sqlite3.connect(w().DB) as c:
        c.execute('CREATE TABLE IF NOT EXISTS tracking54 (venue TEXT,symbol TEXT,t INTEGER,payload TEXT,state TEXT,entry REAL,stop REAL,target REAL,last_ms INTEGER,mfe REAL DEFAULT 0,mae REAL DEFAULT 0,net REAL,r REAL,notice TEXT,PRIMARY KEY(venue,symbol,t))')
def queue_tracking(venue,symbol,r):
    init_tracking(); now=int(time.time()*1000)
    with sqlite3.connect(w().DB) as c:
        if c.execute("SELECT 1 FROM tracking54 WHERE venue=? AND symbol=? AND state IN ('WAIT','OPEN')",(venue,symbol)).fetchone():return False
        return c.execute("INSERT OR IGNORE INTO tracking54 (venue,symbol,t,payload,state,last_ms) VALUES(?,?,?,?,?,?)",(venue,symbol,r['t'],json.dumps(r,default=float),'WAIT',now//60000*60000+59999)).rowcount==1

def step(r,state,entry,stop,target,mfe,mae,b,fee):
    s=1 if r['direction']=='偏多' else -1
    if state=='WAIT':
        if b.open_ms>r['t']+3600000:return 'EXPIRED',entry,stop,target,mfe,mae,None,None
        if not r['lo']<=b.open<=r['hi']:return state,entry,stop,target,mfe,mae,None,None
        entry=float(b.open);stop=float(r['stop']);target=float(r['target'])
        if s*(entry-stop)<=0 or s*(target-entry)<=0 or s*(target-entry)/abs(entry-stop)<1.5:return 'WAIT',None,None,None,mfe,mae,None,None
        state='OPEN'
    risk=abs(entry-stop);price=None
    if s*(b.open-stop)<=0:state='SL';price=b.open
    elif (b.low<=stop if s==1 else b.high>=stop):state='SL';price=stop
    elif (b.high>=target if s==1 else b.low<=target):state='TP';price=target
    elif b.close_ms>=r['t']+86400000:state='TIME';price=b.close
    # Exit bar extremes have unknown ordering; exclude them from excursion statistics.
    if price is None:
        mfe=max(mfe,100*(b.high-entry if s==1 else entry-b.low)/entry)
        mae=max(mae,100*(entry-b.low if s==1 else b.high-entry)/entry)
        return state,entry,stop,target,mfe,mae,None,None
    net=100*s*(price/entry-1)-fee/100
    return state,entry,stop,target,mfe,mae,net,net/(100*risk/entry)

def track_active():
    init_tracking(); mod=w()
    with sqlite3.connect(mod.DB) as c: rows=c.execute("SELECT venue,symbol,t,payload,state,entry,stop,target,last_ms,mfe,mae FROM tracking54 WHERE state IN ('WAIT','OPEN')").fetchall()
    for venue,symbol,t,payload,state,entry,stop,target,last,mfe,mae in rows:
        try:
            r=json.loads(payload);bars=mod.candles(venue,symbol,'1m',220); old=state; net=rr=None
            for b in bars[bars.open_ms>last].itertuples():
                if b.open_ms!=last+1:state='GAP';break
                state,entry,stop,target,mfe,mae,net,rr=step(r,state,entry,stop,target,mfe,mae,b,mod.FEE_BPS+mod.SLIPPAGE_BPS);last=int(b.close_ms)
                if state not in ('WAIT','OPEN'):break
            notice=None
            if old!=state:
                label={'OPEN':'模擬成交','TP':'模擬停利','SL':'模擬停損','TIME':'到期結算','EXPIRED':'訊號失效','GAP':'行情缺口，停止計算'}[state]
                notice=f'📍 BinanceWin V5.4｜{symbol} {r["direction"]}\n{label}'
                if entry is not None:notice+=f'｜進場 {entry:.6g}'
                if net is not None:notice+=f'\n估計報酬 {net:+.2f}%｜{rr:+.2f}R（未含資金費）\n完整持倉分鐘 MFE {mfe:.2f}%｜MAE {mae:.2f}%'
            with sqlite3.connect(mod.DB) as c:c.execute('UPDATE tracking54 SET state=?,entry=?,stop=?,target=?,last_ms=?,mfe=?,mae=?,net=?,r=?,notice=COALESCE(?,notice) WHERE venue=? AND symbol=? AND t=?',(state,entry,stop,target,last,mfe,mae,net,rr,notice,venue,symbol,t))
        except Exception:mod.LOG.exception('Tracking %s failed',symbol)
    with sqlite3.connect(mod.DB) as c: notices=c.execute('SELECT venue,symbol,t,notice FROM tracking54 WHERE notice IS NOT NULL').fetchall()
    for venue,symbol,t,notice in notices:
        try:
            mod.telegram(notice+'\n僅供研究，不自動下單。')
            with sqlite3.connect(mod.DB) as c:c.execute('UPDATE tracking54 SET notice=NULL WHERE venue=? AND symbol=? AND t=?',(venue,symbol,t))
        except Exception:mod.LOG.exception('Tracking notice failed')
def tracking_count():
    init_tracking()
    with sqlite3.connect(w().DB) as c:return c.execute("SELECT COUNT(*) FROM tracking54 WHERE state IN ('WAIT','OPEN')").fetchone()[0]
def tracking_summary():
    init_tracking()
    with sqlite3.connect(w().DB) as c:
        rows=c.execute('SELECT state,COUNT(*) FROM tracking54 GROUP BY state').fetchall(); vals=c.execute('SELECT r FROM tracking54 WHERE r IS NOT NULL ORDER BY last_ms').fetchall()
    d=dict(rows); return f'實時模擬追蹤：停利 {d.get("TP",0)}｜停損 {d.get("SL",0)}｜失效 {d.get("EXPIRED",0)}｜缺口 {d.get("GAP",0)}｜追蹤中 {tracking_count()}'+(f'｜平均 {sum(x[0] for x in vals)/len(vals):+.2f}R' if vals else '')
