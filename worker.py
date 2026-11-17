"""BinanceWin V5.2 - public futures market research alerts; never places orders."""
import os
import time
import json
import random
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import shutil

import requests
import numpy as np
import pandas as pd

LOG = logging.getLogger('binancewin')
HTTP = requests.Session()
HTTP.headers.update({'User-Agent': 'BinanceWinResearch/5.2'})
DB = Path(os.getenv('DB_PATH', str(Path(__file__).with_name('scanner.sqlite3'))))
TIMEOUT = float(os.getenv('HTTP_TIMEOUT', '12'))
TOP_N = max(1, min(100, int(os.getenv('TOP_N', '30'))))
RETRIES = max(0, min(5, int(os.getenv('API_RETRIES', '2'))))
OKX = 'https://www.okx.com'
BINGX = 'https://open-api.bingx.com'
SCAN_OFFSET = 35
# Optional explicitly configured exchange-listed equity-linked swaps.
# Exact symbols must be confirmed from live exchange instrument listings.
EQUITY_SYMBOLS = list(dict.fromkeys(x.strip().upper() for x in os.getenv('EQUITY_SYMBOLS', 'RKLB,TSLA,SNXX,SNDK,SPCX,MU,MUU,NVDA,NVDL').split(',') if x.strip()))
WATCH_ALERTS = os.getenv('WATCH_ALERTS', '0') == '1'
WATCH_MIN_VOLUME = float(os.getenv('WATCH_MIN_VOLUME', '1.2'))
WATCH_MAX_PER_SCAN = max(0, min(10, int(os.getenv('WATCH_MAX_PER_SCAN', '3'))))
FEE_BPS = float(os.getenv('ROUND_TRIP_FEE_BPS', '10'))
SLIPPAGE_BPS = float(os.getenv('ROUND_TRIP_SLIPPAGE_BPS', '6'))
ATR_SL = float(os.getenv('ATR_STOP_MULTIPLE', '1.5'))
RISK_REWARD = float(os.getenv('TAKE_PROFIT_R', '2.0'))
SCORE_STRONG = int(os.getenv('SCORE_STRONG', '90'))
SCORE_CANDIDATE = int(os.getenv('SCORE_CANDIDATE', '80'))
SCORE_WATCH = int(os.getenv('SCORE_WATCH', '70'))
# Conservative: matching a ticker does NOT verify the tracked equity.
# Unverified equity-like ticker contracts are always suppressed unless explicitly allowlisted.
MAX_CANDLE_AGE_MIN = int(os.getenv('MAX_CANDLE_AGE_MIN', '50'))
TZ = ZoneInfo('Asia/Taipei')
ALERT_COOLDOWN_MIN = max(0, int(os.getenv('ALERT_COOLDOWN_MIN', '120')))
SCORE_CHANGE_ALERT = max(1, int(os.getenv('SCORE_CHANGE_ALERT', '15')))
DAILY_REPORT_HOUR = max(0, min(23, int(os.getenv('DAILY_REPORT_HOUR_TW', '21'))))
BACKUP_DIR = Path(os.getenv('BACKUP_DIR', str(DB.parent / 'backups')))
BACKUP_KEEP_DAYS = max(1, int(os.getenv('BACKUP_KEEP_DAYS', '14')))
MAX_STOP_PCT = float(os.getenv('MAX_STOP_PCT', '5'))
MAX_RISK_PER_TRADE_PCT = float(os.getenv('MAX_RISK_PER_TRADE_PCT', '1'))
MAX_NOTIONAL_PCT = float(os.getenv('MAX_NOTIONAL_PCT', '100'))
SIM_CAPITAL_USDT = float(os.getenv('SIM_CAPITAL_USDT', '150'))
# Explicit allowlist of independently confirmed instrument IDs; symbol matching alone is NOT verification.
VERIFIED_EQUITY_CONTRACTS = set(x.strip().upper() for x in os.getenv('VERIFIED_EQUITY_CONTRACTS', '').split(',') if x.strip())



# Strict filters; all are research heuristics, not validated profit probabilities.
BREAKOUT_BUFFER_ATR = float(os.getenv('BREAKOUT_BUFFER_ATR', '0.10'))
MIN_BODY_RATIO = float(os.getenv('MIN_BODY_RATIO', '0.55'))
STRICT_MIN_VOLUME = float(os.getenv('STRICT_MIN_VOLUME', '1.8'))
MAX_EXTENSION_ATR = float(os.getenv('MAX_EXTENSION_ATR', '1.8'))
MAX_ENTRY_DRIFT_ATR = float(os.getenv('MAX_ENTRY_DRIFT_ATR', '0.5'))
MIN_ROOM_R = float(os.getenv('MIN_ROOM_R', '1.5'))
MAX_RSI_LONG = float(os.getenv('MAX_RSI_LONG', '72'))
MIN_RSI_SHORT = float(os.getenv('MIN_RSI_SHORT', '28'))
# Manual UTC time windows: YYYY-MM-DDTHH:MM/YYYY-MM-DDTHH:MM separated by commas.
# Keep empty if no independently verified calendar is maintained.
EVENT_BLACKOUT_UTC = os.getenv('EVENT_BLACKOUT_UTC', '')
EVENT_WINDOWS = []
for _window in EVENT_BLACKOUT_UTC.split(','):
    if not _window.strip(): continue
    try:
        _a, _b = _window.strip().split('/')
        _start = datetime.fromisoformat(_a.replace('Z','+00:00'))
        _end = datetime.fromisoformat(_b.replace('Z','+00:00'))
        if _start.tzinfo is None: _start = _start.replace(tzinfo=timezone.utc)
        if _end.tzinfo is None: _end = _end.replace(tzinfo=timezone.utc)
        if _end <= _start: raise ValueError('end before start')
        EVENT_WINDOWS.append((_start.astimezone(timezone.utc),_end.astimezone(timezone.utc)))
    except Exception as exc:
        raise ValueError(f'Invalid EVENT_BLACKOUT_UTC window: {_window!r}') from exc

