"""BinanceWin V4.2: OKX-first, BingX-fallback, research-only futures scanner."""
import os
import time
import json
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
import numpy as np
import pandas as pd

LOG = logging.getLogger('binancewin')
HTTP = requests.Session()
HTTP.headers.update({'User-Agent': 'BinanceWinResearch/4.2', 'Accept': 'application/json'})
DB = Path(os.getenv('DB_PATH', str(Path(__file__).with_name('scanner.sqlite3'))))
TIMEOUT = 15


def env_float(key, default):
    return float(os.getenv(key, str(default)))


def get_json(url, params=None):
    response = HTTP.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError('Unexpected JSON shape')
    if str(data.get('code', '0')) not in ('0', '200'):
        raise RuntimeError(f"API error code={data.get('code')} msg={str(data.get('msg', ''))[:150]}")
    return data.get('data')


def okx(path, params=None):
    return get_json('https://www.okx.com' + path, params)


def bingx(path, params=None):
    return get_json('https://open-api.bingx.com' + path, params)


def eligible_okx():
    instruments = okx('/api/v5/public/instruments', {'instType': 'SWAP'})
    return {x['instId'] for x in instruments if x.get('state') == 'live' and x.get('settleCcy') == 'USDT' and x.get('ctType') == 'linear' and x.get('instId', '').endswith('-USDT-SWAP')}


def eligible_bingx():
    contracts = bingx('/openApi/swap/v2/quote/contracts')
    if not isinstance(contracts, list):
        raise ValueError('Unexpected BingX contracts response')
    return {x['symbol'] for x in contracts if x.get('symbol', '').endswith('-USDT') and str(x.get('status', '1')).lower() in ('1', 'trading', 'enabled', 'true')}


def rank_okx():
    eligible = eligible_okx()
    tickers = okx('/api/v5/market/tickers', {'instType': 'SWAP'})
    # volCcy24h is base-currency volume for linear swaps; multiply by last price.
    rows = []
    for t in tickers:
        sym = t.get('instId')
        if sym not in eligible:
            continue
        try:
            last = float(t['last'])
            bid, ask = float(t['bidPx']), float(t['askPx'])
            volume = float(t['volCcy24h']) * last
            if last > 0 and volume > 0 and spread_ok(bid, ask):
                rows.append((sym, volume))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(rows, key=lambda x: x[1], reverse=True)


def rank_bingx():
    eligible = eligible_bingx()
    tickers = bingx('/openApi/swap/v2/quote/ticker')
    if not isinstance(tickers, list):
        raise ValueError('Unexpected BingX ticker response')
    rows = []
    for t in tickers:
        sym = t.get('symbol')
        if sym not in eligible:
            continue
        try:
            last = float(t['lastPrice'])
            # BingX 24h quote volume is in USDT.
            volume = float(t['quoteVolume'])
            if last > 0 and volume > 0:
                rows.append((sym, volume))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(rows, key=lambda x: x[1], reverse=True)


def spread_ok(bid, ask):
    return bid > 0 and ask > 0 and ask >= bid and 100 * (ask - bid) / ((ask + bid) / 2) <= env_float('MAX_SPREAD_PCT', 0.1)


def frame(rows):
    df = pd.DataFrame(rows, columns=['open_ms', 'open', 'high', 'low', 'close', 'volume', 'quote_volume', 'close_ms'])
    if df.empty:
        return df
    for col in ('open', 'high', 'low', 'close', 'volume', 'quote_volume'):
        df[col] = pd.to_numeric(df[col], errors='raise')
    df['open_ms'] = df['open_ms'].astype('int64')
    df['close_ms'] = df['close_ms'].astype('int64')
    df = df.sort_values('open_ms').drop_duplicates('open_ms')
    return df[df['close_ms'] < int(time.time() * 1000)].reset_index(drop=True)


def okx_klines(symbol, interval, limit):
    bar = '15m' if interval == '15m' else '1H'
    raw = okx('/api/v5/market/candles', {'instId': symbol, 'bar': bar, 'limit': str(limit)})
    duration = 900000 if interval == '15m' else 3600000
    rows = []
    for x in raw:
        if len(x) < 9 or str(x[8]) != '1':
            continue
        start = int(x[0])
        close = float(x[4])
        base_volume = float(x[5])  # OKX vol = contract count; use volCcy (base units)
        quote_volume = float(x[7])  # volCcyQuote = quote currency volume
        rows.append((start, x[1], x[2], x[3], x[4], base_volume, quote_volume, start + duration - 1))
    return frame(rows)


