BinanceWin V4.5.1 最終版｜iPhone + GitHub + Railway

預設九個監控代號：RKLB、TSLA、SNXX、SNDK、SPCX、MU、MUU、NVDA、NVDL。
注意：不是每個代號都必然在 OKX/BingX 上架。程式只匹配實際可用的 USDT 永續合約代號；同名不代表已確認追蹤美股或 ETF，務必核對官方合約詳情、標的及所在地資格。

安裝：
1. iPhone 下載 ZIP 並解壓縮。
2. GitHub Repository 根目錄的 worker.py 必須以此版本覆蓋，檔名保持 worker.py。
3. requirements.txt 可用此版本覆蓋。
4. Railway Start Command：python worker.py；Cron Schedule 空白；Serverless OFF。
5. Railway Variables：ONCE=0、TEST_TELEGRAM=0、TOP_N=30；保留 Telegram Bot Token 與 Chat ID。
6. EQUITY_SYMBOLS 可不設定（預設九個代號），或填 RKLB,TSLA,SNXX,SNDK,SPCX,MU,MUU,NVDA,NVDL。
7. Logs 查看 WATCHLIST SYMBOL FOUND、WATCHLIST NOT LISTED、Scan complete。
8. 每小時 Telegram 狀態將顯示指定合約匹配數及未匹配代號；匹配不保證標的屬於美股。
9. 若 Railway 未配置持久 Volume，SQLite 去重及績效紀錄可能在部署後遺失。
10. 只研究行情，不下單；歷史方向正確率不等於扣費後獲利。
11. 如 Telegram Token 曾在截圖曝光，請至 BotFather 撤銷並換新。