def in_event_blackout(when):
    when=when.astimezone(timezone.utc)
    return any(a<=when<=b for a,b in EVENT_WINDOWS)

def strict_conditions(d15, d1h, d4h, d5m, row, past, direction, volatility, volume, rv, trend4, trend1, trend5):
    sign=1 if direction=='偏多' else -1
    candle_range=max(float(row.high-row.low),1e-12)
    body=abs(float(row.close-row.open))/candle_range
    boundary=float(past.high.max() if sign==1 else past.low.min())
    breakout=sign*(float(row.close)-boundary)>=BREAKOUT_BUFFER_ATR*volatility
    candle_color=sign*(float(row.close-row.open))>0
    ema20=float(d15.close.ewm(span=20,adjust=False).mean().iloc[-1])
    extension=sign*(float(row.close)-ema20)/volatility
    # Nearest previous swing resistance/support, excluding the 20-bar breakout window.
    older=d15.iloc[-81:-21]
    room_r=None
    if len(older)>=20:
        levels=older.high if sign==1 else older.low
        candidates=levels[levels>float(row.close)] if sign==1 else levels[levels<float(row.close)]
        if not candidates.empty:
            nearest=float(candidates.min() if sign==1 else candidates.max())
            room_r=sign*(nearest-float(row.close))/(ATR_SL*volatility)
    checks={
      '4H趨勢':bool(trend4), '1H動能':bool(trend1),'5M確認':bool(trend5),
      '有效突破':bool(breakout and candle_color and body>=MIN_BODY_RATIO),
      '放量':bool(volume>=STRICT_MIN_VOLUME),
      '避免追價':bool(-0.5<=extension<=MAX_EXTENSION_ATR),
      'RSI不極端':bool(rv<=MAX_RSI_LONG if sign==1 else rv>=MIN_RSI_SHORT),
      '前方空間':bool(room_r is None or room_r>=MIN_ROOM_R),
    }
    return checks,room_r,extension

class MarketError(Exception):
    pass

def request_json(base, path, params=None):
    last = None
    for attempt in range(RETRIES + 1):
        try:
            r = HTTP.get(base + path, params=params, timeout=TIMEOUT)
            if r.status_code in (408, 429, 500, 502, 503, 504):
                raise MarketError(f'HTTP {r.status_code}')
            r.raise_for_status()
            data = r.json()
            code = str(data.get('code', '0')) if isinstance(data, dict) else '0'
            if code not in ('0', '200', ''):
                raise MarketError(f'API code={code} msg={data.get("msg", "")}')
            return data
        except (requests.RequestException, ValueError, MarketError) as exc:
            last = exc
            if attempt == RETRIES:
                break
            time.sleep(min(12, 1.5 ** attempt + random.uniform(.2, .8)))
    raise MarketError(f'{base}{path}: {last}')

def okx_get(path, params=None):
    data = request_json(OKX, path, params)
    if not isinstance(data, dict) or str(data.get('code')) != '0' or not isinstance(data.get('data'), list):
        raise MarketError('Unexpected OKX response')
    return data['data']

def bingx_get(path, params=None):
    data = request_json(BINGX, path, params)
    if not isinstance(data, dict):
        raise MarketError('Unexpected BingX response')
    return data.get('data')

def spread_ok(bid, ask):
    if bid <= 0 or ask <= 0 or ask < bid:
        return False
    return 100 * (ask - bid) / ((ask + bid) / 2) <= float(os.getenv('MAX_SPREAD_PCT', '.15'))

def okx_universe():
    instruments = okx_get('/api/v5/public/instruments', {'instType': 'SWAP'})
    allowed = {x['instId'] for x in instruments if x.get('state') == 'live' and x.get('settleCcy') == 'USDT' and x.get('instId', '').endswith('-USDT-SWAP')}
    tickers = okx_get('/api/v5/market/tickers', {'instType': 'SWAP'})
    ranked = []
    for t in tickers:
        if t.get('instId') not in allowed:
            continue
        try:
            turnover = float(t.get('volCcy24h') or 0) * float(t.get('last') or 0)
            bid, ask = float(t.get('bidPx') or 0), float(t.get('askPx') or 0)
            if turnover > 0 and spread_ok(bid, ask):
                ranked.append((turnover, t['instId']))
        except (ValueError, TypeError):
            continue
    ranked.sort(reverse=True)
    if not ranked:
        raise MarketError('OKX returned no eligible swaps')
    return [s for _, s in ranked[:TOP_N]]

