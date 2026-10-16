"""
ZoneHunter -- long-term signal (weekly closes).

The headline signal is decided on COMPLETED weekly candles only, so it changes at most
once a week:
  * trend: weekly close vs the 40-week SMA (~200 days), its slope, 12-month momentum and
    the 10-week EMA, with hysteresis so one bad week does not flip it
  * value zone: a pullback to the rising 20-week EMA / 40-week SMA area
  * confirmation: a weekly close in the zone that closes up (above the week's open and the
    previous weekly close)

The daily analysis in analysis.py stays available as short-term timing.
Signals follow fixed strategy rules. Educational tool -- not financial advice.
"""

import time

from analysis import ema, fmt, rsi, sma

MIN_WEEKS = 45           # 40-week SMA + 4 weeks of slope
ZONE_LOW = 0.97          # value zone runs from 3% under the 40-week SMA ...
ZONE_HIGH = 1.03         # ... to 3% above the higher of the 20-week EMA and 40-week SMA
STOP_BUF = 0.97          # invalidation 3% under the zone / last weekly swing low
RSI_COOL = 50            # a real pullback: weekly RSI dipped to 50 or lower in the last 6 weeks
COOL_WEEKS = 6
WEEK = 7 * 86400

# Labels describe the situation, not a promise: the backtest found no timing edge in the buy rule.
LABELS = {"buy": "PULLBACK + UP WEEK", "ready": "IN VALUE ZONE", "watch": "UPTREND", "avoid": "NO UPTREND"}


