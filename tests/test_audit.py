"""Offline data-quality acceptance tests; never reaches Coinbase or a broker."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from alpha_oto.audit import audit_bars
from alpha_oto.data import Bar, write_csv


def bars(count=6):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [Bar(start+timedelta(hours=i),"BTC-USD",100,101,99,100,10) for i in range(count)]


class DataAuditTests(unittest.TestCase):
    def test_complete_continuous_history_passes(self):
        b=bars()
        x=audit_bars(b, interval_seconds=3600, now=b[-1].timestamp+timedelta(hours=2))
        self.assertTrue(x["quality_pass"])
        self.assertEqual(x["coverage_between_first_and_last"],1)
        self.assertFalse(x["trade_authorized"])

    def test_missing_hour_is_detected(self):
        b=bars()
        b.pop(2)
        result=audit_bars(b, interval_seconds=3600,now=b[-1].timestamp+timedelta(hours=2))
        self.assertIn("GAPS_IN_CONTINUOUS_MARKET",result["issues"])
        self.assertEqual(result["missing_intervals_between_first_and_last"],1)
        self.assertEqual(result["coverage_between_first_and_last"],round(5/6,8))

    def test_unfinished_bar_is_rejected(self):
        b=bars()
        result=audit_bars(b,interval_seconds=3600,now=b[-1].timestamp+timedelta(minutes=30))
        self.assertFalse(result["quality_pass"])
        self.assertIn("LATEST_BAR_NOT_YET_COMPLETE_OR_FROM_FUTURE",result["issues"])

    def test_session_market_requires_calendar_validation(self):
        b=bars()
        result=audit_bars(b,interval_seconds=3600,continuous=False,now=b[-1].timestamp+timedelta(hours=2))
        self.assertIn("SESSION_CALENDAR_NOT_VALIDATED",result["issues"])
        self.assertIsNone(result["coverage_between_first_and_last"])

    def test_source_file_digest(self):
        b=bars()
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"prices.csv"
            write_csv(p,b)
            result=audit_bars(b,interval_seconds=3600,file_path=p,now=b[-1].timestamp+timedelta(hours=2))
            self.assertEqual(len(result["source_file_sha256"]),64)

    def test_invalid_interval(self):
        with self.assertRaises(ValueError):
            audit_bars(bars(),interval_seconds=0)


if __name__ == "__main__":
    unittest.main()
