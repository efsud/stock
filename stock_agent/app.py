"""Daily-close research signals. No brokerage access or trade execution."""
import argparse
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import ssl
import statistics
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
# python.org macOS installs can lack their optional certificate bundle. Use the
# OS-provided CA bundle, retaining certificate and hostname verification.
HTTPS_CONTEXT = ssl.create_default_context(
    cafile="/etc/ssl/cert.pem" if sys.platform == "darwin" and Path("/etc/ssl/cert.pem").exists() else None)


def request_json(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"User-Agent": "Mozilla/5.0 StockResearch/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    # Never propagate exceptions containing a Telegram URL (the URL contains a token).
    for attempt in range(3):
        try:
            with urlopen(Request(url, data=data, headers=headers), timeout=15, context=HTTPS_CONTEXT) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"HTTP {exc.code}; check provider access/credentials") from None
        except (URLError, TimeoutError, OSError, ValueError):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError("Provider unreachable or invalid response") from None


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def closed_bar(stamp, crypto, tz, now):
    start = datetime.fromtimestamp(stamp, UTC)
    if crypto:
        return start + timedelta(days=1) <= now
    local = start.astimezone(ZoneInfo(tz))
    # Conservative 17:00 local cutoff for the supported North American exchanges.
    # This also excludes partial bars on early-close sessions until 17:00.
    end = local.replace(hour=17, minute=0, second=0, microsecond=0)
    return end.astimezone(UTC) <= now


def history(symbol, now):
    url = "https://query2.finance.yahoo.com/v8/finance/chart/" + quote(symbol, safe="")
    result = request_json(url + "?" + urlencode({"range": "2y", "interval": "1d"}))
    chart = result.get("chart", {})
    if chart.get("error") or not chart.get("result"):
        raise RuntimeError("Symbol unavailable; verify provider ticker and exchange")
    item = chart["result"][0]
    meta = item["meta"]
    if meta.get("symbol", "").upper() != symbol.upper():
        raise RuntimeError("Provider returned a different symbol; refusing substitution")
    crypto = meta.get("instrumentType") == "CRYPTOCURRENCY"
    tz = meta.get("exchangeTimezoneName")
    if not tz or not meta.get("currency"):
        raise RuntimeError("Missing exchange timezone or quote currency")
    q = item["indicators"]["quote"][0]
    adjusted = item["indicators"].get("adjclose", [{}])[0].get("adjclose", [])
    bars = []
    for i, stamp in enumerate(item.get("timestamp", [])):
        if not closed_bar(stamp, crypto, tz, now):
            continue
        vals = [q[k][i] for k in ("open", "high", "low", "close", "volume")]
        if any(v is None or not math.isfinite(v) for v in vals):
            continue
        op, hi, lo, cl, vol = vals
        if min(op, hi, lo, cl) <= 0 or lo > min(op, cl) or hi < max(op, cl) or vol < 0:
            continue
        adj = adjusted[i] if i < len(adjusted) else None
        if adj is None or not math.isfinite(adj) or adj <= 0:
            raise RuntimeError("Adjusted close unavailable; cannot safely compute indicators")
        factor = adj / cl
        bars.append({"t": stamp, "o": op * factor, "h": hi * factor,
                     "l": lo * factor, "c": adj, "v": vol, "raw": cl})
    if len(bars) < 220:
        raise RuntimeError(f"Only {len(bars)} valid closed daily bars; need 220")
    return {"symbol": symbol, "name": meta.get("longName", meta.get("shortName", symbol)),
            "currency": meta["currency"], "crypto": crypto, "timezone": tz, "bars": bars}


def parse_headlines(content, now):
    rows = []
    for item in ET.fromstring(content).findall("./channel/item"):
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", ""))
            title, link = item.findtext("title", ""), item.findtext("link", "")
            if published.tzinfo is None or not timedelta(0) <= now-published <= timedelta(hours=24):
                continue
            if not title or not link.startswith("https://"):
                continue
            # Brief headline only; no article text. Links are data and are never executed.
            short = " ".join(title.split()[:20])
            rows.append({"title": short, "url": link, "published": published.isoformat()})
        except (ValueError, TypeError):
            continue
    return rows[:5]


