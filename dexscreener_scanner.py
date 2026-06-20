#!/usr/bin/env python3
"""
DexScreener new-launch keyword scanner — Render free-tier edition.

Scans the DexScreener token-profiles feed and alerts (Telegram / Discord / log)
when a token on ETH / SOL / BASE / BSC has a summary containing a target keyword
AND clears the volume / market-cap / age filters. New launches are prioritised
(sorted youngest-first, tagged when very fresh). Each alert shows volume, market
cap, launch age, and a tap-to-copy contract address.

You can change the filters live from Telegram by sending commands to the bot
(see /help). A tiny web server is also run so it qualifies as a free Render
"web service"; a keep-alive pinger hitting the URL stops Render sleeping it.

Public endpoints used (no API key required):
  https://api.dexscreener.com/token-profiles/latest/v1        (~60 req/min)
  https://api.dexscreener.com/token-profiles/recent-updates/v1
  https://api.dexscreener.com/latest/dex/tokens/{address}     (vol + mcap + age)

Environment variables (set these in the Render dashboard):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   -> sends + receives Telegram messages
  DISCORD_WEBHOOK_URL                    -> posts to a Discord channel (optional)
  PORT                                   -> set automatically by Render
"""

import html
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# ------------------------- FIXED SETTINGS ----------------------------------

POLL_SECONDS = 30
SEEN_FILE = "seen_tokens.json"
SETTINGS_FILE = "settings.json"

# Friendly names -> the exact DexScreener chainId. Anything not listed is used
# as-is (lowercased), so any valid DexScreener chainId still works.
CHAIN_ALIASES = {
    "eth": "ethereum", "ethereum": "ethereum",
    "sol": "solana", "solana": "solana",
    "base": "base",
    "bsc": "bsc", "bnb": "bsc", "binance": "bsc",
    "tron": "tron", "trx": "tron",
    "sui": "sui",
    "ton": "ton", "gram": "ton",
    "arb": "arbitrum", "arbitrum": "arbitrum",
    "matic": "polygon", "polygon": "polygon",
    "avax": "avalanche", "avalanche": "avalanche",
    "op": "optimism", "optimism": "optimism",
}

# ------------------------- TUNABLE SETTINGS --------------------------------
# These defaults can be changed live from Telegram (see /help). Edits made via
# Telegram are saved to settings.json, but note: on Render's free tier that file
# is wiped on every redeploy, so it falls back to these defaults after a deploy.

DEFAULTS = {
    "chains": ["ethereum", "solana", "base", "bsc", "tron", "sui", "ton"],
    "keywords": ["first", "new"],     # whole-word, case-insensitive
    "require_all_keywords": False,    # True = must contain every keyword
    "min_volume_usd": 15_000,         # 24h volume floor
    "min_market_cap_usd": 10_000,     # market-cap floor (0 = off)
    "max_market_cap_usd": 5_000_000,  # market-cap ceiling (0 = off)
    "max_age_hours": 0,               # only alert younger than this (0 = off)
    "new_launch_hours": 24,           # younger than this gets a NEW LAUNCH tag
}
CONFIG = dict(DEFAULTS)


def normalize_chain(token: str) -> str:
    t = token.strip().lower()
    return CHAIN_ALIASES.get(t, t)

PROFILE_ENDPOINTS = [
    "https://api.dexscreener.com/token-profiles/latest/v1",
    "https://api.dexscreener.com/token-profiles/recent-updates/v1",
]
TOKEN_ENDPOINT = "https://api.dexscreener.com/latest/dex/tokens/{address}"
HEADERS = {"Accept": "application/json", "User-Agent": "dexscreener-scanner/1.0"}

STATUS = {"started": None, "last_scan": None, "matches": 0, "checked": 0}

# --------------------------- CONFIG PERSISTENCE ----------------------------


def load_config() -> None:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k in DEFAULTS:
            if k in saved:
                CONFIG[k] = saved[k]
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def save_config() -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f)
    except OSError as e:
        print(f"[warn] could not save settings: {e}", flush=True)


# --------------------------- ALERT CHANNELS --------------------------------


