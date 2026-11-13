# 合約雷達 V4.0｜iPhone + Telegram 自動提醒

## 功能
- 保留 V3 手機版 Streamlit 選幣、KDJ/RSI/MACD、回測與 CSV。
- 新增獨立 `worker.py`，每 15 分鐘 K 線收盤約 20 秒後自動掃描。
- 符合放量、突破、MACD、RSI、KDJ 條件時傳送 Telegram 訊息。
- 使用 SQLite 對「標的 + 方向 + K 線收盤時間」去重；同一根 K 線不重複提醒。
- 無交易 API Key、不下單、不保證獲利。

## 安裝（需可長時間運行的電腦或雲端主機）
```bash
python -m pip install -r requirements.txt
streamlit run app.py
```
另開終端機，設定環境變數後執行 `python worker.py`，背景掃描才會開始。
`streamlit run app.py` 本身**不會**啟動背景提醒。

### Telegram 設定
1. Telegram 搜尋官方 `@BotFather`，建立 bot，取得 token。
2. 在 Telegram 傳訊息給自己的 bot（按 Start）。
3. 透過 `https://api.telegram.org/bot<你的token>/getUpdates` 查詢訊息中的 `chat.id`。
4. 將 `TELEGRAM_BOT_TOKEN` 與 `TELEGRAM_CHAT_ID` 設成**伺服器環境變數**。不要把 token 寫入程式碼或上傳 GitHub。
5. 測試單次掃描：`ONCE=1 python worker.py`（Windows PowerShell 可用 `$env:ONCE="1"; python worker.py`）。
6. 正式運作：`python worker.py`，並使用主機服務管理器保持常駐。

`.env.example` 僅供參考；程式不會自動讀取 `.env`，必須自行設定環境變數。

## iPhone
將 Streamlit 網站部署到可存取的 HTTPS 網址，再使用 Safari → 分享 → 加入主畫面。**僅有程式檔不等於網站已上線。**

## 注意
- 15 分鐘排程不等於零延遲；網路延遲、API 限速與停機都會影響提醒。
- worker 重啟後依當前最新已收盤 K 線繼續；不補發停機期間所有歷史訊號。
- Telegram API 傳送成功但 SQLite 寫入失敗時，極少數情況仍可能重複提醒。
- 資料庫應放在持久磁碟；多個 worker 同時運作可能造成重複推播，請只啟動一個。
- Streamlit Cloud 不保證常駐背景 worker；需另行部署可長期運作的服務。
- 回測不包含歷史資金費、爆倉與真實撮合，並不代表可實現的交易績效。
