# BinanceWin V5.0｜平衡型研究雷達

本版從使用者先前的 V4.5.1 `worker.py` 升級。**僅研究、告警、離線回測，不會下單**；未提供任何交易 API 密鑰欄位。

## 已檢查的 V4.5.1 問題
- 15m + 1h 兩週期，訊號過度依賴硬性條件。
- 原本的 1/4/24 小時收盤價方向追蹤不是可執行的勝率。
- `WATCHLIST SYMBOL FOUND` 只驗證代號存在，並未驗證美股／ETF 底層標的。
- 30 個熱門合約加 9 個監控代號會去重，因此 34/34 不代表漏掃 5 個。

## 新版
- 4H 大方向、1H MACD、15M 突破/成交量、5M 動能確認，使用**已收盤** K 線；評分 0–100。
- 平衡型：85 分且通過趨勢／突破／放量門檻為強訊號；70 分且符合趨勢與接近突破為候選；55 分且接近突破為觀察。評分非機率。
- ATR(14) 停損 1.5 倍，停利 2R；參考進場為訊號收盤價，非保證成交價。
- SQLite 記錄訊號，後續以 15M K 線高低價判斷 TP/SL，若同根同時觸發，保守視為 SL；24 小時尚未觸發則以最後已知收盤價結算。扣除假設往返手續費與滑價。**這是近似模擬，不是實盤績效**。
- `backtest.py` 離線 CSV 回測：下一根 15M 開盤價模擬進場，訊號只使用當時已收盤 K 線；同時僅允許一筆持倉。提供正報酬比例、平均淨報酬、profit factor、最大回撤、最大連虧。尚未提供 CSV，故未聲稱已取得勝率。
- 九個指定代號 RKLB、TSLA、SNXX、SNDK、SPCX、MU、MUU、NVDA、NVDL：只確認交易所即時合約清單有同名 USDT 永續合約，**不能證明它追蹤美股**。為避免錯誤訊號，預設 `EQUITY_ALLOW_UNVERIFIED=0`，同名合約可被掃描，但不發送交易告警，也不計入 V5 模擬訊號；必須先由交易所官方產品規格確認底層標的，再由使用者自行決定是否開啟。
- Telegram 繁體中文，每小時狀態；去重後實際掃描數量及指定代號匹配數分開顯示。

## Railway 部署
1. 備份目前 GitHub 專案的 `worker.py` 與 `requirements.txt`。
2. 用本 ZIP 的同名檔案取代，並加入 `backtest.py`（可選）。
3. Railway Start Command 保持 `python worker.py`；保留原本 Telegram token/chat id，**不要貼到公開 GitHub**。
4. Railway Variables 設 `TOP_N=30`、`ONCE=0`、`TEST_TELEGRAM=0`。九個指定代號已有預設值；若舊環境有 `EQUITY_SYMBOLS`，請確認包含九個。
5. **建議掛載 Railway Volume 至 `/data`，並設 `DB_PATH=/data/scanner.sqlite3`**，否則重啟或重新部署可能丟失訊號及績效資料；原版資料庫可繼續使用，但需將舊 DB 複製到新路徑才會保留歷史。
6. 在 Deploy Logs 確認 `BinanceWin V5.0 started`、`Scan complete`，再確認 Telegram 每小時狀態。每輪 4 個週期、最多 39 個不同合約，請留意 Railway Trial 資源與交易所限流。
7. 若無法連上交易所或 K 線不完整，程式會記錄錯誤；請勿把 `errors=0` 誤解為交易策略有效。

## 離線回測

需要自己準備**歷史資料**，每個週期一份 CSV：`ETH-USDT-SWAP_5m.csv`、`_15m.csv`、`_1h.csv`、`_4h.csv`。欄位：`open_ms,open,high,low,close,volume,quote_volume,close_ms`（毫秒 Unix 時間）。

```bash
python backtest.py --folder data --symbol ETH-USDT-SWAP --out results.csv
```

這是簡化版歷史模擬：沒有真實訂單簿、資金費率、價格跳空、流動性限制或樣本外最佳化，故不保證未來績效。請以不同市場時段做樣本外測試，並確認訊號後才可取得的資料沒有進入判斷。

## 已知限制
- 未在此環境實際連線 OKX、BingX、Telegram 或 Railway；僅完成本機語法及離線測試。
- 交易所可能調整 API、合約名稱、上市狀態或限制頻率。
- 美股相關代號的真實底層資產**尚未逐一驗證**，不得據此認定可交易美股合約。
- 15M 高低價只能近似觸價，且 24H 超時以最後一根已收盤 K 線結算；重啟前沒有保留的歷史資料不能補算。
- 不包含自動下單、保證獲利或實盤槓桿建議。
