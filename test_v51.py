"""Offline smoke tests. No external APIs, no real Telegram messages."""
import os
import tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as tmp:
    os.environ['DB_PATH'] = str(Path(tmp)/'scanner.sqlite3')
    os.environ['BACKUP_DIR'] = str(Path(tmp)/'backups')
    import worker as w
    w.init_db()
    assert w.DB.exists()
    assert w.fmt_price(123.456) == '123.46'
    assert w.tw_time('2026-11-16T12:29:59+00:00') == '2026/11/16 20:29'
    w.backup_database()
    assert len(list(w.BACKUP_DIR.glob('scanner-*.sqlite3'))) == 1
    assert '每日模擬報告' in w.daily_report()
    assert '台灣時間' in w.status_message({'venue':'OKX','successful':0,'attempted':0,'crypto_listed':0,'equity_matched':0,'equity_missing':[],'equity_unverified':[],'candidates':0,'watch':0,'sent':0,'errors':0,'notes':[]})
    sent=[]
    w.telegram=lambda message:sent.append(message)
    row={'direction':'偏空','grade':'觀察訊號','score':70,'candle':'2026-11-16T12:29:59+00:00','price':100.,'atr':1.,'stop':101.5,'target':97.,'volume':2.,'rsi':35.,'k':30.,'d':40.,'missing':['4H趨勢']}
    assert w.notify('OKX','TEST-USDT-SWAP',row)
    assert not w.notify('OKX','TEST-USDT-SWAP',row)
    row['score']=85;row['grade']='強訊號'
    assert w.notify('OKX','TEST-USDT-SWAP',row)
    assert len(sent)==2
    with w.sqlite3.connect(w.DB) as c:
        assert c.execute('SELECT COUNT(*) FROM alert_state').fetchone()[0]==1
print('PASS: DB init, backup, report, time format, first alert, dedupe, upgrade')
