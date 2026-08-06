"""
new_scanner.py - Finds newly-listed/trending Solana tokens for the /new
command, and pump.fun-style bonding-curve "countdown to graduation" via
SolanaTracker's data API.

IMPORTANT - honesty about reliability:
  - The DexScreener boosted-listings endpoint is documented/stable - the
    primary path below.
  - SolanaTracker's exact endpoint paths/response shape below are based on
    their dashboard description (public Data API, no key required for
    basic access) rather than a verified live response, since this
    environment can't reach their API to confirm. Wrapped so a schema
    mismatch just means "no countdown shown," not a crash. If it comes
    back empty/wrong, send me one real response JSON and I'll fix the
    field mapping in minutes.
"""

import requests

DEXSCREENER_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
SOLANATRACKER_GRADUATING_URL = "https://data.solanatracker.io/tokens/graduating"

# Historically pump.fun tokens "graduate" to Raydium around ~$69k market cap.
# This threshold can and does drift - treat the countdown % as an estimate.
GRADUATION_MCAP_USD = 69000


def get_boosted_solana_tokens(limit: int = 10) -> dict:
    """Recently boosted (paid-promoted) Solana tokens from DexScreener."""
    try:
        resp = requests.get(DEXSCREENER_BOOSTS_URL, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        items = data if isinstance(data, list) else data.get("boosts", [])
        solana_items = [i for i in items if i.get("chainId") == "solana"][:limit]
        return {
            "ok": True,
            "tokens": [
                {
                    "mint": i.get("tokenAddress"),
                    "description": i.get("description", ""),
                    "url": i.get("url"),
                    "icon": i.get("icon"),
                }
                for i in solana_items
            ],
        }
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": str(e), "tokens": []}


def get_graduating_coins(api_key: str = None, limit: int = 10) -> dict:
    """
    Best-effort pull of pump.fun-style coins approaching bonding-curve
    graduation, via SolanaTracker. UNVERIFIED live - see module docstring.
    """
    headers = {"x-api-key": api_key} if api_key else {}
    try:
        resp = requests.get(SOLANATRACKER_GRADUATING_URL, headers=headers, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        coins = data if isinstance(data, list) else data.get("tokens", data.get("data", []))

        results = []
        for c in coins[:limit]:
            try:
                mcap = float(c.get("marketCapUsd", 0) or c.get("usd_market_cap", 0) or 0)
                progress_pct = min(100.0, (mcap / GRADUATION_MCAP_USD) * 100) if mcap else 0.0

                age_minutes = None
                created_ms = c.get("createdAt") or c.get("created_timestamp") or c.get("createdTimestamp")
                if created_ms:
                    import time
                    # Handle both seconds and milliseconds timestamps
                    created_ms = float(created_ms)
                    if created_ms < 10**12:  # looks like seconds, not ms
                        created_ms *= 1000
                    age_minutes = max(0, int((time.time() * 1000 - created_ms) / 60000))

                results.append({
                    "mint": c.get("mint") or c.get("address"),
                    "symbol": c.get("symbol", "?"),
                    "name": c.get("name", "?"),
                    "mcap_usd": mcap,
                    "graduation_progress_pct": progress_pct,
                    "age_minutes": age_minutes,
                })
            except (TypeError, ValueError):
                continue

        return {"ok": True, "tokens": results}
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": str(e), "tokens": []}


SOLANATRACKER_LATEST_URL = "https://data.solanatracker.io/tokens/latest"

# Escalating time windows for the dynamic fallback - if the strict window
# comes back empty (market lull, not a bug), automatically widen instead
# of just saying "nothing found." Always reports which window it actually
# used, so the result stays honest rather than silently stale.
NEW_TOKEN_FALLBACK_WINDOWS_MINUTES = [3, 10, 30, 60]


def get_latest_new_tokens(api_key: str, limit: int = 10) -> dict:
    """
    Genuinely NEW tokens (recent mints), NOT boosted/promoted listings and
    NOT graduating tokens - those are different things DexScreener's boost
    endpoint and SolanaTracker's graduating endpoint already cover.
    Applies automatic window-widening: tries the strictest window first,
    widens if empty, and reports which window actually produced results.
    """
    headers = {"x-api-key": api_key} if api_key else {}
    try:
        resp = requests.get(SOLANATRACKER_LATEST_URL, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        tokens_raw = data if isinstance(data, list) else data.get("tokens", data.get("data", []))
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": str(e), "tokens": [], "window_used_minutes": None}

    import time as _time
    now_ms = _time.time() * 1000

    parsed = []
    for t in tokens_raw:
        try:
            created_ms = t.get("createdAt") or t.get("created_time") or t.get("created_timestamp")
            if created_ms:
                created_ms = float(created_ms)
                if created_ms < 10**12:  # looks like seconds, not ms
                    created_ms *= 1000
                age_minutes = (now_ms - created_ms) / 60000
            else:
                age_minutes = None

            parsed.append({
                "mint": t.get("mint") or t.get("address"),
                "symbol": t.get("symbol", "?"),
                "name": t.get("name", "?"),
                "age_minutes": age_minutes,
            })
        except (TypeError, ValueError):
            continue

    for window in NEW_TOKEN_FALLBACK_WINDOWS_MINUTES:
        within_window = [t for t in parsed if t["age_minutes"] is not None and t["age_minutes"] <= window]
        if within_window:
            return {
                "ok": True,
                "tokens": within_window[:limit],
                "window_used_minutes": window,
                "note": None if window == NEW_TOKEN_FALLBACK_WINDOWS_MINUTES[0]
                        else f"Nothing in the strictest window - widened to last {window} min.",
            }

    return {
        "ok": True,
        "tokens": parsed[:limit],
        "window_used_minutes": None,
        "note": "No tokens found within any time window - showing latest available regardless of age.",
    }
