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
  TELEGRAM_BOT_TOKEN     -> your bot token from @BotFather
  TELEGRAM_CHAT_ID       -> where alerts are POSTED (a channel/group to share a
                            feed with friends, or your own chat for solo use)
  TELEGRAM_ADMIN_CHAT_ID -> optional; the only chat allowed to send /commands.
                            If unset, falls back to TELEGRAM_CHAT_ID.
  DISCORD_WEBHOOK_URL    -> posts to a Discord channel (optional)
  PORT                   -> set automatically by Render
"""

import html
import json
import os
import re
import threading
import time
from urllib.parse import urlparse
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
    "learn_enabled": False,           # log summaries to discover good keywords
}
CONFIG = dict(DEFAULTS)

# Learning mode: harvest summaries of tokens the bot sees (capped per cycle to
# respect rate limits) so /topwords can reveal common words among high-cap ones.
LEARN_LOG_FILE = "observed.json"
LEARN_LOOKUPS_PER_CYCLE = 12
OBSERVED = {}  # address -> {"chain":..., "mcap":..., "desc":...}
STOPWORDS = set(
    "the a an and or of to in is for with on we our you your this that it its are "
    "be as at by from has have had will can could would should not no all any "
    "more most other into than then they them their there here was were been being "
    "but if so up out over under again once only own same too very s t just don now "
    "token coin crypto project community holders supply total market cap chain".split()
)


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


def send_telegram(text: str, chat_id: str = None, parse_mode: str = None) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    target = chat_id or os.getenv("TELEGRAM_CHAT_ID")  # default: broadcast feed
    if not (token and target):
        return
    payload = {"chat_id": target, "text": text, "disable_web_page_preview": False}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10
        )
    except requests.RequestException as e:
        print(f"[warn] telegram send failed: {e}", flush=True)


def send_telegram_photo(photo_url: str, caption: str, chat_id: str = None, parse_mode: str = None) -> bool:
    """Send the banner image with the alert as its caption. Returns True on success."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    target = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not (token and target and photo_url):
        return False
    payload = {"chat_id": target, "photo": photo_url, "caption": caption[:1024]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto", json=payload, timeout=15
        )
        return bool(r.ok and r.json().get("ok"))
    except (requests.RequestException, ValueError):
        return False


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


EXPLORERS = {
    "ethereum": "https://etherscan.io/token/{a}",
    "solana": "https://solscan.io/token/{a}",
    "base": "https://basescan.org/token/{a}",
    "bsc": "https://bscscan.com/token/{a}",
    "tron": "https://tronscan.org/#/token20/{a}",
    "sui": "https://suiscan.xyz/mainnet/coin/{a}",
    "ton": "https://tonviewer.com/{a}",
}


def explorer_url(chain, addr):
    tmpl = EXPLORERS.get(chain)
    return tmpl.format(a=addr) if tmpl else None


TYPE_NAMES = {
    "twitter": "X", "x": "X", "telegram": "Telegram", "discord": "Discord",
    "youtube": "YouTube", "instagram": "Instagram", "reddit": "Reddit",
    "tiktok": "TikTok", "github": "GitHub", "medium": "Medium",
    "facebook": "Facebook", "linkedin": "LinkedIn", "website": "Website",
}
DOMAIN_NAMES = [
    ("x.com", "X"), ("twitter.com", "X"), ("t.co", "X"),
    ("t.me", "Telegram"), ("telegram.me", "Telegram"), ("telegram.org", "Telegram"),
    ("discord.gg", "Discord"), ("discord.com", "Discord"),
    ("youtube.com", "YouTube"), ("youtu.be", "YouTube"),
    ("instagram.com", "Instagram"), ("reddit.com", "Reddit"),
    ("tiktok.com", "TikTok"), ("github.com", "GitHub"), ("medium.com", "Medium"),
    ("facebook.com", "Facebook"), ("fb.com", "Facebook"), ("linkedin.com", "LinkedIn"),
]


def link_name(link, url):
    kind = (link.get("type") or "").lower()
    if kind in TYPE_NAMES:
        return TYPE_NAMES[kind]
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    for dom, name in DOMAIN_NAMES:
        if host == dom or host.endswith("." + dom):
            return name
    label = (link.get("label") or "").strip()
    if label:
        return label
    if kind:
        return kind.capitalize()
    return "Link"