def bingx_universe():
    contracts = bingx_get('/openApi/swap/v2/quote/contracts')
    tickers = bingx_get('/openApi/swap/v2/quote/ticker')
    if isinstance(contracts, dict):
        contracts = [contracts]
    if isinstance(tickers, dict):
        tickers = [tickers]
    if not isinstance(contracts, list) or not isinstance(tickers, list):
        raise MarketError('Unexpected BingX universe')
    allowed = {x['symbol'] for x in contracts if x.get('symbol', '').endswith('-USDT') and str(x.get('status', '1')) in ('1', 'TRADING')}
    ranked = []
    for t in tickers:
        symbol = t.get('symbol')
        if symbol not in allowed:
            continue
        try:
            turnover = float(t.get('quoteVolume') or 0)
            if turnover > 0:
                ranked.append((turnover, symbol))
        except (ValueError, TypeError):
            continue
    ranked.sort(reverse=True)
    if not ranked:
        raise MarketError('BingX returned no eligible swaps')
    return [s for _, s in ranked[:TOP_N]]

def candles(venue, symbol, interval, limit=220):
    duration = {'5m':300000, '15m':900000, '1h':3600000, '4h':14400000}[interval]
    rows = []
    if venue == 'OKX':
        data = okx_get('/api/v5/market/history-candles', {'instId': symbol, 'bar': {'5m':'5m','15m':'15m','1h':'1H','4h':'4H'}[interval], 'limit': min(limit, 300)})
        for x in data:
            if len(x) < 9 or str(x[8]) != '1':
                continue
            start = int(x[0])
            rows.append([start, float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), float(x[7]), start + duration - 1])
    else:
        data = bingx_get('/openApi/swap/v3/quote/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})
        if not isinstance(data, list):
            raise MarketError('Unexpected BingX candles')
        for x in data:
            start = int(x['time'])
            close = float(x['close'])
            base_volume = float(x.get('volume') or 0)
            quote_volume = float(x.get('quoteVolume') or base_volume * close)
            rows.append([start, float(x['open']), float(x['high']), float(x['low']), close, base_volume, quote_volume, start + duration - 1])
    df = pd.DataFrame(rows, columns=['open_ms', 'open', 'high', 'low', 'close', 'volume', 'quote_volume', 'close_ms'])
    if df.empty:
        raise MarketError(f'No closed candles: {venue} {symbol}')
    df = df.drop_duplicates('open_ms').sort_values('open_ms').reset_index(drop=True)
    return df[df.close_ms < int(time.time() * 1000)].reset_index(drop=True)

def macd_hist(close):
    m = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    return m - m.ewm(span=9, adjust=False).mean()

def rsi(close, period=14):
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    ratio = up / down.replace(0, np.nan)
    return (100 - 100/(1 + ratio)).where(down != 0, 100).where(~((up == 0) & (down == 0)), 50)

def kdj(df):
    low, high = df.low.rolling(9).min(), df.high.rolling(9).max()
    rsv = (100 * (df.close - low) / (high - low).replace(0, np.nan)).fillna(50)
    k = rsv.ewm(alpha=1/3, adjust=False).mean()
    d = k.ewm(alpha=1/3, adjust=False).mean()
    return k, d, 3*k-2*d

def signal(d15, d1h):
    result = evaluate(d15, d1h)
    return result if result and result['grade'] == '強訊號' else None

def equity_universe(venue):
    """Match requested tickers to exact live USDT swaps; never invent contracts.

    Some tickers may represent ETFs, leveraged products or unrelated crypto tokens.
    A symbol match alone does NOT prove a contract tracks a US equity.
    """
    if not EQUITY_SYMBOLS:
        return [], list(EQUITY_SYMBOLS)
    if venue == 'OKX':
        instruments = okx_get('/api/v5/public/instruments', {'instType': 'SWAP'})
        live = [x['instId'] for x in instruments
                if x.get('state') == 'live' and x.get('settleCcy') == 'USDT'
                and x.get('instId', '').endswith('-USDT-SWAP')]
        template = lambda ticker: ticker + '-USDT-SWAP'
    else:
        contracts = bingx_get('/openApi/swap/v2/quote/contracts')
        if isinstance(contracts, dict):
            contracts = [contracts]
        live = [x['symbol'] for x in contracts
                if x.get('symbol', '').endswith('-USDT')
                and str(x.get('status', '1')) in ('1', 'TRADING')]
        template = lambda ticker: ticker + '-USDT'
    live = set(live)
    found = [(ticker, template(ticker)) for ticker in EQUITY_SYMBOLS if template(ticker) in live]
    missing = [ticker for ticker in EQUITY_SYMBOLS if template(ticker) not in live]
    for ticker in missing:
        LOG.info('WATCHLIST NOT LISTED on %s: %s', venue, ticker)
    for ticker, symbol in found:
        LOG.info('WATCHLIST SYMBOL FOUND on %s: %s -> %s (underlying unverified)', venue, ticker, symbol)
    return [symbol for _, symbol in found], missing

def atr(df, period=14):
    prev = df.close.shift(1)
    true_range = pd.concat([(df.high-df.low).abs(), (df.high-prev).abs(), (df.low-prev).abs()], axis=1).max(axis=1)
    return true_range.ewm(alpha=1/period, adjust=False, min_periods=period).mean()

def evaluate(d15, d1h, d4h=None, d5m=None, historical=False):
    """Closed-candle-only research score. No score is a probability of profit."""
    if d4h is None or d5m is None or min(len(d15),len(d1h),len(d4h),len(d5m)) < 40:
        return None
    row = d15.iloc[-1]
    now_ms = int(time.time()*1000)
    if not historical and now_ms - int(row.close_ms) > MAX_CANDLE_AGE_MIN*60000:
        raise MarketError('Stale 15m candle')
    past = d15.iloc[-21:-1]
    h = d1h[d1h.close_ms <= row.close_ms]
    four = d4h[d4h.close_ms <= row.close_ms]
    five = d5m[d5m.close_ms <= row.close_ms]
    if min(len(h),len(four),len(five)) < 35:
        return None
    base = float(past.quote_volume.median())
    volume = float(row.quote_volume/base) if base > 0 else 0
    rv = float(rsi(d15.close).iloc[-1]); k,d,j=kdj(d15)
    kval,dval,jval=map(float,(k.iloc[-1],d.iloc[-1],j.iloc[-1]))
    macd1 = float(macd_hist(h.close).iloc[-1])
    macd4 = float(macd_hist(four.close).iloc[-1])
    ma4 = float(four.close.ewm(span=20,adjust=False).mean().iloc[-1])
    ma5 = float(five.close.ewm(span=12,adjust=False).mean().iloc[-1])
    volatility = float(atr(d15).iloc[-1])
    if not all(np.isfinite(x) for x in (volume,rv,kval,dval,jval,macd1,macd4,ma4,ma5,volatility)) or volatility<=0:
        return None
    results=[]
    if in_event_blackout(datetime.fromtimestamp(int(row.close_ms)/1000,timezone.utc)):
        return None
    for direction, sign in [('偏多',1),('偏空',-1)]:
        breakout = row.close > past.high.max() if sign==1 else row.close < past.low.min()
        near = row.close >= past.high.max()-0.5*volatility if sign==1 else row.close <= past.low.min()+0.5*volatility
        trend4 = sign*(float(four.close.iloc[-1])-ma4)>0 and sign*macd4>0
        trend1 = sign*macd1>0
        trend5 = sign*(float(five.close.iloc[-1])-ma5)>0
        momentum = sign*(kval-dval)>0
        rsi_ok = rv>=52 if sign==1 else rv<=48
        volume_ok = volume>=1.5
        checks,room_r,extension = strict_conditions(d15,h,four,five,row,past,direction,volatility,volume,rv,trend4,trend1,trend5)
        # 25 trend + 25 breakout/volume + 20 MACD + 15 RSI + 10 KDJ + 5 liquidity proxy
        score = (15*trend4 + 10*trend5 + 15*breakout + 10*volume_ok + 20*trend1 + 15*rsi_ok + 10*momentum + 5*(base>0))
        # Strict mode: a high numerical score cannot override mandatory safety filters.
        mandatory=all(checks.values()) and momentum and rsi_ok
        if score>=SCORE_STRONG and mandatory:
            grade='強訊號'
        elif score>=SCORE_CANDIDATE and trend4 and trend1 and near:
            grade='候選訊號'
        elif score>=SCORE_WATCH and near:
            grade='觀察訊號'
        else:
            continue
        stop_distance=ATR_SL*volatility
        entry=float(row.close); stop=entry-sign*stop_distance
        if stop<=0 or 100*stop_distance/entry>MAX_STOP_PCT: continue
        target=entry+sign*stop_distance*RISK_REWARD
        missing=[name for name,passed in [('4H趨勢',trend4),('1H動能',trend1),('5M確認',trend5),('突破',breakout),('放量',volume_ok),('RSI',rsi_ok),('KDJ',momentum)] if not passed]
        results.append({'direction':direction,'grade':grade,'score':int(score),'missing':missing,'price':entry,
                        'stop':stop,'target':target,'atr':volatility,'volume':volume,'rsi':rv,'k':kval,'d':dval,
                        'j':jval,'macd':macd1,'checks':checks,'room_r':room_r,'extension_atr':extension,'candle':datetime.fromtimestamp(int(row.close_ms)/1000,timezone.utc).isoformat()})
    return max(results,key=lambda r:r['score']) if results else None

def save_observation(venue, symbol, result):
    with sqlite3.connect(DB, timeout=30) as c:
        cur = c.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?)',
                        (venue, symbol, result['direction'], result['grade'], result['candle'], result['price'], datetime.now(timezone.utc).isoformat()))
        return cur.rowcount > 0

