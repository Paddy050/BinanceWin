"""BinanceWin V4.5.1 - public futures market research alerts; never places orders."""
import os
import time
import json
import random
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd

LOG = logging.getLogger('binancewin')
HTTP = requests.Session()
HTTP.headers.update({'User-Agent': 'BinanceWinResearch/4.5.1'})
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
WATCH_ALERTS = os.getenv('WATCH_ALERTS', '1') == '1'
WATCH_MIN_VOLUME = float(os.getenv('WATCH_MIN_VOLUME', '1.2'))
WATCH_MAX_PER_SCAN = max(0, min(10, int(os.getenv('WATCH_MAX_PER_SCAN', '3'))))


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
    duration = 900000 if interval == '15m' else 3600000
    rows = []
    if venue == 'OKX':
        data = okx_get('/api/v5/market/history-candles', {'instId': symbol, 'bar': '15m' if interval == '15m' else '1H', 'limit': min(limit, 300)})
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

def evaluate(d15, d1h):
    if len(d15) < 45 or len(d1h) < 40:
        return None
    row = d15.iloc[-1]
    past = d15.iloc[-21:-1]
    baseline = past.quote_volume.median()
    multiple = float(row.quote_volume / baseline) if baseline > 0 else 0
    h = d1h[d1h.close_ms <= row.close_ms]
    if len(h) < 40:
        return None
    hist = float(macd_hist(h.close).iloc[-1])
    rv = float(rsi(d15.close).iloc[-1])
    k, d, j = kdj(d15)
    kval, dval, jval = float(k.iloc[-1]), float(d.iloc[-1]), float(j.iloc[-1])
    if not all(np.isfinite(v) for v in (hist, rv, kval, dval, jval, multiple)):
        return None
    required_kdj = os.getenv('REQUIRE_KDJ', 'true').lower() == 'true'
    choices = []
    for direction, breakout, trend, rsi_ok, kdj_ok in [
        ('偏多', bool(row.close > past.high.max()), hist > 0, rv >= float(os.getenv('RSI_LONG', '52')), kval > dval),
        ('偏空', bool(row.close < past.low.min()), hist < 0, rv <= float(os.getenv('RSI_SHORT', '48')), kval < dval),
    ]:
        checks = {'突破': breakout, 'MACD': trend, 'RSI': rsi_ok, 'KDJ': kdj_ok if required_kdj else True, '成交量': multiple >= float(os.getenv('MIN_VOLUME_MULTIPLE', '1.5'))}
        misses = [name for name, passed in checks.items() if not passed]
        if not misses:
            grade = '強訊號'
        elif WATCH_ALERTS and len(misses) == 1 and multiple >= WATCH_MIN_VOLUME:
            grade = '觀察訊號'
        else:
            continue
        choices.append({'direction': direction, 'grade': grade, 'missing': misses, 'price': float(row.close), 'volume': multiple,
                        'rsi': rv, 'k': kval, 'd': dval, 'j': jval, 'macd': hist,
                        'candle': datetime.fromtimestamp(int(row.close_ms)/1000, timezone.utc).isoformat()})
    return next((x for x in choices if x['grade'] == '強訊號'), choices[0] if choices else None)

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

def notify(venue, symbol, row):
    key = (venue, symbol, row['direction'] + '/' + row['grade'], row['candle'])
    with sqlite3.connect(DB, timeout=30) as c:
        if c.execute('SELECT 1 FROM alerts WHERE venue=? AND symbol=? AND direction=? AND candle=?', key).fetchone():
            return False
        message = (f'\U0001f514 BinanceWin V4.5.1\uff5c{row["direction"]}\n'
                   f'\u4ea4\u6613\u6240\uff1a{venue}\uff5c\u5408\u7d04\uff1a{symbol}\n'
                   f'15m \u6536\u76e4 UTC\uff1a{row["candle"]}\n'
                   f'\u6536\u76e4\u50f9\uff1a{row["price"]:.9g} USDT\n'
                   f'\u653e\u91cf\uff1a{row["volume"]:.2f}\u00d7\n'
                   f'RSI\uff1a{row["rsi"]:.1f}\uff5cKDJ\uff1a{row["k"]:.1f}/{row["d"]:.1f}/{row["j"]:.1f}\n'
                   f'1h MACD \u67f1\uff1a{row["macd"]:.7g}\n'
                   '\u7814\u7a76\u8a0a\u865f\uff0c\u975e\u4ea4\u6613\u5efa\u8b70\uff1b\u4e0d\u81ea\u52d5\u4e0b\u55ae\u3002')
        telegram(message)
        c.execute('INSERT INTO alerts VALUES (?,?,?,?,?)', (*key, datetime.now(timezone.utc).isoformat()))
        return True

