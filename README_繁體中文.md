# BinanceWin V5.3 研究版
上傳本 ZIP 解壓後的檔案至原 GitHub main，Railway Start Command 保持 `python worker.py`，ONCE=0，原 Telegram 變數保留。不要上傳 token。先備份現有 SQLite；若已有 Volume，沿用原 DB_PATH。若沒有，先在原服務新增 Volume 掛載 /data，將原資料庫備份移入後才設定 DB_PATH=/data/scanner.sqlite3；無法取回舊檔時不可宣稱已遷移。BACKUP_DIR=/data/backups。備份同磁碟並非異地備份。原表保留，新表 v53、oi53 分開。每日台灣21時報告。

嚴格型才通知，最多三名，120分鐘冷卻。平衡、嚴格、回踩三套研究，強訊號另比較固定、保本、移動停損。ADX 25、OI 一小時增量非負、方向資金費率上限0.05%、BTC 1H/4H MACD同向為初始假設。OI 需約一小時暖機；缺值不視為通過嚴格型。美股九個代號只做交易所匹配，人工核對交易所合約規格及底層後，才將精確 ID 加入 VERIFIED_EQUITY_CONTRACTS（逗號分隔）。不要盲目核准。EVENT_BLACKOUT_UTC 沿用原版手動事件設定。

回測：python backtest.py --folder data --symbol BTC-USDT-SWAP --out results.csv。沿用四個 OHLCV CSV，新增 BTC-USDT-SWAP_context.csv 欄位 close_ms,btc_direction,oi_change,funding_rate，必須是當時可得資料，BTC方向為 -1/0/1，OI及費率為小數。回測及即時共用 simulate：下一根開盤且在區間內才成交，缺K線不計績效，同根停損優先，跳空停損按開盤，保本/移動停損次根生效，24小時到期。固定2R，費用預設16bps。尚未接入實際歷史資金費結算，因此報酬未含資金費；不是完整淨報酬。分組績效非整體資金曲線。無真實行情樣本回測、無實際API連線驗證；BingX衍生數據格式需部署確認。交易所切換期間舊來源未結算需待來源恢復。此版本為研究測試版，非實盤獲利保證。
