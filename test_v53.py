import unittest
import pandas as pd
from strategy53 import simulate,adx
class Test(unittest.TestCase):
 def setUp(self):self.r=dict(t=899999,direction='偏多',price=100,stop=99,lo=99.5,hi=100.5)
 def bars(self,rows):return pd.DataFrame([dict(open_ms=900000+i*900000,close_ms=1799999+i*900000,open=o,high=h,low=l,close=c) for i,(o,h,l,c) in enumerate(rows)])
 def test_stop_first(self):self.assertEqual(simulate(self.r,self.bars([(100,103,98,101)]))['outcome'],'SL')
 def test_no_chase(self):self.assertEqual(simulate(self.r,self.bars([(101,103,98,101)]))['outcome'],'NO_FILL')
 def test_breakeven_next_bar(self):self.assertAlmostEqual(simulate(self.r,self.bars([(100,101.5,99.5,101),(101,101.5,99.9,100)]),exit_mode='breakeven')['net_pct'],-.16)
 def test_gap(self):self.assertAlmostEqual(simulate(self.r,self.bars([(100,101,99.5,100),(98,99,97,98)]))['net_pct'],-2.16)
 def test_adx(self):
  d=pd.DataFrame(dict(high=range(2,102),low=range(100),close=range(1,101)));self.assertGreater(adx(d),90)