def bingx_klines(symbol, interval, limit):
    raw = bingx('/openApi/swap/v3/quote/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})
    if not isinstance(raw, list):
        raise ValueError('Unexpected BingX candles response')
    duration = 900000 if interval == '15m' else 3600000
    rows = []
    for x in raw:
        start = int(x['time'])
        base_volume = float(x['volume'])
        close = float(x['close'])
        # Use an estimated quote turnover if no quoteVolume field is supplied.
        quote_volume = float(x.get('quoteVolume') or base_volume * close)
        rows.append((start, x['open'], x['high'], x['low'], x['close'], base_volume, quote_volume, start + duration - 1))
    return frame(rows)


def macd_hist(close):
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    return macd - macd.ewm(span=9, adjust=False).mean()


def rsi(close, period=14):
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    ratio = up / down.replace(0, np.nan)
    result = 100 - 100 / (1 + ratio)
    return result.where(down != 0, 100).where(~((up == 0) & (down == 0)), 50)


def kdj(df, period=9, smooth=3):
    low = df['low'].rolling(period).min()
    high = df['high'].rolling(period).max()
    rsv = (100 * (df['close'] - low) / (high - low).replace(0, np.nan)).fillna(50)
    k = rsv.ewm(alpha=1 / smooth, adjust=False).mean()
    d = k.ewm(alpha=1 / smooth, adjust=False).mean()
    return k, d, 3 * k - 2 * d


def signal_at(d15, d1h, idx):
    if idx < 40:
        return None
    row = d15.iloc[idx]
    past = d15.iloc[idx - 20:idx]
    baseline = past['quote_volume'].median()
    ratio = float(row['quote_volume'] / baseline) if baseline > 0 else 0
    prior1h = d1h[d1h['close_ms'] <= row['close_ms']]
    if len(prior1h) < 35:
        return None
    macd_value = float(macd_hist(prior1h['close']).iloc[-1])
    rsi_value = float(rsi(d15['close'].iloc[:idx + 1]).iloc[-1])
    k, d, j = kdj(d15.iloc[:idx + 1])
    kval, dval, jval = float(k.iloc[-1]), float(d.iloc[-1]), float(j.iloc[-1])
    if not all(np.isfinite(x) for x in (ratio, macd_value, rsi_value, kval, dval, jval)):
        return None
    if ratio < env_float('MIN_VOLUME_MULTIPLE', 1.5):
        return None
    long_ok = row['close'] > past['high'].max() and macd_value > 0 and rsi_value >= env_float('RSI_LONG', 52)
    short_ok = row['close'] < past['low'].min() and macd_value < 0 and rsi_value <= env_float('RSI_SHORT', 48)
    if os.getenv('REQUIRE_KDJ', 'true').lower() == 'true':
        long_ok = long_ok and kval > dval
        short_ok = short_ok and kval < dval
    direction = '偏多候選' if long_ok else '偏空候選' if short_ok else None
    if not direction:
        return None
    return {'direction': direction, 'price': float(row['close']), 'volume': ratio,
            'candle_utc': pd.to_datetime(int(row['close_ms']) + 1, unit='ms', utc=True).isoformat(),
            'rsi': rsi_value, 'k': kval, 'd': dval, 'j': jval, 'macd': macd_value}


def send_telegram(message):
    token = os.environ['TELEGRAM_BOT_TOKEN'].strip()
    chat_id = os.environ['TELEGRAM_CHAT_ID'].strip()
    response = HTTP.post(f'https://api.telegram.org/bot{token}/sendMessage',
                         json={'chat_id': chat_id, 'text': message}, timeout=TIMEOUT)
    response.raise_for_status()
    if not response.json().get('ok'):
        raise RuntimeError('Telegram rejected message')


def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB, timeout=20) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS alerts (venue TEXT, symbol TEXT, direction TEXT, candle_utc TEXT, sent_utc TEXT, PRIMARY KEY (venue, symbol, direction, candle_utc))')


def notify_signal(venue, symbol, item):
    key = (venue, symbol, item['direction'], item['candle_utc'])
    with sqlite3.connect(DB, timeout=20) as conn:
        if conn.execute('SELECT 1 FROM alerts WHERE venue=? AND symbol=? AND direction=? AND candle_utc=?', key).fetchone():
            return False
        message = (f"🔔 BinanceWin V4.2｜{item['direction']}\n"
                   f"交易所：{venue}｜合約：{symbol}\n"
                   f"15m 收盤 UTC：{item['candle_utc']}\n"
                   f"收盤價：{item['price']:.8g} USDT\n"
                   f"放量：{item['volume']:.2f}×\n"
                   f"RSI：{item['rsi']:.1f}｜KDJ：{item['k']:.1f}/{item['d']:.1f}/{item['j']:.1f}\n"
                   f"1h MACD 柱：{item['macd']:.6g}\n"
                   '研究訊號，非交易建議；不自動下單。')
        send_telegram(message)
        conn.execute('INSERT INTO alerts VALUES (?,?,?,?,?)', (*key, datetime.now(timezone.utc).isoformat()))
        return True


