#!/usr/bin/env python3
"""
ZoneHunter -- monitor for crypto and stocks.

Runs every hour (GitHub Actions or locally):
  * analyses every market in config.json
  * the headline signal is LONG-TERM and decided on the weekly close (longterm.py), so it
    changes at most once a week; the daily analysis is shown as short-term timing
  * writes docs/data.json (overview) and docs/charts/<SYMBOL>.json (details) for the website
  * ntfy alerts: an immediate alert only for a new long-term BUY SIGNAL, plus one update on
    the digest days (default Monday and Thursday) with everything that changed
  * if a market cannot be fetched, its last known result stays on the site (marked stale)
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
import time
import urllib.request
from datetime import datetime, timezone

from analysis import analyse, eur, fetch_crypto, fetch_stock, fmt
from longterm import analyse_long

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(ROOT, "config.json")
STATE_FILE = os.path.join(ROOT, "state.json")
DOCS = os.path.join(ROOT, "docs")
TOPIC_FILE = os.path.join(ROOT, ".ntfy_topic")

STATE_VERSION = 3
CHART_CANDLES = 260
HISTORY_MAX = 60
STALE_MAX_DAYS = 7          # keep showing a market's last result this long when its data fails
NTFY_MAX = 3800             # ntfy messages are limited to 4096 bytes
MARKET_LABEL = {"crypto": "Crypto", "stocks": "Stocks"}
ICON = {"buy": "🟢", "ready": "🟡", "watch": "🔵", "avoid": "🔴"}
ORDER = {"buy": 0, "ready": 1, "watch": 2, "avoid": 3}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
BUY_COOLDOWN_DAYS = 56      # same rule as backtest.py: one buy alert per market per 8 weeks


def days_between(a, b):
    """Days from date string a to date string b; a missing a counts as 'long ago'."""
    if not a:
        return 10 ** 6
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data, compact=False):
    """Write to a temp file and rename, so a crash never leaves half a JSON file behind."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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
        return True
    if not topic:
        print("  (no NTFY_TOPIC set, alert not sent)")
        return False
    if len(message.encode()) > NTFY_MAX:
        message = message.encode()[:NTFY_MAX].decode(errors="ignore").rsplit("\n", 1)[0] + "\n…"
    body = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags or []}
    if click:
        body["click"] = click
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    for i in range(3):
        try:
            urllib.request.urlopen(req, timeout=15).read()
            return True
        except Exception as e:
            print(f"  ntfy error (attempt {i + 1}): {e}")
            time.sleep(3 * (i + 1))
    return False


# ---------------------------------------------------------------- website output

def r(x):
    return None if x is None else float(f"{x:.8g}")


def chart_payload(a, lt):
    closed = a["closed"]
    n = min(len(closed), CHART_CANDLES)
    off = len(closed) - n
    cs = closed[off:]  # closed candles only; the site adds the live crypto candle itself
    rng = range(off, len(closed))
    m_line, m_sig, m_hist = a["macd"]
    lines = []
    if lt["zone"]:
        lines += [{"price": lt["zone"]["high"], "title": "Value zone", "kind": "zone1"},
                  {"price": lt["zone"]["low"], "title": "", "kind": "zone1"}]
    if a["zone1"]:
        lines += [{"price": a["zone1"]["high"], "title": "Short-term zone", "kind": "zone2"},
                  {"price": a["zone1"]["low"], "title": "", "kind": "zone2"}]
    plan = lt["plan"]
    if plan and plan["target"]:
        lines.append({"price": plan["target"], "title": "52-week high", "kind": "target"})
    if plan:
        lines.append({"price": plan["stop"], "title": "Invalidation", "kind": "stop"})
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


