#!/usr/bin/env python3
"""
ZoneHunter -- monitor for crypto and stocks.

Runs every hour (GitHub Actions or locally):
  * analyses every market in config.json
  * writes docs/data.json (overview) and docs/charts/<SYMBOL>.json (details) for the website
  * signals are decided on the daily close, so they change at most once per day
  * sends an ntfy alert only when a signal changes at the close (BUY SIGNAL, GET READY, AVOID),
    plus one daily digest per market
  * remembers in state.json what was already sent (no duplicates)

Usage:
    python3 monitor.py            normal run
    python3 monitor.py --dry-run  no alerts, state.json untouched (website data is written)
    python3 monitor.py --test     send a test alert

Signals follow fixed strategy rules. Educational tool -- not financial advice.
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

from analysis import analyse, eur, fetch_crypto, fetch_stock, fmt

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(ROOT, "config.json")
STATE_FILE = os.path.join(ROOT, "state.json")
DOCS = os.path.join(ROOT, "docs")
TOPIC_FILE = os.path.join(ROOT, ".ntfy_topic")

CHART_CANDLES = 260
HISTORY_MAX = 60
MARKET_LABEL = {"crypto": "Crypto", "stocks": "Stocks"}
ICON = {"buy": "🟢", "ready": "🟡", "watch": "🔵", "avoid": "🔴"}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, ensure_ascii=False, indent=2)


def site_url(config):
    if config.get("site_url"):
        return config["site_url"]
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        owner, name = repo.split("/")
        return f"https://{owner}.github.io/{name}/"
    return None


def ntfy_topic():
    topic = os.environ.get("NTFY_TOPIC")
    if not topic and os.path.exists(TOPIC_FILE):
        topic = open(TOPIC_FILE).read().strip()
    return topic


def send(title, message, priority=3, tags=None, click=None, dry_run=False):
    print(f"\n[alert p{priority}] {title}\n{message}")
    topic = ntfy_topic()
    if dry_run:
        return
    if not topic:
        print("  (no NTFY_TOPIC set, alert not sent)")
        return
    body = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags or []}
    if click:
        body["click"] = click
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"  ntfy error: {e}")


# ---------------------------------------------------------------- website output

def r(x):
    return None if x is None else float(f"{x:.8g}")


def chart_payload(a):
    closed = a["closed"]
    n = min(len(closed), CHART_CANDLES)
    off = len(closed) - n
    cs = closed[off:] + ([] if a["last_closed"] else [a["live"]])
    rng = range(off, len(closed))
    m_line, m_sig, m_hist = a["macd"]
    lines = []
    if a["zone1"]:
        lines += [{"price": a["zone1"]["high"], "title": "Buy zone 1", "kind": "zone1"},
                  {"price": a["zone1"]["low"], "title": "", "kind": "zone1"}]
    if a["zone2"]:
        lines += [{"price": a["zone2"]["high"], "title": "Buy zone 2", "kind": "zone2"},
                  {"price": a["zone2"]["low"], "title": "", "kind": "zone2"}]
    if a["target"]:
        lines.append({"price": a["target"], "title": "Target", "kind": "target"})
    if a["last_bottom"]:
        lines.append({"price": a["last_bottom"], "title": "Trend line", "kind": "stop"})
    ref = a["ref"]
    open_pools = [p for p in a["pools"] if p["status"] == "open" and abs(p["price"] - ref) / ref < 0.2]
    for p in sorted([p for p in open_pools if p["kind"] == "BSL"], key=lambda p: p["price"])[:2]:
        lines.append({"price": p["price"], "title": "BSL", "kind": "bsl"})
    for p in sorted([p for p in open_pools if p["kind"] == "SSL"], key=lambda p: -p["price"])[:2]:
        lines.append({"price": p["price"], "title": "SSL", "kind": "ssl"})
    lines.append({"price": a["profile"]["poc"], "title": "POC", "kind": "poc"})
    return {
        "candles": [[c["t"], r(c["o"]), r(c["h"]), r(c["l"]), r(c["c"]), round(c["v"], 2)] for c in cs],
        "ema50": [[closed[i]["t"], r(a["ema50"][i])] for i in rng],
        "ema200": [[closed[i]["t"], r(a["ema200"][i])] for i in range(max(off, 199), len(closed))],
        "rsi": [[closed[i]["t"], round(a["rsi"][i], 2)] for i in rng if a["rsi"][i] is not None],
        "macd": [[closed[i]["t"], r(m_line[i]), r(m_sig[i]), r(m_hist[i])] for i in range(max(off, 33), len(closed))],
        "markers": [[p["t"], p["kind"], p["label"]] for p in a["labels"] if p["i"] >= off],
        "lines": [l for l in lines if l["price"]],
    }


def detail_payload(a):
    fib, prof = a["fib"], a["profile"]
    return {
        "signal": a["signal"], "plan_close": a["plan_close"], "trend": a["trend"], "weekly_trend": a["weekly_trend"],
        "trend_reasons": a["trend_reasons"], "checklist": a["checklist"], "extras": a["extras"],
        "confluence": a["confluence"], "scenarios": a["scenarios"], "close_day": a["close_day"],
        "zones": [{"low": z["low"], "high": z["high"], "score": z["score"],
                   "items": [{"name": i["name"], "price": i["price"]} for i in z["items"]]} for z in a["zones"][:5]],
        "fib": fib and {"low": fib["low"], "high": fib["high"], "f382": fib["38.2"], "f50": fib["50"], "f618": fib["61.8"]},
        "pullback_vol": a["pullback_vol"], "atr_pct": a["atr_pct"],
        "pools": sorted([{k: p[k] for k in ("kind", "price", "touches", "t_first", "t_last", "status")} for p in a["pools"]
                         if abs(p["price"] - a["ref"]) / a["ref"] < 0.25], key=lambda p: -p["price"]),
        "profile": {"poc": prof["poc"], "vah": prof["vah"], "val": prof["val"], "lookback": prof["lookback"],
                    "hvn": prof["hvn"], "bins": [[r(b["low"]), r(b["high"]), round(b["vol"], 2)] for b in prof["bins"]]},
        "chart": chart_payload(a),
    }


def summary_payload(a, meta, market, source, currency, since):
    closed = a["closed"]
    ref_price = a["live"]["o"] if market == "crypto" else (closed[-2]["c"] if a["last_closed"] else closed[-1]["c"])
    return {
        "symbol": a["symbol"], "name": meta.get("name", a["symbol"]), "base": meta.get("base", a["symbol"]),
        "market": market, "source": source, "currency": currency,
        "price": a["price"], "close": a["ref"], "ref_price": ref_price, "market_open": not a["last_closed"],
        "signal": a["signal"], "signal_since": since, "trend": a["trend"],
        "rsi": round(a["rsi_now"], 1), "confluence": a["confluence"], "in_zone": a["in_zone"],
        "zone1": a["zone1"] and {"low": a["zone1"]["low"], "high": a["zone1"]["high"]},
        "spark": [r(c["c"]) for c in (closed[-59:] + ([] if a["last_closed"] else [a["live"]]))],
    }


# ---------------------------------------------------------------- run

def assets_from(config):
    out = [("crypto", m) for m in config.get("crypto", config.get("coins", []))]
    out += [("stocks", m) for m in config.get("stocks", [])]
    return out


def run(dry_run=False):
    config = load_json(CONFIG_FILE, {})
    state = load_json(STATE_FILE, {"coins": {}, "history": []})
    if state.get("version") != 2:   # new signal model: start alert memory fresh, keep history
        state = {"version": 2, "coins": {}, "digest": {}, "history": state.get("history", [])}
        fresh_start = True
    else:
        fresh_start = False
    url = site_url(config)
    now = datetime.now(timezone.utc)
    summaries, errors = [], []
    digest = {"crypto": [], "stocks": []}
    digest_day = {}

    def log_event(sym, market, kind, title, message, priority, tags):
        state["history"].insert(0, {"time": now.isoformat(timespec="seconds"), "symbol": sym, "market": market,
                                    "kind": kind, "title": title, "message": message})
        del state["history"][HISTORY_MAX:]
        send(title, message, priority, tags, url and (f"{url}#/asset/{sym}" if sym else url), dry_run)

    for market, meta in assets_from(config):
        sym = meta["symbol"]
        s = state["coins"].setdefault(sym, {})
        try:
            if market == "crypto":
                candles, source, last_closed = fetch_crypto(sym, 400)
                currency = "USDT"
            else:
                candles, source, last_closed, currency = fetch_stock(sym)
            a = analyse(sym, candles, config.get("pivot", 7), config.get("risk_eur", 10),
                        config.get("capital_eur", 500), last_closed=last_closed, prev_trend=s.get("trend"))
        except Exception as e:
            print(f"{sym}: {e}", file=sys.stderr)
            errors.append({"symbol": sym, "market": market, "error": str(e)[:300]})
            continue

        name = meta.get("name", sym)
        label = name if market == "crypto" else f"{name} ({sym})"
        sig = a["signal"]
        new_close = s.get("day") != a["close_day"]
        prev_sig = s.get("signal")

        # Alerts only when the signal changes at a new daily close
        if new_close and s.get("day") is not None and sig["key"] != prev_sig:
            if sig["key"] == "buy":
                log_event(sym, market, "buy", f"🟢 BUY SIGNAL: {label}",
                          f"{sig['reason']}.\n{sig['plan']}\nStrength {sig['strength']}/100. Check the chart yourself.",
                          5, ["green_circle"])
            elif sig["key"] == "ready":
                log_event(sym, market, "ready", f"🟡 GET READY: {label} is in its buy zone",
                          f"{sig['reason']}.\n{sig['plan']}", 3, ["yellow_circle"])
            elif sig["key"] == "avoid" and prev_sig in ("buy", "ready", "watch"):
                log_event(sym, market, "avoid", f"🔴 AVOID: {label} lost its uptrend",
                          f"{sig['reason']}.\n{sig['plan']}", 4, ["red_circle"])
        if new_close or sig["key"] != prev_sig:
            if sig["key"] != prev_sig:
                s["since"] = a["close_day"]
            s["signal"] = sig["key"]
        s["day"] = a["close_day"]
        s["trend"] = a["daily_trend"]
        if new_close:
            digest_day[market] = a["close_day"]

        save_json(os.path.join(DOCS, "charts", f"{sym}.json"), detail_payload(a), compact=True)
        summaries.append(summary_payload(a, meta, market, source, currency, s.get("since", a["close_day"])))
        digest[market].append((sig["key"], f"{ICON[sig['key']]} {name} {fmt(a['ref'])} · {sig['label']}"))
        print(f"{sym:<9} {fmt(a['ref']):>12}  {sig['label']:<11} {sig['strength']:>3}/100  {a['trend']}")

    # One digest per market per daily close
    order = {"buy": 0, "ready": 1, "watch": 2, "avoid": 3}
    for market, day in digest_day.items():
        if fresh_start or state["digest"].get(market) == day or not digest[market]:
            state["digest"][market] = day
            continue
        rows = sorted(digest[market], key=lambda x: order[x[0]])
        counts = {k: sum(1 for x in rows if x[0] == k) for k in order}
        head = (f"{counts['buy']} buy · {counts['ready']} get ready · {counts['watch']} watch · {counts['avoid']} avoid")
        body = "\n".join(x[1] for x in rows if x[0] in ("buy", "ready")) or "No buy zones reached today."
        log_event(None, market, "digest", f"📊 Daily digest: {MARKET_LABEL[market]}", f"{head}\n{body}", 2, ["bar_chart"])
        state["digest"][market] = day

    if fresh_start and not dry_run:
        send("🎯 ZoneHunter is live", f"Tracking {len(summaries)} markets. You'll get an alert when a signal changes at the daily close.",
             3, ["dart"], url)

    save_json(os.path.join(DOCS, "data.json"), {
        "generated_at": now.isoformat(timespec="seconds"),
        "config": {"risk_eur": config.get("risk_eur", 10), "capital_eur": config.get("capital_eur", 500)},
        "assets": summaries, "errors": errors, "history": state["history"],
    }, compact=True)
    if not dry_run:
        save_json(STATE_FILE, state)


def main():
    ap = argparse.ArgumentParser(description="ZoneHunter monitor")
    ap.add_argument("--dry-run", action="store_true", help="No alerts, state.json untouched")
    ap.add_argument("--test", action="store_true", help="Send a test alert")
    args = ap.parse_args()
    if args.test:
        send("🔔 ZoneHunter test", "If you can read this, your alerts work.", 3, ["bell"],
             site_url(load_json(CONFIG_FILE, {})))
        return
    run(args.dry_run)


if __name__ == "__main__":
    main()
