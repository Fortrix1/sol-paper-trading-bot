"""
position_tracker.py - Live holdings tracker + whale buy/sell detection.

FEATURES:
  - Tracks who buys/sells your held tokens and how much
  - Estimates if whale activity is bullish or bearish
  - Real-time position updates with full details
  - Fast async fetching for minimal delay
  - Shows exact token amounts, USD values, wallet addresses
"""

import json
import time
import asyncio
import aiohttp
import requests
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from collections import defaultdict

import config
from price_feed import get_token_info, get_sol_usd_price
from phantom_connector import phantom


@dataclass
class WhaleActivity:
    wallet: str
    action: str  # "BUY" or "SELL"
    amount_tokens: float
    amount_usd: float
    timestamp: float
    tx_signature: Optional[str] = None
    is_dev: bool = False
    is_known_whale: bool = False


@dataclass
class PositionSnapshot:
    mint: str
    symbol: str
    entry_price_usd: float
    current_price_usd: float
    tokens_held: float
    invested_usd: float
    current_value_usd: float
    pnl_usd: float
    pnl_percent: float
    peak_price_usd: float
    peak_gain_pct: float
    change_pct: float
    age_seconds: float
    whale_activities: List[WhaleActivity]
    top_holders: List[Dict]
    timestamp: float


