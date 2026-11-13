"""Binance futures research scanner. Alerts only; no orders or trading credentials."""
import os
import time
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
import requests
import numpy as np
import pandas as pd

BASE = "https://fapi.binance.com"
DB = Path(__file__).with_name("scanner.sqlite3")
LOG = logging.getLogger("alerts")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "FuturesResearchAlerts/4.1"})
TIMEOUT = 15


def api(path, params=None):
    try:
        r = HTTP.get(BASE + path, params=params, timeout=TIMEOUT)
        if r.status_code == 451:
            raise RuntimeError("Binance Futures API HTTP 451: access restricted for this server/location; scanning unavailable")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and isinstance(data.get("code"), (int, float)) and data["code"] < 0:
            raise RuntimeError(f"Binance API error code {data['code']}: {data.get('msg', '')}")
        return data
    except requests.RequestException as exc:
        LOG.error("Binance request failed for %s: %s", path, exc)
        raise


def klines(symbol, interval="15m", limit=250):
    raw = api("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    cols = ["open_ms", "open", "high", "low", "close", "volume", "close_ms", "quote_volume", "trades", "taker_base", "taker_quote", "unused"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = pd.to_numeric(df[c])
    for c in ["open_ms", "close_ms"]:
        df[c] = df[c].astype("int64")
    return df[df["close_ms"] < int(time.time() * 1000)].reset_index(drop=True)


def macd_hist(close):
    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    macd = fast - slow
    return macd - macd.ewm(span=9, adjust=False).mean()


def rsi(close, period=14):
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    ratio = up / down.replace(0, np.nan)
    result = 100 - 100 / (1 + ratio)
    return result.where(down != 0, 100).where(~((up == 0) & (down == 0)), 50)


def kdj(df, period=9, smooth=3):
    low = df["low"].rolling(period).min()
    high = df["high"].rolling(period).max()
    rsv = (100 * (df["close"] - low) / (high - low).replace(0, np.nan)).fillna(50)
    k = rsv.ewm(alpha=1 / smooth, adjust=False).mean()
    d = k.ewm(alpha=1 / smooth, adjust=False).mean()
    return k, d, 3 * k - 2 * d


def signal_at(d15, d1h, idx, volume_multiple, rsi_long=52, rsi_short=48, require_kdj=True):
    if idx < 40:
        return None, np.nan, {}
    row = d15.iloc[idx]
    past = d15.iloc[idx - 20:idx]
    baseline = past["quote_volume"].median()
    ratio = row["quote_volume"] / baseline if baseline > 0 else 0
    prior1h = d1h[d1h["close_ms"] <= row["close_ms"]]
    if len(prior1h) < 35:
        return None, ratio, {}
    macd_value = float(macd_hist(prior1h["close"]).iloc[-1])
    rsi_value = float(rsi(d15["close"].iloc[:idx + 1]).iloc[-1])
    k, d, j = kdj(d15.iloc[:idx + 1])
    kval, dval, jval = float(k.iloc[-1]), float(d.iloc[-1]), float(j.iloc[-1])
    cross_up = kval > dval and float(k.iloc[-2]) <= float(d.iloc[-2])
    cross_down = kval < dval and float(k.iloc[-2]) >= float(d.iloc[-2])
    info = {"RSI": round(rsi_value, 2), "K": round(kval, 2), "D": round(dval, 2), "J": round(jval, 2),
            "MACD柱": round(macd_value, 6), "KDJ金叉": cross_up, "KDJ死叉": cross_down,
            "訊號時間UTC": pd.to_datetime(row["close_ms"], unit="ms", utc=True).isoformat()}
    if ratio < volume_multiple:
        return None, ratio, info
    long_ok = row["close"] > past["high"].max() and macd_value > 0 and rsi_value >= rsi_long
    short_ok = row["close"] < past["low"].min() and macd_value < 0 and rsi_value <= rsi_short
    if require_kdj:
        long_ok = long_ok and kval > dval
        short_ok = short_ok and kval < dval
    if long_ok:
        return "偏多候選", ratio, info
    if short_ok:
        return "偏空候選", ratio, info
    return None, ratio, info


def init_db():
    with sqlite3.connect(DB, timeout=20) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS alerts
        (symbol TEXT, direction TEXT, candle_utc TEXT, sent_utc TEXT, status TEXT,
         PRIMARY KEY(symbol, direction, candle_utc))""")


def scan():
    info = api("/fapi/v1/exchangeInfo")
    allowed = {s["symbol"] for s in info["symbols"]
               if s["status"] == "TRADING" and s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT"}
    ranked = sorted((t for t in api("/fapi/v1/ticker/24hr") if t["symbol"] in allowed),
                    key=lambda t: float(t["quoteVolume"]), reverse=True)
    books = {b["symbol"]: b for b in api("/fapi/v1/ticker/bookTicker")}
    out = []
    for t in ranked[:int(os.getenv("TOP_N", "30"))]:
        sym = t["symbol"]
        try:
            book = books[sym]
            bid, ask = float(book["bidPrice"]), float(book["askPrice"])
            if bid <= 0 or ask <= 0 or 100 * (ask - bid) / ((ask + bid) / 2) > float(os.getenv("MAX_SPREAD_PCT", "0.1")):
                continue
            d15 = klines(sym)
            d1h = klines(sym, "1h", 110)
            if len(d15) < 45 or len(d1h) < 35:
                continue
            direction, ratio, details = signal_at(d15, d1h, len(d15) - 1,
                float(os.getenv("MIN_VOLUME_MULTIPLE", "1.5")),
                float(os.getenv("RSI_LONG", "52")), float(os.getenv("RSI_SHORT", "48")),
                os.getenv("REQUIRE_KDJ", "true").lower() == "true")
            if direction:
                out.append({"symbol": sym, "direction": direction,
                            "price": float(d15.iloc[-1]["close"]), "volume": ratio,
                            "details": details})
        except Exception as exc:
            LOG.warning("%s failed: %s", sym, exc)
    return out


def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Railway Variables")
    r = HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": chat_id, "text": message}, timeout=TIMEOUT)
    if not r.ok:
        # Never log response URLs, which may include the secret bot token.
        try:
            description = r.json().get("description", "Telegram request failed")
        except ValueError:
            description = "Telegram request failed"
        raise RuntimeError(f"Telegram HTTP {r.status_code}: {description}")
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError("Telegram rejected message")


def notify_once(row):
    candle = row["details"]["訊號時間UTC"]
    key = (row["symbol"], row["direction"], candle)
    with sqlite3.connect(DB, timeout=20) as c:
        existing = c.execute("SELECT status FROM alerts WHERE symbol=? AND direction=? AND candle_utc=?", key).fetchone()
        if existing and existing[0] == "sent":
            return False
        d = row["details"]
        msg = (f"🔔 合約雷達｜{row['direction']}\n"
               f"標的：{row['symbol']}\n"
               f"已收盤 15m K 線：{candle}\n"
               f"收盤價：{row['price']:.8g} USDT\n"
               f"放量：{row['volume']:.2f}×\n"
               f"RSI：{d['RSI']:.1f}｜KDJ：{d['K']:.1f}/{d['D']:.1f}/{d['J']:.1f}\n"
               f"1h MACD 柱：{d['MACD柱']:.6g}\n"
               "僅供研究，非交易建議。")
        try:
            send_telegram(msg)
        except Exception:
            c.execute("""INSERT INTO alerts VALUES (?,?,?,?,?)
                ON CONFLICT(symbol,direction,candle_utc) DO UPDATE SET sent_utc=excluded.sent_utc,status=excluded.status""",
                (*key, datetime.now(timezone.utc).isoformat(), "failed"))
            raise
        c.execute("""INSERT INTO alerts VALUES (?,?,?,?,?)
            ON CONFLICT(symbol,direction,candle_utc) DO UPDATE SET sent_utc=excluded.sent_utc,status=excluded.status""",
            (*key, datetime.now(timezone.utc).isoformat(), "sent"))
        return True


def run_once():
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before running")
    init_db()
    rows = scan()
    sent = 0
    for row in rows:
        try:
            sent += int(notify_once(row))
        except Exception as exc:
            LOG.error("Telegram send failed for %s: %s", row["symbol"], exc)
    LOG.info("Scan complete: %d candidates, %d notifications", len(rows), sent)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.getenv("TEST_TELEGRAM") == "1":
        send_telegram("✅ BinanceWin V4.1 Telegram 測試成功。此訊息不代表行情掃描正常。")
        LOG.info("Telegram test message sent successfully")
    elif os.getenv("ONCE") == "1":
        run_once()
    else:
        while True:
            now = time.time()
            wait = 900 - (now % 900) + 20
            time.sleep(wait)
            try:
                run_once()
            except Exception:
                LOG.exception("Scan cycle failed")