def scan_once():
    errors = []
    venue, symbols = None, None
    for name, probe in [('OKX', okx_universe), ('BingX', bingx_universe)]:
        try:
            symbols = probe()
            if len(candles(name, symbols[0], '15m', 60)) < 40:
                raise MarketError('Candle probe returned insufficient history')
            venue = name
            break
        except Exception as exc:
            LOG.error('%s unavailable: %s', name, exc)
            errors.append(f'{name}: {str(exc)[:120]}')
    if not venue:
        stats = {'venue': 'NONE', 'attempted': 0, 'successful': 0, 'candidates': 0, 'watch': 0, 'equity': 0, 'equity_missing': [], 'sent': 0, 'errors': len(errors), 'notes': errors}
        LOG.error('Scan complete: %s', json.dumps(stats, ensure_ascii=False))
        return stats
    stats = {'venue': venue, 'attempted': 0, 'successful': 0, 'candidates': 0, 'watch': 0, 'equity': 0, 'equity_missing': [], 'sent': 0, 'errors': 0, 'notes': errors}
    # Add an explicit equity-linked watchlist separately from crypto TOP_N.
    try:
        extra, missing = equity_universe(venue)
        stats['equity'] = len(extra)
        stats['equity_missing'] = missing
        symbols = list(dict.fromkeys(symbols + extra))
    except Exception as exc:
        LOG.warning('Equity watchlist discovery failed: %s', exc)
        stats['notes'].append('美股相關合約清單查詢失敗')
        stats['equity_missing'] = EQUITY_SYMBOLS[:]
    watch_sent = 0
    for symbol in symbols:
        stats['attempted'] += 1
        try:
            d15 = candles(venue, symbol, '15m', 220)
            d1h = candles(venue, symbol, '1h', 110)
            if len(d15) < 45 or len(d1h) < 40:
                raise MarketError('Insufficient candle history')
            stats['successful'] += 1
            track_performance(venue, symbol, d15)
            result = evaluate(d15, d1h)
            if result:
                if result['grade'] == '強訊號':
                    stats['candidates'] += 1
                else:
                    stats['watch'] += 1
                if save_observation(venue, symbol, result):
                    if result['grade'] == '強訊號' or watch_sent < WATCH_MAX_PER_SCAN:
                        if notify(venue, symbol, result):
                            stats['sent'] += 1
                            if result['grade'] == '觀察訊號':
                                watch_sent += 1
        except Exception as exc:
            stats['errors'] += 1
            LOG.warning('%s %s failed: %s', venue, symbol, exc)
    LOG.info('Scan complete: %s', json.dumps(stats, ensure_ascii=False))
    return stats

def status_message(stats, last_error=None):
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    rate = 100 * stats['successful'] / stats['attempted'] if stats['attempted'] else 0
    notes = '; '.join(stats.get('notes', []))[:250] or '\u7121'
    equity_line = f'\n指定合約匹配：{stats.get("equity", 0)}/{len(EQUITY_SYMBOLS)}；未匹配：{", ".join(stats.get("equity_missing", [])) or "無"}'
    return (f'\U0001f4e1 BinanceWin V4.5.1 \u6bcf\u5c0f\u6642\u72c0\u614b\n\u6642\u9593\uff1a{now}\n'
            f'\u884c\u60c5\u4f86\u6e90\uff1a{stats["venue"]}\n\u6383\u63cf\uff1a{stats["successful"]}/{stats["attempted"]}\uff5c\u6210\u529f\u7387\uff1a{rate:.0f}%\n'
            f'\u5019\u9078\u8a0a\u865f\uff1a{stats["candidates"]}\uff5c\u65b0\u901a\u77e5\uff1a{stats["sent"]}\n'
            f'\u55ae\u5e63\u932f\u8aa4\uff1a{stats["errors"]}\n\u4f86\u6e90\u5207\u63db\uff0f\u9023\u7dda\uff1a{notes}\n'
            f'{equity_line}\n\u6700\u65b0\u7570\u5e38\uff1a{last_error or "\u7121"}\n\u50c5\u4f9b\u7814\u7a76\uff0c\u4e0d\u81ea\u52d5\u4e0b\u55ae\u3002')

def due_scan_slot(now):
    """Most recent 15-minute boundary + offset, expressed as integer slot."""
    return int((now - SCAN_OFFSET) // 900)

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    init_db()
    if os.getenv('TEST_TELEGRAM', '0') == '1':
        telegram('\u2705 BinanceWin V4.5.1 Telegram \u6e2c\u8a66\u6210\u529f\uff1b\u4e0d\u4ee3\u8868\u884c\u60c5\u6383\u63cf\u6210\u529f\u3002')
        return
    if os.getenv('ONCE', '0') == '1':
        stats = scan_once()
        telegram(status_message(stats))
        return
    LOG.info('BinanceWin V4.5.1 started; OKX primary, BingX fallback, TOP_N=%s, equity_watch=%s', TOP_N, ','.join(EQUITY_SYMBOLS))
    last_stats = {'venue': 'WAITING', 'attempted': 0, 'successful': 0, 'candidates': 0, 'watch': 0, 'equity': 0, 'equity_missing': [], 'sent': 0, 'errors': 0, 'notes': []}
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
