import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import streamlit as st

BASE = 'https://fapi.binance.com'
DB = Path(__file__).with_name('scanner.sqlite3')
TIMEOUT = 12

st.set_page_config(page_title='合約雷達｜手機選幣', page_icon='📈', layout='wide', initial_sidebar_state='collapsed')

st.markdown('''<style>
:root { color-scheme: dark; }
.stApp { background: #0b1120; color: #e5edf8; }
.block-container { max-width: 1040px; padding-top: 1rem; padding-bottom: 5rem; }
h1 { font-size: clamp(1.65rem, 5.6vw, 2.4rem) !important; letter-spacing: -.03em; }
[data-testid="stHeader"] { background: #0b1120; }
[data-testid="stSidebar"] { background: #111c30; }
[data-testid="stMetric"] { background: #152139; border: 1px solid #263650; border-radius: 15px; padding: 12px; }
.stButton > button[kind="primary"] { width: 100%; min-height: 48px; border-radius: 12px; background: #d6a92c; color: #0a1120; border: 0; font-weight: 750; }
.stButton > button { min-height: 44px; border-radius: 12px; }
.stDownloadButton > button { width: 100%; min-height: 44px; border-radius: 12px; }
div[data-testid="stTabs"] button { font-size: .92rem; }
.mobile-card { background: #152139; border: 1px solid #293b57; border-radius: 17px; padding: 15px; margin: 10px 0; }
.mobile-card .symbol { font-size: 1.17rem; font-weight: 800; }
.mobile-card .muted { color: #a6b5cb; font-size: .84rem; }
.mobile-card .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }
.mobile-card .stat { background: #0d1728; border-radius: 10px; padding: 10px; font-size: .82rem; }
.mobile-card .stat b { display:block; font-size: 1rem; color: #f0f5ff; }
.mobile-card .long { color: #58d6a3; }
.mobile-card .short { color: #ff8c96; }
@media (max-width: 640px) {
 .block-container { padding-left: .9rem; padding-right: .9rem; padding-top: .6rem; }
 [data-testid="stHorizontalBlock"] { gap: .5rem; }
 [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] { min-width: 0 !important; }
 div[data-testid="stTabs"] button { padding-left: .45rem; padding-right: .45rem; font-size: .8rem; }
 .stDataFrame { overflow-x: auto; }
}
</style>''', unsafe_allow_html=True)


@st.cache_data(ttl=45, show_spinner=False)
def api(path, params=None):
    r = requests.get(BASE + path, params=params, timeout=TIMEOUT, headers={'User-Agent':'ResearchScanner/1.0'})
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and 'code' in data and data['code'] < 0:
        raise RuntimeError(str(data))
    return data

@st.cache_data(ttl=90, show_spinner=False)
def universe():
    info = api('/fapi/v1/exchangeInfo')
    eligible = {s['symbol'] for s in info['symbols'] if s['status']=='TRADING' and s['contractType']=='PERPETUAL' and s['quoteAsset']=='USDT'}
    ticks = api('/fapi/v1/ticker/24hr')
    return sorted([(x['symbol'], float(x['quoteVolume'])) for x in ticks if x['symbol'] in eligible], key=lambda x:x[1], reverse=True)

@st.cache_data(ttl=45, show_spinner=False)
def spreads():
    return {x['symbol']:x for x in api('/fapi/v1/ticker/bookTicker')}

@st.cache_data(ttl=50, show_spinner=False)
def klines(symbol, interval='15m', limit=250):
    raw = api('/fapi/v1/klines', {'symbol':symbol, 'interval':interval, 'limit':limit})
    cols = ['open_ms','open','high','low','close','volume','close_ms','quote_volume','trades','taker_base','taker_quote','unused']
    df = pd.DataFrame(raw, columns=cols)
    for c in ['open','high','low','close','volume','quote_volume']:
        df[c] = pd.to_numeric(df[c])
    df['open_ms'] = df['open_ms'].astype('int64')
    df['close_ms'] = df['close_ms'].astype('int64')
    # Exclude any not-yet-closed candle (close_ms is inclusive).
    return df[df['close_ms'] < int(time.time()*1000)].reset_index(drop=True)

def macd_hist(close):
    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    macd = fast - slow
    return macd - macd.ewm(span=9, adjust=False).mean()

def rsi(close, period=14):
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    ratio = up / down.replace(0, np.nan)
    result = 100 - 100/(1+ratio)
    return result.where(down != 0, 100).where(~((up == 0) & (down == 0)), 50)

def kdj(df, period=9, smooth=3):
    low = df['low'].rolling(period).min()
    high = df['high'].rolling(period).max()
    rsv = (100*(df['close']-low)/(high-low).replace(0,np.nan)).fillna(50)
    k = rsv.ewm(alpha=1/smooth, adjust=False).mean()
    d = k.ewm(alpha=1/smooth, adjust=False).mean()
    return k, d, 3*k-2*d

