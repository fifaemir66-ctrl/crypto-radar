"""Tests for the long-term signal and the alert schedule. Run: python3 -m unittest discover tests"""

import json
import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import longterm  # noqa: E402
import monitor   # noqa: E402

DAY = 86400
MONDAY = 1767571200   # 2026-01-05 00:00 UTC, a Monday


def candles_from(prices, start=MONDAY):
    out = []
    for i, p in enumerate(prices):
        prev = prices[i - 1] if i else p
        out.append({"t": start + i * DAY, "o": prev, "h": max(p, prev) * 1.005, "l": min(p, prev) * 0.995, "c": p, "v": 1000.0})
    return out


def uptrend_with_pullback(days=600):
    """Steady rise, a ~12% pullback to the 20/40-week area, then one up week."""
    prices = [100 * math.exp(0.0012 * i) + 3 * math.sin(i / 9) for i in range(days - 35)]
    top = prices[-1]
    prices += [top * (1 - 0.0045 * k) for k in range(1, 29)]      # 4 weeks down
    low = prices[-1]
    prices += [low * (1 + 0.006 * k) for k in range(1, 8)]          # one up week
    return prices


class LongTermSignal(unittest.TestCase):
    def test_only_completed_weeks_count(self):
        closed = candles_from([100] * 17)          # Mon 5 Jan .. Wed 21 Jan
        now = MONDAY + 16 * DAY + 3600              # Wednesday 21 Jan: the 3rd week is unfinished
        weeks = longterm.completed_weeks(closed, now)
        self.assertEqual(len(weeks), 2)
        self.assertEqual(len(longterm.completed_weeks(closed, MONDAY + 21 * DAY)), 3)

    def test_downtrend_is_avoid(self):
        prices = [300 * math.exp(-0.0015 * i) for i in range(500)]
        closed = candles_from(prices)
        lt = longterm.analyse_long("X", closed, now=closed[-1]["t"] + 8 * DAY)
        self.assertEqual(lt["signal"]["key"], "avoid")
        self.assertEqual(lt["trend_dir"], "down")

    def test_uptrend_far_above_zone_is_watch(self):
        prices = [100 * math.exp(0.004 * i) for i in range(500)]
        closed = candles_from(prices)
        lt = longterm.analyse_long("X", closed, now=closed[-1]["t"] + 8 * DAY)
        self.assertEqual(lt["trend_dir"], "up")
        self.assertEqual(lt["signal"]["key"], "watch")

    def test_not_enough_history(self):
        closed = candles_from([100 + i for i in range(200)])
        lt = longterm.analyse_long("X", closed, now=closed[-1]["t"] + 8 * DAY)
        self.assertEqual(lt["signal"]["key"], "avoid")
        self.assertIn("Not enough history", lt["signal"]["reason"])

    def test_signal_ignores_the_unfinished_week(self):
        closed = candles_from(uptrend_with_pullback())
        end = longterm.completed_weeks(closed, closed[-1]["t"] + 8 * DAY)[-1]["ws"] + longterm.WEEK
        base = longterm.analyse_long("X", [c for c in closed if c["t"] < end], now=end)
        # add a crash day in the next (unfinished) week: the signal must not change
        crash = dict(closed[-1], t=end + DAY, o=1, h=1, l=0.5, c=0.6)
        later = longterm.analyse_long("X", [c for c in closed if c["t"] < end] + [crash], now=end + 2 * DAY)
        self.assertEqual(base["signal"], later["signal"])

    def test_hysteresis_keeps_trend(self):
        closed = candles_from(uptrend_with_pullback())
        lt = longterm.analyse_long("X", closed, prev_trend="up", now=closed[-1]["t"] + 8 * DAY)
        self.assertIn(lt["trend_dir"], ("up",))


class ZoneFloor(unittest.TestCase):
    def test_close_under_zone_floor_is_not_in_zone(self):
        closed = candles_from(uptrend_with_pullback())
        lt = longterm.analyse_long("X", closed, prev_trend="up", now=closed[-1]["t"] + 8 * DAY)
        crash = [dict(c) for c in closed]
        floor = lt["zone"]["low"]
        # last full week closes 10% under the zone floor
        for c in crash[-7:]:
            c.update(o=floor * 0.9, h=floor * 0.92, l=floor * 0.88, c=floor * 0.9)
        lt2 = longterm.analyse_long("X", crash, prev_trend="up", now=closed[-1]["t"] + 8 * DAY)
        self.assertFalse(lt2["in_zone"])
        self.assertNotEqual(lt2["signal"]["key"], "buy")