class PositionTracker:
    """
    Tracks live positions with whale activity monitoring.
    """

    def __init__(self):
        self.price_cache: Dict[str, Tuple[float, float]] = {}  # mint -> (price, timestamp)
        self.safety_cache: Dict[str, Tuple[dict, float]] = {}  # mint -> (safety_data, timestamp)
        self.whale_history: Dict[str, List[WhaleActivity]] = defaultdict(list)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    def _get_cached_price(self, mint: str) -> Optional[float]:
        if mint in self.price_cache:
            price, ts = self.price_cache[mint]
            if time.time() - ts < config.PRICE_CACHE_TTL_SECONDS:
                return price
        return None

    def _set_cached_price(self, mint: str, price: float):
        self.price_cache[mint] = (price, time.time())

    async def fetch_price_fast(self, mint: str) -> Optional[float]:
        """Async price fetch with caching."""
        cached = self._get_cached_price(mint)
        if cached is not None:
            return cached

        try:
            session = await self._get_session()
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                pairs = data.get("pairs") or []
                if not pairs:
                    return None
                best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
                price = float(best.get("priceUsd", 0) or 0)
                self._set_cached_price(mint, price)
                return price
        except Exception as e:
            print(f"Fast price fetch failed for {mint}: {e}")
            return None

    async def fetch_token_transfers(self, mint: str, limit: int = 50) -> List[Dict]:
        """
        Fetch recent token transfers for a mint via Helius.
        Returns list of transfer events with buyer/seller info.
        """
        try:
            # Use Helius API to get recent token transfers
            url = f"https://api.helius.xyz/v0/addresses/{mint}/transactions"
            params = {"api-key": config.HELIUS_API_KEY, "limit": limit}

            session = await self._get_session()
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return []
                txns = await resp.json()
                if not isinstance(txns, list):
                    return []

                transfers = []
                for tx in txns:
                    token_transfers = tx.get("tokenTransfers", [])
                    for transfer in token_transfers:
                        if transfer.get("mint") != mint:
                            continue

                        amount = transfer.get("tokenAmount", 0)
                        from_addr = transfer.get("fromUserAccount", "unknown")
                        to_addr = transfer.get("toUserAccount", "unknown")

                        # Determine if buy or sell
                        # If from is a known DEX/liquidity pool, it's a buy
                        # If to is a known DEX/liquidity pool, it's a sell
                        action = "TRANSFER"
                        if self._is_liquidity_pool(from_addr):
                            action = "BUY"
                        elif self._is_liquidity_pool(to_addr):
                            action = "SELL"

                        transfers.append({
                            "wallet": to_addr if action == "BUY" else from_addr,
                            "action": action,
                            "amount": amount,
                            "timestamp": tx.get("timestamp", time.time()),
                            "signature": tx.get("signature"),
                            "from": from_addr,
                            "to": to_addr,
                        })

                return transfers
        except Exception as e:
            print(f"Transfer fetch failed for {mint}: {e}")
            return []

    def _is_liquidity_pool(self, address: str) -> bool:
        """Best-effort check if address is a known DEX pool."""
        # Common DEX program addresses / pool markers
        known_pools = [
            "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
            "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM
            "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun
            "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",  # Migration wrapper
        ]
        return address in known_pools

    def detect_whale_activity(self, mint: str, transfers: List[Dict], current_price: float) -> List[WhaleActivity]:
        """
        Analyzes transfers to find whale buys/sells.
        Returns list of WhaleActivity objects.
        """
        activities = []
        sol_price = get_sol_usd_price()

        for t in transfers:
            if t["action"] not in ("BUY", "SELL"):
                continue

            amount_tokens = float(t["amount"])
            amount_usd = amount_tokens * current_price

            # Whale threshold check
            if amount_usd < config.WHALE_ALERT_MIN_USD:
                continue

            # Check if this wallet is the dev
            from conviction_engine import ConvictionEngine
            engine = ConvictionEngine()
            # We'd need to check creator, but for now use a simple heuristic

            activity = WhaleActivity(
                wallet=t["wallet"],
                action=t["action"],
                amount_tokens=amount_tokens,
                amount_usd=amount_usd,
                timestamp=t["timestamp"],
                tx_signature=t.get("signature"),
                is_dev=False,  # Will be set by caller if known
                is_known_whale=False,
            )
            activities.append(activity)

        # Sort by USD amount (biggest first)
        activities.sort(key=lambda a: a.amount_usd, reverse=True)
        return activities[:10]  # Top 10 whale activities

    def analyze_whale_sentiment(self, activities: List[WhaleActivity]) -> Dict:
        """
        Analyzes whale activity to determine bullish/bearish sentiment.
        Returns {sentiment, total_buy_usd, total_sell_usd, net_flow_usd, count_buy, count_sell}
        """
        total_buy = sum(a.amount_usd for a in activities if a.action == "BUY")
        total_sell = sum(a.amount_usd for a in activities if a.action == "SELL")
        count_buy = sum(1 for a in activities if a.action == "BUY")
        count_sell = sum(1 for a in activities if a.action == "SELL")
        net_flow = total_buy - total_sell

        if net_flow > total_buy * 0.3:
            sentiment = "🟢 BULLISH"
            emoji = "🚀"
        elif net_flow < -total_sell * 0.3:
            sentiment = "🔴 BEARISH"
            emoji = "📉"
        else:
            sentiment = "🟡 NEUTRAL"
            emoji = "⚖️"

        return {
            "sentiment": sentiment,
            "emoji": emoji,
            "total_buy_usd": total_buy,
            "total_sell_usd": total_sell,
            "net_flow_usd": net_flow,
            "count_buy": count_buy,
            "count_sell": count_sell,
            "avg_buy_usd": total_buy / count_buy if count_buy > 0 else 0,
            "avg_sell_usd": total_sell / count_sell if count_sell > 0 else 0,
        }

    async def get_position_snapshot(self, mint: str, position_data: dict) -> Optional[PositionSnapshot]:
        """
        Builds a full position snapshot with live data.
        position_data comes from real_trader.positions or paper_wallet.
        """
        current_price = await self.fetch_price_fast(mint)
        if current_price is None:
            # Fallback to sync
            info = get_token_info(mint)
            if not info.get("ok"):
                return None
            current_price = info["price_usd"]

        entry = position_data.get("entry_price_usd", 0)
        tokens = position_data.get("tokens_held", 0)
        sol_spent = position_data.get("sol_spent", 0)
        sol_price = get_sol_usd_price()

        invested_usd = sol_spent * sol_price if sol_price > 0 else 0
        current_value_usd = tokens * current_price
        pnl_usd = current_value_usd - invested_usd
        pnl_percent = (pnl_usd / invested_usd * 100) if invested_usd > 0 else 0
        change_pct = ((current_price - entry) / entry * 100) if entry > 0 else 0

        peak = position_data.get("peak_price_usd", entry)
        peak_gain = ((peak - entry) / entry * 100) if entry > 0 else 0
        age = time.time() - position_data.get("timestamp", time.time())

        # Fetch whale activity
        transfers = await self.fetch_token_transfers(mint, limit=30)
        whale_activities = self.detect_whale_activity(mint, transfers, current_price)

        # Get top holders info
        from honeypot_check import HoneypotChecker
        checker = HoneypotChecker(
            api_endpoint=f"https://api.rugcheck.xyz/v1/tokens/{mint}/report",
            api_key=config.RUGCHECK_API_KEY,
        )
        safety = checker.check_token_safety(mint)
        top_holders = safety.get("top_holders_list", [])[:config.TRACK_TOP_HOLDERS_COUNT]

        return PositionSnapshot(
            mint=mint,
            symbol=position_data.get("symbol", "?"),
            entry_price_usd=entry,
            current_price_usd=current_price,
            tokens_held=tokens,
            invested_usd=invested_usd,
            current_value_usd=current_value_usd,
            pnl_usd=pnl_usd,
            pnl_percent=pnl_percent,
            peak_price_usd=peak,
            peak_gain_pct=peak_gain,
            change_pct=change_pct,
            age_seconds=age,
            whale_activities=whale_activities,
            top_holders=top_holders,
            timestamp=time.time(),
        )

    def format_position_update(self, snapshot: PositionSnapshot, is_real: bool = False) -> str:
        """Formats a position snapshot for Telegram with full details."""
        mode = "🔴 REAL" if is_real else "🔵 PAPER"
        arrow = "🟢" if snapshot.change_pct >= 0 else "🔴"
        age_str = f"{int(snapshot.age_seconds // 60)}m" if snapshot.age_seconds < 3600 else f"{int(snapshot.age_seconds // 3600)}h"

        lines = [
            f"{mode} *{snapshot.symbol}*",
            f"",
            f"{arrow} *P&L: {snapshot.pnl_percent:+.2f}%* (`{snapshot.pnl_usd:+.2f} USD`)",
            f"",
            f"*— Price —*",
            f"Entry: `${snapshot.entry_price_usd:.8f}`",
            f"Current: `${snapshot.current_price_usd:.8f}`",
            f"Peak: `${snapshot.peak_price_usd:.8f}` (`{snapshot.peak_gain_pct:+.1f}%`)",
            f"Change: `{snapshot.change_pct:+.2f}%`",
            f"",
            f"*— Holdings —*",
            f"Tokens: `{snapshot.tokens_held:,.2f}`",
            f"Invested: `${snapshot.invested_usd:.2f}`",
            f"Value now: `${snapshot.current_value_usd:.2f}`",
            f"",
        ]

        # Auto exit status
        if snapshot.change_pct >= config.TAKE_PROFIT_PERCENT:
            lines.append(f"🔔 *AUTO TP TRIGGERED* (+{config.TAKE_PROFIT_PERCENT:.0f}%)")
        elif snapshot.change_pct <= config.STOP_LOSS_PERCENT:
            lines.append(f"🔔 *AUTO SL TRIGGERED* ({config.STOP_LOSS_PERCENT:.0f}%)")
        else:
            lines.append(f"⏳ Hold — TP: +{config.TAKE_PROFIT_PERCENT:.0f}% | SL: {config.STOP_LOSS_PERCENT:.0f}%")

        lines.append("")

        # Whale activity section
        if snapshot.whale_activities:
            sentiment = self.analyze_whale_sentiment(snapshot.whale_activities)
            lines.append(f"*— Whale Activity ({sentiment['emoji']} {sentiment['sentiment']}) —*")
            lines.append(f"Net flow: `${sentiment['net_flow_usd']:+.0f}` (Buy: ${sentiment['total_buy_usd']:,.0f} | Sell: ${sentiment['total_sell_usd']:,.0f})")
            lines.append("")

            for i, w in enumerate(snapshot.whale_activities[:5], 1):
                action_emoji = "🟢" if w.action == "BUY" else "🔴"
                short_wallet = f"{w.wallet[:6]}...{w.wallet[-4:]}"
                lines.append(f"{i}. {action_emoji} `{short_wallet}` — `{w.amount_tokens:,.0f}` tokens (`${w.amount_usd:,.0f}`)")
            lines.append("")
        else:
            lines.append("*— Whale Activity —*")
            lines.append("No significant whale moves detected recently.")
            lines.append("")

        # Top holders
        if snapshot.top_holders:
            lines.append(f"*— Top Holders —*")
            for h in snapshot.top_holders[:5]:
                addr = h.get("address") or "unknown"
                short = f"{addr[:6]}...{addr[-4:]}"
                pct = h.get("pct", 0)
                lines.append(f"`{short}` — `{pct:.1f}%`")
            lines.append("")

        lines.append(f"_Updated: {age_str} ago_")
        return "
".join(lines)

    def format_whale_alert(self, activity: WhaleActivity, symbol: str, current_price: float) -> str:
        """Formats a single whale activity alert."""
        action_emoji = "🟢🐋" if activity.action == "BUY" else "🔴🐋"
        dev_tag = " 👤DEV" if activity.is_dev else ""

        return (
            f"{action_emoji} *WHALE {activity.action} — {symbol}*{dev_tag}
"
            f"
"
            f"Wallet: `{activity.wallet[:6]}...{activity.wallet[-4:]}`
"
            f"Amount: `{activity.amount_tokens:,.0f}` tokens
"
            f"Value: `${activity.amount_usd:,.0f}`
"
            f"Price: `${current_price:.8f}`
"
            f"
"
            f"_This may affect price. Monitor closely._"
        )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# Singleton
tracker = PositionTracker()