def signal_at(d15, d1h, idx, volume_multiple, rsi_long=52, rsi_short=48, require_kdj=True):
    if idx < 40:
        return None, np.nan, {}
    row = d15.iloc[idx]
    past = d15.iloc[idx-20:idx]
    baseline = past['quote_volume'].median()
    ratio = row['quote_volume']/baseline if baseline > 0 else 0
    # No future data: only 1h candles closed by the signal candle.
    prior1h = d1h[d1h['close_ms'] <= row['close_ms']]
    if len(prior1h) < 35:
        return None, ratio, {}
    macd_value = float(macd_hist(prior1h['close']).iloc[-1])
    rsi_value = float(rsi(d15['close'].iloc[:idx+1]).iloc[-1])
    k, d, j = kdj(d15.iloc[:idx+1])
    kval, dval, jval = float(k.iloc[-1]), float(d.iloc[-1]), float(j.iloc[-1])
    cross_up = kval > dval and float(k.iloc[-2]) <= float(d.iloc[-2])
    cross_down = kval < dval and float(k.iloc[-2]) >= float(d.iloc[-2])
    info = {'RSI':round(rsi_value,2),'K':round(kval,2),'D':round(dval,2),'J':round(jval,2),
            'MACD柱':round(macd_value,6),'KDJ金叉':cross_up,'KDJ死叉':cross_down,
            '訊號時間UTC':pd.to_datetime(row['close_ms'],unit='ms',utc=True).isoformat()}
    if ratio < volume_multiple:
        return None, ratio, info
    long_ok = row['close'] > past['high'].max() and macd_value > 0 and rsi_value >= rsi_long
    short_ok = row['close'] < past['low'].min() and macd_value < 0 and rsi_value <= rsi_short
    if require_kdj:
        long_ok = long_ok and kval > dval
        short_ok = short_ok and kval < dval
    if long_ok:
        return '偏多候選', ratio, info
    if short_ok:
        return '偏空候選', ratio, info
    return None, ratio, info

def init_db():
    with sqlite3.connect(DB) as c:
        c.execute('CREATE TABLE IF NOT EXISTS scans (scanned_utc TEXT, symbol TEXT, direction TEXT, price REAL, volume_multiple REAL, spread_pct REAL, quote_volume REAL, oi_change_pct REAL, funding_pct REAL)')

def oi_change(symbol):
    # The open interest history endpoint may not support every contract.
    try:
        data = api('/futures/data/openInterestHist', {'symbol':symbol,'period':'15m','limit':5})
        if len(data) >= 5:
            a, b = float(data[-5]['sumOpenInterest']), float(data[-1]['sumOpenInterest'])
            return 100*(b/a-1) if a else np.nan
    except Exception:
        pass
    return np.nan

def funding(symbol):
    try:
        return float(api('/fapi/v1/premiumIndex', {'symbol':symbol})['lastFundingRate'])*100
    except Exception:
        return np.nan

def scan(top_n, max_spread, min_vol, rsi_long=52, rsi_short=48, require_kdj=True):
    books = spreads()
    selected = universe()[:top_n]
    out = []
    progress = st.progress(0, text='掃描中')
    for i, (sym, qv) in enumerate(selected):
        progress.progress((i+1)/len(selected), text=f'掃描 {sym} ({i+1}/{len(selected)})')
        try:
            b = books.get(sym)
            if not b: continue
            bid, ask = float(b['bidPrice']), float(b['askPrice'])
            if bid <= 0 or ask <= 0: continue
            spread_pct = (ask-bid)/((ask+bid)/2)*100
            if spread_pct > max_spread: continue
            d15, d1h = klines(sym), klines(sym, '1h', 110)
            if len(d15) < 40 or len(d1h) < 35: continue
            direction, ratio, details = signal_at(d15,d1h,len(d15)-1,min_vol,rsi_long,rsi_short,require_kdj)
            if direction:
                out.append({'標的':sym,'方向':direction,'收盤價':float(d15.iloc[-1]['close']), '放量倍數':round(ratio,2), '價差%':round(spread_pct,4),'24h成交額(USDT)':round(qv), 'OI 1h變動%':oi_change(sym),'資金費率%':funding(sym), **details})
        except Exception as exc:
            st.session_state.setdefault('scan_errors', []).append(f'{sym}: {exc}')
    progress.empty()
    return sorted(out, key=lambda x:x['放量倍數'], reverse=True)

def save_scan(rows):
    if not rows: return
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB) as c:
        c.executemany('INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?)', [(now,r['標的'],r['方向'],r['收盤價'],r['放量倍數'],r['價差%'],r['24h成交額(USDT)'],r['OI 1h變動%'],r['資金費率%']) for r in rows])

