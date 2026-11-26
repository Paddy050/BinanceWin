import unittest,tempfile
from pathlib import Path
from types import SimpleNamespace
import worker as w
from tracking54 import step,init_tracking,tracking_count
class TrackingTests(unittest.TestCase):
 def setUp(self):self.r=dict(t=0,direction='偏空',lo=99,hi=101,stop=102,target=96)
 def bar(self,o,h,l,t=60000):return SimpleNamespace(open_ms=t,close_ms=t+59999,open=o,high=h,low=l,close=o)
 def test_target(self):self.assertEqual(step(self.r,'WAIT',None,None,None,0,0,self.bar(100,101,95),16)[0],'TP')
 def test_stop_first(self):self.assertEqual(step(self.r,'WAIT',None,None,None,0,0,self.bar(100,103,95),16)[0],'SL')
 def test_wait(self):self.assertEqual(step(self.r,'WAIT',None,None,None,0,0,self.bar(98,100,95),16)[0],'WAIT')
 def test_expire(self):self.assertEqual(step(self.r,'WAIT',None,None,None,0,0,self.bar(100,101,99,3660000),16)[0],'EXPIRED')
 def test_status(self):
  old=w.DB
  with tempfile.TemporaryDirectory() as d:
   try:
    w.DB=Path(d)/'db';w.init_db();init_tracking();self.assertEqual(tracking_count(),0)
    s=w.status_message(dict(venue='OKX',successful=35,attempted=35,candidates=0,watch=1,sent=0,errors=0,notes=[],equity_unverified=['SECRET'],equity_matched=9));self.assertNotIn('SECRET',s);self.assertNotIn('人工核准',s)
   finally:w.DB=old
