"""
trade_estimator.py - AI-style market estimator that answers:
  "Should I buy, hold, or sell this token RIGHT NOW?"

Analyzes:
  - Price momentum (1h, 5min trends)
  - Whale flow (buy pressure vs sell pressure)
  - Liquidity depth (can you actually exit?)
  - Bonding curve stage (pre-graduation = bullish)
  - Social signals (Twitter/Telegram/Website presence)
  - Dev reputation (smart money vs serial rugger)
  - Holder concentration (top 10 %)
  - Recent volume trend

Returns a Signal object with:
  - verdict: STRONG_BUY / BUY / HOLD / SELL / STRONG_SELL / AVOID
  - confidence: 0-100
  - reasoning: list of human-readable bullet points
  - risk_level: LOW / MEDIUM / HIGH / EXTREME
  - suggested_size_sol: how much SOL to risk (0 = don't touch)
  - time_horizon: "minutes", "hours", "days"
"""

import time
from typing import List, Optional
from dataclasses import dataclass

import config
from conviction_engine import ConvictionEngine
from async_price_feed import get_token_info_async, get_sol_usd_price_async
from whale_labeler import labeler
from honeypot_check import HoneypotChecker


@dataclass
class TradeSignal:
    mint: str
    symbol: str
    verdict: str           # STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL, AVOID
    confidence: int        # 0-100
    reasoning: List[str]
    risk_level: str        # LOW, MEDIUM, HIGH, EXTREME
    suggested_size_sol: float
    time_horizon: str      # minutes, hours, days
    current_price_usd: float
    target_price_usd: float
    stop_price_usd: float
    entry_score: float     # 0-100 composite


