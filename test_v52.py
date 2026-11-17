import unittest
import pandas as pd
import numpy as np
import worker
from datetime import datetime,timezone

class StrictTests(unittest.TestCase):
 def test_event_windows(self):
  self.assertFalse(worker.in_event_blackout(datetime(2026,11,17,tzinfo=timezone.utc)))
 def test_no_risk_budget_in_message_source(self):
  import inspect
  source=inspect.getsource(worker.notify)
  self.assertNotIn('風險試算',source)
  self.assertIn('確認狀態',source)
 def test_strict_breakout(self):
  # A weak small-body breakout must not satisfy strict confirmation.
  n=100; d=pd.DataFrame({'open':np.ones(n)*100,'close':np.ones(n)*100,'high':np.ones(n)*101,'low':np.ones(n)*99})
  row=pd.Series({'open':101.,'close':101.1,'high':102.,'low':100.})
  checks,_,_=worker.strict_conditions(d,d,d,d,row,d.iloc[-21:-1],'偏多',1.,2.,60.,True,True,True)
  self.assertFalse(checks['有效突破'])
 def test_backtest_metrics(self):
  from backtest import metrics
  result=metrics([{'net_pct':2},{'net_pct':-1}]);self.assertEqual(result['count'],2)
if __name__=='__main__':unittest.main()