def headlines(symbol, now):
    url = "https://finance.yahoo.com/rss/headline?" + urlencode({"s": symbol, "region": "US", "lang": "en-US"})
    try:
        with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10, context=HTTPS_CONTEXT) as response:
            return parse_headlines(response.read(1_000_000), now)
    except (HTTPError, URLError, TimeoutError, OSError, ET.ParseError):
        raise RuntimeError("News feed unavailable; event coverage is incomplete") from None


def rsi(closes, periods=14):
    changes = [b-a for a, b in zip(closes, closes[1:])]
    gains = statistics.mean(max(x, 0) for x in changes[:periods])
    losses = statistics.mean(max(-x, 0) for x in changes[:periods])
    for change in changes[periods:]:
        gains = (gains * (periods-1) + max(change, 0)) / periods
        losses = (losses * (periods-1) + max(-change, 0)) / periods
    return 50 if gains == losses == 0 else (100 if losses == 0 else 100 - 100/(1+gains/losses))


def completed_weeks(data, now):
    """Friday-week observations, excluding weekends and the unfinished week."""
    tz = ZoneInfo(data["timezone"])
    local_today = now.astimezone(tz).date()
    weeks = {}
    for bar in data["bars"]:
        day = datetime.fromtimestamp(bar["t"], UTC).astimezone(tz).date()
        monday = day - timedelta(days=day.weekday())
        if day.weekday() < 5 and monday + timedelta(days=5) <= local_today:
            weeks[monday] = {**bar, "session_date": day.isoformat()}
    return weeks