class TradeEstimator:
    """
    The "brain" that gives actionable trade advice.
    Not financial advice — just structured signal synthesis.
    """

    VERDICTS = ["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL", "AVOID"]
    RISK_LEVELS = ["LOW", "MEDIUM", "HIGH", "EXTREME"]

    def __init__(self):
        self.conviction = ConvictionEngine()

    async def estimate(self, mint: str, live_record: dict = None,
                       safety: dict = None, dev_rep: dict = None) -> TradeSignal:
        """
        Full async estimation pipeline.
        """
        # 1. Fetch all data concurrently
        info_task = get_token_info_async(mint)
        sol_price_task = get_sol_usd_price_async()

        if safety is None:
            checker = HoneypotChecker(
                api_endpoint=f"https://api.rugcheck.xyz/v1/tokens/{mint}/report",
                api_key=config.RUGCHECK_API_KEY,
                sell_tax_threshold=config.SELL_TAX_THRESHOLD,
            )
            safety = checker.check_token_safety(mint)

        info = await info_task
        sol_price = await sol_price_task

        # 2. Conviction score (reuse existing engine)
        eval_result = self.conviction.evaluate(
            mint, live_record=live_record, safety=safety, dev_rep=dev_rep, info=info if info.get("ok") else None
        )
        score = eval_result["score"]

        # 3. Build reasoning and determine verdict
        reasoning = []
        risk_score = 0  # lower = safer
        bullish_points = 0
        bearish_points = 0

        # --- Safety checks ---
        if not safety.get("is_safe"):
            bearish_points += 5
            reasoning.append("🚫 Contract failed safety check — AVOID")
            risk_score += 50
        else:
            bullish_points += 1
            reasoning.append("✅ Contract passed safety check")

        if safety.get("mint_authority"):
            bearish_points += 3
            reasoning.append("❌ Mint authority active — dev can print tokens")
            risk_score += 20
        if safety.get("freeze_authority"):
            bearish_points += 2
            reasoning.append("❌ Freeze authority active")
            risk_score += 15
        if safety.get("lp_locked") is False:
            bearish_points += 2
            reasoning.append("⚠️ Liquidity not locked — rug pull risk")
            risk_score += 15

        creator_pct = safety.get("creator_holding_pct")
        if creator_pct and creator_pct > 20:
            bearish_points += 2
            reasoning.append(f"⚠️ Dev holds {creator_pct:.1f}% — high concentration")
            risk_score += 10
        elif creator_pct and creator_pct < 2:
            bullish_points += 1
            reasoning.append("✅ Dev renounced most holdings")

        # --- Price / Market data ---
        if info.get("ok"):
            price = info["price_usd"]
            liq = info.get("liquidity_usd", 0)
            vol = info.get("volume_24h_usd", 0)
            buys = info.get("buys_h1", 0)
            sells = info.get("sells_h1", 0)
            age = info.get("age_minutes")

            if liq >= 50000 and vol >= 10000:
                bullish_points += 2
                reasoning.append(f"💧 Strong liquidity ${liq:,.0f} + healthy volume")
                risk_score -= 5
            elif liq >= 10000:
                bullish_points += 1
                reasoning.append(f"💧 Decent liquidity ${liq:,.0f}")
            elif liq < 1000:
                bearish_points += 3
                reasoning.append(f"💀 Low liquidity ${liq:,.0f} — hard to exit")
                risk_score += 25

            # Buy/sell pressure
            if buys > 0 or sells > 0:
                ratio = buys / (sells + 1)
                if ratio > 2:
                    bullish_points += 2
                    reasoning.append(f"🟢 Buy pressure {buys}:{sells} (ratio {ratio:.1f}x)")
                elif ratio < 0.5:
                    bearish_points += 2
                    reasoning.append(f"🔴 Sell pressure {buys}:{sells} — distribution")
                else:
                    reasoning.append(f"⚖️ Balanced flow {buys}:{sells}")

            # Age
            if age is not None:
                if age < 5:
                    bullish_points += 2
                    reasoning.append(f"⚡ Just launched ({age}m) — earliest entry")
                    risk_score += 10
                elif age < 30:
                    bullish_points += 1
                    reasoning.append(f"⚡ Very fresh ({age}m)")
                    risk_score += 5
                elif age > 360:
                    bearish_points += 1
                    reasoning.append(f"⏱️ {age//60}h old — may have pumped already")

            # Dead check
            if info.get("is_dead"):
                bearish_points += 5
                reasoning.append("💀 Token appears dead (no volume/liquidity)")
                risk_score += 30
        else:
            price = 0.0
            reasoning.append("⚠️ No price data available")
            risk_score += 10
            bearish_points += 1

        # --- Bonding curve ---
        bonding = eval_result.get("bonding_progress")
        if bonding is not None:
            if 70 <= bonding <= 95:
                bullish_points += 3
                reasoning.append(f"🎯 Bonding {bonding:.0f}% — PRE-GRADUATION SWEET SPOT")
            elif 50 <= bonding < 70:
                bullish_points += 2
                reasoning.append(f"📈 Bonding {bonding:.0f}% — building momentum")
            elif bonding > 95:
                bearish_points += 1
                reasoning.append(f"🎓 Bonding {bonding:.0f}% — may be late to party")

        # --- Initial buy / whale launch ---
        init_buy = eval_result.get("initial_buy_sol")
        if init_buy and init_buy >= 5.0:
            bullish_points += 2
            reasoning.append(f"🐋 Whale launch: {init_buy:.2f} SOL initial buy")
        elif init_buy and init_buy < 0.1:
            bearish_points += 1
            reasoning.append(f"😴 Weak launch: {init_buy:.2f} SOL initial buy")

        # --- Dev rep / Smart money ---
        dev_wallet = eval_result.get("dev_wallet")
        if dev_wallet:
            lbl = labeler.get_label(dev_wallet)
            if lbl:
                if lbl.category == "known_dev":
                    bullish_points += 2
                    reasoning.append(f"🏆 Known smart-money dev: {lbl.name}")
                elif lbl.category == "serial_rugger":
                    bearish_points += 5
                    reasoning.append(f"🚫 Serial rugger dev: {lbl.name}")
                    risk_score += 30
            else:
                # Check smart money tracker
                from smart_money_tracker import smart_money
                sm = smart_money.check_wallet(dev_wallet)
                if sm:
                    total = sm["wins"] + sm["losses"]
                    wr = (sm["wins"] / total * 100) if total > 0 else 0
                    if "smart_money" in sm.get("tags", []):
                        bullish_points += 2
                        reasoning.append(f"🏆 Smart money dev ({wr:.0f}% WR)")
                    elif "serial_rugger" in sm.get("tags", []):
                        bearish_points += 3
                        reasoning.append(f"🚫 Serial rugger ({wr:.0f}% WR)")
                        risk_score += 25

        # --- Bundle risk ---
        bundle_score = eval_result.get("breakdown", {}).get("bundle_risk", 100)
        if bundle_score < 40:
            bearish_points += 3
            reasoning.append("🎭 High bundle/sybil risk detected")
            risk_score += 20
        elif bundle_score < 70:
            bearish_points += 1
            reasoning.append("⚠️ Some bundle signs — proceed with caution")
            risk_score += 5

        # --- Socials ---
        social_score = eval_result.get("breakdown", {}).get("socials", 0)
        if social_score >= 75:
            bullish_points += 1
            reasoning.append("✅ Strong social presence (X + TG + Web)")
        elif social_score <= 20:
            bearish_points += 1
            reasoning.append("🚫 No socials — major red flag")
            risk_score += 10

        # 4. Compute final verdict
        net_score = bullish_points - bearish_points
        entry_score = score  # from conviction engine

        # Override: if safety failed, always AVOID
        if not safety.get("is_safe"):
            verdict = "AVOID"
            confidence = 95
        # Override: if serial rugger, hard avoid
        elif dev_wallet and labeler.get_category(dev_wallet) == "serial_rugger":
            verdict = "AVOID"
            confidence = 90
        elif entry_score >= 85 and net_score >= 3 and risk_score < 30:
            verdict = "STRONG_BUY"
            confidence = min(95, int(entry_score))
        elif entry_score >= 70 and net_score >= 1 and risk_score < 40:
            verdict = "BUY"
            confidence = min(85, int(entry_score))
        elif entry_score >= 55 and net_score >= 0 and risk_score < 50:
            verdict = "HOLD"
            confidence = min(70, int(entry_score))
        elif net_score < -2 or risk_score >= 50:
            verdict = "STRONG_SELL" if net_score < -3 else "SELL"
            confidence = min(80, 50 + abs(net_score) * 10)
        else:
            verdict = "HOLD"
            confidence = 50

        # 5. Risk level
        if risk_score < 10:
            risk_level = "LOW"
        elif risk_score < 25:
            risk_level = "MEDIUM"
        elif risk_score < 45:
            risk_level = "HIGH"
        else:
            risk_level = "EXTREME"

        # 6. Suggested size & targets
        if verdict in ("STRONG_BUY", "BUY"):
            if risk_level == "LOW":
                suggested_size = config.DEFAULT_BUY_SIZE_SOL * 1.5
            elif risk_level == "MEDIUM":
                suggested_size = config.DEFAULT_BUY_SIZE_SOL
            else:
                suggested_size = config.DEFAULT_BUY_SIZE_SOL * 0.5
            time_horizon = "hours"
        elif verdict in ("SELL", "STRONG_SELL"):
            suggested_size = 0.0
            time_horizon = "minutes"
        else:
            suggested_size = 0.0
            time_horizon = "hours"

        # Price targets
        if price > 0 and verdict in ("STRONG_BUY", "BUY"):
            target = price * (1 + config.TAKE_PROFIT_PERCENT / 100)
            stop = price * (1 + config.STOP_LOSS_PERCENT / 100)
        else:
            target = 0.0
            stop = 0.0

        return TradeSignal(
            mint=mint,
            symbol=info.get("symbol", "?") if info.get("ok") else "?",
            verdict=verdict,
            confidence=confidence,
            reasoning=reasoning,
            risk_level=risk_level,
            suggested_size_sol=suggested_size,
            time_horizon=time_horizon,
            current_price_usd=price if info.get("ok") else 0.0,
            target_price_usd=target,
            stop_price_usd=stop,
            entry_score=entry_score,
        )

    def format_signal(self, signal: TradeSignal) -> str:
        """Pretty Telegram message."""
        emoji_map = {
            "STRONG_BUY": "🚀", "BUY": "🟢", "HOLD": "🟡",
            "SELL": "🔴", "STRONG_SELL": "🛑", "AVOID": "🚫",
        }
        risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "EXTREME": "☠️"}

        e = emoji_map.get(signal.verdict, "❓")
        re = risk_emoji.get(signal.risk_level, "❓")

        lines = [
            f"{e} *{signal.verdict}* — {signal.symbol}",
            f"`{signal.mint}`",
            f"",
            f"*Confidence:* `{signal.confidence}%`",
            f"*Risk:* {re} `{signal.risk_level}`",
            f"*Entry Score:* `{signal.entry_score:.0f}/100`",
            f"",
        ]

        if signal.current_price_usd > 0:
            lines.append(f"💰 Price: `${signal.current_price_usd:.8f}`")
        if signal.target_price_usd > 0:
            lines.append(f"🎯 Target: `${signal.target_price_usd:.8f}` (+{config.TAKE_PROFIT_PERCENT:.0f}%)")
        if signal.stop_price_usd > 0:
            lines.append(f"🛑 Stop: `${signal.stop_price_usd:.8f}` ({config.STOP_LOSS_PERCENT:.0f}%)")

        if signal.suggested_size_sol > 0:
            usd = signal.suggested_size_sol * (config.DEFAULT_BUY_SIZE_SOL * 150 / config.DEFAULT_BUY_SIZE_SOL)  # rough
            lines.append(f"💡 Suggested: `{signal.suggested_size_sol:.2f} SOL` (~${signal.suggested_size_sol * 150:.0f})")
        lines.append(f"⏱️ Horizon: `{signal.time_horizon}`")
        lines.append("")
        lines.append("*— Reasoning —*")
        for r in signal.reasoning[:8]:
            lines.append(r)
        lines.append("")
        lines.append("_This is an algorithmic estimate, not financial advice._")

        return "\n".join(lines)


# Singleton
estimator = TradeEstimator()