def track_performance(venue, symbol, d15):
    """Retrospective close-to-close movement; not executable PnL or backtest win rate."""
    now_ms = int(time.time() * 1000)
    with sqlite3.connect(DB, timeout=30) as c:
        pending = c.execute('SELECT o.direction,o.grade,o.candle,o.price,p.hours FROM observations o CROSS JOIN (SELECT 1 hours UNION ALL SELECT 4 UNION ALL SELECT 24) p LEFT JOIN performance f ON f.venue=o.venue AND f.symbol=o.symbol AND f.direction=o.direction AND f.grade=o.grade AND f.candle=o.candle AND f.hours=p.hours WHERE o.venue=? AND o.symbol=? AND f.hours IS NULL AND o.recorded_utc>=?',
                            (venue, symbol, datetime.fromtimestamp(time.time()-7*86400, timezone.utc).isoformat())).fetchall()
        for direction, grade, candle, price, hours_ in pending:
            target_ms = int(datetime.fromisoformat(candle).timestamp()*1000) + hours_*3600000
            if target_ms > now_ms or price <= 0:
                continue
            later = d15[d15.close_ms >= target_ms]
            if later.empty:
                continue
            observed = float(later.iloc[0].close)
            signed_return = 100 * (observed/price-1) * (1 if direction == '偏多' else -1)
            c.execute('INSERT OR IGNORE INTO performance VALUES (?,?,?,?,?,?,?,?)',
                      (venue, symbol, direction, grade, candle, hours_, signed_return, datetime.now(timezone.utc).isoformat()))