def analyse(data, benchmark, config, now, horizon="SIX_MONTH"):
    if horizon not in ("SIX_MONTH", "ONE_YEAR"):
        raise ValueError("Unknown research horizon")
    lookback = 26 if horizon == "SIX_MONTH" else 52
    short_window = lookback // 2
    weeks = completed_weeks(data, now)
    keys = sorted(weeks)
    required = lookback + 4
    if len(keys) < required:
        raise ValueError(f"Need {required} completed weeks for {horizon}; found {len(keys)}")
    if any(b-a != timedelta(days=7) for a, b in zip(keys[-required:-1], keys[-required+1:])):
        raise ValueError(f"Missing weekly observations in the {horizon} research window")
    weekly = [weeks[key]["c"] for key in keys]
    decision_price = weekly[-1]
    long_average = statistics.mean(weekly[-lookback:])
    short_average = statistics.mean(weekly[-short_window:])
    rising = long_average > statistics.mean(weekly[-lookback-4:-4])
    period_return = (decision_price / weekly[-lookback-1] - 1) * 100
    returns = [math.log(b/a) for a, b in zip(weekly[-lookback-1:-1], weekly[-lookback:])]
    volatility = statistics.stdev(returns) * math.sqrt(52) * 100
    peak, drawdown = weekly[-lookback-1], 0
    for close in weekly[-lookback:]:
        peak = max(peak, close)
        drawdown = min(drawdown, (close/peak-1)*100)
    relative = None
    if benchmark:
        bench_weeks = completed_weeks(benchmark, now)
        window_keys = keys[-lookback-1:]
        # Match the exact Friday-week keys; a missing endpoint never shortens
        # the requested six-month/year comparison into a shorter lookback.
        if all(key in bench_weeks for key in window_keys):
            start, end = window_keys[0], window_keys[-1]
            relative = period_return - (bench_weeks[end]["c"]/bench_weeks[start]["c"]-1)*100
    checks = (decision_price > long_average, decision_price > short_average,
              short_average > long_average, rising, period_return > 0,
              relative is not None and relative > 0)
    score = sum(checks) * 100 / len(checks)
    entry = (all(checks) and decision_price/long_average <= 1 + config.get("max_buy_extension_pct", 15)/100
             and volatility <= config.get("max_buy_annualized_volatility_pct", 60))
    exit_signal = (decision_price < long_average and short_average < long_average
                   and not rising and period_return < 0)
    bars = data["bars"]
    c = [b["c"] for b in bars]
    price = c[-1]
    momentum = rsi(c)  # Report-only diagnostic, never an entry/exit criterion.
    ranges = [max(b["h"]-b["l"], abs(b["h"]-a["c"]), abs(b["l"]-a["c"]))
              for a, b in zip(bars, bars[1:])]
    atr = statistics.mean(ranges[:14])
    for value in ranges[14:]:
        atr = (atr * 13 + value) / 14
    atr_pct = atr / price * 100
    asof = datetime.fromtimestamp(bars[-1]["t"], UTC)
    limit = config["max_crypto_age_days" if data["crypto"] else "max_equity_age_days"]
    fresh = timedelta(0) <= now-asof <= timedelta(days=limit)
    if keys[-1] < now.astimezone(ZoneInfo(data["timezone"])).date() - timedelta(days=14):
        fresh = False
    signal = "DATA_STALE" if not fresh else "BUY_CANDIDATE" if entry else "EXIT_REVIEW" if exit_signal else "WAIT"
    reasons = [f"Decision uses {lookback} completed weeks through {weeks[keys[-1]]['session_date']}",
               f"Weekly close {'above' if decision_price > long_average else 'below'} {lookback}-week average",
               f"{short_window}-week average {'above' if short_average > long_average else 'below'} {lookback}-week average",
               f"{lookback}-week average {'rising' if rising else 'flat/falling'} versus four weeks earlier",
               f"{lookback}-week adjusted return {period_return:+.1f}%; weekly-close maximum drawdown {drawdown:.1f}%"]
    if relative is None:
        reasons.append("Full-horizon benchmark data unavailable; buy signals disabled")
    else:
        reasons.append(f"{lookback}-week relative return {relative:+.1f} percentage points vs {config['benchmark']} (native currencies)")
    if not fresh:
        reasons.append("Data too old; trading signals suppressed")
    risk = "VERY HIGH" if data["crypto"] or volatility >= 60 else "HIGH" if volatility >= 35 else "MODERATE" if volatility >= 20 else "LOWER"
    volume_avg = statistics.mean(b["v"] for b in bars[-21:-1])
    volume_ratio = bars[-1]["v"] / volume_avg if volume_avg > 0 else 0
    move = (price/c[-2]-1)*100
    event = fresh and (abs(move) >= config["unusual_move_pct"] or volume_ratio >= config["unusual_volume_ratio"])
    stop = max(0, price - 2 * atr)
    return {"symbol": data["symbol"], "horizon": horizon, "name": data["name"], "currency": data["currency"],
            "lookback_weeks": lookback, "decision_date": weeks[keys[-1]]["session_date"],
            "decision_price": weeks[keys[-1]]["raw"], "period_return_pct": round(period_return, 2),
            "max_weekly_drawdown_pct": round(drawdown, 2),
            "asof": asof.isoformat(), "session_date": asof.astimezone(ZoneInfo(data["timezone"])).date().isoformat(),
            "price_source": "https://finance.yahoo.com/quote/" + quote(data["symbol"], safe=""),
            "price": round(bars[-1]["raw"], 4),
            "signal": signal, "strength": round(score), "risk": risk,
            "annualized_volatility_pct": round(volatility, 2), "atr_pct": round(atr_pct, 2),
            "rsi": round(momentum, 2), "daily_move_pct": round(move, 2),
            "volume_ratio": round(volume_ratio, 2), "unusual_activity": event,
            "illustrative_invalidation": round(stop * bars[-1]["raw"]/price, 4),
            "reasons": reasons, "risk_note": config.get("risk_notes", {}).get(data["symbol"],
            "Company, sector, liquidity, earnings and currency risks require separate review."),
            "owned": data["symbol"] in config.get("holdings", [])}


