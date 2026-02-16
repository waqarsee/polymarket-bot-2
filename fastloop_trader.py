#!/usr/bin/env python3
"""
Simmer FastLoop Trading Skill (SAFE / RESEARCH MODE)

- Discovers Polymarket fast markets via Gamma API
- Pulls price signal (Binance candles or CoinGecko snapshot)
- Evaluates momentum + divergence + fee-aware filter
- Prints opportunities (NO TRADE EXECUTION)

Railway-friendly:
- Starts a tiny HTTP server on $PORT so the deployment stays "healthy"
- Runs one scan cycle at startup
- Optionally loops every N seconds if LOOP_SECONDS is set

Environment:
- SIMMER_API_KEY (optional in this SAFE file; used only for portfolio/positions if you enable it)
- SIMMER_SPRINT_SIGNAL: binance | coingecko
- SIMMER_SPRINT_ASSET: BTC | ETH | SOL
- SIMMER_SPRINT_WINDOW: 5m | 15m
- SIMMER_SPRINT_ENTRY: float (default 0.05)
- SIMMER_SPRINT_MOMENTUM: float percent (default 0.2)
- SIMMER_SPRINT_LOOKBACK: int minutes (default 5)
- SIMMER_SPRINT_MIN_TIME: int seconds (default 60)
- SIMMER_SPRINT_VOL_CONF: true/false (default true)
- LOOP_SECONDS: if set (e.g., 300), loops every N seconds (Railway worker style)
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

# Safe stdout reconfigure (some Python envs don’t support it)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

# =============================================================================
# Configuration (config.json > env vars > defaults)
# =============================================================================

CONFIG_SCHEMA = {
    "entry_threshold": {"default": 0.05, "env": "SIMMER_SPRINT_ENTRY", "type": float,
                        "help": "Min price divergence from 50¢ to flag signal"},
    "min_momentum_pct": {"default": 0.2, "env": "SIMMER_SPRINT_MOMENTUM", "type": float,
                         "help": "Min % move in lookback window to flag signal"},
    "signal_source": {"default": "binance", "env": "SIMMER_SPRINT_SIGNAL", "type": str,
                      "help": "Price feed source (binance, coingecko)"},
    "lookback_minutes": {"default": 5, "env": "SIMMER_SPRINT_LOOKBACK", "type": int,
                         "help": "Minutes of price history for momentum calc"},
    "min_time_remaining": {"default": 60, "env": "SIMMER_SPRINT_MIN_TIME", "type": int,
                           "help": "Skip fast markets with less than this many seconds remaining"},
    "asset": {"default": "BTC", "env": "SIMMER_SPRINT_ASSET", "type": str,
              "help": "Asset to scan (BTC, ETH, SOL)"},
    "window": {"default": "5m", "env": "SIMMER_SPRINT_WINDOW", "type": str,
               "help": "Market window duration (5m or 15m)"},
    "volume_confidence": {"default": True, "env": "SIMMER_SPRINT_VOL_CONF", "type": bool,
                          "help": "Weight signal by volume (higher volume = more confident)"},
    "quiet": {"default": False, "env": "QUIET", "type": bool,
              "help": "Only output key events"},
}

ASSET_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
ASSET_PATTERNS = {
    "BTC": ["bitcoin up or down"],
    "ETH": ["ethereum up or down"],
    "SOL": ["solana up or down"],
}
COINGECKO_ASSETS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}


def _load_config(schema, skill_file, config_filename="config.json"):
    from pathlib import Path
    config_path = Path(skill_file).parent / config_filename
    file_cfg = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                file_cfg = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    result = {}
    for key, spec in schema.items():
        if key in file_cfg:
            result[key] = file_cfg[key]
        elif spec.get("env") and os.environ.get(spec["env"]):
            val = os.environ.get(spec["env"])
            type_fn = spec.get("type", str)
            try:
                if type_fn == bool:
                    result[key] = str(val).lower() in ("true", "1", "yes")
                else:
                    result[key] = type_fn(val)
            except (ValueError, TypeError):
                result[key] = spec.get("default")
        else:
            result[key] = spec.get("default")
    return result


cfg = _load_config(CONFIG_SCHEMA, __file__)
ENTRY_THRESHOLD = cfg["entry_threshold"]
MIN_MOMENTUM_PCT = cfg["min_momentum_pct"]
SIGNAL_SOURCE = cfg["signal_source"]
LOOKBACK_MINUTES = cfg["lookback_minutes"]
MIN_TIME_REMAINING = cfg["min_time_remaining"]
ASSET = cfg["asset"].upper()
WINDOW = cfg["window"]
VOLUME_CONFIDENCE = cfg["volume_confidence"]
QUIET = cfg["quiet"]


# =============================================================================
# HTTP helper
# =============================================================================

def _api_request(url, method="GET", data=None, headers=None, timeout=15):
    try:
        req_headers = headers or {}
        if "User-Agent" not in req_headers:
            req_headers["User-Agent"] = "simmer-fastloop_safe/1.0"

        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = Request(url, data=body, headers=req_headers, method=method)
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except HTTPError as e:
        try:
            raw = e.read().decode("utf-8")
            j = json.loads(raw) if raw else {}
            return {"error": j.get("detail", raw or str(e)), "status_code": e.code}
        except Exception:
            return {"error": str(e), "status_code": getattr(e, "code", None)}
    except URLError as e:
        return {"error": f"Connection error: {getattr(e, 'reason', e)}"}
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# Polymarket fast markets
# =============================================================================

def discover_fast_markets(asset="BTC", window="5m"):
    patterns = ASSET_PATTERNS.get(asset, ASSET_PATTERNS["BTC"])
    url = (
        "https://gamma-api.polymarket.com/markets"
        "?limit=25&closed=false&tag=crypto&order=createdAt&ascending=false"
    )
    result = _api_request(url)
    if not result or (isinstance(result, dict) and result.get("error")):
        return [], result

    markets = []
    for m in result:
        q = (m.get("question") or "").lower()
        slug = m.get("slug", "")
        matches_window = f"-{window}-" in slug
        if any(p in q for p in patterns) and matches_window:
            end_time = _parse_fast_market_end_time(m.get("question", ""))
            markets.append({
                "question": m.get("question", ""),
                "slug": slug,
                "end_time": end_time,
                "outcome_prices": m.get("outcomePrices", "[]"),
                "fee_rate_bps": int(m.get("fee_rate_bps") or m.get("feeRateBps") or 0),
            })
    return markets, None


def _parse_fast_market_end_time(question: str):
    """
    Parse end time from questions like:
    'Bitcoin Up or Down - February 16, 7:35PM-7:40PM ET'
    Uses America/New_York to handle EST/EDT correctly.
    """
    import re
    from zoneinfo import ZoneInfo

    pattern = r'(\w+ \d+),.*?-\s*(\d{1,2}:\d{2}(?:AM|PM))\s*ET'
    match = re.search(pattern, question)
    if not match:
        return None

    try:
        date_str = match.group(1)       # e.g. "February 16"
        time_str = match.group(2)       # e.g. "7:40PM"
        year = datetime.now().year
        dt_str = f"{date_str} {year} {time_str}"

        dt = datetime.strptime(dt_str, "%B %d %Y %I:%M%p")
        dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def find_best_market(markets):
    now = datetime.now(timezone.utc)
    candidates = []
    for m in markets:
        end_time = m.get("end_time")
        if not end_time:
            continue
        remaining = (end_time - now).total_seconds()
        if remaining > MIN_TIME_REMAINING:
            candidates.append((remaining, m))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


# =============================================================================
# Price signal
# =============================================================================

def get_binance_momentum(symbol="BTCUSDT", lookback_minutes=5):
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval=1m&limit={lookback_minutes}"
    )
    result = _api_request(url)

    # Detailed debug output if Binance fails
    if not result:
        return None, "Binance: no response"

    if isinstance(result, dict) and result.get("error"):
        return None, f"Binance error: {result.get('error')} (status={result.get('status_code')})"

    if isinstance(result, dict):
        return None, f"Binance unexpected response: {result}"

    try:
        candles = result
        if len(candles) < 2:
            return None, "Binance: not enough candles"

        price_then = float(candles[0][1])
        price_now = float(candles[-1][4])
        momentum_pct = ((price_now - price_then) / price_then) * 100.0
        direction = "up" if momentum_pct > 0 else "down"

        volumes = [float(c[5]) for c in candles]
        avg_volume = sum(volumes) / len(volumes) if volumes else 0
        latest_volume = volumes[-1] if volumes else 0
        volume_ratio = (latest_volume / avg_volume) if avg_volume > 0 else 1.0

        return {
            "momentum_pct": momentum_pct,
            "direction": direction,
            "price_now": price_now,
            "price_then": price_then,
            "avg_volume": avg_volume,
            "latest_volume": latest_volume,
            "volume_ratio": volume_ratio,
            "candles": len(candles),
        }, None
    except Exception as e:
        return None, f"Binance parse error: {e}"


def get_coingecko_snapshot(asset_id="bitcoin"):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={asset_id}&vs_currencies=usd"
    result = _api_request(url)
    if not result or (isinstance(result, dict) and result.get("error")):
        return None, f"CoinGecko error: {result.get('error') if isinstance(result, dict) else 'no response'}"

    price_now = result.get(asset_id, {}).get("usd")
    if not price_now:
        return None, "CoinGecko: missing price"

    # Snapshot only (no momentum without tracking history)
    return {
        "momentum_pct": 0.0,
        "direction": "neutral",
        "price_now": float(price_now),
        "price_then": float(price_now),
        "avg_volume": 0.0,
        "latest_volume": 0.0,
        "volume_ratio": 1.0,
        "candles": 0,
    }, None


def get_momentum(asset="BTC", source="binance", lookback=5):
    if source == "binance":
        symbol = ASSET_SYMBOLS.get(asset, "BTCUSDT")
        return get_binance_momentum(symbol, lookback)
    if source == "coingecko":
        cg_id = COINGECKO_ASSETS.get(asset, "bitcoin")
        return get_coingecko_snapshot(cg_id)
    return None, f"Unknown signal source: {source}"


# =============================================================================
# Strategy evaluation (SAFE: prints only)
# =============================================================================

def run_once():
    def log(msg, force=False):
        if not QUIET or force:
            print(msg)

    print("⚡ Simmer FastLoop (SAFE / Research Mode)")
    print("=" * 60)

    log(f"⚙️  Config: ASSET={ASSET} WINDOW={WINDOW} SIGNAL={SIGNAL_SOURCE} "
        f"LOOKBACK={LOOKBACK_MINUTES}m MIN_MOM={MIN_MOMENTUM_PCT}% ENTRY={ENTRY_THRESHOLD} "
        f"MIN_TIME={MIN_TIME_REMAINING}s VOL_CONF={'on' if VOLUME_CONFIDENCE else 'off'}")

    log(f"\n🔍 Discovering {ASSET} fast markets...")
    markets, err = discover_fast_markets(ASSET, WINDOW)
    if err:
        log(f"  ❌ Market discovery error: {err}", force=True)
        return

    log(f"  Found {len(markets)} active fast markets")
    if not markets:
        print("📊 Summary: No markets available")
        return

    best = find_best_market(markets)
    if not best:
        log(f"  No fast markets with >{MIN_TIME_REMAINING}s remaining")
        print("📊 Summary: No tradeable markets (too close to expiry)")
        return

    end_time = best.get("end_time")
    remaining = (end_time - datetime.now(timezone.utc)).total_seconds() if end_time else 0
    log(f"\n🎯 Selected: {best['question']}")
    log(f"  Expires in: {remaining:.0f}s")

    # Parse YES price
    try:
        prices = json.loads(best.get("outcome_prices", "[]"))
        market_yes_price = float(prices[0]) if prices else 0.5
    except Exception:
        market_yes_price = 0.5
    log(f"  Current YES price: ${market_yes_price:.3f}")

    fee_rate_bps = best.get("fee_rate_bps", 0)
    fee_rate = fee_rate_bps / 10000
    if fee_rate > 0:
        log(f"  Fee rate: {fee_rate:.0%}")

    log(f"\n📈 Fetching {ASSET} price signal ({SIGNAL_SOURCE})...")
    momentum, m_err = get_momentum(ASSET, SIGNAL_SOURCE, LOOKBACK_MINUTES)
    if not momentum:
        log(f"  ❌ Failed to fetch price data: {m_err}", force=True)
        print("📊 Summary: No signal (price feed failed)")
        return

    log(f"  Price: ${momentum['price_now']:,.2f} (was ${momentum['price_then']:,.2f})")
    log(f"  Momentum: {momentum['momentum_pct']:+.3f}%")
    log(f"  Direction: {momentum['direction']}")
    if VOLUME_CONFIDENCE:
        log(f"  Volume ratio: {momentum['volume_ratio']:.2f}x avg")

    log(f"\n🧠 Analyzing...")
    mom_abs = abs(momentum["momentum_pct"])
    direction = momentum["direction"]

    if mom_abs < MIN_MOMENTUM_PCT:
        log(f"  ⏸️  Momentum {mom_abs:.3f}% < minimum {MIN_MOMENTUM_PCT}% — skip")
        print(f"📊 Summary: No signal (momentum too weak: {mom_abs:.3f}%)")
        return

    if VOLUME_CONFIDENCE and momentum["volume_ratio"] < 0.5:
        log(f"  ⏸️  Low volume ({momentum['volume_ratio']:.2f}x avg) — skip")
        print("📊 Summary: No signal (low volume)")
        return

    if direction == "up":
        side = "YES"
        divergence = 0.50 + ENTRY_THRESHOLD - market_yes_price
        rationale = f"{ASSET} up {momentum['momentum_pct']:+.3f}% but YES only ${market_yes_price:.3f}"
        buy_price = market_yes_price
    else:
        side = "NO"
        divergence = market_yes_price - (0.50 - ENTRY_THRESHOLD)
        rationale = f"{ASSET} down {momentum['momentum_pct']:+.3f}% but YES still ${market_yes_price:.3f}"
        buy_price = 1 - market_yes_price

    if divergence <= 0:
        log(f"  ⏸️  Market already priced in (divergence {divergence:.3f}) — skip")
        print("📊 Summary: No signal (priced in)")
        return

    # Fee-aware filter (fast markets fee on winnings)
    if fee_rate > 0:
        win_profit = (1 - buy_price) * (1 - fee_rate)
        breakeven = buy_price / (win_profit + buy_price) if (win_profit + buy_price) > 0 else 1.0
        fee_penalty = breakeven - 0.50
        min_div = fee_penalty + 0.02
        log(f"  Breakeven win rate: {breakeven:.1%} (fee-adjusted); min divergence ≈ {min_div:.3f}")
        if divergence < min_div:
            log(f"  ⏸️  Divergence {divergence:.3f} < fee-adjusted minimum {min_div:.3f} — skip")
            print("📊 Summary: No signal (fees eat edge)")
            return

    print("\n✅ SIGNAL FOUND (research only; no trade executed)")
    print(f"  Action: {side}")
    print(f"  Reason: {rationale}")
    print(f"  Divergence: {divergence:.3f}")
    print(f"  Market URL: https://polymarket.com/event/{best['slug']}")
    print("📊 Summary: Signal printed (no execution)")


# =============================================================================
# Railway healthcheck server (optional)
# =============================================================================

def start_health_server():
    """
    Starts a tiny HTTP server so Railway 'Web Service' stays healthy.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK\n")

        def log_message(self, format, *args):
            # silence default HTTP logs
            return

    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-http", action="store_true", help="Do not start health server")
    parser.add_argument("--loop", type=int, default=None, help="Loop every N seconds")
    args = parser.parse_args()

    if not args.no_http:
        port = start_health_server()
        if not QUIET:
            print(f"🩺 Health server listening on :{port}")

    loop_seconds = args.loop
    if loop_seconds is None:
        env_loop = os.environ.get("LOOP_SECONDS")
        loop_seconds = int(env_loop) if env_loop else None

    if loop_seconds:
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"❌ Runtime error: {e}")
            time.sleep(loop_seconds)
    else:
        run_once()


if __name__ == "__main__":
    main()
