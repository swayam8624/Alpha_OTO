"""Offline gap-repair regressions: empty windows must not crash or fabricate data."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alpha_oto.data import Bar, coinbase_candles, read_csv, write_csv
from alpha_oto.repair import repair_coinbase


class EmptyResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return b"[]"


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.path = Path(self.t.name) / "BTC-USD.csv"
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.needed = start + timedelta(hours=2)
        self.bars = [Bar(start + timedelta(hours=i), "BTC-USD", 100, 101, 99, 100, 1)
                     for i in (0, 1, 3, 4, 5)]
        write_csv(self.path, self.bars)

    def test_empty_coinbase_response_is_tolerated_only_when_explicit(self):
        with patch('alpha_oto.data.urlopen', return_value=EmptyResponse()), patch('alpha_oto.data.sleep'):
            bars = coinbase_candles('BTC-USD', self.needed,
                                  self.needed + timedelta(hours=1), allow_empty=True)
            self.assertEqual(bars, [])
            with self.assertRaisesRegex(ValueError, 'Empty OHLCV series'):
                coinbase_candles('BTC-USD', self.needed,
                                 self.needed + timedelta(hours=1))

    def test_missing_candle_unavailable_does_not_crash_or_fill(self):
        original = self.path.read_bytes()
        with patch('alpha_oto.repair.coinbase_candles', return_value=[]) as fetch:
            result = repair_coinbase(str(self.path))
        self.assertEqual(result['repair_status'], 'PARTIAL_SOURCE_UNAVAILABLE')
        self.assertEqual(result['recovered_intervals'], 0)
        self.assertEqual(result['unresolved_intervals'], 1)
        self.assertEqual(result['unresolved_timestamps_utc'], [self.needed.isoformat()])
        self.assertFalse(result['quality_pass'])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(read_csv(result['output_csv'])), 5)
        self.assertTrue(Path(result['output_csv'] + '.repair.json').exists())
        self.assertTrue(fetch.call_args.kwargs['allow_empty'])

    def test_true_candle_is_recovered_without_fabrication(self):
        actual = Bar(self.needed, 'BTC-USD', 100, 102, 98, 101, 3)
        with patch('alpha_oto.repair.coinbase_candles', return_value=[actual]):
            result = repair_coinbase(str(self.path))
        self.assertEqual(result['repair_status'], 'COMPLETE')
        self.assertEqual(result['recovered_intervals'], 1)
        self.assertEqual(result['unresolved_intervals'], 0)
        loaded = read_csv(result['output_csv'])
        self.assertEqual(len(loaded), 6)
        self.assertEqual(loaded[2], actual)

    def test_unexpected_source_exception_is_not_masked(self):
        with patch('alpha_oto.repair.coinbase_candles', side_effect=ConnectionError('offline')):
            with self.assertRaisesRegex(ConnectionError, 'offline'):
                repair_coinbase(str(self.path))
        self.assertFalse(self.path.with_name('BTC-USD_repaired.csv').exists())

    def test_invalid_interval_fails_before_network(self):
        with self.assertRaisesRegex(ValueError, 'does not support'):
            repair_coinbase(str(self.path), interval_seconds=123)

    def test_no_gaps_does_not_call_endpoint(self):
        self.bars.insert(2, Bar(self.needed, 'BTC-USD', 100, 101, 99, 100, 1))
        write_csv(self.path, self.bars)
        with patch('alpha_oto.repair.coinbase_candles') as fetch:
            result = repair_coinbase(str(self.path))
        fetch.assert_not_called()
        self.assertEqual(result['repair_status'], 'NO_GAPS')
        self.assertEqual(result['unresolved_intervals'], 0)


if __name__ == '__main__':
    unittest.main()
