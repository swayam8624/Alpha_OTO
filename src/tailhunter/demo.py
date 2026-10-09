"""DELIBERATELY SYNTHETIC demo with a contrived extreme-price scenario.

Never interpret this generated series as genuine market history or evidence
that the scout could predict such an outcome in a live exchange.
"""
from datetime import datetime, timedelta, timezone
from .core import Quote

IST = timezone(timedelta(hours=5, minutes=30))


def make_demo() -> list[Quote]:
    anchor = datetime(2026, 9, 22, 10, 0, tzinfo=IST)
    result = []
    for right in ("CE", "PE"):
        for i in range(100):
            # Contrived mini-rally followed by an implausibly large option jump.
            # The deliberately exaggerated later prices verify code paths ONLY.
            if i < 20:
                underlying = 20_000 + i * 0.15
            elif i < 35:
                underlying = 20_003 + (i - 19) * 41
            else:
                underlying = 20_618 + (i - 35) * 9

            if right == "CE":
                if i < 22:
                    mid = 0.46
                elif i < 29:
                    mid = 0.46 * (1.16 ** (i - 21))
                elif i < 59:
                    mid = (0.46 * (1.16 ** 7)) * (1.21 ** (i - 28))
                else:
                    mid = (0.46 * (1.16 ** 7)) * (1.21 ** 30) * (0.955 ** (i - 58))
                volume = 90 if i < 21 else (420 if i in (23, 24, 25, 26, 27, 28, 29) else 130)
                iv = 0.24 if i < 22 else 0.24 + min(i - 21, 25) * 0.014
                bid = round(max(0.05, mid * 0.92), 2)
                ask = round(max(bid + 0.05, mid * 1.08), 2)
                lots = 100
            else:
                # Put contracts stay inexpensive as the underlying rises.
                mid = max(0.07, 1.2 * (0.98 ** i))
                bid = round(max(0.05, mid * 0.9), 2)
                ask = round(max(bid + 0.05, mid * 1.1), 2)
                volume = 100
                iv = 0.24
                lots = 30
            result.append(Quote(
                timestamp=anchor + timedelta(minutes=i),
                contract=f"SYNTH_NIFTY_{right}_20400_20260922",
                right=right, expiry="2026-09-22", underlying=underlying,
                bid=bid, ask=ask, bid_lots=lots, ask_lots=lots,
                volume=volume, iv=iv, lot_size=25, event_public=0,
            ))
    return result