def backtest(symbol, min_vol, hold, fee_pct, slip_pct, rsi_long=52, rsi_short=48, require_kdj=True):
    d15 = klines(symbol, '15m', 1000)
    d1h = klines(symbol, '1h', 350)
    trades = []
    next_available = 21
    for i in range(35, len(d15)-hold-1):
        if i < next_available: continue
        side, ratio, _ = signal_at(d15,d1h,i,min_vol,rsi_long,rsi_short,require_kdj)
        if not side: continue
        direction = 1 if side == '偏多候選' else -1
        entry = float(d15.iloc[i+1]['open'])
        exit_price = float(d15.iloc[i+hold]['close'])
        # Price return minus two fees and estimated adverse slippage on each side.
        gross = direction*(exit_price/entry-1)*100
        net = gross-2*(fee_pct+slip_pct)
        trades.append({'訊號時間(UTC)':pd.to_datetime(d15.iloc[i]['close_ms'],unit='ms',utc=True), '方向':side, '進場':entry, '出場':exit_price, '毛報酬%':gross, '淨報酬%':net})
        next_available = i+hold+1
    return pd.DataFrame(trades)

st.title('📈 合約雷達')
st.caption('幣安 USDT 永續合約 · 台灣時間 UTC+8 · 公開行情 · 不下單')
init_db()

with st.expander('⚙️ 掃描設定（點擊展開）', expanded=False):
    top_n = st.slider('成交額前 N 名', 5, 60, 30, 5)
    max_spread = st.number_input('最大買賣價差 (%)', min_value=0.01, max_value=2.0, value=0.10, step=0.01)
    min_vol = st.number_input('最低放量倍數', min_value=1.0, max_value=10.0, value=1.5, step=0.1)
    rsi_long = st.slider('偏多 RSI 最低門檻', 50, 75, 52)
    rsi_short = st.slider('偏空 RSI 最高門檻', 25, 50, 48)
    require_kdj = st.checkbox('要求 KDJ 同向（K > D 偏多；K < D 偏空）', value=True)
    st.caption('15 分鐘突破與放量 + 1 小時 MACD + 15 分鐘 RSI/KDJ；只使用已收盤 K 線。')

tab1, tab2, tab3, tab4 = st.tabs(['🔎 選幣', '🧪 回測', '🗂️ 紀錄', '🔔 提醒'])
with tab1:
    st.subheader('短線候選掃描')
    if st.button('🔎 立即掃描前 ' + str(top_n) + ' 名合約', type='primary', key='scan'):
        st.session_state['scan_errors'] = []
        try:
            rows = scan(top_n, max_spread, min_vol, rsi_long, rsi_short, require_kdj)
            st.session_state['scan_rows'] = rows
            st.session_state['last_scan_time'] = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
            save_scan(rows)
        except Exception as exc:
            st.error(f'無法取得幣安公開資料：{exc}')
    rows = st.session_state.get('scan_rows', [])
    if st.session_state.get('last_scan_time'):
        st.caption('上次掃描：' + st.session_state['last_scan_time'] + '（台灣時間）')
    if rows:
        st.warning('🔔 發現新候選訊號（僅於你手動掃描、且頁面開啟時顯示；不是背景推播）。')
        st.success(f'符合條件：{len(rows)} 個；顯示放量倍數最高的 5 個')
        from html import escape
        for r in rows[:5]:
            direction = r['方向']
            cls = 'long' if '多' in direction else 'short'
            def val(v, suffix=''):
                return '—' if pd.isna(v) else f'{v:,.3f}{suffix}'
            st.markdown(f"""<div class="mobile-card">
              <div class="symbol">{escape(r['標的'])} <span class="{cls}">｜{escape(direction)}</span></div>
              <div class="muted">訊號 K 線收盤價：{r['收盤價']:,.8g} USDT</div>
              <div class="stats">
                <div class="stat">放量倍數<b>{val(r['放量倍數'])}×</b></div>
                <div class="stat">買賣價差<b>{val(r['價差%'],'%')}</b></div>
                <div class="stat">OI 近 1 小時變動<b>{val(r['OI 1h變動%'],'%')}</b></div>
                <div class="stat">最新資金費率<b>{val(r['資金費率%'],'%')}</b></div>
              </div>
              <div class="muted" style="margin-top:10px">RSI：{r['RSI']:.1f} · KDJ：{r['K']:.1f}/{r['D']:.1f}/{r['J']:.1f} · 1h MACD柱：{r['MACD柱']:.5g}</div>
              <div class="muted" style="margin-top:6px">訊號收盤：{escape(r['訊號時間UTC'])} · 24h 成交額：{r['24h成交額(USDT)']:,.0f} USDT · 非進場指令</div>
            </div>""", unsafe_allow_html=True)
        st.download_button('⬇️ 匯出本輪候選 CSV', pd.DataFrame(rows).to_csv(index=False).encode('utf-8-sig'), 'candidates.csv', 'text/csv')
    elif st.session_state.get('last_scan_time'):
        st.info('本輪沒有符合全部條件的標的。不要為了交易而放寬風險條件。')
    else:
        st.info('點擊「立即掃描」開始找候選標的。')
    if st.session_state.get('scan_errors'):
        with st.expander(f'⚠️ 部分標的讀取失敗（{len(st.session_state["scan_errors"])}）'):
            st.write(st.session_state['scan_errors'])