def detail_payload(a, lt):
    fib, prof = a["fib"], a["profile"]
    return {
        # long-term (headline)
        "signal": lt["signal"], "plan_close": lt["plan"], "trend": lt["trend"], "trend_reasons": lt["trend_reasons"],
        "checklist": lt["checklist"], "scenarios": lt["scenarios"], "close_day": lt["close_day"],
        # short-term timing (daily)
        "short": {"signal": a["signal"], "trend": a["trend"], "weekly_trend": a["weekly_trend"],
                  "checklist": a["checklist"], "trend_reasons": a["trend_reasons"], "close_day": a["close_day"],
                  "plan_close": a["plan_close"]},
        "weekly_trend": a["weekly_trend"], "extras": a["extras"], "confluence": a["confluence"],
        "zones": [{"low": z["low"], "high": z["high"], "score": z["score"],
                   "items": [{"name": i["name"], "price": i["price"]} for i in z["items"]]} for z in a["zones"][:5]],
        "fib": fib and {"low": fib["low"], "high": fib["high"], "f382": fib["38.2"], "f50": fib["50"], "f618": fib["61.8"]},
        "pullback_vol": a["pullback_vol"], "atr_pct": a["atr_pct"],
        "pools": sorted([{k: p[k] for k in ("kind", "price", "touches", "t_first", "t_last", "status")} for p in a["pools"]
                         if abs(p["price"] - a["ref"]) / a["ref"] < 0.25], key=lambda p: -p["price"]),
        "profile": {"poc": prof["poc"], "vah": prof["vah"], "val": prof["val"], "lookback": prof["lookback"],
                    "hvn": prof["hvn"], "bins": [[r(b["low"]), r(b["high"]), round(b["vol"], 2)] for b in prof["bins"]]},
        "chart": chart_payload(a, lt),
    }


