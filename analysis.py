"""
ZoneHunter -- analysis engine for crypto and stocks.

Runs the pullback-to-zone strategy on daily candles. Signals are decided on the
last CLOSED daily candle only, so they change at most once per day:
  * trend: daily structure (HH/HL) + EMA 50, with hysteresis, filtered by the weekly trend
  * buy zones: old tops, Fibonacci, EMA 50, volume profile and liquidity (equal lows)
  * liquidity: equal highs (BSL) / equal lows (SSL), sweeps, volume profile (POC / value area)
  * trigger: hammer, bullish engulfing or SSL sweep inside the zone
  * risk/reward and position size; 7 extra confirmation indicators

Signals follow fixed strategy rules. Educational tool -- not financial advice.
"""

import csv
import io
import json
import time
import urllib.request

STOP_BUF = 0.985  # stop 1.5% below the bottom of the zone

BINANCE_URLS = [
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
]


# ================================================================ data

def _get(url, as_json=True):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (radar)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    return json.loads(raw) if as_json else raw.decode()


def _binance(symbol, limit):
    last_err = None
    for base in BINANCE_URLS:
        try:
            raw = _get(f"{base}?symbol={symbol}&interval=1d&limit={limit}")
            return [{"t": k[0] // 1000, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                     "c": float(k[4]), "v": float(k[5])} for k in raw]
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Binance: {last_err}")


def _coinbase(symbol):
    base = symbol[:-4] if symbol.endswith("USDT") else symbol[:-3]
    raw = _get(f"https://api.exchange.coinbase.com/products/{base}-USD/candles?granularity=86400")
    rows = sorted(raw, key=lambda k: k[0])
    return [{"t": int(k[0]), "o": float(k[3]), "h": float(k[2]), "l": float(k[1]),
             "c": float(k[4]), "v": float(k[5])} for k in rows]


def fetch_crypto(symbol, limit=400):
    """Returns (candles, source, last_is_closed). The last crypto candle is always still open."""
    try:
        return _binance(symbol, limit), "Binance", False
    except Exception as e:
        try:
            return _coinbase(symbol), "Coinbase", False
        except Exception as e2:
            raise RuntimeError(f"No data for {symbol} ({e}; Coinbase: {e2})")


def _yahoo(ticker):
    last_err = None
    for host in ("query1", "query2"):
        for attempt in range(2):
            try:
                d = _get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?range=2y&interval=1d")
                res = d["chart"]["result"][0]
                q = res["indicators"]["quote"][0]
                candles = []
                for i, t in enumerate(res["timestamp"]):
                    o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
                    if None in (o, h, l, c):
                        continue
                    candles.append({"t": int(t), "o": o, "h": h, "l": l, "c": c, "v": float(v or 0)})
                meta = res["meta"]
                reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
                now = time.time()
                live = bool(reg) and reg["start"] <= now < reg["end"] and candles[-1]["t"] >= reg["start"] - 3600
                if meta.get("regularMarketPrice") and live:
                    candles[-1]["c"] = float(meta["regularMarketPrice"])
                return candles, not live, meta.get("currency", "USD")
            except Exception as e:
                last_err = e
                time.sleep(1.5)
    raise RuntimeError(f"Yahoo: {last_err}")


def _stooq(ticker):
    raw = _get(f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d", as_json=False)
    rows = list(csv.DictReader(io.StringIO(raw)))
    if not rows or "Close" not in rows[0]:
        raise RuntimeError("Stooq: no data")
    candles = []
    for r in rows[-500:]:
        t = int(time.mktime(time.strptime(r["Date"], "%Y-%m-%d")))
        candles.append({"t": t, "o": float(r["Open"]), "h": float(r["High"]), "l": float(r["Low"]),
                        "c": float(r["Close"]), "v": float(r.get("Volume") or 0)})
    return candles


def fetch_stock(ticker):
    """Returns (candles, source, last_is_closed, currency)."""
    try:
        candles, closed, cur = _yahoo(ticker)
        return candles, "Yahoo Finance", closed, cur
    except Exception as e:
        try:
            return _stooq(ticker), "Stooq", True, "USD"
        except Exception as e2:
            raise RuntimeError(f"No data for {ticker} ({e}; {e2})")


# ================================================================ indicators

def ema(values, n):
    out, k, prev = [], 2 / (n + 1), None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def sma(values, n):
    out = [None] * len(values)
    for i in range(n - 1, len(values)):
        w = values[i - n + 1:i + 1]
        if None not in w:
            out[i] = sum(w) / n
    return out


def rma(values, n):
    """Wilder moving average; accepts leading None values."""
    out = [None] * len(values)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None or len(values) - start < n:
        return out
    prev = sum(values[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


def rsi(values, n=14):
    out = [None] * len(values)
    if len(values) <= n:
        return out
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    avg_g, avg_l = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(values)):
        if i > n:
            avg_g = (avg_g * (n - 1) + gains[i - 1]) / n
            avg_l = (avg_l * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def true_range(candles):
    tr = [candles[0]["h"] - candles[0]["l"]]
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]["c"]
        tr.append(max(c["h"] - c["l"], abs(c["h"] - p), abs(c["l"] - p)))
    return tr


def atr(candles, n=14):
    return rma(true_range(candles), n)


def macd(closes, fast=12, slow=26, signal=9):
    line = [a - b for a, b in zip(ema(closes, fast), ema(closes, slow))]
    sig = ema(line, signal)
    return line, sig, [a - b for a, b in zip(line, sig)]


def stoch_rsi(rsi_vals, n=14, k=3, d=3):
    raw = [None] * len(rsi_vals)
    for i in range(n - 1, len(rsi_vals)):
        w = rsi_vals[i - n + 1:i + 1]
        if None in w:
            continue
        lo, hi = min(w), max(w)
        raw[i] = 50.0 if hi == lo else (rsi_vals[i] - lo) / (hi - lo) * 100
    kk = sma(raw, k)
    return kk, sma(kk, d)


def bollinger(closes, n=20, mult=2):
    mid = sma(closes, n)
    up, lo = [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1:i + 1]
        sd = (sum((x - mid[i]) ** 2 for x in w) / n) ** 0.5
        up[i], lo[i] = mid[i] + mult * sd, mid[i] - mult * sd
    return mid, up, lo


def obv(candles):
    out, total = [0.0], 0.0
    for i in range(1, len(candles)):
        if candles[i]["c"] > candles[i - 1]["c"]:
            total += candles[i]["v"]
        elif candles[i]["c"] < candles[i - 1]["c"]:
            total -= candles[i]["v"]
        out.append(total)
    return out


def adx(candles, n=14):
    pdm, mdm = [None], [None]
    for i in range(1, len(candles)):
        up = candles[i]["h"] - candles[i - 1]["h"]
        dn = candles[i - 1]["l"] - candles[i]["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    tr = [None] + true_range(candles)[1:]
    atr_s, p_s, m_s = rma(tr, n), rma(pdm, n), rma(mdm, n)
    pdi, mdi, dx = [None] * len(candles), [None] * len(candles), [None] * len(candles)
    for i in range(len(candles)):
        if atr_s[i]:
            pdi[i] = 100 * p_s[i] / atr_s[i]
            mdi[i] = 100 * m_s[i] / atr_s[i]
            s = pdi[i] + mdi[i]
            dx[i] = 0.0 if s == 0 else 100 * abs(pdi[i] - mdi[i]) / s
    return rma(dx, n), pdi, mdi


# ================================================================ structure

def pivots(candles, length):
    """Swing highs/lows: highest/lowest point within `length` candles on both sides."""
    highs, lows = [], []
    for i in range(length, len(candles) - length):
        h, l = candles[i]["h"], candles[i]["l"]
        left, right = candles[i - length:i], candles[i + 1:i + length + 1]
        if h > max(c["h"] for c in left) and h >= max(c["h"] for c in right):
            highs.append(i)
        if l < min(c["l"] for c in left) and l <= min(c["l"] for c in right):
            lows.append(i)
    return highs, lows


def label_pivots(candles, highs, lows):
    labels = []
    for kind, idxs, key, up, down, first in (("top", highs, "h", "HH", "LH", "Top"),
                                             ("bottom", lows, "l", "HL", "LL", "Low")):
        prev = None
        for i in idxs:
            p = candles[i][key]
            labels.append({"i": i, "t": candles[i]["t"], "price": p, "kind": kind,
                           "label": first if prev is None else (up if p > prev else down)})
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
        return "Doji (indecision)", "neutral"
    if lower >= 2 * body and upper <= 0.35 * rng:
        return ("Green hammer" if green else "Red hammer"), "bullish"
    if upper >= 2 * body and lower <= 0.35 * rng:
        return "Shooting star", "bearish"
    if body >= 0.7 * rng:
        return ("Strong green candle" if green else "Strong red candle"), ("bullish" if green else "bearish")
    return ("Small green candle" if green else "Small red candle"), "neutral"


def liquidity_pools(closed, atr_now, lookback=150, piv_len=3):
    """Equal highs (BSL) and equal lows (SSL): places where many stop-losses sit.

    status: open (untouched), swept (wick through, close back) or broken (close through).
    """
    start = max(0, len(closed) - lookback)
    seg = closed[start:]
    highs, lows = pivots(seg, piv_len)
    ref_price = seg[-1]["c"]
    tol = max(0.25 * atr_now, ref_price * 0.002)
    pools = []
    for kind, idxs, key in (("BSL", highs, "h"), ("SSL", lows, "l")):
        pts = sorted((seg[i][key], i) for i in idxs)
        clusters = []
        for p, i in pts:
            if clusters and p - clusters[-1][0][0] <= tol:
                clusters[-1].append((p, i))
            else:
                clusters.append([(p, i)])
        for cl in clusters:
            if len(cl) < 2:
                continue
            level = max(p for p, _ in cl) if kind == "BSL" else min(p for p, _ in cl)
            last_i = max(i for _, i in cl)

            def status_of(candles):
                swept = False
                for c in candles:
                    beyond_close = c["c"] > level if kind == "BSL" else c["c"] < level
                    beyond_wick = c["h"] > level if kind == "BSL" else c["l"] < level
                    if beyond_close:
                        return "broken"
                    swept = swept or beyond_wick
                return "swept" if swept else "open"

            after = seg[last_i + 1:]
            pools.append({
                "kind": kind, "price": level, "touches": len(cl),
                "t_first": seg[min(i for _, i in cl)]["t"], "t_last": seg[last_i]["t"],
                "status": status_of(after),
                "status_before_last": status_of(after[:-1]) if after else "open",
                "formed_before_last": last_i < len(seg) - 1,
            })
    return pools


def volume_profile(closed, lookback=120, bins=40):
    seg = closed[-lookback:]
    lo, hi = min(c["l"] for c in seg), max(c["h"] for c in seg)
    step = (hi - lo) / bins or 1e-9
    vol = [0.0] * bins
    for c in seg:
        b0 = min(bins - 1, int((c["l"] - lo) / step))
        b1 = min(bins - 1, int((c["h"] - lo) / step))
        share = c["v"] / (b1 - b0 + 1)
        for b in range(b0, b1 + 1):
            vol[b] += share
    total = sum(vol) or 1
    poc = max(range(bins), key=lambda b: vol[b])
    lo_b, hi_b, acc = poc, poc, vol[poc]
    while acc < 0.7 * total and (lo_b > 0 or hi_b < bins - 1):
        down = vol[lo_b - 1] if lo_b > 0 else -1
        up = vol[hi_b + 1] if hi_b < bins - 1 else -1
        if up >= down:
            hi_b += 1; acc += up
        else:
            lo_b -= 1; acc += down
    mean = total / bins
    hvn, cur = [], None
    for b in range(bins):
        if vol[b] > 1.3 * mean:
            if cur and cur["hi_b"] == b - 1:
                cur["hi_b"] = b
            else:
                cur = {"lo_b": b, "hi_b": b}
                hvn.append(cur)
    edge = lambda b: lo + b * step
    return {
        "bins": [{"low": edge(b), "high": edge(b + 1), "vol": vol[b]} for b in range(bins)],
        "poc": edge(poc) + step / 2, "val": edge(lo_b), "vah": edge(hi_b + 1),
        "hvn": [{"low": edge(z["lo_b"]), "high": edge(z["hi_b"] + 1)} for z in hvn],
        "lookback": len(seg),
    }


def cluster_levels(levels, tol=0.015):
    """Merge levels within `tol` (1.5%) of each other into zones."""
    levels = sorted(levels, key=lambda x: x["price"])
    zones = []
    for lv in levels:
        if zones and lv["price"] <= zones[-1]["low"] * (1 + tol):
            zones[-1]["items"].append(lv)
            zones[-1]["high"] = max(zones[-1]["high"], lv["price"])
        else:
            zones.append({"low": lv["price"], "high": lv["price"], "items": [lv]})
    for z in zones:
        z["score"] = len(z["items"])
    return zones




# ================================================================ formatting

def fmt(x):
    if x is None:
        return "-"
    ax = abs(x)
    if ax >= 1000:
        return f"{x:,.0f}"
    if ax >= 1:
        return f"{x:,.2f}"
    if ax >= 0.01:
        return f"{x:.4f}"
    return f"{x:.8f}".rstrip("0")


def dec(x, n=1):
    return f"{x:.{n}f}"


def eur(x):
    return f"€{x:,.0f}"


def day_key(t):
    """Calendar date (UTC) of a candle; stable even if the data source shifts the timestamp."""
    return time.strftime("%Y-%m-%d", time.gmtime(t))


# ================================================================ weekly trend

def weekly_candles(closed):
    weeks = []
    for c in closed:
        key = time.strftime("%G-%V", time.gmtime(c["t"]))
        if weeks and weeks[-1]["key"] == key:
            wk = weeks[-1]
            wk["h"] = max(wk["h"], c["h"]); wk["l"] = min(wk["l"], c["l"]); wk["c"] = c["c"]
        else:
            weeks.append({"key": key, "t": c["t"], "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]})
    return weeks


def weekly_trend(closed):
    weeks = weekly_candles(closed)
    if len(weeks) < 25:
        return "neutral", "Not enough weekly history"
    wc = [w["c"] for w in weeks]
    e20 = ema(wc, 20)
    rising = e20[-1] > e20[-5]
    if wc[-1] > e20[-1] and rising:
        return "up", f"Weekly close above the rising 20-week EMA ({fmt(e20[-1])})"
    if wc[-1] < e20[-1] and not rising:
        return "down", f"Weekly close below the falling 20-week EMA ({fmt(e20[-1])})"
    return "neutral", f"Weekly trend undecided around the 20-week EMA ({fmt(e20[-1])})"


# ================================================================ analysis

SIGNALS = {
    "buy": "BUY SIGNAL",
    "ready": "GET READY",
    "watch": "WATCH",
    "avoid": "AVOID",
}


def analyse(symbol, candles, pivot_len=7, risk_eur=10, capital=500, last_closed=False, prev_trend=None):
    """All signal logic uses the last CLOSED daily candle. `live` is only used for display."""
    if last_closed:
        closed, live = candles, candles[-1]
    else:
        closed, live = candles[:-1], candles[-1]
    closes = [c["c"] for c in closed]
    last = closed[-1]
    ref = last["c"]            # reference price for all decisions: the last daily close
    price = live["c"]          # live price, for display only

    ema20, ema50, ema200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    rsi14 = rsi(closes, 14)
    atr14 = atr(closed, 14)
    atr_now = atr14[-1] or (last["h"] - last["l"])
    m_line, m_sig, m_hist = macd(closes)
    adx14, pdi, mdi = adx(closed, 14)
    st_k, st_d = stoch_rsi(rsi14)
    bb_mid, bb_up, bb_lo = bollinger(closes)
    obv_v = obv(closed)
    obv_sma = sma(obv_v, 20)

    highs, lows = pivots(closed, pivot_len)
    labels = label_pivots(closed, highs, lows)
    tops = [x for x in labels if x["kind"] == "top"]
    bottoms = [x for x in labels if x["kind"] == "bottom"]

    # --- 1. trend: daily structure + EMA 50, with hysteresis, filtered by the weekly trend
    score, reasons = 0, []
    if len(tops) >= 2:
        hh = tops[-1]["price"] > tops[-2]["price"]
        score += 1 if hh else -1
        reasons.append(f"Last swing high is a {'higher high (HH)' if hh else 'lower high (LH)'}: {fmt(tops[-1]['price'])} vs {fmt(tops[-2]['price'])}")
    if len(bottoms) >= 2:
        hl = bottoms[-1]["price"] > bottoms[-2]["price"]
        score += 1 if hl else -1
        reasons.append(f"Last swing low is a {'higher low (HL)' if hl else 'lower low (LL)'}: {fmt(bottoms[-1]['price'])} vs {fmt(bottoms[-2]['price'])}")
    above_ema = ref > ema50[-1]
    score += 1 if above_ema else -1
    reasons.append(f"Daily close {'above' if above_ema else 'below'} the EMA 50 ({fmt(ema50[-1])})")
    ema_slope = ema50[-1] - ema50[-11]
    score += 1 if ema_slope > 0 else -1
    reasons.append(f"EMA 50 is {'rising' if ema_slope > 0 else 'falling'}")

    # hysteresis: a trend only flips when the evidence is clearly on the other side
    if score >= 2 or (prev_trend == "up" and score >= 1):
        daily = "up"
    elif score <= -2 or (prev_trend == "down" and score <= -1):
        daily = "down"
    else:
        daily = "sideways"
    w_trend, w_reason = weekly_trend(closed)
    reasons.append(w_reason)
    trend_ok = daily == "up" and w_trend != "down"
    if daily == "up":
        trend = "Strong uptrend" if score >= 4 and w_trend == "up" else "Uptrend"
        if w_trend == "down":
            trend = "Daily up, weekly down"
    else:
        trend = "Downtrend" if daily == "down" else "Sideways"

    # --- Fibonacci of the last swing
    fib = None
    if len(tops) >= 2:
        a, b = tops[-2]["i"], tops[-1]["i"]
        sl_i = min(range(a, b + 1), key=lambda i: closed[i]["l"])
        sh_i = max(range(sl_i, len(closed)), key=lambda i: closed[i]["h"])
        sl, sh = closed[sl_i]["l"], closed[sh_i]["h"]
        diff = sh - sl
        fib = {"low": sl, "high": sh, "low_i": sl_i, "high_i": sh_i,
               "38.2": sh - 0.382 * diff, "50": sh - 0.5 * diff, "61.8": sh - 0.618 * diff}

    # --- liquidity and volume profile
    pools = liquidity_pools(closed, atr_now)
    profile = volume_profile(closed)
    ssl_open = sorted((p for p in pools if p["kind"] == "SSL" and p["status"] == "open" and p["price"] < ref),
                      key=lambda p: -p["price"])
    bsl_open = sorted((p for p in pools if p["kind"] == "BSL" and p["status"] == "open" and p["price"] > ref),
                      key=lambda p: p["price"])
    sweep = next((p for p in pools if p["kind"] == "SSL" and p["formed_before_last"]
                  and p["status_before_last"] == "open" and last["l"] < p["price"] < last["c"]), None)
    sweep_bear = next((p for p in pools if p["kind"] == "BSL" and p["formed_before_last"]
                       and p["status_before_last"] == "open" and last["h"] > p["price"] > last["c"]), None)

    # --- support levels below the last close -> buy zones
    levels = []
    for t in tops:
        if t["price"] < ref:
            levels.append({"price": t["price"], "name": f"Old high {t['label']}"})
    for bt in bottoms[-4:]:
        if bt["price"] < ref:
            levels.append({"price": bt["price"], "name": f"Swing low {bt['label']}"})
    if fib:
        for k in ("38.2", "50", "61.8"):
            if fib[k] < ref:
                levels.append({"price": fib[k], "name": f"Fibonacci {k}%"})
    if ema50[-1] < ref:
        levels.append({"price": ema50[-1], "name": "EMA 50"})
    if profile["poc"] < ref:
        levels.append({"price": profile["poc"], "name": "Volume POC"})
    for z in profile["hvn"]:
        mid = (z["low"] + z["high"]) / 2
        if mid < ref and abs(mid - profile["poc"]) / ref > 0.01:
            levels.append({"price": mid, "name": "High-volume node"})
    for p in ssl_open[:2]:
        levels.append({"price": p["price"], "name": f"Liquidity: equal lows ({p['touches']}x)"})
    levels = [lv for lv in levels if lv["price"] > ref * 0.8]
    zones = cluster_levels(levels)
    zones.sort(key=lambda z: -z["high"])
    # a buy zone needs confluence: at least 2 overlapping levels (a single line is too weak)
    strong = [z for z in zones if z["score"] >= 2]
    zone1 = strong[0] if strong else (zones[0] if zones else None)
    rest = [z for z in zones if z is not zone1 and z["high"] < zone1["low"]] if zone1 else []
    zone2 = max(rest, key=lambda z: (z["score"], z["high"])) if rest else None

    # --- target: first obstacle above (old high, swing high or open BSL)
    above = [t["price"] for t in tops if t["price"] > ref]
    if fib and fib["high"] > ref:
        above.append(fib["high"])
    if bsl_open:
        above.append(bsl_open[0]["price"])
    target = min(above) if above else None
    last_bottom = bottoms[-1]["price"] if bottoms else None

    # --- 2. location (decided on the daily close; a wick into the zone with a close just above also counts)
    in_zone = bool(zone1 and (
        zone1["low"] * 0.99 <= ref <= zone1["high"] * 1.01
        or (last["l"] <= zone1["high"] * 1.005 and zone1["high"] < ref <= zone1["high"] * 1.02)))
    dist_zone = (ref - zone1["high"]) / ref * 100 if zone1 else None

    # --- 3/4. RSI and trigger candle
    r_now, r_prev = rsi14[-1], rsi14[-6]
    rsi_ok = r_now < 70
    pattern, bias = candle_pattern(last, closed[-2])
    trigger = bias == "bullish" or sweep is not None
    trigger_text = pattern + (f" + liquidity sweep at {fmt(sweep['price'])}" if sweep else "")

    # --- volume
    vols = [c["v"] for c in closed]
    avg30 = sum(vols[-30:]) / 30
    vol_ratio = vols[-1] / avg30 if avg30 else 0
    pullback_vol = None
    if fib:
        up_leg, pb_leg = vols[fib["low_i"]:fib["high_i"] + 1], vols[fib["high_i"] + 1:]
        if up_leg and pb_leg and sum(up_leg):
            pullback_vol = (sum(pb_leg) / len(pb_leg)) / (sum(up_leg) / len(up_leg))

    # --- stop below the zone, and below open liquidity just under it (stops get hunted there)
    def stop_for(zone):
        stop = zone["low"] * STOP_BUF
        below = [p["price"] for p in ssl_open if zone["low"] * 0.97 <= p["price"] < zone["low"]]
        if below:
            stop = min(stop, min(below) * 0.995)
        return stop

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

    plan_close = plan(ref, stop_for(zone1), target) if zone1 else None
    plan_z1 = plan(zone1["high"], stop_for(zone1), target) if zone1 else None
    plan_z2 = plan(zone2["high"], stop_for(zone2), target) if zone2 else None
    rr_ok = bool(plan_close and plan_close["rr"] >= 2)

    # --- extra confirmation
    pct_b = None
    if bb_up[-1] is not None and bb_up[-1] != bb_lo[-1]:
        pct_b = (ref - bb_lo[-1]) / (bb_up[-1] - bb_lo[-1])
    hist_up = m_hist[-1] > m_hist[-2]
    a_now = adx14[-1] or 0
    k_now = st_k[-1]
    extras = [
        {"name": "EMA 200", "state": "ok" if ref > ema200[-1] else "no",
         "text": f"Close {'above' if ref > ema200[-1] else 'below'} the EMA 200 ({fmt(ema200[-1])}): long-term {'up' if ref > ema200[-1] else 'down'}"},
        {"name": "EMA 20/50", "state": "ok" if ema20[-1] > ema50[-1] else "no",
         "text": f"EMA 20 {'above' if ema20[-1] > ema50[-1] else 'below'} EMA 50: short-term momentum {'strong' if ema20[-1] > ema50[-1] else 'weak'}"},
        {"name": "MACD", "state": "ok" if m_hist[-1] > 0 or (hist_up and m_hist[-1] > m_hist[-3]) else "no",
         "text": f"Histogram {'positive' if m_hist[-1] > 0 else 'negative'} and {'rising' if hist_up else 'falling'}"},
        {"name": "ADX", "state": "ok" if a_now >= 20 and pdi[-1] > mdi[-1] else ("wait" if a_now < 20 else "no"),
         "text": f"ADX {a_now:.0f}: {'strong' if a_now >= 25 else 'moderate' if a_now >= 20 else 'weak'} trend, "
                 f"{'buyers (+DI)' if pdi[-1] > mdi[-1] else 'sellers (-DI)'} in control"},
        {"name": "Stoch RSI", "state": "ok" if k_now is not None and k_now < 30 else ("no" if k_now is not None and k_now > 80 else "wait"),
         "text": f"K {k_now or 0:.0f}: " + ("oversold, room to bounce" if (k_now or 50) < 30 else "overbought" if (k_now or 50) > 80 else "neutral")},
        {"name": "Bollinger", "state": "ok" if pct_b is not None and pct_b < 0.3 else ("no" if pct_b is not None and pct_b > 0.95 else "wait"),
         "text": f"%B {pct_b or 0:.2f}: " + ("near the lower band" if (pct_b or .5) < 0.3 else "at the upper band" if (pct_b or .5) > 0.95 else "mid-band")},
        {"name": "OBV", "state": "ok" if obv_sma[-1] is not None and obv_v[-1] > obv_sma[-1] else "no",
         "text": "Accumulation: OBV above its 20-day average" if obv_sma[-1] is not None and obv_v[-1] > obv_sma[-1] else "Distribution: OBV below its 20-day average"},
    ]
    conf = sum(1 for e in extras if e["state"] == "ok")
    atr_pct = atr_now / ref * 100
    extras.append({"name": "ATR", "state": "info", "text": f"Average daily move ≈ {atr_pct:.1f}% ({fmt(atr_now)})"})

    # --- liquidity check
    if sweep:
        liq = {"state": "ok", "text": f"Equal lows at {fmt(sweep['price'])} were swept and reclaimed: stops have been taken"}
    elif sweep_bear:
        liq = {"state": "no", "text": f"Equal highs at {fmt(sweep_bear['price'])} were swept and rejected: caution"}
    elif ssl_open and (ref - ssl_open[0]["price"]) / ref < 0.03:
        liq = {"state": "wait", "text": f"Untapped equal lows at {fmt(ssl_open[0]['price'])} ({(ref - ssl_open[0]['price']) / ref * 100:.1f}% lower): price may still dip there"}
    else:
        liq = {"state": "ok", "text": "No untapped liquidity just below price"}

    checklist = [
        {"name": "Trend", "state": "ok" if trend_ok else "no", "text": f"{trend} (weekly: {w_trend})"},
        {"name": "Buy zone", "state": "ok" if in_zone else "wait",
         "text": "Daily close in buy zone 1" if in_zone else (f"{dist_zone:.1f}% above buy zone 1" if dist_zone is not None else "No zone found")},
        {"name": "EMA 50", "state": "ok" if above_ema else "no",
         "text": f"Close {'above' if above_ema else 'below'} EMA 50 ({fmt(ema50[-1])}), {'rising' if ema_slope > 0 else 'falling'}"},
        {"name": "RSI", "state": "ok" if rsi_ok else "no", "text": f"{r_now:.1f} ({'falling' if r_now < r_prev else 'rising'}, was {r_prev:.1f})"},
        {"name": "Trigger", "state": "ok" if trigger else "no", "text": trigger_text},
        {"name": "Liquidity", "state": liq["state"], "text": liq["text"]},
        {"name": "Volume", "state": "ok" if vol_ratio >= 0.8 else "wait", "text": f"{vol_ratio * 100:.0f}% of the 30-day average"},
        {"name": "Risk/reward", "state": "ok" if rr_ok else "no",
         "text": f"{plan_close['rr']:.1f}x from the close" if plan_close else "Cannot be calculated"},
        {"name": "Confirmation", "state": "ok" if conf >= 4 else "wait", "text": f"{conf} of 7 extra indicators positive"},
    ]

    # --- signal (strategy rules)
    if not trend_ok:
        key = "avoid"
        why = {"down": "Downtrend", "sideways": "No clear trend"}.get(daily, "Weekly trend is down")
        if daily == "up":
            why = "Weekly trend is down"
        reason = f"{why}: the strategy only buys in uptrends"
        plan_text = (f"Stay out. Re-check when price makes a higher low and closes above the EMA 50 ({fmt(ema50[-1])}).")
    elif in_zone and trigger and rsi_ok and rr_ok:
        key = "buy"
        p = plan_close
        reason = f"Uptrend, daily close in the buy zone with a trigger ({trigger_text.lower()})"
        plan_text = (f"Buy setup at the daily close. Entry ≈ {fmt(p['entry'])}, stop {fmt(p['stop'])} (−{p['stop_pct']:.1f}%), "
                     f"target {fmt(p['target'])} (+{p['win_pct']:.1f}%). Position {eur(p['position'])} → max loss ≈ {eur(p['loss_eur'])}.")
    elif in_zone:
        key = "ready"
        missing = []
        if not trigger:
            missing.append("a trigger candle")
        if not rsi_ok:
            missing.append("RSI below 70")
        if not rr_ok:
            missing.append("a 2x risk/reward")
        reason = "In the buy zone, waiting for " + (" and ".join(missing) if len(missing) < 3 else ", ".join(missing[:-1]) + " and " + missing[-1])
        p = plan_z1
        plan_text = (f"Price is in the buy zone {fmt(zone1['low'])}–{fmt(zone1['high'])}. Wait for a daily close with a hammer, "
                     "bullish engulfing or liquidity sweep."
                     + (f" Then: entry ≈ {fmt(p['entry'])}, stop {fmt(p['stop'])}, target {fmt(p['target'])}." if p else ""))
    else:
        key = "watch"
        if zone1:
            reason = f"Uptrend, waiting for a pullback to {fmt(zone1['low'])}–{fmt(zone1['high'])} ({dist_zone:.1f}% lower)"
            plan_text = (f"Don't chase. Wait for a pullback into the buy zone {fmt(zone1['low'])}–{fmt(zone1['high'])}, "
                         f"{dist_zone:.1f}% below the last close.")
        else:
            reason = "Uptrend, no buy zone below price yet"
            plan_text = "Don't chase. Wait for a pullback that forms a new higher low."

    # --- signal strength 0-100
    pts = (20 if daily == "up" else 0) + (5 if score >= 4 else 0)
    pts += {"up": 10, "neutral": 5}.get(w_trend, 0)
    if in_zone:
        pts += 20
    elif dist_zone is not None and dist_zone < 3:
        pts += 10
    elif dist_zone is not None and dist_zone < 6:
        pts += 5
    pts += (20 if trigger else 0) + (5 if rsi_ok else 0) + (10 if rr_ok else 0) + round(15 * conf / 7)
    strength = min(100, pts) if key != "avoid" else min(30, pts)

    scenarios = []
    if zone1:
        scenarios.append({"key": "A", "title": f"Pullback to buy zone 1 ({fmt(zone1['low'])} – {fmt(zone1['high'])})",
                          "text": "Wait for a daily close with a hammer, bullish engulfing or liquidity sweep in the zone.", "plan": plan_z1})
    if zone2:
        scenarios.append({"key": "B", "title": f"Deeper pullback to buy zone 2 ({fmt(zone2['low'])} – {fmt(zone2['high'])})",
                          "text": f"Stronger zone ({zone2['score']} levels overlap). Same trigger required.", "plan": plan_z2})
    if target:
        what = "untapped equal highs" if bsl_open and target == bsl_open[0]["price"] else "resistance"
        scenarios.append({"key": "C", "title": f"Daily close above {fmt(target)} ({what})",
                          "text": "Breakout: the trend continues. Don't chase, wait for the next pullback.", "plan": None})
    if last_bottom:
        scenarios.append({"key": "D", "title": f"Daily close below {fmt(last_bottom)}",
                          "text": "Last swing low broken: the uptrend is in danger. No buys.", "plan": None})

    return {
        "symbol": symbol, "price": price, "ref": ref, "live": live, "closed": closed, "last_closed": last_closed,
        "close_day": day_key(last["t"]),
        "ema20": ema20, "ema50": ema50, "ema200": ema200, "rsi": rsi14,
        "macd": (m_line, m_sig, m_hist), "labels": labels,
        "trend": trend, "daily_trend": daily, "weekly_trend": w_trend, "trend_ok": trend_ok,
        "trend_score": score, "trend_reasons": reasons,
        "fib": fib, "zones": zones, "zone1": zone1, "zone2": zone2,
        "target": target, "last_bottom": last_bottom,
        "pools": pools, "profile": profile, "sweep": sweep,
        "in_zone": in_zone, "dist_zone": dist_zone,
        "signal": {"key": key, "label": SIGNALS[key], "reason": reason, "plan": plan_text, "strength": strength},
        "pattern": trigger_text, "rsi_now": r_now, "vol_ratio": vol_ratio, "pullback_vol": pullback_vol,
        "checklist": checklist, "extras": extras, "confluence": conf,
        "scenarios": scenarios, "plan_close": plan_close, "atr_pct": atr_pct,
    }