class Schedule(unittest.TestCase):
    def test_digest_days(self):
        cfg = {"digest_days": ["mon", "thu"], "digest_hour_utc": 6}
        mon = datetime(2026, 10, 19, 7, tzinfo=timezone.utc)
        self.assertTrue(monitor.digest_due(cfg, {}, mon))
        self.assertFalse(monitor.digest_due(cfg, {"last_digest": "2026-10-19"}, mon))
        self.assertFalse(monitor.digest_due(cfg, {}, mon.replace(hour=5)))
        self.assertFalse(monitor.digest_due(cfg, {}, mon + timedelta(days=1)))   # Tuesday
        self.assertTrue(monitor.digest_due(cfg, {}, mon + timedelta(days=3)))    # Thursday

    def test_days_between(self):
        self.assertEqual(monitor.days_between("2026-10-01", "2026-10-15"), 14)
        self.assertGreater(monitor.days_between(None, "2026-10-15"), 1000)


class MonitorRun(unittest.TestCase):
    """End-to-end runs of monitor.run() with fake data and a fake ntfy."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "docs", "charts"))
        with open(os.path.join(self.dir, "config.json"), "w") as f:
            json.dump({"crypto": [{"symbol": "AAAUSDT", "name": "Aaa", "base": "AAA"}],
                       "stocks": [{"symbol": "BBB", "name": "Bbb", "base": "BBB"}]}, f)
        self.patches = [mock.patch.object(monitor, n, os.path.join(self.dir, p)) for n, p in
                        (("CONFIG_FILE", "config.json"), ("STATE_FILE", "state.json"), ("DOCS", "docs"))]
        self.sent = []
        self.patches.append(mock.patch.object(monitor, "send", side_effect=lambda t, m, *a, **k: self.sent.append((t, m)) or True))
        for p in self.patches:
            p.start()
        self.prices = {"AAAUSDT": [100 * math.exp(0.004 * i) for i in range(600)],
                       "BBB": [300 * math.exp(-0.0015 * i) for i in range(600)]}
        self.fail = set()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def fake_crypto(self, sym, limit=1000):
        if sym in self.fail:
            raise RuntimeError("down")
        c = candles_from(self.prices[sym], start=self.start)
        return c + [dict(c[-1], t=c[-1]["t"] + DAY)], "Binance", False

    def fake_stock(self, sym):
        if sym in self.fail:
            raise RuntimeError("down")
        return candles_from(self.prices[sym], start=self.start), "Yahoo Finance", True, "USD"

    def run_at(self, now):
        # data always ends the day before `now`
        self.start = int(now.timestamp()) // DAY * DAY - len(self.prices["AAAUSDT"]) * DAY
        with mock.patch.object(monitor, "fetch_crypto", self.fake_crypto), mock.patch.object(monitor, "fetch_stock", self.fake_stock):
            return monitor.run(now=now)

    def test_first_run_then_quiet_then_digest(self):
        tue = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
        self.run_at(tue)
        self.assertEqual(len(self.sent), 1)                 # the one-time "long-term mode" message
        self.sent.clear()
        for h in range(1, 30):                              # every hour Tue → Wed: no alerts at all
            self.run_at(tue + timedelta(hours=h))
        self.assertEqual(self.sent, [])
        thu = datetime(2026, 10, 22, 6, 5, tzinfo=timezone.utc)
        self.run_at(thu)
        self.run_at(thu + timedelta(hours=1))
        self.assertEqual(len(self.sent), 1)                 # exactly one digest on Thursday
        self.assertIn("ZoneHunter update", self.sent[0][0])

    def test_failed_market_stays_on_site_as_stale(self):
        tue = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
        self.run_at(tue)
        self.fail.add("BBB")
        summaries, errors, _ = self.run_at(tue + timedelta(hours=1))
        self.assertEqual([e["symbol"] for e in errors], ["BBB"])
        bbb = [s for s in summaries if s["symbol"] == "BBB"]
        self.assertTrue(bbb and bbb[0]["stale"])

    def test_buy_alert_once_with_cooldown(self):
        tue = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
        state = {"version": monitor.STATE_VERSION, "changes": [], "history": [], "last_digest": "2026-10-20",
                 "coins": {"AAAUSDT": {"signal": "watch", "day": "2026-10-11", "lt_trend": "up"}}}
        with open(os.path.join(self.dir, "state.json"), "w") as f:
            json.dump(state, f)
        buy = {"signal": {"key": "buy", "label": "BUY SIGNAL", "reason": "r", "plan": "p", "strength": 90},
               "trend": "Long-term uptrend", "trend_dir": "up", "trend_score": 4, "trend_reasons": [],
               "checklist": [], "scenarios": [], "plan": None, "zone": {"low": 1, "high": 2}, "in_zone": True,
               "dist_zone": 0, "close_day": "2026-10-18", "weeks": 80}
        with mock.patch.object(monitor, "analyse_long", return_value=buy):
            self.run_at(tue)
            self.run_at(tue + timedelta(hours=1))
        buys = [t for t, _ in self.sent if "Pullback" in t]
        self.assertEqual(len(buys), 1)   # AAA turned buy (BBB had no earlier signal): one alert, no repeat an hour later
        # two weeks later AAA flips watch → buy again: inside the 8-week cooldown, so no new alert
        st = json.load(open(os.path.join(self.dir, "state.json")))
        st["coins"]["AAAUSDT"]["signal"] = "watch"
        json.dump(st, open(os.path.join(self.dir, "state.json"), "w"))
        with mock.patch.object(monitor, "analyse_long", return_value={**buy, "close_day": "2026-11-01"}):
            self.run_at(tue + timedelta(days=14))
        self.assertEqual(len([t for t, _ in self.sent if "Pullback" in t]), 1)


    def _buy_setup(self):
        state = {"version": monitor.STATE_VERSION, "changes": [], "history": [], "last_digest": "2026-10-20",
                 "coins": {"AAAUSDT": {"signal": "watch", "day": "2026-10-11", "lt_trend": "up"},
                           "BBB": {"signal": "avoid", "day": "2026-10-11", "lt_trend": "down"}}}
        with open(os.path.join(self.dir, "state.json"), "w") as f:
            json.dump(state, f)
        return {"signal": {"key": "buy", "label": "PULLBACK + UP WEEK", "reason": "r", "plan": "p", "strength": 90},
                "trend": "Long-term uptrend", "trend_dir": "up", "trend_score": 4, "trend_reasons": [],
                "checklist": [], "scenarios": [], "plan": None, "zone": {"low": 1, "high": 2}, "in_zone": True,
                "dist_zone": 0, "close_day": "2026-10-18", "weeks": 80}

    def test_buys_are_bundled_and_held_until_morning(self):
        buy = self._buy_setup()
        night = datetime(2026, 10, 20, 0, 30, tzinfo=timezone.utc)
        with mock.patch.object(monitor, "analyse_long", return_value=buy):
            self.run_at(night)
            self.assertEqual(self.sent, [])                       # nothing at night
            self.run_at(night.replace(hour=6, minute=10))
        self.assertEqual(len(self.sent), 1)                       # both markets in one message
        self.assertIn("2 markets", self.sent[0][0])

    def test_failed_send_keeps_buy_pending_and_out_of_history(self):
        buy = self._buy_setup()
        morning = datetime(2026, 10, 20, 7, tzinfo=timezone.utc)
        with mock.patch.object(monitor, "analyse_long", return_value=buy), \
             mock.patch.object(monitor, "send", return_value=False):
            _, _, state = self.run_at(morning)
        self.assertEqual(state["history"], [])
        self.assertEqual(len(state["pending_buys"]), 2)
        self.assertNotIn("buy_alert_day", state["coins"]["AAAUSDT"])
        with mock.patch.object(monitor, "analyse_long", return_value=buy):
            self.run_at(morning.replace(hour=8))                  # ntfy is back: the alert goes out now
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
