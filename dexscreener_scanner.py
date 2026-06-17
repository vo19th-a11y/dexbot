#!/usr/bin/env python3
"""
DexScreener new-launch keyword scanner — Render free-tier edition.

Scans the DexScreener token-profiles feed (the part of a token page holding its
summary/description). Alerts when a token on ETH / SOL / BASE / BSC has a
summary containing a target keyword AND clears the volume / market-cap filters.
New launches are prioritised: alerts are sorted newest-first and very fresh
tokens are flagged with a NEW LAUNCH marker. Each alert reports volume, market
cap and launch age.

It also runs a tiny web server so it qualifies as a free Render "web service".
A keep-alive pinger hitting the URL every few minutes stops Render sleeping it.

Public endpoints used (no API key required):
  https://api.dexscreener.com/token-profiles/latest/v1        (~60 req/min)
  https://api.dexscreener.com/token-profiles/recent-updates/v1
  https://api.dexscreener.com/latest/dex/tokens/{address}     (vol + mcap + age)

Environment variables (set these in the Render dashboard):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   -> sends Telegram messages
  DISCORD_WEBHOOK_URL                    -> posts to a Discord channel (optional)
  PORT                                   -> set automatically by Render
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# ----------------------------- CONFIG --------------------------------------

CHAINS = {"ethereum", "solana", "base", "bsc"}

# Whole-word, case-insensitive. "new" matches the word new, not "news"/"renew".
KEYWORDS = [
    "first",
    "new",
]

# A token must clear this 24h volume (in USD) to trigger an alert.
MIN_VOLUME_USD = 400_000

# Optional market-cap floor. Set to 0 to disable (market cap is still shown).
MIN_MARKET_CAP_USD = 0

# Market-cap ceiling: tokens ABOVE this are skipped. Set to 0 to disable.
MAX_MARKET_CAP_USD = 5_000_000

# Only alert on launches younger than this many hours. Set to 0 to disable
# (when disabled, older tokens still alert but are sorted below newer ones).
MAX_AGE_HOURS = 0

# Tokens younger than this get a "NEW LAUNCH" tag and sort to the top.
NEW_LAUNCH_HOURS = 24

# Require ALL keywords (True) or ANY one of them (False).
REQUIRE_ALL_KEYWORDS = False

POLL_SECONDS = 30
SEEN_FILE = "seen_tokens.json"

PROFILE_ENDPOINTS = [
    "https://api.dexscreener.com/token-profiles/latest/v1",
    "https://api.dexscreener.com/token-profiles/recent-updates/v1",
]
TOKEN_ENDPOINT = "https://api.dexscreener.com/latest/dex/tokens/{address}"

HEADERS = {"Accept": "application/json", "User-Agent": "dexscreener-scanner/1.0"}

PATTERNS = [re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE) for kw in KEYWORDS]

# Shared status so the web page can show the bot is alive.
STATUS = {"started": None, "last_scan": None, "matches": 0, "checked": 0}

# --------------------------- ALERT CHANNELS --------------------------------


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[warn] telegram send failed: {e}", flush=True)


def send_discord(text: str) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return
    try:
        requests.post(url, json={"content": text}, timeout=10)
    except requests.RequestException as e:
        print(f"[warn] discord send failed: {e}", flush=True)


def hours_since(created_at_ms):
    if not created_at_ms:
        return None
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(
        created_at_ms / 1000, timezone.utc
    )
    return max(delta.total_seconds() / 3600, 0)


def format_age(created_at_ms) -> str:
    h = hours_since(created_at_ms)
    if h is None:
        return "unknown"
    secs = int(h * 3600)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def alert(profile: dict, matched: list, volume: float, market_cap, created_at_ms) -> None:
    chain = profile.get("chainId", "?")
    addr = profile.get("tokenAddress", "?")
    url = profile.get("url") or f"https://dexscreener.com/{chain}/{addr}"
    desc = (profile.get("description") or "").strip()
    mcap_str = f"${market_cap:,.0f}" if market_cap is not None else "n/a"

    age_h = hours_since(created_at_ms)
    is_new = age_h is not None and age_h <= NEW_LAUNCH_HOURS
    header = "\U0001F195 NEW LAUNCH" if is_new else "\U0001F6A8 Match"

    msg = (
        f"{header} on {chain.upper()}\n"
        f"Keywords: {', '.join(matched)}\n"
        f"Age: {format_age(created_at_ms)}\n"
        f"24h Volume: ${volume:,.0f}\n"
        f"Market Cap: {mcap_str}\n"
        f"Token: {addr}\n"
        f"Summary: {desc[:400]}\n"
        f"{url}"
    )
    print("\n" + msg + "\n", flush=True)
    send_telegram(msg)
    send_discord(msg)
    STATUS["matches"] += 1


# ----------------------------- CORE LOGIC ----------------------------------


def load_seen() -> set:
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen: set) -> None:
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f)
    except OSError as e:
        print(f"[warn] could not save seen file: {e}", flush=True)


def matched_keywords(description: str) -> list:
    text = description or ""
    hits = [kw for kw, pat in zip(KEYWORDS, PATTERNS) if pat.search(text)]
    if REQUIRE_ALL_KEYWORDS and len(hits) != len(KEYWORDS):
        return []
    return hits


def get_token_metrics(token_address: str):
    """Return (volume_24h, market_cap, pair_created_at_ms) from the token's top
    pair, or None on error. market_cap / created_at may be None if not reported.
    """
    try:
        resp = requests.get(
            TOKEN_ENDPOINT.format(address=token_address), headers=HEADERS, timeout=15
        )
        if resp.status_code == 429:
            print("[warn] rate limited (429) on metrics lookup, backing off", flush=True)
            time.sleep(60)
            return None
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        if not pairs:
            return (0.0, None, None)
        # Use the pair with the highest 24h volume as the token's primary market.
        best = max(pairs, key=lambda p: float((p.get("volume") or {}).get("h24") or 0))
        volume = float((best.get("volume") or {}).get("h24") or 0)
        raw_mcap = best.get("marketCap")
        market_cap = float(raw_mcap) if raw_mcap is not None else None
        created_at = best.get("pairCreatedAt")  # ms since epoch, or absent
        return (volume, market_cap, created_at)
    except (requests.RequestException, ValueError) as e:
        print(f"[warn] metrics lookup failed for {token_address}: {e}", flush=True)
        return None


def fetch_profiles(endpoint: str) -> list:
    resp = requests.get(endpoint, headers=HEADERS, timeout=15)
    if resp.status_code == 429:
        print("[warn] rate limited (429), backing off", flush=True)
        time.sleep(60)
        return []
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        data = data.get("data", [])
    return data if isinstance(data, list) else []


def scan_once(seen: set) -> None:
    candidates = []  # collected this cycle, then sorted newest-first before alerting
    queued = set()   # avoid double-processing a token that appears in both feeds

    for endpoint in PROFILE_ENDPOINTS:
        try:
            profiles = fetch_profiles(endpoint)
        except requests.RequestException as e:
            print(f"[warn] fetch failed for {endpoint}: {e}", flush=True)
            continue

        for p in profiles:
            if p.get("chainId") not in CHAINS:
                continue

            key = f"{p.get('chainId')}:{p.get('tokenAddress')}"
            if key in seen or key in queued:
                continue

            hits = matched_keywords(p.get("description", ""))
            STATUS["checked"] += 1
            if not hits:
                continue  # keyword miss; not marked seen, cheap to re-check

            metrics = get_token_metrics(p.get("tokenAddress", ""))
            if metrics is None:
                continue  # lookup failed; retry next cycle
            volume, market_cap, created_at = metrics

            if volume < MIN_VOLUME_USD:
                continue  # below volume floor; re-check later as volume may grow
            if MIN_MARKET_CAP_USD and (market_cap is None or market_cap < MIN_MARKET_CAP_USD):
                continue  # below market-cap floor
            if MAX_MARKET_CAP_USD and (market_cap is None or market_cap > MAX_MARKET_CAP_USD):
                continue  # above market-cap ceiling (unknown market cap also skipped)

            age_h = hours_since(created_at)
            if MAX_AGE_HOURS and (age_h is None or age_h > MAX_AGE_HOURS):
                continue  # too old (or unknown age) for the new-launch window

            # sort key: newer launches first; unknown age sorts last
            sort_age = age_h if age_h is not None else float("inf")
            candidates.append((sort_age, key, p, hits, volume, market_cap, created_at))
            queued.add(key)

    # Prioritise new launches: youngest first.
    candidates.sort(key=lambda c: c[0])
    for _, key, p, hits, volume, market_cap, created_at in candidates:
        alert(p, hits, volume, market_cap, created_at)
        seen.add(key)  # only mark seen once it has actually alerted

    STATUS["last_scan"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def scanner_loop() -> None:
    seen = load_seen()
    STATUS["started"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(
        f"Scanning {', '.join(sorted(CHAINS))} for words {KEYWORDS} "
        f"(min 24h vol ${MIN_VOLUME_USD:,}, max mcap ${MAX_MARKET_CAP_USD:,}) "
        f"every {POLL_SECONDS}s. New launches prioritised.",
        flush=True,
    )
    while True:
        try:
            scan_once(seen)
            save_seen(seen)
        except Exception as e:  # keep the loop alive no matter what
            print(f"[error] scan loop: {e}", flush=True)
        time.sleep(POLL_SECONDS)


# ----------------------------- WEB SERVER ----------------------------------
# Render needs an open port to treat this as a live web service. This also
# gives the keep-alive pinger something to hit.


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "alive", **STATUS}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence per-request logging
        pass


def main() -> None:
    threading.Thread(target=scanner_loop, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Web server listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