def social_lines(profile):
    """Return every link a token lists, labeled by platform, deduped + ordered."""
    out, seen = [], set()
    for link in profile.get("links") or []:
        u = (link.get("url") or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append((link_name(link, u), u))
    rank = {"X": 0, "Telegram": 1, "Website": 9}
    out.sort(key=lambda item: rank.get(item[0], 5))  # X & Telegram first, Website last
    return out


def alert(profile: dict, matched: list, volume: float, market_cap, created_at_ms) -> None:
    chain = profile.get("chainId", "?")
    addr = profile.get("tokenAddress", "?")
    url = profile.get("url") or f"https://dexscreener.com/{chain}/{addr}"
    desc = (profile.get("description") or "").strip()[:300]
    mcap_str = f"${market_cap:,.0f}" if market_cap is not None else "n/a"

    age_h = hours_since(created_at_ms)
    is_new = age_h is not None and age_h <= CONFIG["new_launch_hours"]
    header = "\U0001F195 NEW LAUNCH" if is_new else "\U0001F6A8 Match"

    links = social_lines(profile)
    links_plain = "".join(f"{name}: {u}\n" for name, u in links)
    explorer = explorer_url(chain, addr)
    explorer_plain = f"Explorer: {explorer}\n" if explorer else ""

    plain = (
        f"{header} on {chain.upper()}\n"
        f"Keywords: {', '.join(matched)}\n"
        f"Age: {format_age(created_at_ms)}\n"
        f"24h Volume: ${volume:,.0f}\n"
        f"Market Cap: {mcap_str}\n"
        f"Token: {addr}\n"
        f"{explorer_plain}"
        f"Summary: {desc}\n"
        f"{links_plain}"
        f"{url}"
    )
    print("\n" + plain + "\n", flush=True)

    e = html.escape
    links_tg = "".join(f"{name}: {e(u)}\n" for name, u in links)
    explorer_tg = f"Explorer: {e(explorer)}\n" if explorer else ""
    telegram_msg = (
        f"{header} on {e(chain.upper())}\n"
        f"Keywords: {e(', '.join(matched))}\n"
        f"Age: {e(format_age(created_at_ms))}\n"
        f"24h Volume: ${volume:,.0f}\n"
        f"Market Cap: {e(mcap_str)}\n"
        f"Token (tap to copy): <code>{e(addr)}</code>\n"
        f"{explorer_tg}"
        f"Summary: {e(desc)}\n"
        f"{links_tg}"
        f"{e(url)}"
    )
    banner = (profile.get("header") or profile.get("icon") or "").strip()
    sent = False
    if banner:
        sent = send_telegram_photo(banner, telegram_msg, parse_mode="HTML")
    if not sent:
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


def load_observed() -> None:
    global OBSERVED
    try:
        with open(LEARN_LOG_FILE, "r", encoding="utf-8") as f:
            OBSERVED = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        OBSERVED = {}


def save_observed() -> None:
    try:
        # keep the file bounded: most recent ~3000 tokens
        if len(OBSERVED) > 3000:
            for k in list(OBSERVED.keys())[: len(OBSERVED) - 3000]:
                OBSERVED.pop(k, None)
        with open(LEARN_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(OBSERVED, f)
    except OSError as e:
        print(f"[warn] could not save observations: {e}", flush=True)


def record_observation(addr: str, chain: str, market_cap, desc: str) -> None:
    if not addr:
        return
    OBSERVED[addr] = {"chain": chain, "mcap": market_cap, "desc": (desc or "")[:500]}


def top_words(min_mcap: float, limit: int = 20):
    counts = {}
    samples = 0
    for rec in OBSERVED.values():
        mc = rec.get("mcap")
        if mc is None or mc < min_mcap:
            continue
        samples += 1
        seen_in_desc = set()
        for w in re.findall(r"[a-zA-Z]{3,}", rec.get("desc", "").lower()):
            if w in STOPWORDS or w in seen_in_desc:
                continue
            seen_in_desc.add(w)  # count each word once per token
            counts[w] = counts.get(w, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return samples, ranked


def scan_once(seen: set) -> None:
    candidates = []
    queued = set()
    learn_lookups = 0  # cap extra API calls used purely for learning

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
            addr = p.get("tokenAddress", "")
            desc = p.get("description", "")
            hits = matched_keywords(desc)
            STATUS["checked"] += 1

            learn_on = CONFIG.get("learn_enabled")
            is_match = bool(hits) and key not in seen and key not in queued
            want_learn = learn_on and addr not in OBSERVED and learn_lookups < LEARN_LOOKUPS_PER_CYCLE

            if not (is_match or want_learn):
                continue

            metrics = get_token_metrics(addr)
            if metrics is None:
                continue
            volume, market_cap, created_at = metrics

            # Learning: record what we saw (any token, any size) for /topwords.
            if learn_on:
                record_observation(addr, p.get("chainId"), market_cap, desc)
                learn_lookups += 1

            if not is_match:
                continue

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

    if CONFIG.get("learn_enabled"):
        save_observed()

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
    "🧠 LEARN (find good keywords from real tokens)\n"
    "/learn on - start logging summaries of tokens it sees (off by default)\n"
    "/topwords 50m - most common words among logged tokens above that market cap\n"
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
        elif cmd == "learn":
            CONFIG["learn_enabled"] = arg.lower() in ("on", "true", "yes", "1")
        elif cmd == "topwords":
            min_mcap = parse_amount(arg) if arg else 50_000_000
            samples, ranked = top_words(min_mcap)
            if not ranked:
                return (
                    f"No tokens logged at or above ${min_mcap:,.0f} yet.\n"
                    "Turn on /learn and let it run a while, then try again."
                )
            lines = ", ".join(f"{w} ({n})" for w, n in ranked)
            return (
                f"Most common words in {samples} tokens >= ${min_mcap:,.0f}:\n{lines}\n\n"
                "Set these with /keywords word1,word2"
            )
        else:
            return f"Unknown command. {HELP_TEXT}"
    except (ValueError, IndexError):
        return f"Couldn't read that value. {HELP_TEXT}"

    save_config()
    return "Updated.\n\n" + format_settings()


def command_loop() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    # Commands are obeyed only from the admin chat. If no separate admin chat is
    # set, fall back to the broadcast chat (single-user setup, original behaviour).
    admin = os.getenv("TELEGRAM_ADMIN_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
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
                msg = (
                    upd.get("message")
                    or upd.get("edited_message")
                    or upd.get("channel_post")
                )
                if not msg:
                    continue
                chat_id = str(msg.get("chat", {}).get("id"))
                text = (msg.get("text") or "").strip()
                if admin and chat_id != str(admin):
                    continue  # only the admin chat may change settings
                if text.startswith("/"):
                    send_telegram(handle_command(text), chat_id=chat_id)
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
    load_observed()
    threading.Thread(target=scanner_loop, daemon=True).start()
    threading.Thread(target=command_loop, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Web server listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