def summary_payload(a, lt, meta, market, source, currency, since):
    closed = a["closed"]
    ref_price = a["live"]["o"] if market == "crypto" else (closed[-2]["c"] if a["last_closed"] else closed[-1]["c"])
    return {
        "symbol": a["symbol"], "name": meta.get("name", a["symbol"]), "base": meta.get("base", a["symbol"]),
        "market": market, "source": source, "currency": currency,
        "price": a["price"], "close": a["ref"], "ref_price": ref_price, "market_open": not a["last_closed"],
        "signal": lt["signal"], "signal_since": since, "trend": lt["trend"],
        "short": {"key": a["signal"]["key"], "label": a["signal"]["label"], "reason": a["signal"]["reason"]},
        "rsi": round(a["rsi_now"], 1), "confluence": a["confluence"], "in_zone": lt["in_zone"],
        "zone1": lt["zone"] and {"low": lt["zone"]["low"], "high": lt["zone"]["high"]},
        "spark": [r(c["c"]) for c in (closed[-59:] + ([] if a["last_closed"] else [a["live"]]))],
        "stale": False, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- digest

SHORT = {"buy": "pullback + up week", "ready": "in value zone", "watch": "uptrend", "avoid": "no uptrend"}
DIGEST_ROWS = 8
FOOTER = "Your monthly plan continues regardless of these signals. Educational, not advice."


def digest_due(config, state, now):
    """True on a digest day (default Monday and Thursday) after the digest hour, once per day."""
    days = [d.lower()[:3] for d in config.get("digest_days", ["mon", "thu"])]
    hour = config.get("digest_hour_utc", 6)
    today = now.strftime("%Y-%m-%d")
    return WEEKDAYS[now.weekday()] in days and now.hour >= hour and state.get("last_digest") != today


def row_name(x):
    return ("Speculative · " if x["market"] == "crypto" else "") + x["name"]


def digest_message(summaries, changes, errors, now):
    lines = []
    if errors:   # first, so a long message never cuts it off
        lines += ["⚠️ No fresh data for: " + ", ".join(e["symbol"] for e in errors), ""]
    fresh = [x for x in summaries if not x.get("stale")]
    for market in ("crypto", "stocks"):
        rows = [x for x in fresh if x["market"] == market]
        if rows:
            c = {k: sum(1 for x in rows if x["signal"]["key"] == k) for k in ORDER}
            lines.append(f"{MARKET_LABEL[market]}: {c['buy']} up week · {c['ready']} value zone · {c['watch']} uptrend · {c['avoid']} no uptrend")
    lines.append("")
    if changes:
        lines.append("Changed since the last update:")
        for ch in changes[:DIGEST_ROWS]:
            lines.append(f"{ICON[ch['to']]} {ch['name']}: {SHORT.get(ch['from'], '?')} → {SHORT[ch['to']]}")
        if len(changes) > DIGEST_ROWS:
            lines.append(f"+{len(changes) - DIGEST_ROWS} more on the site")
    else:
        lines.append("No signal changes since the last update.")
    hot = sorted((x for x in fresh if x["signal"]["key"] in ("buy", "ready")),
                 key=lambda x: (ORDER[x["signal"]["key"]], x["market"] == "crypto", -x["signal"]["strength"]))
    if hot:
        lines += ["", "Pullbacks now:"]
        for x in hot[:DIGEST_ROWS]:
            lines.append(f"{ICON[x['signal']['key']]} {row_name(x)} {fmt(x['close'])} · {SHORT[x['signal']['key']]}")
        if len(hot) > DIGEST_ROWS:
            lines.append(f"+{len(hot) - DIGEST_ROWS} more on the site")
    lines += ["", FOOTER]
    return "\n".join(lines)


def buys_message(buys):
    lines = [f"{ICON['buy']} {row_name(x)} {fmt(x['close'])}: {x['signal']['reason']}." for x in buys]
    lines += ["", "If you already planned to own these, this is a calmer moment to add part of your monthly amount. "
              "Historically this did not beat buying on a fixed date. Check the chart yourself."]
    return "\n".join(lines)


# ---------------------------------------------------------------- run

def assets_from(config):
    out = [("crypto", m) for m in config.get("crypto", config.get("coins", []))]
    out += [("stocks", m) for m in config.get("stocks", [])]
    return out


def seed_history(sym, closed, ts, weeks=40):
    """For a new market or after a model change: replay the last weeks so 'since' and the trend memory are right."""
    prev_trend, key, since = None, None, None
    for k in range(weeks, -1, -1):
        lt = analyse_long(sym, closed, prev_trend, ts - k * 7 * 86400)
        if lt["weeks"] < 45:
            continue
        if lt["signal"]["key"] != key:
            key, since = lt["signal"]["key"], lt["close_day"]
        prev_trend = lt["trend_dir"]
    return {"signal": key, "since": since, "lt_trend": prev_trend}


def run(dry_run=False, now=None):
    config = load_json(CONFIG_FILE, {})
    state = load_json(STATE_FILE, {"coins": {}, "history": []})
    fresh_start = state.get("version") != STATE_VERSION
    if fresh_start:   # new signal model: start the alert memory fresh, keep the history
        state = {"version": STATE_VERSION, "coins": {}, "changes": [], "history": state.get("history", [])}
    state.setdefault("changes", [])
    state.setdefault("pending_buys", [])
    previous = {x["symbol"]: x for x in load_json(os.path.join(DOCS, "data.json"), {}).get("assets", [])}
    url = site_url(config)
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    hour = config.get("digest_hour_utc", 6)
    summaries, errors = [], []

    def log_event(sym, market, kind, title, message, priority, tags):
        """Send, and only record in the history what was really sent."""
        ok = send(title, message, priority, tags, url and (f"{url}#/asset/{sym}" if sym else url), dry_run)
        if ok:
            state["history"].insert(0, {"time": now.isoformat(timespec="seconds"), "symbol": sym, "market": market,
                                        "kind": kind, "title": title, "message": message})
            del state["history"][HISTORY_MAX:]
        return ok

    for market, meta in assets_from(config):
        sym = meta["symbol"]
        name = meta.get("name", sym)
        s = dict(state["coins"].get(sym, {}))   # work on a copy: only committed when everything for this market worked
        try:
            if market == "crypto":
                candles, source, last_closed = fetch_crypto(sym)
                currency = "USDT" if source == "Binance" else "USD"
            else:
                candles, source, last_closed, currency = fetch_stock(sym)
            a = analyse(sym, candles, config.get("pivot", 7), config.get("risk_eur", 10),
                        config.get("capital_eur", 500), last_closed=last_closed, prev_trend=s.get("trend"))
            if "lt_trend" not in s:
                seed = seed_history(sym, a["closed"], ts)
                s.update({"signal": seed["signal"], "since": seed["since"], "lt_trend": seed["lt_trend"]})
            lt = analyse_long(sym, a["closed"], s.get("lt_trend"), ts,
                              config.get("risk_eur", 10), config.get("capital_eur", 500))
            sig = lt["signal"]
            day, prev_day, prev_sig = lt["close_day"], s.get("day"), s.get("signal")

            if day is None or (prev_day is not None and day <= prev_day):
                # same weekly close as stored (a source switch can move the numbers a little) or an older close:
                # never record a change, alert or rewind the state
                pass
            else:
                if prev_day is not None and sig["key"] != prev_sig:
                    state["changes"].append({"symbol": sym, "name": name, "market": market, "day": day,
                                             "from": prev_sig, "to": sig["key"]})
                    if sig["key"] == "buy" and days_between(s.get("buy_alert_day"), day) >= BUY_COOLDOWN_DAYS:
                        state["pending_buys"] = [x for x in state["pending_buys"] if x["symbol"] != sym]
                        state["pending_buys"].append({"symbol": sym, "day": day})
                if sig["key"] != prev_sig:
                    s["since"] = day
                s["signal"], s["day"], s["lt_trend"] = sig["key"], day, lt["trend_dir"]
            s["trend"] = a["daily_trend"]

            detail = detail_payload(a, lt)
            summary = summary_payload(a, lt, meta, market, source, currency, s.get("since") or day)
        except Exception as e:
            print(f"{sym}: {e}", file=sys.stderr)
            errors.append({"symbol": sym, "market": market, "error": str(e)[:300]})
            old = previous.get(sym)
            age = (ts - datetime.fromisoformat(old["updated_at"]).timestamp()) / 86400 if old and old.get("updated_at") else None
            if old and age is not None and age <= STALE_MAX_DAYS:
                summaries.append({**old, "stale": True})   # keep the last known result on the site
            continue

        state["coins"][sym] = s
        save_json(os.path.join(DOCS, "charts", f"{sym}.json"), detail, compact=True)
        summaries.append(summary)
        print(f"{sym:<9} {fmt(a['ref']):>12}  {sig['label']:<18} {sig['strength']:>3}/100  {lt['trend']}  "
              f"| short-term {a['signal']['label']}")

    # drop changes that were reverted before the digest (e.g. uptrend → value zone → uptrend)
    net = {}
    for ch in state["changes"]:
        first = net.get(ch["symbol"], ch)
        net[ch["symbol"]] = {**ch, "from": first["from"]}
    state["changes"] = [ch for ch in net.values() if ch["from"] != ch["to"]]

    # new buy signals: one bundled message, never at night (crypto weeks close at 00:00 UTC)
    by_sym = {x["symbol"]: x for x in summaries if not x.get("stale")}
    pending = [p for p in state["pending_buys"] if p["symbol"] in by_sym and by_sym[p["symbol"]]["signal"]["key"] == "buy"]
    state["pending_buys"] = pending
    if fresh_start:
        state["pending_buys"] = []
        state["last_digest"] = now.strftime("%Y-%m-%d")
        if not dry_run:
            send("🎯 ZoneHunter: long-term mode", f"Tracking {len(summaries)} markets on the weekly close. "
                 "You'll get an update on Monday and Thursday, and an extra message only when a market in a "
                 "long-term uptrend pulls back and closes a week higher.", 3, ["dart"], url)
    elif pending and now.hour >= hour:
        buys = [by_sym[p["symbol"]] for p in pending]
        stocks_only = all(x["market"] == "stocks" for x in buys)
        title = (f"🟢 Pullback + up week: {buys[0]['name']}" if len(buys) == 1
                 else f"🟢 {len(buys)} markets pulled back and closed a week higher")
        if log_event(buys[0]["symbol"] if len(buys) == 1 else None, "all", "buy", title, buys_message(buys),
                     4 if stocks_only else 3, ["green_circle"]):
            for p in pending:
                state["coins"].setdefault(p["symbol"], {})["buy_alert_day"] = p["day"]
            state["pending_buys"] = []

    if not fresh_start and digest_due(config, state, now) and summaries:
        title = f"📊 ZoneHunter update · {now.strftime('%a %d %b')}"
        if log_event(None, "all", "digest", title, digest_message(summaries, state["changes"], errors, now), 3, ["bar_chart"]):
            state["changes"] = []
            state["last_digest"] = now.strftime("%Y-%m-%d")

    save_json(os.path.join(DOCS, "data.json"), {
        "generated_at": now.isoformat(timespec="seconds"),
        "config": {"risk_eur": config.get("risk_eur", 10), "capital_eur": config.get("capital_eur", 500),
                   "digest_days": config.get("digest_days", ["mon", "thu"])},
        "assets": summaries, "errors": errors, "history": state["history"],
    }, compact=True)
    if not dry_run:
        save_json(STATE_FILE, state)
    return summaries, errors, state


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
