#!/usr/bin/env python3
"""
ZoneHunter -- backtest of the long-term signal.

Replays the weekly signal week by week on years of history (no look-ahead: each week only
sees the daily candles up to its own close) and reports:
  * how often a new BUY SIGNAL fires (= how many immediate alerts you would get)
  * the average return 13 / 26 / 52 weeks after a BUY, versus after any random week
  * how often the long-term trend flips

Usage:
    python3 backtest.py                 all markets in config.json
    python3 backtest.py BTCUSDT SPY     only these
    python3 backtest.py --json out.json also save the raw results

Caveats: the market list is today's survivors (no dead coins), windows overlap and the
number of buys per market is small, so treat every number as rough.
Educational tool -- past results say nothing certain about the future.
"""

import argparse
import json
import os
import statistics
import sys
import time

from analysis import _get, check_fresh
from longterm import WEEK, analyse_long, week_start

ROOT = os.path.dirname(os.path.abspath(__file__))
HORIZONS = (13, 26, 52)
COOLDOWN = 8   # same rule as the monitor: at most one buy alert per market per 8 weeks


def crypto_history(symbol, days=2500):
    """Binance gives 1000 daily candles per call: page back in time."""
    out, end = {}, None
    while len(out) < days:
        url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1d&limit=1000"
        if end:
            url += f"&endTime={end}"
        raw = _get(url)
        if not raw:
            break
        for k in raw:
            out[k[0]] = {"t": k[0] // 1000, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                         "c": float(k[4]), "v": float(k[5])}
        end = raw[0][0] - 1
        if len(raw) < 1000:
            break
    candles = [out[k] for k in sorted(out)]
    return candles[:-1]   # the last candle is still open


def stock_history(ticker):
    d = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=10y&interval=1d")
    res = d["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    candles = []
    for i, t in enumerate(res["timestamp"]):
        o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None not in (o, h, l, c):
            candles.append({"t": int(t), "o": o, "h": h, "l": l, "c": c, "v": float(v or 0)})
    if candles and time.time() - candles[-1]["t"] < 86400:
        candles = candles[:-1]   # today may be a partial session
    return candles


def replay(symbol, closed):
    """Run the long-term signal at every completed week, exactly as the monitor would have."""
    rows, prev_trend, prev_key = [], None, None
    week_ends = sorted({week_start(c["t"]) + WEEK for c in closed})
    j = 0
    for we in week_ends:
        while j < len(closed) and closed[j]["t"] < we:
            j += 1
        seen = closed[:j]
        if not seen:
            continue
        lt = analyse_long(symbol, seen, prev_trend, now=we)
        if lt["weeks"] < 45:
            continue
        key = lt["signal"]["key"]
        rows.append({"week_end": we, "close": seen[-1]["c"], "key": key, "new": key != prev_key,
                     "trend": lt["trend_dir"]})
        prev_trend, prev_key = lt["trend_dir"], key
    return rows


def stats(symbol, rows):
    closes = [r["close"] for r in rows]
    years = len(rows) / 52.18 if rows else 0
    buys, last = [], -999
    for i, r in enumerate(rows):
        if r["key"] == "buy" and r["new"] and i > 0 and i - last >= COOLDOWN:
            buys.append(i)
            last = i
    flips = sum(1 for i in range(1, len(rows)) if rows[i]["trend"] != rows[i - 1]["trend"])
    out = {"symbol": symbol, "weeks": len(rows), "years": round(years, 1),
           "buy_alerts": len(buys), "buy_alerts_per_year": round(len(buys) / years, 1) if years else 0,
           "trend_flips_per_year": round(flips / years, 1) if years else 0,
           "share_in_uptrend": round(sum(r["trend"] == "up" for r in rows) / len(rows) * 100) if rows else 0}
    for h in HORIZONS:
        after_buy = [closes[i + h] / closes[i] - 1 for i in buys if i + h < len(closes)]
        any_week = [closes[i + h] / closes[i] - 1 for i in range(len(closes) - h)]
        out[f"buy_{h}w"] = round(statistics.mean(after_buy) * 100, 1) if after_buy else None
        out[f"buy_{h}w_win"] = round(sum(x > 0 for x in after_buy) / len(after_buy) * 100) if after_buy else None
        out[f"any_{h}w"] = round(statistics.mean(any_week) * 100, 1) if any_week else None
        # excess return of every buy vs the median week IN AN UPTREND of the same market (the fair comparison)
        up_weeks = [closes[i + h] / closes[i] - 1 for i in range(len(closes) - h) if rows[i]["trend"] == "up"]
        base = statistics.median(up_weeks) if up_weeks else 0
        out[f"excess_{h}w"] = [x - base for x in after_buy]
        out[f"n_{h}w"] = len(after_buy)
    # avoid periods: what did the market do while the signal said AVOID?
    avoid = [closes[i + 13] / closes[i] - 1 for i, r in enumerate(rows) if r["key"] == "avoid" and i + 13 < len(closes)]
    out["avoid_13w"] = round(statistics.mean(avoid) * 100, 1) if avoid else None
    return out


def main():
    ap = argparse.ArgumentParser(description="ZoneHunter long-term backtest")
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--json", help="save raw results to this file")
    args = ap.parse_args()
    config = json.load(open(os.path.join(ROOT, "config.json")))
    assets = [("crypto", m["symbol"]) for m in config.get("crypto", [])] + [("stocks", m["symbol"]) for m in config.get("stocks", [])]
    if args.symbols:
        assets = [x for x in assets if x[1] in args.symbols]

    results = []
    head = f"{'market':<9}{'yrs':>5}{'buys/yr':>8}{'flips/yr':>9}{'up%':>5} | {'buy→13w':>8}{'any 13w':>8} | {'buy→26w':>8}{'any 26w':>8} | {'buy→52w':>8}{'win%':>5}{'any 52w':>8} | {'avoid→13w':>9}"
    print(head)
    print("-" * len(head))
    for market, sym in assets:
        try:
            closed = crypto_history(sym) if market == "crypto" else stock_history(sym)
            check_fresh(sym, closed + [closed[-1]], 6)
            st = stats(sym, replay(sym, closed))
        except Exception as e:
            print(f"{sym:<9} error: {e}", file=sys.stderr)
            continue
        st["market"] = market
        results.append(st)
        f = lambda x: "–" if x is None else f"{x:+.1f}"
        print(f"{sym:<9}{st['years']:>5}{st['buy_alerts_per_year']:>8}{st['trend_flips_per_year']:>9}{st['share_in_uptrend']:>5} | "
              f"{f(st['buy_13w']):>8}{f(st['any_13w']):>8} | {f(st['buy_26w']):>8}{f(st['any_26w']):>8} | "
              f"{f(st['buy_52w']):>8}{(st['buy_52w_win'] if st['buy_52w_win'] is not None else '–'):>5}{f(st['any_52w']):>8} | {f(st['avoid_13w']):>9}")

    if results:
        def avg(k):
            xs = [r[k] for r in results if r[k] is not None]
            return round(statistics.mean(xs), 1) if xs else None
        total_buys = sum(r["buy_alerts_per_year"] for r in results)
        print("-" * len(head))
        for market in ("stocks", "crypto"):
            parts = []
            for h in HORIZONS:
                xs = [x for r in results if r["market"] == market for x in r[f"excess_{h}w"]]
                if xs:
                    parts.append(f"{h}w {statistics.median(xs) * 100:+.1f}% (n={len(xs)})")
            if parts:
                print(f"{market.capitalize()}: pooled median excess of a buy vs an uptrend week: " + " · ".join(parts))
        print(f"Average: buy→13w {avg('buy_13w')}% vs any {avg('any_13w')}% · buy→26w {avg('buy_26w')}% vs any {avg('any_26w')}% · "
              f"buy→52w {avg('buy_52w')}% vs any {avg('any_52w')}% · avoid→13w {avg('avoid_13w')}%")
        print(f"Alerts: ≈ {total_buys:.0f} buy alerts per year over {len(results)} markets "
              f"(≈ {total_buys / 52:.1f} per week) + 2 updates per week")
    if args.json:
        json.dump(results, open(args.json, "w"), indent=2)


if __name__ == "__main__":
    main()