def probe_venue(venue):
    ranked = rank_okx() if venue == 'OKX' else rank_bingx()
    if not ranked:
        raise RuntimeError('No eligible instruments with usable 24h volume')
    symbol = ranked[0][0]
    fetch = okx_klines if venue == 'OKX' else bingx_klines
    a, b = fetch(symbol, '15m', 80), fetch(symbol, '1h', 80)
    if len(a) < 45 or len(b) < 35:
        raise RuntimeError('Probe candles incomplete')
    return ranked


def scan_venue(venue, ranked):
    fetch = okx_klines if venue == 'OKX' else bingx_klines
    target = max(1, min(100, int(os.getenv('TOP_N', '30'))))
    attempted = successful = sent = candidates = 0
    errors = []
    for symbol, _ in ranked[:target]:
        attempted += 1
        try:
            d15 = fetch(symbol, '15m', 250)
            d1h = fetch(symbol, '1h', 110)
            if len(d15) < 45 or len(d1h) < 35:
                raise ValueError('Insufficient closed candles')
            successful += 1
            item = signal_at(d15, d1h, len(d15) - 1)
            if item:
                candidates += 1
                if notify_signal(venue, symbol, item):
                    sent += 1
        except Exception as exc:
            LOG.warning('%s %s scan failed: %s', venue, symbol, str(exc)[:200])
            errors.append(f'{symbol}: {str(exc)[:90]}')
    if successful == 0:
        raise RuntimeError('All symbol scans failed; ' + '; '.join(errors[:2]))
    return {'venue': venue, 'attempted': attempted, 'successful': successful,
            'candidates': candidates, 'sent': sent, 'errors': len(errors)}


def run_cycle():
    errors = []
    for venue in ('OKX', 'BingX'):
        try:
            ranked = probe_venue(venue)
            LOG.info('API probe OK: %s, %d eligible ranked instruments', venue, len(ranked))
            result = scan_venue(venue, ranked)
            LOG.info('Scan complete: %s', json.dumps(result, ensure_ascii=False))
            return result
        except Exception as exc:
            LOG.error('%s unavailable: %s', venue, str(exc)[:300])
            errors.append(f'{venue}: {str(exc)[:180]}')
    raise RuntimeError('Both exchanges unavailable | ' + ' | '.join(errors))


def status_message(result, error=None):
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    if error:
        return f'⚠️ BinanceWin V4.2 每小時狀態\n時間：{now}\n掃描失敗，未產生訊號。\n原因：{str(error)[:400]}'
    return (f'📡 BinanceWin V4.2 每小時狀態\n時間：{now}\n'
            f"行情來源：{result['venue']}\n掃描：{result['successful']}/{result['attempted']} 合約\n"
            f"候選訊號：{result['candidates']}｜新通知：{result['sent']}\n"
            f"單幣錯誤：{result['errors']}\n僅供研究，不自動下單。")


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if not os.getenv('TELEGRAM_BOT_TOKEN') or not os.getenv('TELEGRAM_CHAT_ID'):
        raise RuntimeError('Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID')
    init_db()
    if os.getenv('TEST_TELEGRAM', '0') == '1':
        send_telegram('✅ BinanceWin V4.2 Telegram 測試成功；此訊息不代表行情 API 已通過測試。')
        LOG.info('Telegram test sent')
        return
    if os.getenv('PROBE_ONLY', '0') == '1':
        for venue in ('OKX', 'BingX'):
            try:
                ranked = probe_venue(venue)
                LOG.info('PROBE SUCCESS %s: %d instruments', venue, len(ranked))
            except Exception as exc:
                LOG.error('PROBE FAILED %s: %s', venue, str(exc)[:300])
        return
    if os.getenv('ONCE', '0') == '1':
        result = run_cycle()
        send_telegram(status_message(result))
        return
    last_hour = None
    while True:
        now = time.time()
        time.sleep(max(1, 900 - (now % 900) + 20))
        hour = int(time.time() // 3600)
        try:
            result = run_cycle()
            if hour != last_hour:
                send_telegram(status_message(result))
                last_hour = hour
        except Exception as exc:
            LOG.exception('Scan cycle failed')
            if hour != last_hour:
                try:
                    send_telegram(status_message(None, exc))
                    last_hour = hour
                except Exception:
                    LOG.exception('Hourly failure notification failed')


if __name__ == '__main__':
    main()
