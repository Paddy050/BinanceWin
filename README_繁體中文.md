# BinanceWin V5.2 嚴格型｜Railway 部署指南

## 版本來源
以本次可檢查的 `BinanceWin_V5.1_Final/worker.py` 為基礎修改，保留原有資料庫表、API 與 Railway 執行方式。**尚未實際連接使用者的 GitHub/Railway 或交易所 API**。

## 新增功能
- Telegram 簡化：移除本金／風險預算／名義倉位試算；「確認狀態」顯示全部通過、部分通過、未通過；仍保留 SL、TP1、TP2、量、RSI、KDJ。
- 嚴格型強訊號：預設 90 分，且 4H、1H、5M 同向，收盤有效突破前 20 根 K 棒極值（突破緩衝 0.1 ATR）、實體佔 K 棒至少 55%、成交量 ≥ 1.8 倍、避免 RSI 極端、價格過度偏離 EMA20、前方歷史價位有足夠空間。
- 預設僅推播強訊號；候選／觀察仍記錄資料庫，若要開啟觀察通知可設 `WATCH_ALERTS=1`。
- 發通知前另抓即時 ticker，若偏離訊號收盤參考價超過 0.5 ATR 則不通知，避免提示過時價格。這是通知過濾，並非實際可成交價格保證。
- 手動重大事件黑窗 `EVENT_BLACKOUT_UTC`，不會自行抓取 CPI／FOMC／非農行事曆。未設定時不啟用，**不能聲稱已自動避開新聞**。
- 延續 OKX 優先／BingX 備援、15 分鐘掃描、SQLite 模擬追蹤與每日報告、每日 SQLite 備份、九代號 RKLB TSLA SNXX SNDK SPCX MU MUU NVDA NVDL。僅名稱匹配不代表美股底層驗證，未人工驗證預設禁止通知。

## 更新流程（先備份）
1. 在 GitHub 將目前 V5.1 `worker.py`、`backtest.py`、`requirements.txt` 下載備份；不要刪除舊的 SQLite。
2. 將本 ZIP 的 `worker.py`、`backtest.py`、`requirements.txt`、`test_v52.py` 上傳到原本 GitHub repository 的根目錄（不要再建新專案）。
3. Railway 維持原有 GitHub main 自動部署；啟動命令 `python worker.py`；原有 `TELEGRAM_BOT_TOKEN` 與 `TELEGRAM_CHAT_ID` 不需重新公開或貼給任何人。
4. 部署後檢查 Logs 有 `BinanceWin V5.2 started`，Telegram 應出現精簡訊息；未出現強訊號不等於故障，查看每小時狀態與掃描 Logs。
5. 先確認 Volume 已掛載到 `/data`，才將 `DB_PATH=/data/scanner.sqlite3` 與 `BACKUP_DIR=/data/backups` 設為 Railway Variables。**變更路徑前須先搬移既有 SQLite，否則會像是紀錄消失。** 每日備份若和主 DB 在同一 Volume，不算異地備份。
6. 請勿設定 `ONCE=1` 或 `TEST_TELEGRAM=1` 作為常駐模式。`WATCH_ALERTS=0` 為預設；如有需求再開啟。

## 嚴格型參數（環境變數，可選）
| 名稱 | 預設 | 用途 |
|---|---|---|
| SCORE_STRONG | 90 | 強訊號門檻，另需全部嚴格條件通過 |
| STRICT_MIN_VOLUME | 1.8 | 放量倍率 |
| BREAKOUT_BUFFER_ATR | 0.10 | 突破緩衝 ATR 倍數 |
| MIN_BODY_RATIO | 0.55 | K 棒實體／全長下限 |
| MAX_EXTENSION_ATR | 1.8 | 距 EMA20 順勢延伸上限 |
| MAX_ENTRY_DRIFT_ATR | 0.5 | 通知前價格漂移上限 |
| MIN_ROOM_R | 1.5 | 前方歷史價位的最小空間（R） |
| MAX_RSI_LONG | 72 | 偏多 RSI 上限 |
| MIN_RSI_SHORT | 28 | 偏空 RSI 下限 |
| WATCH_ALERTS | 0 | 預設不推播候選／觀察 |
| EVENT_BLACKOUT_UTC | 空白 | 手動事件時間窗，UTC 起訖，以逗號分隔 |

事件黑窗範例：`2026-12-01T12:00Z/2026-12-01T14:00Z,2026-12-02T18:00Z/2026-12-02T20:00Z`（僅格式示範，**不是實際事件時間**）。

## 回測與績效注意
`python backtest.py --folder data --symbol BTC-USDT-SWAP --out results.csv`；CSV 檔案需提供同名 5m、15m、1h、4h 四個時間週期，欄位詳見 `backtest.py`。回測使用下一根 15M 開盤模擬進場，假設固定手續費、滑價、24h 出場，同根同時觸及 TP／SL 時先算 SL。它**不是實際成交紀錄**，未考慮資金費率、委託簿深度與跳空穿價，且不代表策略已獲利。

重要：原 V5.1 的 `settle_v5` 使用訊號收盤價作為假設成交價，未確認實際掛單成交。資料庫裡的舊紀錄與新紀錄都應標示為**模擬結果**，不可當作真實勝率。

## 測試
在本機或 CI 執行 `python -m unittest test_v52.py`，以及 `python -m py_compile worker.py backtest.py`。部署後仍須驗證 API、Telegram、SQLite 寫入與備份。
