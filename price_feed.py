"""
price_feed.py - Pulls live price/liquidity data from DexScreener's public API.
"""

import requests

DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
SOL_MINT = "So11111111111111111111111111111111111111112"


def get_token_info(mint: str) -> dict:
    """
    Returns the best (highest-liquidity) pair for a given mint.
    { ok, price_usd, liquidity_usd, volume_24h_usd, mcap_usd, pair_url,
      symbol, name, age_minutes, is_dead, twitter_url, telegram_url,
      website_url, is_boosted } or { ok: False, error }
    """
    try:
        resp = requests.get(DEXSCREENER_TOKENS_URL.format(mint), timeout=8)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return {"ok": False, "error": "No trading pairs found for this token."}

        best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))

        age_minutes = None
        created_at = best.get("pairCreatedAt")
        if created_at:
            import time
            age_minutes = max(0, int((time.time() * 1000 - created_at) / 60000))

        base = best.get("baseToken") or {}
        liquidity_usd = float((best.get("liquidity") or {}).get("usd", 0) or 0)
        volume_24h_usd = float((best.get("volume") or {}).get("h24", 0) or 0)

        # A "dead" coin: technically tradeable, but nobody actually is.
        # Separate from honeypot/contract safety - this is an activity check.
        is_dead = liquidity_usd < 1000 or volume_24h_usd < 500

        # Social links, if DexScreener has them indexed for this pair
        info_block = best.get("info") or {}
        socials = info_block.get("socials") or []
        websites = info_block.get("websites") or []
        twitter_url = next((s.get("url") for s in socials if s.get("type") == "twitter"), None)
        telegram_url = next((s.get("url") for s in socials if s.get("type") == "telegram"), None)
        website_url = websites[0].get("url") if websites else None

        # "Dex paid" / boosted listing - best effort, field name varies by
        # DexScreener API version, so don't let a schema change crash the card.
        is_boosted = bool(best.get("boosts", {}).get("active", 0)) if isinstance(best.get("boosts"), dict) else False

        # Buy/sell pressure - tells us when sentiment shifted
        txns = best.get("txns") or {}
        h1_txns = txns.get("h1") or {}
        buys_h1 = int(h1_txns.get("buys", 0) or 0)
        sells_h1 = int(h1_txns.get("sells", 0) or 0)

        return {
            "ok": True,
            "symbol": base.get("symbol", "?"),
            "name": base.get("name", "?"),
            "price_usd": float(best.get("priceUsd", 0) or 0),
            "liquidity_usd": liquidity_usd,
            "volume_24h_usd": volume_24h_usd,
            "mcap_usd": float(best.get("marketCap", 0) or best.get("fdv", 0) or 0),
            "pair_url": best.get("url"),
            "age_minutes": age_minutes,
            "is_dead": is_dead,
            "twitter_url": twitter_url,
            "telegram_url": telegram_url,
            "website_url": website_url,
            "is_boosted": is_boosted,
            "buys_h1": buys_h1,
            "sells_h1": sells_h1,
        }
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": str(e)}


def get_sol_usd_price() -> float:
    """Fetches the current SOL/USD price. Returns 0.0 on failure."""
    info = get_token_info(SOL_MINT)
    if info["ok"]:
        return info["price_usd"]
    return 0.0
