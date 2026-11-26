# BinanceWin V5.4 精準進場追蹤研究版
1. 先備份 GitHub V5.3 與 SQLite。解壓 ZIP，將根目錄檔案上傳原 GitHub main，覆蓋同名檔案並新增 tracking54.py。Railway Start Command 保持 python worker.py。
2. 原 Telegram 變數、ONCE=0、TOP_N=30 保留。若已有 Volume，沿用 DB_PATH；沒有 Volume 時先取回舊資料庫，再掛載 /data 並設定 DB_PATH=/data/scanner.sqlite3、BACKUP_DIR=/data/backups。無法取回舊檔就不能保證歷史保留。備份不是異地備份。
3. 確認 Railway Deployment successful、Logs 顯示 V5.4，Telegram 每小時狀態不再列人工核准與未驗證名單。九個指定合約仍需人工驗證，不能僅靠代號核准。原有 VERIFIED_EQUITY_CONTRACTS 不變。
4. 15分鐘掃描，強訊號前三名與冷卻保留。新增獨立 tracking54 表，每分鐘嘗試取得原交易所已收盤1m K線；訊號建立後下一完整分鐘起，開盤落在區間且至少1.5R才假設成交，訊號K線後60分鐘失效，24小時到期。固定原停損停利，遇同根雙觸及停損優先，缺K線停止結算。MFE/MAE 排除退出分鐘。報酬扣估計費用與滑價，未含實際資金費。不是實際成交。掃描或網路延遲可能使通知晚於一分鐘。若同分鐘成交並結算，只發結算通知。
5. 每日報告中文前三名，30筆以下標示樣本不足；原 v53 三策略模擬及歷史資料保留，與新增追蹤分開。市場環境為 BTC MACD 方向簡易分類。舊版回測仍用15m，不等於新增1m追蹤。原版美股驗證及事件手動黑名單保留。
限制：未接入美股獨立行情/交易日曆、實際資金費結算、策略自動淘汰、新權重、RSI/OI背離或完整市場環境分類；新增追蹤尚未做真實行情回測或 Railway/API 實測。既有 app.py 非 Railway worker，未修改。Telegram 網路重試可能重複通知。不要把評分當勝率。測試見 TEST_RESULTS.txt。