def performance_summary():
    with sqlite3.connect(DB, timeout=30) as c:
        rows = c.execute('SELECT hours,COUNT(*),AVG(return_pct),SUM(CASE WHEN return_pct>0 THEN 1 ELSE 0 END) FROM performance WHERE grade=? GROUP BY hours ORDER BY hours', ('強訊號',)).fetchall()
    return '｜'.join(f'{h}h {n}筆/方向正確{wins/n:.0%}/均變動{avg:+.2f}%' for h,n,avg,wins in rows if n) or '尚無成熟樣本'

def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB, timeout=30) as c:
        c.execute('CREATE TABLE IF NOT EXISTS alerts (venue TEXT, symbol TEXT, direction TEXT, candle TEXT, sent_utc TEXT, PRIMARY KEY(venue,symbol,direction,candle))')
        c.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS observations (venue TEXT, symbol TEXT, direction TEXT, grade TEXT, candle TEXT, price REAL, recorded_utc TEXT, PRIMARY KEY(venue,symbol,direction,grade,candle))')
        c.execute('CREATE TABLE IF NOT EXISTS performance (venue TEXT, symbol TEXT, direction TEXT, grade TEXT, candle TEXT, hours INTEGER, return_pct REAL, checked_utc TEXT, PRIMARY KEY(venue,symbol,direction,grade,candle,hours))')
        c.execute('CREATE TABLE IF NOT EXISTS alert_state (venue TEXT,symbol TEXT,direction TEXT,grade TEXT,score INTEGER,active INTEGER,last_sent_utc TEXT,updated_utc TEXT,PRIMARY KEY(venue,symbol,direction))')
        c.execute('CREATE TABLE IF NOT EXISTS v5_signals (venue TEXT,symbol TEXT,direction TEXT,grade TEXT,candle TEXT,entry REAL,stop REAL,target REAL,score INTEGER,created_utc TEXT,outcome TEXT,net_pct REAL,settled_utc TEXT,PRIMARY KEY(venue,symbol,direction,grade,candle))')

def get_state(key):
    with sqlite3.connect(DB, timeout=30) as c:
        row = c.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return row[0] if row else None

def set_state(key, value):
    with sqlite3.connect(DB, timeout=30) as c:
        c.execute('INSERT OR REPLACE INTO state (key,value) VALUES (?,?)', (key, str(value)))

def telegram(message):
    token, chat = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID')
    payload = json.dumps({'chat_id': chat, 'text': message}, ensure_ascii=True).encode('ascii')
    r = HTTP.post(f'https://api.telegram.org/bot{token}/sendMessage', data=payload, headers={'Content-Type': 'application/json; charset=utf-8'}, timeout=TIMEOUT)
    r.raise_for_status()
    result = r.json()
    if not result.get('ok'):
        raise RuntimeError(f'Telegram error: {result.get("description")}')

def tw_time(iso):
    return datetime.fromisoformat(iso).astimezone(TZ).strftime('%Y/%m/%d %H:%M')

def fmt_price(v):
    return f'{v:,.2f}' if abs(v)>=10 else f'{v:,.5f}'.rstrip('0').rstrip('.')

def notify(venue,symbol,row):
    """Only first appearance, material score rise, upgrade, direction change or expired cooldown.
    Update state only after Telegram confirms success; scans continue every 15 minutes.
    """
    now=datetime.now(timezone.utc)
    with sqlite3.connect(DB,timeout=30) as c:
        prev=c.execute('SELECT grade,score,active,last_sent_utc FROM alert_state WHERE venue=? AND symbol=? AND direction=?',
                       (venue,symbol,row['direction'])).fetchone()
        reason='首次發現'
        if prev and prev[2]:
            old_grade,old_score,_,last_sent=prev
            elapsed=(now-datetime.fromisoformat(last_sent)).total_seconds()/60 if last_sent else 1e9
            if row['grade']=='強訊號' and old_grade!='強訊號': reason='升級強訊號'
            elif row['score']-old_score>=SCORE_CHANGE_ALERT: reason=f'評分增強 {old_score}→{row["score"]}'
            elif elapsed>=ALERT_COOLDOWN_MIN and row['grade']=='強訊號': reason='強訊號持續'
            else:
                # Persist latest grade/score, not a new alert; retain last sent timestamp.
                c.execute('UPDATE alert_state SET grade=?,score=?,updated_utc=? WHERE venue=? AND symbol=? AND direction=?',
                          (row['grade'],row['score'],now.isoformat(),venue,symbol,row['direction']))
                return False
        checks=row.get('checks',{})
        passed=sum(bool(x) for x in checks.values())
        if checks and passed==len(checks): confirmation='全部通過'
        elif passed: confirmation=f'部分通過 {passed}/{len(checks)}'
        else: confirmation='未通過'
        missing=', '.join(k for k,v in checks.items() if not v)
        tp1=row['price']+(row['target']-row['price'])*(1.5/RISK_REWARD)
        message=(f'🔔 BinanceWin V5.2｜{reason}｜{row["direction"]} {row["grade"]} {row["score"]}/100\n'
                 f'{venue}｜{symbol}｜{tw_time(row["candle"])}\n'
                 f'進場參考 {fmt_price(row["price"])}｜SL {fmt_price(row["stop"])}\n'
                 f'TP1 {fmt_price(tp1)}｜TP2 {fmt_price(row["target"])}\n'
                 f'量 {row["volume"]:.2f}×｜RSI {row["rsi"]:.1f}｜KDJ {row["k"]:.0f}/{row["d"]:.0f}\n'
                 f'確認狀態：{confirmation}'+(f'（未過：{missing}）' if missing else '')+'\n'
                 '研究訊號｜非實際成交｜不自動下單。')
        telegram(message)
        c.execute('INSERT INTO alert_state (venue,symbol,direction,grade,score,active,last_sent_utc,updated_utc) VALUES (?,?,?,?,?,1,?,?) '
                  'ON CONFLICT(venue,symbol,direction) DO UPDATE SET grade=excluded.grade,score=excluded.score,active=1,last_sent_utc=excluded.last_sent_utc,updated_utc=excluded.updated_utc',
                  (venue,symbol,row['direction'],row['grade'],row['score'],now.isoformat(),now.isoformat()))
        c.execute('INSERT OR IGNORE INTO alerts VALUES (?,?,?,?,?)',(venue,symbol,row['direction']+'/'+row['grade'],row['candle'],now.isoformat()))
        return True

