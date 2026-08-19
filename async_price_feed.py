"""
async_price_feed.py - Lightning-fast async price fetching with batch support,
aggressive caching, and automatic fallback chains.

Fixes the "bot sometimes delays in fetching info" problem by:
  1. Concurrent aiohttp requests (no more blocking the event loop)
  2. Batch fetching up to 30 tokens in parallel
  3. In-memory LRU cache with sub-second TTL for hot tokens
  4. Fallback chain: DexScreener → Jupiter Price API → Helius
"""

import asyncio
import time
import aiohttp
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from collections import OrderedDict

import config

# --- Cache ---
@dataclass
class _CacheEntry:
    data: dict
    timestamp: float

class _LRUCache:
    def __init__(self, ttl_seconds: float = 6.0, maxsize: int = 500):
        self.ttl = ttl_seconds
        self.maxsize = maxsize
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[dict]:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.time() - entry.timestamp > self.ttl:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return entry.data

    async def set(self, key: str, value: dict):
        async with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = _CacheEntry(value, time.time())
            while len(self._store) > self.maxsize:
                self._store.popitem(last=False)

    async def invalidate(self, key: str):
        async with self._lock:
            self._store.pop(key, None)

_price_cache = _LRUCache(ttl_seconds=config.PRICE_CACHE_TTL_SECONDS, maxsize=500)


# --- Low-level fetchers ---

async def _fetch_dexscreener(session: aiohttp.ClientSession, mint: str) -> Optional[dict]:
    """Fast path: DexScreener public API."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            pairs = data.get("pairs") or []
            if not pairs:
                return None
            best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
            base = best.get("baseToken") or {}
            age_minutes = None
            created_at = best.get("pairCreatedAt")
            if created_at:
                age_minutes = max(0, int((time.time() * 1000 - created_at) / 60000))
            return {
                "ok": True,
                "source": "dexscreener",
                "symbol": base.get("symbol", "?"),
                "name": base.get("name", "?"),
                "price_usd": float(best.get("priceUsd", 0) or 0),
                "liquidity_usd": float((best.get("liquidity") or {}).get("usd", 0) or 0),
                "volume_24h_usd": float((best.get("volume") or {}).get("h24", 0) or 0),
                "mcap_usd": float(best.get("marketCap", 0) or best.get("fdv", 0) or 0),
                "fdv_usd": float(best.get("fdv", 0) or 0),
                "pair_url": best.get("url"),
                "age_minutes": age_minutes,
                "is_dead": float((best.get("liquidity") or {}).get("usd", 0) or 0) < 1000,
                "buys_h1": int((best.get("txns") or {}).get("h1", {}).get("buys", 0) or 0),
                "sells_h1": int((best.get("txns") or {}).get("h1", {}).get("sells", 0) or 0),
            }
    except Exception:
        return None


async def _fetch_jupiter_price(session: aiohttp.ClientSession, mint: str) -> Optional[dict]:
    """Fallback: Jupiter Price API v2 (free, fast)."""
    url = "https://api.jup.ag/price/v2"
    try:
        async with session.get(url, params={"ids": mint}, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            item = (data.get("data") or {}).get(mint)
            if not item:
                return None
            price = float(item.get("price", 0) or 0)
            if price <= 0:
                return None
            return {
                "ok": True,
                "source": "jupiter",
                "symbol": "?",
                "name": "?",
                "price_usd": price,
                "liquidity_usd": 0.0,
                "volume_24h_usd": 0.0,
                "mcap_usd": 0.0,
                "fdv_usd": 0.0,
                "pair_url": None,
                "age_minutes": None,
                "is_dead": False,
                "buys_h1": 0,
                "sells_h1": 0,
            }
    except Exception:
        return None


async def _fetch_helius_price(session: aiohttp.ClientSession, mint: str) -> Optional[dict]:
    """Last resort: Helius token metadata + price estimation."""
    try:
        rpc_url = config.SOLANA_RPC_URL
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAsset",
            "params": {"id": mint},
        }
        async with session.post(rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            result = data.get("result", {})
            token_info = result.get("token_info", {})
            price = float(token_info.get("price_info", {}).get("price_per_token", 0) or 0)
            if price <= 0:
                return None
            return {
                "ok": True,
                "source": "helius",
                "symbol": token_info.get("symbol", "?"),
                "name": token_info.get("name", "?"),
                "price_usd": price,
                "liquidity_usd": 0.0,
                "volume_24h_usd": 0.0,
                "mcap_usd": 0.0,
                "fdv_usd": 0.0,
                "pair_url": None,
                "age_minutes": None,
                "is_dead": False,
                "buys_h1": 0,
                "sells_h1": 0,
            }
    except Exception:
        return None


# --- Public API ---

_session: Optional[aiohttp.ClientSession] = None

async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=50, limit_per_host=20),
            timeout=aiohttp.ClientTimeout(total=10),
        )
    return _session


async def get_token_info_async(mint: str, use_cache: bool = True) -> dict:
    """
    Async, cached, multi-source price fetch.
    Returns the same shape as price_feed.get_token_info() for drop-in compat.
    """
    if use_cache:
        cached = await _price_cache.get(mint)
        if cached is not None:
            return cached

    session = await _get_session()

    # Try DexScreener first (richest data)
    result = await _fetch_dexscreener(session, mint)
    if result is None:
        result = await _fetch_jupiter_price(session, mint)
    if result is None:
        result = await _fetch_helius_price(session, mint)

    if result is None:
        return {"ok": False, "error": "All price sources failed"}

    if use_cache:
        await _price_cache.set(mint, result)
    return result


async def batch_get_token_info(mints: List[str], use_cache: bool = True) -> Dict[str, dict]:
    """
    Fetch prices for up to 30 tokens concurrently.
    Returns {mint: info_dict}.
    """
    results = {}
    to_fetch = []

    if use_cache:
        for m in mints:
            cached = await _price_cache.get(m)
            if cached is not None:
                results[m] = cached
            else:
                to_fetch.append(m)
    else:
        to_fetch = mints

    if not to_fetch:
        return results

    # Limit concurrency to be polite to APIs
    semaphore = asyncio.Semaphore(15)

    async def _fetch_one(mint: str) -> Tuple[str, dict]:
        async with semaphore:
            return mint, await get_token_info_async(mint, use_cache=False)

    tasks = [asyncio.create_task(_fetch_one(m)) for m in to_fetch]
    for task in asyncio.as_completed(tasks):
        mint, info = await task
        results[mint] = info
        if info.get("ok") and use_cache:
            await _price_cache.set(mint, info)

    return results


async def get_sol_usd_price_async() -> float:
    """Fast async SOL price."""
    info = await get_token_info_async("So11111111111111111111111111111111111111112")
    return info.get("price_usd", 0.0) if info.get("ok") else 0.0


async def invalidate_cache(mint: str):
    await _price_cache.invalidate(mint)


async def close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None
