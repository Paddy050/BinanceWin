# BinanceWin V5.1｜平衡型智慧合約研究雷達

本版以實際提供的 `BinanceWin_V5.0_Final.zip` 之 `worker.py` 為基礎修訂。只使用公開市場資料；**不會自動下單**、不使用交易 API 金鑰，也不保證獲利。

## 功能與限制

- OKX 優先、BingX 故障時備援；每 15 分鐘掃描一次，熱門 USDT 永續合約前 30 名，另納入 9 個指定代號，合約去重後統計。
- 4H／1H／15M／5M **已收盤** K 線、MACD／RSI／KDJ／成交量／突破評分。85 分且符合趨勢、突破、放量才列強訊號；70 分候選、55 分觀察。分數不是勝率。
- 同標的同方向智慧通知：首次發現、升級強訊號、評分比前次至少增加 15 分，或強訊號持續超過 120 分鐘才提醒。訊號消失會在資料庫標記失效，但不逐筆洗版；若 Telegram 發送失敗不記錄成功。每輪最多 3 則非強訊號通知。
- ATR(14)×1.5 參考停損，TP1=1.5R、TP2=2R；停損距離超過價格 5% 不發訊號。附單筆風險預算／名義倉位上限示例，**不是下單建議或保證最大虧損**；槓桿、費用、滑價、跳空與爆倉可能造成更多損失。
- SQLite 保留訊號與模擬結算；每晚 21:00 台灣時間寄送每日報告；每小時狀態報告。模擬以 15M K 線高低點判斷 TP/SL，同根兩者皆觸及時先算 SL；24H 超時按收盤價，扣假設手續費與滑價。這不是實際成交績效，且先前 V5.0 既有模擬紀錄會保留。
- `backtest.py` 是沿用 V5.0 的 CSV 離線回測工具，需自行提供 5M、15M、1H、4H 歷史 CSV；沒有實際歷史資料時不會產生可靠勝率。
- 九個指定代號：`RKLB,TSLA,SNXX,SNDK,SPCX,MU,MUU,NVDA,NVDL`。**交易所上架同名合約並不能驗證其追蹤股票／ETF。** 預設不發這九個同名合約的交易告警。僅在查閱交易所官方合約規格、核對底層標的與風險後，才可將實際合約 ID 填入 `VERIFIED_EQUITY_CONTRACTS`；這是人工核准清單，程式本身沒有獨立驗證官方文件。

## Railway 手機部署

1. **先備份** GitHub 的 `worker.py`、`requirements.txt`，並評估現有 SQLite 紀錄是否需先匯出。不要在 GitHub 上傳 `.env` 或 Bot Token。
2. 將 ZIP 內 `worker.py`、`requirements.txt` 覆蓋 GitHub 根目錄同名檔案；`backtest.py` 可一併更新。保留 `python worker.py` 作為 Railway Start Command。
3. 保留 Railway Variables 中 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`TOP_N=30`、`ONCE=0`、`TEST_TELEGRAM=0`。若設了 `EQUITY_SYMBOLS`，確認九個代號齊全。
4. **持久化儲存必須先建立 Railway Volume，掛載 `/data`，才設定 `DB_PATH=/data/scanner.sqlite3` 與 `BACKUP_DIR=/data/backups`。** 只設定 DB_PATH 而未掛載 Volume，無法保證跨部署保存。若已有舊 SQLite，**改路徑不會自動搬移**，請先從舊部署匯出資料庫再移入 Volume；否則新路徑會建立空白 DB。Railway Trial 是否支援 Volume 依當前方案及介面為準。
5. 部署後檢查 Railway Logs：`BinanceWin V5.1 started`、`DB_PATH=...`、`Scan complete`，再確認 Telegram 訊號與每日報告。
6. 預設會每天產生一份 SQLite 一致性備份並保留 14 天；**若 BACKUP_DIR 不在持久化 Volume，備份也會隨部署消失。** 同一個 Volume 的備份不是異地備份；若 Volume 被刪除仍會一併遺失，建議定期下載到外部儲存。

## 可選 Variables（可不新增，程式有預設值）

| 變數 | 預設 | 說明 |
|---|---|---|
| `ALERT_COOLDOWN_MIN` | `120` | 強訊號持續提醒冷卻分鐘 |
| `SCORE_CHANGE_ALERT` | `15` | 分數增強提醒門檻 |
| `WATCH_MAX_PER_SCAN` | `3` | 每輪非強訊號最多通知 |
| `DAILY_REPORT_HOUR_TW` | `21` | 台灣時間每日報告小時 |
| `MAX_STOP_PCT` | `5` | ATR 停損距離超過此百分比不發訊號 |
| `SIM_CAPITAL_USDT` | `150` | 純研究風險試算本金 |
| `MAX_RISK_PER_TRADE_PCT` | `1` | 單筆研究風險預算百分比 |
| `MAX_NOTIONAL_PCT` | `100` | 研究名義倉位占本金上限百分比 |
| `BACKUP_KEEP_DAYS` | `14` | 本地每日備份保留天數 |
| `VERIFIED_EQUITY_CONTRACTS` | 空 | **人工查核後**才填入交易所合約 ID，例：`TSLA-USDT-SWAP`（僅為格式示例，非已驗證標的） |

## 本機測試

```bash
pip install -r requirements.txt
python -m py_compile worker.py backtest.py
python test_v51.py
```

## 仍需實際驗證

- 本環境無法連線 OKX、BingX、Telegram 或 Railway，因此未實測交易所即時回傳格式、網路錯誤重試與真實通知。
- 未取得交易所官方文件證實九個代號的底層資產；目前全部屬於未驗證，不應視為美股可交易保證。
- 回測工具沒有真實訂單簿、資金費率、交易所合約規格、跳空或清算模擬；請勿將研究分數或模擬勝率當成實盤保證。