def expire_alerts(seen):
    """Deactivate vanished observations, without spamming per-symbol cancellation alerts."""
    now=datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB,timeout=30) as c:
        rows=c.execute('SELECT venue,symbol,direction FROM alert_state WHERE active=1').fetchall()
        for venue,symbol,direction in rows:
            if (venue,symbol,direction) not in seen:
                c.execute('UPDATE alert_state SET active=0,updated_utc=? WHERE venue=? AND symbol=? AND direction=?',
                          (now,venue,symbol,direction))


def record_v5_signal(venue,symbol,result):
    with sqlite3.connect(DB,timeout=30) as c:
        c.execute('INSERT OR IGNORE INTO v5_signals (venue,symbol,direction,grade,candle,entry,stop,target,score,created_utc) VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (venue,symbol,result['direction'],result['grade'],result['candle'],result['price'],result['stop'],result['target'],result['score'],datetime.now(timezone.utc).isoformat()))

def settle_v5(venue,symbol,d15):
    """Conservative same-bar TP/SL tie: SL first. Only bars after signal close."""
    with sqlite3.connect(DB,timeout=30) as c:
        pending=c.execute('SELECT direction,grade,candle,entry,stop,target FROM v5_signals WHERE venue=? AND symbol=? AND outcome IS NULL AND created_utc>=?',
                          (venue,symbol,datetime.fromtimestamp(time.time()-8*86400,timezone.utc).isoformat())).fetchall()
        for direction,grade,candle,entry,stop,target in pending:
            start_ms=int(datetime.fromisoformat(candle).timestamp()*1000)
            future=d15[(d15.close_ms>start_ms) & (d15.close_ms<=start_ms+24*3600000)]
            if future.empty: continue
            outcome=None; exit_price=None
            for bar in future.itertuples():
                hit_sl=bar.low<=stop if direction=='偏多' else bar.high>=stop
                hit_tp=bar.high>=target if direction=='偏多' else bar.low<=target
                if hit_sl: outcome='SL';exit_price=stop;break
                if hit_tp: outcome='TP';exit_price=target;break
            if outcome is None and int(future.iloc[-1].close_ms)-start_ms>=24*3600000-60000:
                outcome='24H';exit_price=float(future.iloc[-1].close)
            if outcome:
                sign=1 if direction=='偏多' else -1
                pnl=100*sign*(exit_price/entry-1)-(FEE_BPS+SLIPPAGE_BPS)/100
                c.execute('UPDATE v5_signals SET outcome=?,net_pct=?,settled_utc=? WHERE venue=? AND symbol=? AND direction=? AND grade=? AND candle=?',
                          (outcome,pnl,datetime.now(timezone.utc).isoformat(),venue,symbol,direction,grade,candle))

def v5_summary():
    with sqlite3.connect(DB,timeout=30) as c:
        rows=c.execute("SELECT outcome,net_pct FROM v5_signals WHERE outcome IS NOT NULL AND grade='強訊號'").fetchall()
    if not rows:return '尚無已結算強訊號（非實際交易）'
    n=len(rows); wins=sum(p>0 for _,p in rows); avg=sum(p for _,p in rows)/n
    tp=sum(o=='TP' for o,_ in rows);sl=sum(o=='SL' for o,_ in rows)
    return f'{n}筆｜淨報酬為正 {wins/n:.0%}｜平均 {avg:+.2f}%｜TP {tp}／SL {sl}（模擬）'

def live_price(venue,symbol):
    if venue=='OKX':
        rows=okx_get('/api/v5/market/ticker',{'instId':symbol})
        return float(rows[0]['last'])
    data=bingx_get('/openApi/swap/v2/quote/ticker',{'symbol':symbol})
    if isinstance(data,list):data=data[0]
    return float(data['lastPrice'])

def entry_price_safe(venue,symbol,result):
    price=live_price(venue,symbol)
    if price<=0 or not np.isfinite(price):return False
    return abs(price-result['price'])<=MAX_ENTRY_DRIFT_ATR*result['atr']

def scan_once():
    errors=[];venue=None;symbols=[]
    for name,probe in [('OKX',okx_universe),('BingX',bingx_universe)]:
        try:
            symbols=probe()
            if len(candles(name,symbols[0],'15m',60))<40:raise MarketError('Insufficient probe candles')
            venue=name;break
        except Exception as exc:
            errors.append(f'{name}: {str(exc)[:100]}');LOG.warning('%s unavailable: %s',name,exc)
    stats={'venue':venue or 'NONE','attempted':0,'successful':0,'crypto_listed':len(symbols),'equity_matched':0,
           'equity_missing':EQUITY_SYMBOLS[:],'equity_verified':0,'equity_unverified':[],
           'candidates':0,'watch':0,'sent':0,'errors':0,'notes':errors}
    if not venue:return stats
    try:
        extras,missing=equity_universe(venue)
        stats['equity_matched']=len(extras);stats['equity_missing']=missing
        stats['equity_unverified']=[x for x in EQUITY_SYMBOLS if x not in missing and not any(sym.startswith(x+'-') for sym in VERIFIED_EQUITY_CONTRACTS)]
        stats['equity_verified']=len(extras)-len(stats['equity_unverified'])
        # The exchange instrument name alone is insufficient proof of equity underlying.
        symbols=list(dict.fromkeys(symbols+extras))
    except Exception as exc:
        stats['notes'].append(f'監控名單查詢失敗: {str(exc)[:80]}')
    watch_sent=0
    seen=set()
    evaluated=set()
    for symbol in symbols:
        stats['attempted']+=1
        try:
            d15=candles(venue,symbol,'15m',220)
            d1=candles(venue,symbol,'1h',110)
            d4=candles(venue,symbol,'4h',110)
            d5=candles(venue,symbol,'5m',110)
            if min(len(d15),len(d1),len(d4),len(d5))<40:raise MarketError('Insufficient history')
            stats['successful']+=1
            settle_v5(venue,symbol,d15)
            # Continue legacy tracking for existing V4.5.1 observations.
            track_performance(venue,symbol,d15)
            result=evaluate(d15,d1,d4,d5)
            evaluated.add((venue,symbol))
            if not result:continue
            is_equity_name=symbol.split('-')[0] in EQUITY_SYMBOLS
            if is_equity_name and symbol not in VERIFIED_EQUITY_CONTRACTS:
                LOG.info('EQUITY UNVERIFIED suppressed alert: %s %s score=%s',symbol,result['direction'],result['score'])
                continue
            seen.add((venue,symbol,result['direction']))
            if result['grade']=='強訊號':stats['candidates']+=1
            else:stats['watch']+=1
            record_v5_signal(venue,symbol,result)
            if result['grade']=='強訊號' or (WATCH_ALERTS and watch_sent<WATCH_MAX_PER_SCAN):
                if not entry_price_safe(venue,symbol,result):
                    LOG.info('ENTRY DRIFT filtered: %s',symbol)
                    continue
                if notify(venue,symbol,result):
                    stats['sent']+=1
                    if result['grade']!='強訊號':watch_sent+=1
        except Exception as exc:
            stats['errors']+=1
            LOG.warning('%s %s failed: %s',venue,symbol,exc)
    if evaluated:
        with sqlite3.connect(DB,timeout=30) as c:
            for v,sym,direction in c.execute('SELECT venue,symbol,direction FROM alert_state WHERE active=1').fetchall():
                if (v,sym) in evaluated and (v,sym,direction) not in seen:
                    c.execute('UPDATE alert_state SET active=0,updated_utc=? WHERE venue=? AND symbol=? AND direction=?',
                              (datetime.now(timezone.utc).isoformat(),v,sym,direction))
    LOG.info('Scan complete: %s',json.dumps(stats,ensure_ascii=False))
    return stats

def status_message(stats,last_error=None):
    now=datetime.now(TZ).strftime('%Y/%m/%d %H:%M 台灣時間')
    rate=100*stats['successful']/stats['attempted'] if stats['attempted'] else 0
    return (f'📡 BinanceWin V5.2 每小時狀態\n時間：{now}\n行情來源：{stats["venue"]}\n'
            f'去重後掃描：{stats["successful"]}/{stats["attempted"]}（{rate:.0f}%）\n'
            f'加密熱門名單：{stats.get("crypto_listed", 0)}｜指定代號匹配：{stats.get("equity_matched", 0)}/{len(EQUITY_SYMBOLS)}\n'
            f'未匹配：{", ".join(stats.get("equity_missing", [])) or "無"}\n'
            f'已人工核准合約：{stats.get("equity_verified",0)}｜未驗證：{", ".join(stats.get("equity_unverified", [])) or "無"}\n'
            f'強訊號：{stats["candidates"]}｜候選/觀察：{stats["watch"]}｜已發通知：{stats["sent"]}\n'
            f'單幣錯誤：{stats["errors"]}｜異常：{last_error or "; ".join(stats["notes"]) or "無"}\n'
            f'歷史模擬（假設成交）：{v5_summary()}\n僅供研究，不自動下單。')

def backup_database():
    """Consistent SQLite backup on same persistent mount. Local backup is not off-site."""
    BACKUP_DIR.mkdir(parents=True,exist_ok=True)
    day=datetime.now(TZ).strftime('%Y-%m-%d')
    dest=BACKUP_DIR / f'scanner-{day}.sqlite3'
    if not dest.exists():
        with sqlite3.connect(DB,timeout=30) as src, sqlite3.connect(dest) as dst:
            src.backup(dst)
        LOG.info('SQLite backup created: %s',dest)
    cutoff=datetime.now(TZ).date()-timedelta(days=BACKUP_KEEP_DAYS)
    for old in BACKUP_DIR.glob('scanner-????-??-??.sqlite3'):
        try:
            if datetime.strptime(old.stem[8:],'%Y-%m-%d').date()<cutoff: old.unlink()
        except ValueError: pass

def daily_report():
    """Summary of stored simulated signals, not real filled trades."""
    since=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
    with sqlite3.connect(DB,timeout=30) as c:
        new=c.execute('SELECT grade,COUNT(*) FROM v5_signals WHERE created_utc>=? GROUP BY grade',(since,)).fetchall()
        closed=c.execute('SELECT outcome,net_pct FROM v5_signals WHERE settled_utc>=? AND outcome IS NOT NULL',(since,)).fetchall()
        pending=c.execute('SELECT COUNT(*) FROM v5_signals WHERE outcome IS NULL').fetchone()[0]
    counts=dict(new)
    if closed:
        win=sum(p>0 for _,p in closed)/len(closed)
        avg=sum(p for _,p in closed)/len(closed)
        result=f'{len(closed)} 筆｜淨正報酬比例 {win:.1%}｜平均 {avg:+.2f}%'
    else: result='尚無已結算訊號'
    return (f'📊 BinanceWin V5.2 每日模擬報告｜{datetime.now(TZ):%Y/%m/%d}\n'
            f'近24小時：強訊號 {counts.get("強訊號",0)}｜候選 {counts.get("候選訊號",0)}｜觀察 {counts.get("觀察訊號",0)}\n'
            f'近24小時結算：{result}\n未結算：{pending} 筆\n'
            f'歷史強訊號：{v5_summary()}\n模擬不等於實盤，無自動下單。')

def due_scan_slot(now):
    """Most recent 15-minute boundary + offset, expressed as integer slot."""
    return int((now - SCAN_OFFSET) // 900)

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    init_db()
    LOG.info('DB_PATH=%s persistent_mount_check=%s',DB,DB.parent == Path('/data'))
    if os.getenv('TEST_TELEGRAM', '0') == '1':
        telegram('\u2705 BinanceWin V5.2 Telegram \u6e2c\u8a66\u6210\u529f\uff1b\u4e0d\u4ee3\u8868\u884c\u60c5\u6383\u63cf\u6210\u529f\u3002')
        return
    if os.getenv('ONCE', '0') == '1':
        stats = scan_once()
        telegram(status_message(stats))
        return
    LOG.info('BinanceWin V5.2 started; OKX primary, BingX fallback, TOP_N=%s, equity_watch=%s', TOP_N, ','.join(EQUITY_SYMBOLS))
    last_stats = {'venue': 'WAITING', 'attempted': 0, 'successful': 0, 'candidates': 0, 'watch': 0, 'crypto_listed': 0, 'equity_matched': 0, 'equity_missing': EQUITY_SYMBOLS[:], 'equity_unverified': [], 'sent': 0, 'errors': 0, 'notes': []}
    last_error = None
    while True:
        now = time.time()
        slot = due_scan_slot(now)
        previous = get_state('last_scan_slot')
        if previous != str(slot):
            # Record the slot before scanning: avoids duplicate cycles on restarts.
            set_state('last_scan_slot', slot)
            try:
                last_stats = scan_once()
                last_error = None if last_stats['successful'] > 0 else '\u672c\u8f2a\u6c92\u6709\u6210\u529f\u53d6\u5f97\u884c\u60c5'
                set_state('last_stats', json.dumps(last_stats, ensure_ascii=False))
            except Exception as exc:
                last_error = str(exc)[:200]
                LOG.exception('Scan cycle failed')
        else:
            saved = get_state('last_stats')
            if saved:
                try:
                    last_stats = json.loads(saved)
                except ValueError:
                    pass
        # Backup once per local day; do not report success until SQLite backup finishes.
        today=datetime.now(TZ).strftime('%Y-%m-%d')
        if get_state('last_backup_day')!=today:
            try:
                backup_database()
                set_state('last_backup_day',today)
            except Exception: LOG.exception('SQLite backup failed')
        local=datetime.now(TZ)
        if local.hour>=DAILY_REPORT_HOUR and get_state('last_daily_report_day')!=today:
            try:
                telegram(daily_report())
                set_state('last_daily_report_day',today)
            except Exception: LOG.exception('Daily report failed; will retry')
        # Heartbeat on UTC hour, rather than 3600s after container boot.
        hour = int(time.time() // 3600)
        if get_state('last_heartbeat_hour') != str(hour):
            try:
                telegram(status_message(last_stats, last_error))
                set_state('last_heartbeat_hour', hour)
                LOG.info('Hourly status sent for UTC hour %s', hour)
            except Exception:
                LOG.exception('Hourly status failed; retrying later')
        # Avoid sleeping across a scan boundary. Cap sleep for failed Telegram retries.
        next_boundary = (int((time.time() - SCAN_OFFSET) // 900) + 1) * 900 + SCAN_OFFSET
        time.sleep(max(1, min(30, next_boundary - time.time())))

if __name__ == '__main__':
    main()
