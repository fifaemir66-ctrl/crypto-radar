"""
Crypto Radar -- analyse-engine.

Voert de technische-analyse-checklist uit op dagcandles:
trend (HH/HL + EMA 50), steunzones (oude toppen, Fibonacci, EMA 50),
RSI, candle-patroon, volume en risico/winst met positiegrootte.

Educatief hulpmiddel -- geen financieel advies.
"""

import json
import urllib.request

STOP_BUF = 0.985  # stop 1,5% onder de onderkant van de zone

BINANCE_URLS = [
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
]


# ---------------------------------------------------------------- data

def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-radar/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _binance(symbol, interval, limit):
    last_err = None
    for base in BINANCE_URLS:
        try:
            raw = _get_json(f"{base}?symbol={symbol}&interval={interval}&limit={limit}")
            return [{"t": k[0] // 1000, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                     "c": float(k[4]), "v": float(k[5])} for k in raw]
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Binance: {last_err}")


def _coinbase(symbol):
    """Reservebron (alleen dagcandles, max 300). Coinbase noteert in USD."""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol[:-3]
    raw = _get_json(f"https://api.exchange.coinbase.com/products/{base}-USD/candles?granularity=86400")
    rows = sorted(raw, key=lambda k: k[0])
    return [{"t": int(k[0]), "o": float(k[3]), "h": float(k[2]), "l": float(k[1]),
             "c": float(k[4]), "v": float(k[5])} for k in rows]


def fetch_klines(symbol, interval="1d", limit=400):
    """Geeft (candles, bron). De laatste candle is nog niet gesloten."""
    try:
        return _binance(symbol, interval, limit), "Binance"
    except Exception as e:
        if interval != "1d":
            raise
        try:
            return _coinbase(symbol), "Coinbase"
        except Exception as e2:
            raise RuntimeError(f"Geen data voor {symbol} ({e}; Coinbase: {e2})")


# ---------------------------------------------------------------- indicatoren

def ema(values, n):
    out, k, prev = [], 2 / (n + 1), None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(values, n=14):
    out = [None] * len(values)
    if len(values) <= n:
        return out
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for i in range(n, len(values)):
        if i > n:
            avg_g = (avg_g * (n - 1) + gains[i - 1]) / n
            avg_l = (avg_l * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def pivots(candles, length):
    """Toppen/bodems: hoogste/laagste punt binnen `length` candles links en rechts."""
    highs, lows = [], []
    for i in range(length, len(candles) - length):
        h, l = candles[i]["h"], candles[i]["l"]
        left = candles[i - length:i]
        right = candles[i + 1:i + length + 1]
        if h > max(c["h"] for c in left) and h >= max(c["h"] for c in right):
            highs.append(i)
        if l < min(c["l"] for c in left) and l <= min(c["l"] for c in right):
            lows.append(i)
    return highs, lows


def label_pivots(candles, highs, lows):
    labels = []
    prev = None
    for i in highs:
        p = candles[i]["h"]
        labels.append({"i": i, "t": candles[i]["t"], "price": p, "kind": "top",
                       "label": "Top" if prev is None else ("HH" if p > prev else "LH")})
        prev = p
    prev = None
    for i in lows:
        p = candles[i]["l"]
        labels.append({"i": i, "t": candles[i]["t"], "price": p, "kind": "bodem",
                       "label": "Bodem" if prev is None else ("HL" if p > prev else "LL")})
        prev = p
    return sorted(labels, key=lambda x: x["i"])


def candle_pattern(c, prev):
    body = abs(c["c"] - c["o"])
    rng = c["h"] - c["l"] or 1e-9
    upper = c["h"] - max(c["o"], c["c"])
    lower = min(c["o"], c["c"]) - c["l"]
    green = c["c"] > c["o"]
    prev_body = abs(prev["c"] - prev["o"])
    prev_real = prev_body >= 0.25 * ((prev["h"] - prev["l"]) or 1e-9)

    if prev_real and green and prev["c"] < prev["o"] and c["c"] >= prev["o"] and c["o"] <= prev["c"] and body > prev_body:
        return "Bullish engulfing", "bullish"
    if prev_real and not green and prev["c"] > prev["o"] and c["o"] >= prev["c"] and c["c"] <= prev["o"] and body > prev_body:
        return "Bearish engulfing", "bearish"
    if body <= 0.1 * rng:
        return "Doji (besluiteloosheid)", "neutraal"
    if lower >= 2 * body and upper <= 0.35 * rng:
        return ("Hammer (groen)" if green else "Hammer (rood)"), "bullish"
    if upper >= 2 * body and lower <= 0.35 * rng:
        return "Shooting star", "bearish"
    if body >= 0.7 * rng:
        return ("Sterke groene candle" if green else "Sterke rode candle"), ("bullish" if green else "bearish")
    return ("Kleine groene candle" if green else "Kleine rode candle"), "neutraal"


def cluster_levels(levels, tol=0.015):
    """Voeg niveaus samen die binnen `tol` (1,5%) van elkaar liggen tot zones."""
    levels = sorted(levels, key=lambda x: x["price"])
    zones = []
    for lv in levels:
        if zones and lv["price"] <= zones[-1]["low"] * (1 + tol):
            z = zones[-1]
            z["items"].append(lv)
            z["high"] = max(z["high"], lv["price"])
        else:
            zones.append({"low": lv["price"], "high": lv["price"], "items": [lv]})
    for z in zones:
        z["score"] = len(z["items"])
    return zones


# ---------------------------------------------------------------- formattering

def fmt(x):
    if x is None:
        return "-"
    if x >= 1000:
        return f"{x:,.0f}".replace(",", ".")
    if x >= 1:
        return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{x:.5f}".replace(".", ",")


def dec(x):
    return f"{x:.1f}".replace(".", ",")


def eur(x):
    return f"€{x:,.0f}".replace(",", ".")


# ---------------------------------------------------------------- analyse

def analyse(symbol, candles, pivot_len=7, risk_eur=10, capital=500):
    live = candles[-1]
    closed = candles[:-1]  # laatste candle is nog niet gesloten
    closes = [c["c"] for c in closed]
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    rsi14 = rsi(closes, 14)
    price = live["c"]

    highs, lows = pivots(closed, pivot_len)
    labels = label_pivots(closed, highs, lows)
    tops = [x for x in labels if x["kind"] == "top"]
    bottoms = [x for x in labels if x["kind"] == "bodem"]

    # --- 1. trend
    score, reasons = 0, []
    if len(tops) >= 2:
        if tops[-1]["price"] > tops[-2]["price"]:
            score += 1; reasons.append(f"Laatste top is een HH ({fmt(tops[-1]['price'])} > {fmt(tops[-2]['price'])})")
        else:
            score -= 1; reasons.append(f"Laatste top is een LH ({fmt(tops[-1]['price'])} < {fmt(tops[-2]['price'])})")
    if len(bottoms) >= 2:
        if bottoms[-1]["price"] > bottoms[-2]["price"]:
            score += 1; reasons.append(f"Laatste bodem is een HL ({fmt(bottoms[-1]['price'])} > {fmt(bottoms[-2]['price'])})")
        else:
            score -= 1; reasons.append(f"Laatste bodem is een LL ({fmt(bottoms[-1]['price'])} < {fmt(bottoms[-2]['price'])})")
    above_ema = price > ema50[-1]
    score += 1 if above_ema else -1
    reasons.append(f"Prijs {'boven' if above_ema else 'onder'} EMA 50 ({fmt(ema50[-1])})")
    ema_slope = ema50[-1] - ema50[-11]
    score += 1 if ema_slope > 0 else -1
    reasons.append("EMA 50 stijgt" if ema_slope > 0 else "EMA 50 daalt")
    if score >= 4:
        trend, trend_ok = "Sterk omhoog", True
    elif score >= 2:
        trend, trend_ok = "Omhoog", True
    elif score <= -2:
        trend, trend_ok = "Omlaag", False
    else:
        trend, trend_ok = "Zijwaarts", False

    # --- Fibonacci: laagste punt tussen vorige en laatste top -> hoogste punt daarna
    fib = None
    if len(tops) >= 2:
        a, b = tops[-2]["i"], tops[-1]["i"]
        sl_i = min(range(a, b + 1), key=lambda i: closed[i]["l"])
        sh_i = max(range(sl_i, len(closed)), key=lambda i: closed[i]["h"])
        sl, sh = closed[sl_i]["l"], closed[sh_i]["h"]
        diff = sh - sl
        fib = {"low": sl, "high": sh, "low_i": sl_i, "high_i": sh_i,
               "38.2": sh - 0.382 * diff, "50": sh - 0.5 * diff, "61.8": sh - 0.618 * diff}

    # --- steunniveaus onder de prijs -> zones
    levels = []
    for t in tops:
        if t["price"] < price:
            levels.append({"price": t["price"], "name": f"Oude top {t['label']}"})
    for bt in bottoms[-4:]:
        if bt["price"] < price:
            levels.append({"price": bt["price"], "name": f"Bodem {bt['label']}"})
    if fib:
        for k in ("38.2", "50", "61.8"):
            if fib[k] < price:
                levels.append({"price": fib[k], "name": f"Fibonacci {k.replace('.', ',')}%"})
    if ema50[-1] < price:
        levels.append({"price": ema50[-1], "name": "EMA 50"})
    levels = [lv for lv in levels if lv["price"] > price * 0.8]
    zones = cluster_levels(levels)
    zones.sort(key=lambda z: -z["high"])  # dichtstbijzijnde eerst
    zone1 = zones[0] if zones else None
    zone2 = max(zones[1:], key=lambda z: (z["score"], z["high"])) if len(zones) > 1 else None

    resist = sorted(t["price"] for t in tops if t["price"] > price)
    target = resist[0] if resist else None
    if fib and fib["high"] > price:
        target = min(target, fib["high"]) if target else fib["high"]

    last_bottom = bottoms[-1]["price"] if bottoms else None

    # --- 2. plek
    in_zone = bool(zone1 and zone1["low"] * 0.995 <= price <= zone1["high"] * 1.005)
    dist_zone = (price - zone1["high"]) / price * 100 if zone1 else None

    # --- 4. RSI
    r_now, r_prev = rsi14[-1], rsi14[-6]
    rsi_ok = r_now < 70

    # --- 5. candle (laatste gesloten)
    pattern, bias = candle_pattern(closed[-1], closed[-2])
    candle_ok = bias == "bullish"

    # --- 6. volume
    vols = [c["v"] for c in closed]
    avg30 = sum(vols[-30:]) / 30
    vol_ratio = vols[-1] / avg30 if avg30 else 0
    vol_ok = vol_ratio >= 0.8
    pullback_vol = None
    if fib:
        up_leg = vols[fib["low_i"]:fib["high_i"] + 1]
        pb_leg = vols[fib["high_i"] + 1:]
        if up_leg and pb_leg:
            pullback_vol = (sum(pb_leg) / len(pb_leg)) / (sum(up_leg) / len(up_leg))

    # --- 7. risico/winst
    def plan(entry, stop, tgt):
        if not (entry and stop and tgt) or stop >= entry or tgt <= entry:
            return None
        stop_pct = (entry - stop) / entry
        full = risk_eur / stop_pct
        position = min(full, capital)
        return {"entry": entry, "stop": stop, "target": tgt,
                "stop_pct": stop_pct * 100, "win_pct": (tgt - entry) / entry * 100,
                "rr": (tgt - entry) / (entry - stop),
                "position": position, "capped": position < full,
                "loss_eur": position * stop_pct, "win_eur": position * (tgt - entry) / entry}

    plan_now = plan(price, zone1["low"] * STOP_BUF if zone1 else None, target)
    plan_z1 = plan(zone1["high"], zone1["low"] * STOP_BUF, target) if zone1 else None
    plan_z2 = plan(zone2["high"], zone2["low"] * STOP_BUF, target) if zone2 else None
    rr_ok = bool(plan_now and plan_now["rr"] >= 2)

    checklist = [
        {"name": "Trend", "state": "ok" if trend_ok else "nee", "text": trend},
        {"name": "Plek", "state": "ok" if in_zone else "wacht",
         "text": "In zone 1" if in_zone else (f"{dec(dist_zone)}% boven zone 1" if dist_zone is not None else "Geen zone gevonden")},
        {"name": "EMA 50", "state": "ok" if above_ema else "nee",
         "text": f"Prijs {'boven' if above_ema else 'onder'} EMA 50 ({fmt(ema50[-1])}), {'stijgend' if ema_slope > 0 else 'dalend'}"},
        {"name": "RSI", "state": "ok" if rsi_ok else "nee",
         "text": f"{dec(r_now)} ({'daalt' if r_now < r_prev else 'stijgt'}, was {dec(r_prev)})"},
        {"name": "Candle", "state": "ok" if candle_ok else "nee", "text": pattern},
        {"name": "Volume", "state": "ok" if vol_ok else "wacht", "text": f"{vol_ratio * 100:.0f}% van 30-daags gemiddelde"},
        {"name": "Risico/winst", "state": "ok" if rr_ok else "nee",
         "text": f"{dec(plan_now['rr'])}x bij instap nu" if plan_now else "Niet te berekenen"},
    ]

    setup = trend_ok and in_zone and candle_ok and rsi_ok and rr_ok
    if setup:
        status = {"key": "setup", "label": "Setup",
                  "title": "Mogelijke setup volgens je checklist",
                  "text": "Alle punten zijn groen. Controleer het zelf op de chart voordat je iets doet."}
    elif not trend_ok:
        status = {"key": "down", "label": "Trend negatief" if score <= -2 else "Geen trend",
                  "title": "Geen setup: trend werkt niet mee",
                  "text": "Volgens je regels koop je alleen in een uptrend. Wachten."}
    elif in_zone:
        status = {"key": "zone", "label": "In zone",
                  "title": "In de koopzone: wacht op signaal",
                  "text": "De prijs staat in zone 1. Wacht op een gesloten hammer of bullish engulfing."}
    else:
        status = {"key": "wait", "label": "Wachten",
                  "title": "Geen setup: wachten",
                  "text": "De trend is goed, maar de prijs is nog niet op een goede plek."}

    scenarios = []
    if zone1:
        scenarios.append({"key": "A", "title": f"Terugval naar zone 1 ({fmt(zone1['low'])} – {fmt(zone1['high'])})",
                          "text": "Wacht op een gesloten hammer of bullish engulfing in de zone.", "plan": plan_z1})
    if zone2:
        scenarios.append({"key": "B", "title": f"Diepere terugval naar zone 2 ({fmt(zone2['low'])} – {fmt(zone2['high'])})",
                          "text": f"Sterkere zone ({zone2['score']} niveaus samen). Zelfde signaal afwachten.", "plan": plan_z2})
    if target:
        scenarios.append({"key": "C", "title": f"Dagslot boven {fmt(target)}",
                          "text": "Uitbraak: de trend gaat verder. Niet najagen, wacht op de volgende terugval.", "plan": None})
    if last_bottom:
        scenarios.append({"key": "D", "title": f"Dagslot onder {fmt(last_bottom)}",
                          "text": "Laatste bodem gebroken: trend in gevaar. Geen koopplannen.", "plan": None})

    return {
        "symbol": symbol, "price": price, "live": live, "closed": closed,
        "ema50": ema50, "ema200": ema200, "rsi": rsi14, "labels": labels,
        "trend": trend, "trend_ok": trend_ok, "trend_score": score, "trend_reasons": reasons,
        "fib": fib, "zones": zones, "zone1": zone1, "zone2": zone2,
        "target": target, "last_bottom": last_bottom,
        "in_zone": in_zone, "dist_zone": dist_zone, "setup": setup, "status": status,
        "pattern": pattern, "rsi_now": r_now, "vol_ratio": vol_ratio, "pullback_vol": pullback_vol,
        "checklist": checklist, "scenarios": scenarios, "plan_now": plan_now,
    }