def week_start(t):
    """Monday 00:00 UTC of the ISO week that contains timestamp t."""
    day = int(t // 86400) * 86400
    return day - time.gmtime(day).tm_wday * 86400


def completed_weeks(closed, now=None):
    """Weekly candles from closed daily candles; a week counts once Monday 00:00 UTC has passed."""
    now = time.time() if now is None else now
    weeks = []
    for c in closed:
        ws = week_start(c["t"])
        if weeks and weeks[-1]["ws"] == ws:
            w = weeks[-1]
            w["h"] = max(w["h"], c["h"]); w["l"] = min(w["l"], c["l"]); w["c"] = c["c"]
            w["v"] += c["v"]; w["last_t"] = c["t"]
        else:
            weeks.append({"ws": ws, "t": c["t"], "last_t": c["t"], "o": c["o"], "h": c["h"],
                          "l": c["l"], "c": c["c"], "v": c["v"]})
    while weeks and now < weeks[-1]["ws"] + WEEK:
        weeks.pop()
    return weeks


def _day(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def analyse_long(symbol, closed, prev_trend=None, now=None, risk_eur=10, capital=500):
    """Long-term signal on completed weeks. `closed` must hold closed daily candles only."""
    weeks = completed_weeks(closed, now)
    if len(weeks) < MIN_WEEKS:
        reason = f"Not enough history for a long-term view ({len(weeks)} of {MIN_WEEKS} weeks)"
        return {
            "signal": {"key": "avoid", "label": LABELS["avoid"], "reason": reason,
                       "plan": "Wait until there is enough weekly history.", "strength": 0},
            "trend": "Unknown", "trend_dir": None, "trend_score": 0, "trend_reasons": [reason],
            "checklist": [{"name": "History", "state": "no", "text": reason}],
            "scenarios": [], "plan": None, "zone": None, "in_zone": False, "dist_zone": None,
            "close_day": _day(weeks[-1]["last_t"]) if weeks else None, "weeks": len(weeks),
        }

    wc = [w["c"] for w in weeks]
    sma40, ema20, ema10 = sma(wc, 40), ema(wc, 20), ema(wc, 10)
    rsi_w = rsi(wc, 14)
    last, prev = weeks[-1], weeks[-2]
    ref = last["c"]

    # --- trend: four +/-1 votes (score is -4, -2, 0, 2 or 4)
    lookback = 52 if len(wc) > 52 else 26
    mom = ref / wc[-1 - lookback] - 1
    rising = sma40[-1] > sma40[-5]
    votes = [
        (ref > sma40[-1], f"Weekly close {'above' if ref > sma40[-1] else 'below'} the 40-week average ({fmt(sma40[-1])})"),
        (rising, f"40-week average is {'rising' if rising else 'falling'}"),
        (mom > 0, f"{'12' if lookback == 52 else '6'}-month return {mom * 100:+.0f}%"),
        (ema10[-1] > sma40[-1], f"10-week EMA {'above' if ema10[-1] > sma40[-1] else 'below'} the 40-week average"),
    ]
    score = sum(1 if ok else -1 for ok, _ in votes)
    if score >= 2 or (prev_trend == "up" and score >= 0):
        direction = "up"
    elif score <= -2 or (prev_trend == "down" and score <= 0):
        direction = "down"
    else:
        direction = "sideways"
    trend = {"up": "Long-term uptrend" if score < 4 else "Strong long-term uptrend",
             "down": "Long-term downtrend", "sideways": "No clear long-term trend"}[direction]

    # --- value zone and confirmation
    zone = {"low": sma40[-1] * ZONE_LOW, "high": max(ema20[-1], sma40[-1]) * ZONE_HIGH}
    # a weekly close under the zone floor is a breakdown, not a pullback
    in_zone = zone["low"] <= ref <= zone["high"] or (zone["low"] <= ref <= zone["high"] * 1.02 and last["l"] <= zone["high"])
    dist_zone = max(0.0, (ref - zone["high"]) / ref * 100)
    confirm = ref > last["o"] and ref > prev["c"]
    r_now = rsi_w[-1]
    rsi_ok = r_now is not None and r_now < 70
    recent_rsi = [x for x in rsi_w[-COOL_WEEKS:] if x is not None]
    cooled = bool(recent_rsi) and min(recent_rsi) <= RSI_COOL
    high52 = max(w["h"] for w in weeks[-52:])
    drawdown = (ref - high52) / high52 * 100

    # --- plan: invalidation under the zone and the last 8-week low; target = 52-week high
    swing_low = min(w["l"] for w in weeks[-8:])
    stop = min(zone["low"], swing_low) * STOP_BUF
    target = high52 if high52 > ref * 1.05 else None
    plan = None
    if stop < ref:
        stop_pct = (ref - stop) / ref
        full = risk_eur / stop_pct
        position = min(full, capital)
        plan = {"entry": ref, "stop": stop, "target": target, "stop_pct": stop_pct * 100,
                "win_pct": (target - ref) / ref * 100 if target else None,
                "rr": (target - ref) / (ref - stop) if target else None,
                "position": position, "capped": position < full,
                "loss_eur": position * stop_pct, "win_eur": position * (target - ref) / ref if target else None}

    close_day = _day(last["last_t"])
    checklist = [
        {"name": "Long-term trend", "state": "ok" if direction == "up" else "no", "text": f"{trend} (score {score:+d} of 4)"},
        {"name": "40-week average", "state": "ok" if ref > sma40[-1] and rising else "no", "text": votes[0][1] + ", " + votes[1][1].lower()},
        {"name": "Momentum", "state": "ok" if mom > 0 else "no", "text": votes[2][1]},
        {"name": "Value zone", "state": "ok" if in_zone else "wait",
         "text": f"Weekly close in the value zone {fmt(zone['low'])}–{fmt(zone['high'])}" if in_zone
         else f"{dist_zone:.1f}% above the value zone {fmt(zone['low'])}–{fmt(zone['high'])}" if ref > zone["high"]
         else f"Below the value zone ({fmt(zone['low'])})"},
        {"name": "Weekly confirmation", "state": "ok" if confirm else "wait",
         "text": "Week closed up (above its open and last week's close)" if confirm else "Week did not close up yet"},
        {"name": "Real pullback", "state": "ok" if cooled else "wait",
         "text": f"Weekly RSI {'dipped to' if cooled else 'stayed above'} {RSI_COOL} in the last {COOL_WEEKS} weeks"
                 + (f" (low {min(recent_rsi):.0f})" if recent_rsi else "")},
        {"name": "Weekly RSI", "state": "ok" if rsi_ok else "no", "text": f"{(r_now or 0):.0f}" + (" (overheated)" if not rsi_ok else "")},
        {"name": "From 52-week high", "state": "info", "text": f"{drawdown:.0f}% ({fmt(high52)})"},
    ]

    if direction != "up":
        key = "avoid"
        reason = f"{trend}: no new money here by these rules (not a sell signal)"
        plan_text = f"These rules add nothing new here until the weekly close is back above a rising 40-week average ({fmt(sma40[-1])})."
    elif in_zone and confirm and rsi_ok and cooled:
        key = "buy"
        reason = "Long-term uptrend, pulled back to the value zone and the week closed higher"
        plan_text = ("If you already planned to own this, this is a calmer moment to add part of your monthly amount. "
                     f"The setup is broken on a weekly close under {fmt(stop)} (−{plan['stop_pct']:.0f}%). "
                     "Historically this did not beat buying on a fixed date.")
    elif in_zone:
        key = "ready"
        missing = [m for m, ok in (("a real pullback (weekly RSI ≤ 50)", cooled), ("a week that closes up", confirm),
                                   ("weekly RSI below 70", rsi_ok)) if not ok]
        reason = "Long-term uptrend and in the value zone, waiting for " + " and ".join(missing)
        plan_text = f"Price is in the value zone {fmt(zone['low'])}–{fmt(zone['high'])}. A week that closes up turns this into a buy signal."
    else:
        key = "watch"
        reason = f"Long-term uptrend, {dist_zone:.0f}% above the value zone: holders stay in"
        plan_text = (f"Holders can stay in. New money: wait for a pullback to {fmt(zone['low'])}–{fmt(zone['high'])} "
                     f"({dist_zone:.0f}% lower).")

    pts = {"up": 35, "sideways": 10, "down": 0}[direction] + (10 if score == 4 else 0)
    pts += 25 if in_zone else 12 if dist_zone < 8 else 5 if dist_zone < 15 else 0
    pts += (10 if confirm else 0) + (10 if cooled else 0) + (5 if rsi_ok else 0) + (5 if mom > 0.1 else 0)
    strength = min(100, pts) if key != "avoid" else min(30, pts)

    scenarios = [
        {"key": "A", "title": f"Pullback to the value zone ({fmt(zone['low'])} – {fmt(zone['high'])})",
         "text": "In an uptrend this is where long-term buyers often step in. A week that closes higher confirms it.", "plan": None},
        {"key": "B", "title": f"Weekly close under {fmt(stop)}",
         "text": "The long-term setup is broken. No new buys; holders reconsider.", "plan": None},
    ]
    if target:
        scenarios.append({"key": "C", "title": f"New 52-week high above {fmt(target)}",
                          "text": "The long-term trend continues. Don't chase; the next pullback is the opportunity.", "plan": None})

    return {
        "signal": {"key": key, "label": LABELS[key], "reason": reason, "plan": plan_text, "strength": strength},
        "trend": trend, "trend_dir": direction, "trend_score": score,
        "trend_reasons": [v[1] for v in votes],
        "checklist": checklist, "scenarios": scenarios, "plan": plan,
        "zone": zone, "in_zone": in_zone, "dist_zone": dist_zone, "sma40": sma40[-1],
        "close_day": close_day, "weeks": len(weeks), "rsi_w": r_now, "momentum": mom,
    }