def message(row):
    action = row["signal"]
    if action == "EXIT_REVIEW":
        action = "SELL / REDUCE REVIEW" if row["owned"] else "AVOID / EXIT IF HELD"
    return (f"{row['symbol']} — {action} | {row['horizon']}\n{row['name']}\n"
            f"Daily close: {row['price']} {row['currency']} | session {row['session_date']}\n"
            f"Technical strength: {row['strength']}/100 (not a probability)\n"
            f"Historical risk: {row['risk']} | volatility {row['annualized_volatility_pct']}% | ATR {row['atr_pct']}%\n"
            + "\n".join(row["reasons"]) + "\n"
            f"Daily move {row['daily_move_pct']:+.2f}% | volume {row['volume_ratio']:.2f}×\n"
            f"Illustrative 2-ATR invalidation: {row['illustrative_invalidation']} {row['currency']} (not a guaranteed fill)\n"
            f"Risks: {row['risk_note']}\n"
            f"Price source: {row['price_source']}\n"
            "6–12 month price-history research; check news, earnings, valuation and portfolio overlap before acting.")


class AlertState:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS signals(symbol TEXT PRIMARY KEY, signal TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS delivered(key TEXT PRIMARY KEY)")

    def signal(self, symbol):
        row = self.db.execute("SELECT signal FROM signals WHERE symbol=?", (symbol,)).fetchone()
        return row[0] if row else None

    def seen(self, key):
        return self.db.execute("SELECT 1 FROM delivered WHERE key=?", (key,)).fetchone() is not None

    def mark(self, key):
        self.db.execute("INSERT OR IGNORE INTO delivered VALUES (?)", (key,))
        self.db.commit()

    def set_signal(self, symbol, signal):
        self.db.execute("INSERT OR REPLACE INTO signals VALUES (?, ?)", (symbol, signal))
        self.db.commit()


def telegram(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("Telegram is not configured; run python3 -m stock_agent setup-telegram")
    # Single message, safely below Telegram's 4096-character limit.
    response = request_json(f"https://api.telegram.org/bot{token}/sendMessage",
                            {"chat_id": chat, "text": text[:3900]})
    if not response.get("ok"):
        raise RuntimeError("Telegram rejected the message; check chat ID and bot permissions")


def deliver(rows, errors, state, send=telegram, news=None):
    for row in rows:
        symbol, signal = row["symbol"], row["signal"]
        state_symbol = f"{symbol}:{row['horizon']}"
        previous = state.signal(state_symbol)
        # Stale data must not reset a valid regime and cause duplicate trade alerts.
        if signal == "DATA_STALE":
            continue
        if signal != previous:
            if signal in ("BUY_CANDIDATE", "EXIT_REVIEW"):
                send(message(row))  # Persist only after successful delivery; failures retry next scan.
            state.set_signal(state_symbol, signal)
        if row["unusual_activity"]:
            key = f"activity:{symbol}:{row['asof']}"
            if not state.seen(key):
                send("UNUSUAL ACTIVITY — investigate cause\n" + message(row))
                state.mark(key)
    for symbol, items in (news or {}).items():
        for item in items:
            digest = hashlib.sha256(item["url"].encode()).hexdigest()
            key = f"news:{symbol}:{digest}"
            if not state.seen(key):
                send(f"NEWS REVIEW — {symbol}\n{item['title']}\n{item['published']}\n{item['url']}\n"
                     "Provider-associated headline; relevance and price impact are unverified. Not a buy/sell signal.")
                state.mark(key)
    # Daily health alerts, so silent provider failures cannot look like healthy monitoring.
    if errors:
        day = datetime.now(UTC).date().isoformat()
        digest = hashlib.sha256(json.dumps(errors, sort_keys=True).encode()).hexdigest()[:16]
        key = f"health:{day}:{digest}"
        if not state.seen(key):
            send("MONITOR DATA ISSUES — affected symbols have no current signal\n" +
                 "\n".join(f"{s}: {e}" for s, e in errors.items()))
            state.mark(key)


def daily_due(config, state, now):
    schedule = config["daily_notification"]
    local = now.astimezone(ZoneInfo(schedule["timezone"]))
    target = local.replace(hour=schedule["hour"], minute=schedule["minute"], second=0, microsecond=0)
    key = f"daily-summary:{local.date().isoformat()}"
    due = target <= local < target + timedelta(minutes=schedule["window_minutes"])
    return due and not state.seen(key), key


def daily_summary(rows, errors, max_new=3):
    valid = [r for r in rows if r["signal"] != "DATA_STALE"]
    if not valid:
        return "Analysis unavailable today. No buy/sell recommendations."
    # Suppress conflicting timeframes rather than recommending both buying and
    # selling the same investment in a compact message without an explanation.
    buys = {r["symbol"] for r in valid if r["signal"] == "BUY_CANDIDATE"}
    exits = {r["symbol"] for r in valid if r["signal"] == "EXIT_REVIEW"}
    conflicts = buys & exits
    # Holdings always remain visible; only the strongest outside ideas use the
    # limited Telegram space. Rank symbols once across both holding horizons.
    ranked = sorted((r for r in valid if not r["owned"] and r["signal"] == "BUY_CANDIDATE"
                     and r["symbol"] not in conflicts),
                    key=lambda r: (-r.get("strength", 0), r.get("annualized_volatility_pct", float("inf")), r["symbol"]))
    selected = set(list(dict.fromkeys(r["symbol"] for r in ranked))[:max(0, int(max_new))])
    lines = []
    for horizon, label in (("SIX_MONTH", "6-month"), ("ONE_YEAR", "1-year")):
        candidates = [r for r in valid if r["horizon"] == horizon and r["symbol"] not in conflicts
                      and (r["owned"] or r["symbol"] in selected)]
        buy = sorted({r["symbol"] for r in candidates if r["signal"] == "BUY_CANDIDATE"},
                     key=lambda s: (not any(r["symbol"] == s and r["owned"] for r in candidates), s))
        sell = sorted({r["symbol"] for r in candidates if r["signal"] == "EXIT_REVIEW" and r["owned"]})
        actions = []
        if buy:
            actions.append("Buy " + ", ".join(buy))
        if sell:
            actions.append("Sell " + ", ".join(sell))
        if actions:
            lines.append(label + ": " + "; ".join(actions) + ".")
    if not lines:
        lines = ["No buys or sells recommended" + (" from available data." if errors else ".")]
    else:
        lines.insert(0, "6–12 month research signals")
    if conflicts:
        lines.append("Mixed signals: " + ", ".join(sorted(conflicts)) + ".")
    if errors:
        affected = sorted({key.removesuffix(" news") for key in errors})
        brief = ", ".join(affected[:3])
        if len(affected) > 3:
            brief += f" (+{len(affected)-3} more)"
        lines.append("Incomplete data: " + brief + ".")
    return "\n".join(lines)


def deliver_daily(report, config, state, now=None, send=telegram):
    now = now or datetime.now(UTC)
    due, key = daily_due(config, state, now)
    if due:
        send(daily_summary(report["results"], report["errors"], config.get("max_new_ideas_daily", 3)))
        state.mark(key)
    return due


def universe(config):
    groups = {}
    if config.get("universe_file"):
        path = Path(config["universe_file"])
        if not path.is_absolute():
            path = ROOT / path
        groups = json.loads(path.read_text())["groups"]
    sectors = {symbol: group for group, symbols in groups.items() for symbol in symbols}
    symbols = list(dict.fromkeys(config["watchlist"] + config.get("holdings", []) + [config["benchmark"]]
                                + config.get("discovery", []) + list(sectors)))
    return symbols, sectors


def discovery_gate(data, config):
    bars = data["bars"][-20:]
    price = bars[-1]["raw"]
    volume = statistics.mean(b["v"] for b in bars)
    turnover = statistics.mean(b["raw"] * b["v"] for b in bars)
    eligible = (price >= config.get("discovery_min_price", 5)
                and volume >= config.get("discovery_min_volume20", 50000)
                and turnover >= config.get("discovery_min_turnover20", 1000000))
    return eligible, volume, turnover


def scan(config, notify=False):
    now = datetime.now(UTC)
    rows, errors, cache, news = [], {}, {}, {}
    symbols, sectors = universe(config)
    priority = set(config["watchlist"] + config.get("holdings", []) + [config["benchmark"]])
    def fetch(symbol):
        if symbol in config.get("unresolved", {}):
            return symbol, None, config["unresolved"][symbol]
        try:
            return symbol, history(symbol, now), None
        except (RuntimeError, KeyError, IndexError, TypeError, ValueError) as exc:
            return symbol, None, str(exc)
    def collect(results):
        for symbol, data, error in results:
            if error:
                errors[symbol] = error
            else:
                cache[symbol] = data
    # Fetch the user's holdings and benchmark first, before wider discovery.
    print(f"Scanning {len(symbols)} symbols; holdings and benchmark first.", flush=True)
    collect(map(fetch, (s for s in symbols if s in priority)))
    workers = max(1, min(4, config.get("discovery_workers", 3)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        collect(pool.map(fetch, (s for s in symbols if s not in priority)))
    benchmark = cache.get(config["benchmark"])
    if benchmark:
        age = now - datetime.fromtimestamp(benchmark["bars"][-1]["t"], UTC)
        if age > timedelta(days=config["max_equity_age_days"]):
            benchmark = None
    for symbol, data in cache.items():
        try:
            for horizon in ("SIX_MONTH", "ONE_YEAR"):
                row = analyse(data, benchmark, config, now, horizon)
                row["source"] = "watchlist" if symbol in config["watchlist"] else "discovery"
                row["sector_group"] = sectors.get(symbol, "priority_holdings")
                eligible, volume, turnover = discovery_gate(data, config)
                row["discovery_eligible"] = eligible
                row["average_volume20"] = round(volume)
                row["average_turnover20_native_currency"] = round(turnover)
                if symbol not in priority and not eligible and row["signal"] == "BUY_CANDIDATE":
                    row["signal"] = "WAIT"
                    row["reasons"].append("Outside idea excluded: minimum price/volume/turnover gate failed")
                rows.append(row)
                if row["signal"] == "DATA_STALE":
                    errors[symbol] = "Stale daily prices; signals suppressed"
        except (ValueError, KeyError, ZeroDivisionError, statistics.StatisticsError) as exc:
            errors[symbol] = f"Analysis unavailable: {exc}"
    if config.get("news_enabled", False):
        for symbol in config["watchlist"]:
            if symbol in config.get("unresolved", {}):
                continue
            try:
                news[symbol] = headlines(symbol, now)
            except RuntimeError as exc:
                errors[f"{symbol} news"] = str(exc)
    report = {"generated_at": now.isoformat(), "data_source": "Yahoo Finance unofficial daily chart endpoint",
              "horizon": "26-week and 52-week research using completed Friday-week closes; daily Telegram delivery",
              "benchmark": config["benchmark"], "coverage": {"requested": len(symbols), "analysed": len(cache), "sector_groups": list(dict.fromkeys(sectors.values()))},
              "results": sorted(rows, key=lambda x: (x["source"] != "watchlist", -x["strength"])), "news": news, "errors": errors}
    folder = ROOT / "reports"
    folder.mkdir(exist_ok=True)
    (folder / "latest.json").write_text(json.dumps(report, indent=2))
    lines = ["# Daily market research", "", f"Generated: {now.isoformat()}", "",
             "Decisions use completed weekly closes over 26 and 52 weeks. Latest daily prices are context only. Scores are rule counts, not success probabilities.", "",
             "| Symbol | Horizon | Close | Signal | Strength | Risk | Volatility |", "|---|---|---:|---|---:|---|---:|"]
    for row in report["results"]:
        lines.append(f"| {row['symbol']} | {row['horizon']} | {row['price']} {row['currency']} | {row['signal']} | {row['strength']}/100 | {row['risk']} | {row['annualized_volatility_pct']}% |")
    for row in report["results"]:
        lines.extend(["", f"## {row['symbol']}", "", message(row)])
    if errors:
        lines.extend(["", "## Data issues", ""] + [f"- {s}: {e}" for s, e in errors.items()])
    lines.extend(["", "## Recent news for review", "", "Provider-associated headlines; relevance and price impact unverified."])
    for symbol, items in news.items():
        for item in items:
            lines.append(f"- {symbol}: {item['title']} — {item['published']} — {item['url']}")
    (folder / "latest.md").write_text("\n".join(lines) + "\n")
    print(f"{now.isoformat()} | analysed {len(cache)} symbols, {len(rows)} horizon results | data issues {len(errors)} | reports/latest.md", flush=True)
    if notify:
        state = AlertState(ROOT / "runtime" / "alerts.sqlite3")
        try:
            deliver_daily(report, config, state)
        finally:
            state.db.close()
    return report


def choose_destination(token, bot_username):
    while True:
        updates = request_json(f"https://api.telegram.org/bot{token}/getUpdates")
        if not updates.get("ok"):
            raise RuntimeError("Cannot list chats. Send /start to your bot and try again; an existing webhook can prevent chat discovery.")
        chats = {str(u["message"]["chat"]["id"]): u["message"]["chat"]
                 for u in updates.get("result", []) if "message" in u}
        if chats:
            for chat_id, chat in chats.items():
                print(f"Chat {chat_id}: {chat.get('title', chat.get('first_name', 'unnamed'))}")
            print("Enter the number after 'Chat' for the chat where you want alerts.")
        else:
            print(f"No chats found. In Telegram, open @{bot_username} (your new bot, not BotFather).")
            print("Tap Start or send /start, then return here and press Enter to refresh.")
        answer = input("Destination chat ID (Enter = refresh; q = cancel): ").strip()
        if answer.lower() == "q":
            raise RuntimeError("Setup cancelled; no credentials were saved")
        if not answer:
            continue
        if not answer.lstrip("-").isdigit() or answer == "0":
            print("Use a numeric chat ID, such as 123456789. Press Enter to refresh the list.")
            continue
        return answer


def setup_telegram():
    print("Create a bot with @BotFather, then open your bot chat and send /start.")
    token = getpass.getpass("Bot token (hidden; stored only in ignored .env): ").strip()
    if not token or any(ch.isspace() for ch in token):
        raise RuntimeError("Invalid token format")
    me = request_json(f"https://api.telegram.org/bot{token}/getMe")
    if not me.get("ok"):
        raise RuntimeError("Bot authentication failed")
    chat_id = choose_destination(token, me.get("result", {}).get("username", "your_bot"))
    path = ROOT / ".env"
    existing = path.read_text().splitlines() if path.exists() else []
    existing = [line for line in existing if not line.startswith(("TELEGRAM_BOT_TOKEN=", "TELEGRAM_CHAT_ID="))]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(existing + [f"TELEGRAM_BOT_TOKEN={token}", f"TELEGRAM_CHAT_ID={chat_id}"]) + "\n")
    path.chmod(0o600)
    print("Saved. Run test-telegram to send a connection test to this chat.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["scan", "watch", "setup-telegram", "test-telegram"])
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--notify", action="store_true", help="Send the brief daily Telegram summary if due")
    args = parser.parse_args()
    load_env()
    try:
        if args.command == "setup-telegram":
            setup_telegram()
            return
        if args.command == "test-telegram":
            telegram("Stock agent connection test. Daily-close monitoring is available; this is not a trading signal.")
            print("Telegram connection test delivered.")
            return
        config = json.loads(args.config.read_text())
        if args.command == "scan":
            scan(config, args.notify)
            return
        if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
            raise RuntimeError("Configure Telegram before starting watch")
        # A process lock prevents duplicate notifications from concurrent watchers.
        import fcntl
        (ROOT / "runtime").mkdir(exist_ok=True)
        with (ROOT / "runtime" / "watch.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another watch process is already running") from None
            print("Daily summary scheduled for 12 noon America/Toronto; routine detailed alerts are disabled.", flush=True)
            while True:
                try:
                    config = json.loads(args.config.read_text())
                    state = AlertState(ROOT / "runtime" / "alerts.sqlite3")
                    try:
                        due, _ = daily_due(config, state, datetime.now(UTC))
                    finally:
                        state.db.close()
                    if due:
                        scan(config, True)
                except Exception as exc:
                    print(f"Scan failed: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(max(60, config["poll_seconds"]))
    except (RuntimeError, OSError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
