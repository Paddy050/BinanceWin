"""BinanceWin V4.4 - public futures market research alerts; never places orders."""
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
HTTP.headers.update({'User-Agent': 'BinanceWinResearch/4.4'})
DB = Path(os.getenv('DB_PATH', str(Path(__file__).with_name('scanner.sqlite3'))))
TIMEOUT = float(os.getenv('HTTP_TIMEOUT', '12'))
TOP_N = max(1, min(100, int(os.getenv('TOP_N', '30'))))
RETRIES = max(0, min(5, int(os.getenv('API_RETRIES', '2'))))
OKX = 'https://www.okx.com'
BINGX = 'https://open-api.bingx.com'
SCAN_OFFSET = 35

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
    if not all(np.isfinite(v) for v in (hist, rv, kval, dval, jval)):
        return None
    if multiple < float(os.getenv('MIN_VOLUME_MULTIPLE', '1.5')):
        return None
    long_ok = row.close > past.high.max() and hist > 0 and rv >= float(os.getenv('RSI_LONG', '52'))
    short_ok = row.close < past.low.min() and hist < 0 and rv <= float(os.getenv('RSI_SHORT', '48'))
    if os.getenv('REQUIRE_KDJ', 'true').lower() == 'true':
        long_ok = long_ok and kval > dval
        short_ok = short_ok and kval < dval
    direction = 'åå¤åé¸' if long_ok else 'åç©ºåé¸' if short_ok else None
    if not direction:
        return None
    return {'direction': direction, 'price': float(row.close), 'volume': multiple,
            'rsi': rv, 'k': kval, 'd': dval, 'j': jval, 'macd': hist,
            'candle': datetime.fromtimestamp(int(row.close_ms)/1000, timezone.utc).isoformat()}

def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB, timeout=30) as c:
        c.execute('CREATE TABLE IF NOT EXISTS alerts (venue TEXT, symbol TEXT, direction TEXT, candle TEXT, sent_utc TEXT, PRIMARY KEY(venue,symbol,direction,candle))')
        c.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

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
    r = HTTP.post(f'https://api.telegram.org/bot{token}/sendMessage', json={'chat_id': chat, 'text': message}, timeout=TIMEOUT)
    r.raise_for_status()
    result = r.json()
    if not result.get('ok'):
        raise RuntimeError(f'Telegram error: {result.get("description")}')

def notify(venue, symbol, row):
    key = (venue, symbol, row['direction'], row['candle'])
    with sqlite3.connect(DB, timeout=30) as c:
        if c.execute('SELECT 1 FROM alerts WHERE venue=? AND symbol=? AND direction=? AND candle=?', key).fetchone():
            return False
        message = (f'ð BinanceWin V4.4ï½{row["direction"]}\n'
                   f'äº¤ææï¼{venue}ï½åç´ï¼{symbol}\n'
                   f'15m æ¶ç¤ UTCï¼{row["candle"]}\n'
                   f'æ¶ç¤å¹ï¼{row["price"]:.9g} USDT\n'
                   f'æ¾éï¼{row["volume"]:.2f}Ã\n'
                   f'RSIï¼{row["rsi"]:.1f}ï½KDJï¼{row["k"]:.1f}/{row["d"]:.1f}/{row["j"]:.1f}\n'
                   f'1h MACD æ±ï¼{row["macd"]:.7g}\n'
                   'ç ç©¶è¨èï¼éäº¤æå»ºè­°ï¼ä¸èªåä¸å®ã')
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
        stats = {'venue': 'NONE', 'attempted': 0, 'successful': 0, 'candidates': 0, 'sent': 0, 'errors': len(errors), 'notes': errors}
        LOG.error('Scan complete: %s', json.dumps(stats, ensure_ascii=False))
        return stats
    stats = {'venue': venue, 'attempted': 0, 'successful': 0, 'candidates': 0, 'sent': 0, 'errors': 0, 'notes': errors}
    for symbol in symbols:
        stats['attempted'] += 1
        try:
            d15 = candles(venue, symbol, '15m', 220)
            d1h = candles(venue, symbol, '1h', 110)
            if len(d15) < 45 or len(d1h) < 40:
                raise MarketError('Insufficient candle history')
            stats['successful'] += 1
            result = signal(d15, d1h)
            if result:
                stats['candidates'] += 1
                if notify(venue, symbol, result):
                    stats['sent'] += 1
        except Exception as exc:
            stats['errors'] += 1
            LOG.warning('%s %s failed: %s', venue, symbol, exc)
    LOG.info('Scan complete: %s', json.dumps(stats, ensure_ascii=False))
    return stats

def status_message(stats, last_error=None):
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    rate = 100 * stats['successful'] / stats['attempted'] if stats['attempted'] else 0
    notes = '; '.join(stats.get('notes', []))[:250] or 'ç¡'
    return (f'ð¡ BinanceWin V4.4 æ¯å°æçæ\næéï¼{now}\n'
            f'è¡æä¾æºï¼{stats["venue"]}\nææï¼{stats["successful"]}/{stats["attempted"]}ï½æåçï¼{rate:.0f}%\n'
            f'åé¸è¨èï¼{stats["candidates"]}ï½æ°éç¥ï¼{stats["sent"]}\n'
            f'å®å¹£é¯èª¤ï¼{stats["errors"]}\nä¾æºåæï¼é£ç·ï¼{notes}\n'
            f'ææ°ç°å¸¸ï¼{last_error or "ç¡"}\nåä¾ç ç©¶ï¼ä¸èªåä¸å®ã')

def due_scan_slot(now):
    """Most recent 15-minute boundary + offset, expressed as integer slot."""
    return int((now - SCAN_OFFSET) // 900)

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    init_db()
    if os.getenv('TEST_TELEGRAM', '0') == '1':
        telegram('â BinanceWin V4.4 Telegram æ¸¬è©¦æåï¼ä¸ä»£è¡¨è¡ææææåã')
        return
    if os.getenv('ONCE', '0') == '1':
        stats = scan_once()
        telegram(status_message(stats))
        return
    LOG.info('BinanceWin V4.4 started; OKX primary, BingX fallback, TOP_N=%s', TOP_N)
    last_stats = {'venue': 'WAITING', 'attempted': 0, 'successful': 0, 'candidates': 0, 'sent': 0, 'errors': 0, 'notes': []}
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
                last_error = None if last_stats['successful'] > 0 else 'æ¬è¼ªæ²ææååå¾è¡æ'
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
