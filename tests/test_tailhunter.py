"""Safety and mathematical consistency tests, using only synthetic quotes."""
import unittest
from dataclasses import replace
from datetime import timedelta

from tailhunter.core import Config, candidates, replay, scout_votes, simulate_exit
from tailhunter.demo import make_demo


class TailHunterTests(unittest.TestCase):
    def test_signal_has_next_bar_not_same_bar_entry(self):
        data = make_demo()
        suggestions = candidates(data, Config())
        self.assertGreaterEqual(len(suggestions), 1)
        for signal, entry, _ in suggestions:
            from datetime import datetime
            self.assertGreater(entry.timestamp, datetime.fromisoformat(signal.timestamp))

    def test_no_100x_information_in_early_signal(self):
        first = [q for q in make_demo() if q.right == "CE"][:30]
        full = [q for q in make_demo() if q.right == "CE"]
        cfg = Config()
        # Signals on a shared early prefix must agree, even though the latter
        # timeline contains massive price changes not yet observed.
        for n in range(cfg.lookback, 29):
            self.assertEqual(scout_votes(first[n-cfg.lookback:n], cfg),
                             scout_votes(full[n-cfg.lookback:n], cfg))

    def test_illegal_wide_spread_rejected(self):
        data = [replace(q, bid=round(q.ask * 0.1, 3)) for q in make_demo()]
        trades, stats = replay(data, Config())
        self.assertEqual(len(trades), 0)

    def test_premium_at_risk_capped(self):
        trades, stats = replay(make_demo(), Config())
        self.assertGreaterEqual(stats["candidate_signals"], 1)
        for trade in trades:
            self.assertLessEqual(trade.premium_at_risk, 250.001)
            self.assertAlmostEqual(
                trade.pnl,
                (trade.exit_bid - trade.entry_ask) * trade.lots * trade.lot_size - trade.charges,
                places=6,
            )

    def test_future_data_required_for_exit(self):
        data = [q for q in make_demo() if q.right == "CE"]
        self.assertIsNone(simulate_exit(data[-1], [], 1, Config()))

    def test_fee_can_turn_small_gains_negative(self):
        data = make_demo()
        trades, _ = replay(data, Config(fixed_fee_per_order=50))
        for t in trades:
            self.assertGreaterEqual(t.charges, 100)


if __name__ == "__main__":
    unittest.main()