with tab2:
    st.warning('固定單一標的的基準回測；不含歷史資金費、爆倉及實際撮合，不代表動態選幣績效。')
    symbol = st.text_input('合約交易對', value='ETHUSDT').strip().upper()
    hold = st.slider('持有幾根 15 分鐘 K 線', 1, 32, 8)
    c1, c2 = st.columns(2)
    fee = c1.number_input('單邊手續費 (%)', 0.0, 1.0, 0.05, 0.01)
    slip = c2.number_input('單邊滑價 (%)', 0.0, 1.0, 0.03, 0.01)
    if st.button('🧪 執行基準回測', type='primary'):
        try:
            st.session_state['backtest_trades'] = backtest(symbol, min_vol, hold, fee, slip, rsi_long, rsi_short, require_kdj)
            st.session_state['backtest_symbol'] = symbol
        except Exception as exc:
            st.error(f'回測失敗：{exc}')
    trades = st.session_state.get('backtest_trades')
    if trades is not None:
        if trades.empty:
            st.info('樣本期間沒有觸發訊號。')
        else:
            st.caption(f'最近一次回測：{st.session_state.get("backtest_symbol", "")}')
            equity = (1 + trades['淨報酬%'] / 100).cumprod()
            dd = (equity / equity.cummax() - 1) * 100
            a, b = st.columns(2)
            a.metric('交易次數', len(trades))
            b.metric('勝率', f'{(trades["淨報酬%"] > 0).mean()*100:.1f}%')
            c, d = st.columns(2)
            c.metric('每筆平均淨報酬', f'{trades["淨報酬%"].mean():.2f}%')
            d.metric('最大回撤（連乘假設）', f'{dd.min():.2f}%')
            st.line_chart(pd.DataFrame({'模擬淨值（起始1）': equity.to_numpy()}, index=range(1, len(equity)+1)))
            with st.expander('查看逐筆回測交易'):
                st.dataframe(trades, hide_index=True, use_container_width=True)
            st.download_button('⬇️ 下載回測 CSV', trades.to_csv(index=False).encode('utf-8-sig'), 'backtest.csv', 'text/csv')

with tab3:
    with sqlite3.connect(DB) as c:
        history = pd.read_sql_query('SELECT * FROM scans ORDER BY scanned_utc DESC LIMIT 500', c)
    if history.empty:
        st.info('尚無符合條件的掃描紀錄。')
    else:
        st.metric('已儲存候選紀錄', len(history))
        st.dataframe(history, use_container_width=True, hide_index=True)
        st.download_button('⬇️ 下載掃描紀錄 CSV', history.to_csv(index=False).encode('utf-8-sig'), 'scan_history.csv', 'text/csv')

st.divider()
st.caption('研究與紙上交易工具 · 不自動下單 · 不保證獲利 · 請遵守所在地法規及幣安服務條款')

with tab4:
    st.subheader('🔔 Telegram 自動掃描')
    st.info('背景掃描由獨立 worker.py 執行。關閉 iPhone 網頁後，只要部署主機仍運作，就能繼續監控。')
    st.markdown('**預設條件：** 每 15 分鐘 K 線收盤後掃描一次成交額前 30 名，使用 KDJ / RSI / MACD / 放量 / 突破訊號；同一標的、方向、K 線只提醒一次。')
    st.caption('Telegram Token 與 Chat ID 必須在伺服器環境變數設定，請勿貼到網頁或公開程式碼。')
    with sqlite3.connect(DB) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS alerts
        (symbol TEXT, direction TEXT, candle_utc TEXT, sent_utc TEXT, status TEXT,
         PRIMARY KEY(symbol, direction, candle_utc))""")
        recent = pd.read_sql_query('SELECT * FROM alerts ORDER BY sent_utc DESC LIMIT 30', c)
    if recent.empty:
        st.info('目前沒有背景提醒紀錄。')
    else:
        st.dataframe(recent, hide_index=True, use_container_width=True)
    st.caption('提醒是研究候選，不是保證成交的進場指令；請先用紙上交易驗證。')