def send_telegram(text: str, parse_mode: str = None) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": False}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10
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
    desc = (profile.get("description") or "").strip()[:400]
    mcap_str = f"${market_cap:,.0f}" if market_cap is not None else "n/a"

    age_h = hours_since(created_at_ms)
    is_new = age_h is not None and age_h <= CONFIG["new_launch_hours"]
    header = "\U0001F195 NEW LAUNCH" if is_new else "\U0001F6A8 Match"

    plain = (
        f"{header} on {chain.upper()}\n"
        f"Keywords: {', '.join(matched)}\n"
        f"Age: {format_age(created_at_ms)}\n"
        f"24h Volume: ${volume:,.0f}\n"
        f"Market Cap: {mcap_str}\n"
        f"Token: {addr}\n"
        f"Summary: {desc}\n"
        f"{url}"
    )
    print("\n" + plain + "\n", flush=True)

    e = html.escape
    telegram_msg = (
        f"{header} on {e(chain.upper())}\n"
        f"Keywords: {e(', '.join(matched))}\n"
        f"Age: {e(format_age(created_at_ms))}\n"
        f"24h Volume: ${volume:,.0f}\n"
        f"Market Cap: {e(mcap_str)}\n"
        f"Token (tap to copy): <code>{e(addr)}</code>\n"
        f"Summary: {e(desc)}\n"
        f"{e(url)}"
    )
    send_telegram(telegram_msg, parse_mode="HTML")

    discord_msg = plain.replace(f"Token: {addr}", f"Token: `{addr}`")
    send_discord(discord_msg)

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
    kws = CONFIG["keywords"]
    hits = [kw for kw in kws if re.search(rf"\b{re.escape(kw)}\b", text, re.IGNORECASE)]
    if CONFIG["require_all_keywords"] and len(hits) != len(kws):
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
        best = max(pairs, key=lambda p: float((p.get("volume") or {}).get("h24") or 0))
        volume = float((best.get("volume") or {}).get("h24") or 0)
        raw_mcap = best.get("marketCap")
        market_cap = float(raw_mcap) if raw_mcap is not None else None
        created_at = best.get("pairCreatedAt")
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
    candidates = []
    queued = set()

    for endpoint in PROFILE_ENDPOINTS:
        try:
            profiles = fetch_profiles(endpoint)
        except requests.RequestException as e:
            print(f"[warn] fetch failed for {endpoint}: {e}", flush=True)
            continue

        for p in profiles:
            if p.get("chainId") not in CONFIG["chains"]:
                continue

            key = f"{p.get('chainId')}:{p.get('tokenAddress')}"
            if key in seen or key in queued:
                continue

            hits = matched_keywords(p.get("description", ""))
            STATUS["checked"] += 1
            if not hits:
                continue

            metrics = get_token_metrics(p.get("tokenAddress", ""))
            if metrics is None:
                continue
            volume, market_cap, created_at = metrics

            if volume < CONFIG["min_volume_usd"]:
                continue
            if CONFIG["min_market_cap_usd"] and (
                market_cap is None or market_cap < CONFIG["min_market_cap_usd"]
            ):
                continue
            if CONFIG["max_market_cap_usd"] and (
                market_cap is None or market_cap > CONFIG["max_market_cap_usd"]
            ):
                continue

            age_h = hours_since(created_at)
            if CONFIG["max_age_hours"] and (age_h is None or age_h > CONFIG["max_age_hours"]):
                continue

            sort_age = age_h if age_h is not None else float("inf")
            candidates.append((sort_age, key, p, hits, volume, market_cap, created_at))
            queued.add(key)

    candidates.sort(key=lambda c: c[0])  # youngest first
    for _, key, p, hits, volume, market_cap, created_at in candidates:
        alert(p, hits, volume, market_cap, created_at)
        seen.add(key)

    STATUS["last_scan"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def scanner_loop() -> None:
    seen = load_seen()
    STATUS["started"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"Scanner started. {format_settings()}", flush=True)
    while True:
        try:
            scan_once(seen)
            save_seen(seen)
        except Exception as e:
            print(f"[error] scan loop: {e}", flush=True)
        time.sleep(POLL_SECONDS)


# ------------------------ TELEGRAM COMMAND LISTENER ------------------------


def parse_amount(s: str) -> int:
    """Accept things like 15000, 15k, 1.5m, $40,000."""
    s = s.strip().lower().replace(",", "").replace("$", "").replace("_", "")
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    return int(float(s) * mult)


def format_settings() -> str:
    c = CONFIG
    age = f"{c['max_age_hours']}h" if c["max_age_hours"] else "off"
    minmc = f"${c['min_market_cap_usd']:,}" if c["min_market_cap_usd"] else "off"
    maxmc = f"${c['max_market_cap_usd']:,}" if c["max_market_cap_usd"] else "off"
    return (
        "Current criteria:\n"
        f"- Chains: {', '.join(CONFIG['chains'])}\n"
        f"- Keywords: {', '.join(c['keywords'])} "
        f"({'ALL' if c['require_all_keywords'] else 'ANY'})\n"
        f"- Min 24h volume: ${c['min_volume_usd']:,}\n"
        f"- Market cap: {minmc} to {maxmc}\n"
        f"- Max age filter: {age}\n"
        f"- NEW LAUNCH tag under: {c['new_launch_hours']}h"
    )


HELP_TEXT = (
    "🤖 DexScreener Scanner — what you can do\n"
    "Send any command below to change the bot live. No code or redeploy needed.\n"
    "Values accept 15000, 15k, 1.5m, or $40,000.\n"
    "\n"
    "📊 VIEW\n"
    "/status - show all current criteria\n"
    "/help - this message\n"
    "\n"
    "💧 VOLUME & MARKET CAP\n"
    "/minvol 15k - minimum 24h volume to alert\n"
    "/minmcap 10k - market-cap floor (0 = off)\n"
    "/maxmcap 5m - market-cap ceiling (0 = off)\n"
    "\n"
    "🆕 AGE / NEW LAUNCHES\n"
    "/maxage 72 - only alert tokens younger than N hours (0 = off)\n"
    "/newlaunch 24 - tag tokens younger than N hours as NEW LAUNCH\n"
    "\n"
    "🔤 KEYWORDS (matched in the token summary)\n"
    "/keywords first,new - set the words to look for\n"
    "/requireall on - need ALL keywords (on) or ANY one (off)\n"
    "\n"
    "⛓️ CHAINS\n"
    "/chains - show active chains\n"
    "/chains add tron - add chain(s)\n"
    "/chains remove ton - remove chain(s)\n"
    "/chains set eth,sol,base - replace the whole list\n"
    "Names like eth, bnb, arb, gram are understood automatically.\n"
    "\n"
    "ℹ️ Notes\n"
    "- A token must pass EVERY active filter to alert.\n"
    "- Each token alerts once; tap the address to copy it.\n"
    "- On Render's free tier, a redeploy resets these to defaults."
)


def handle_command(text: str) -> str:
    parts = text.split()
    cmd = parts[0].lower().lstrip("/").split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""
    rest = " ".join(parts[1:])

    try:
        if cmd in ("status", "settings", "start"):
            return format_settings()
        if cmd == "help":
            return HELP_TEXT
        if cmd == "minvol":
            CONFIG["min_volume_usd"] = parse_amount(arg)
        elif cmd == "minmcap":
            CONFIG["min_market_cap_usd"] = parse_amount(arg)
        elif cmd == "maxmcap":
            CONFIG["max_market_cap_usd"] = parse_amount(arg)
        elif cmd == "maxage":
            CONFIG["max_age_hours"] = int(float(arg))
        elif cmd == "newlaunch":
            CONFIG["new_launch_hours"] = int(float(arg))
        elif cmd == "keywords":
            kws = [k.strip() for k in rest.replace(",", " ").split() if k.strip()]
            if not kws:
                return "Give at least one keyword, e.g. /keywords first,new"
            CONFIG["keywords"] = kws
        elif cmd == "chains":
            tokens = parts[1:]
            if not tokens:
                return format_settings() + "\n\nChange with: /chains add tron | /chains remove ton | /chains set eth,sol,base"
            sub = tokens[0].lower()
            if sub in ("add", "remove", "set"):
                items = [normalize_chain(t) for t in " ".join(tokens[1:]).replace(",", " ").split() if t.strip()]
            else:
                sub, items = "set", [normalize_chain(t) for t in " ".join(tokens).replace(",", " ").split() if t.strip()]
            if not items:
                return "Give at least one chain, e.g. /chains add tron"
            cur = list(CONFIG["chains"])
            if sub == "set":
                cur = items
            elif sub == "add":
                cur += [c for c in items if c not in cur]
            elif sub == "remove":
                cur = [c for c in cur if c not in items]
            if not cur:
                return "At least one chain must stay active."
            CONFIG["chains"] = cur
        elif cmd == "requireall":
            CONFIG["require_all_keywords"] = arg.lower() in ("on", "true", "yes", "1")
        else:
            return f"Unknown command. {HELP_TEXT}"
    except (ValueError, IndexError):
        return f"Couldn't read that value. {HELP_TEXT}"

    save_config()
    return "Updated.\n\n" + format_settings()


def command_loop() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed = os.getenv("TELEGRAM_CHAT_ID")
    if not token:
        print("[info] no TELEGRAM_BOT_TOKEN; command listener off", flush=True)
        return
    print("[info] Telegram command listener active. Send /help in the chat.", flush=True)
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=40
            )
            resp.raise_for_status()
            for upd in resp.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat_id = str(msg.get("chat", {}).get("id"))
                text = (msg.get("text") or "").strip()
                if allowed and chat_id != str(allowed):
                    continue  # only obey the configured chat
                if text.startswith("/"):
                    send_telegram(handle_command(text))
        except requests.RequestException as e:
            print(f"[warn] command poll failed: {e}", flush=True)
            time.sleep(5)


# ----------------------------- WEB SERVER ----------------------------------


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "alive", "config": CONFIG, **STATUS}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        # UptimeRobot (and some monitors) send HEAD, not GET. Answer 200.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

    def log_message(self, *args):
        pass


def main() -> None:
    load_config()
    threading.Thread(target=scanner_loop, daemon=True).start()
    threading.Thread(target=command_loop, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Web server listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
