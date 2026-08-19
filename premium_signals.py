"""
premium_signals.py - Premium alert engine for high-conviction entries.
Filters for probability + timing. Fewer signals, higher quality.

FEATURES:
  - Score >= 80 only
  - Minimum $5k liquidity
  - Token age < 30 minutes
  - Cooldown per token (no spam)
  - Tracks win rate of premium signals
  - Auto-alerts on early-stage coins with full breakdown
"""

import json
import time
import os
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

import config
from conviction_engine import ConvictionEngine
from price_feed import get_token_info, get_sol_usd_price
from honeypot_check import HoneypotChecker


@dataclass
class PremiumSignal:
    mint: str
    symbol: str
    score: float
    verdict: str
    verdict_emoji: str
    price_usd: float
    liquidity_usd: float
    age_minutes: Optional[float]
    bonding_progress: Optional[float]
    initial_buy_sol: Optional[float]
    dev_wallet: Optional[str]
    timestamp: float
    notes: List[str]
    alerted: bool = False
    outcome: str = "PENDING"  # PENDING, WIN, LOSS


class PremiumSignalEngine:
    """
    Generates premium-quality signals with strict filtering.
    """

    def __init__(self, state_file: str = config.PREMIUM_STATE_FILE):
        self.state_file = state_file
        self.signals: Dict[str, PremiumSignal] = {}  # mint -> signal
        self.cooldowns: Dict[str, float] = {}  # mint -> last_alert_time
        self.engine = ConvictionEngine()
        self._load_state()

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                for mint, sig_dict in data.get("signals", {}).items():
                    self.signals[mint] = PremiumSignal(**sig_dict)
                self.cooldowns = data.get("cooldowns", {})
            except (json.JSONDecodeError, TypeError):
                pass

    def _save_state(self):
        with open(self.state_file, "w") as f:
            json.dump({
                "signals": {m: asdict(s) for m, s in self.signals.items()},
                "cooldowns": self.cooldowns,
            }, f, indent=2, default=str)

    def _is_on_cooldown(self, mint: str) -> bool:
        last = self.cooldowns.get(mint, 0)
        return (time.time() - last) < config.PREMIUM_COOLDOWN_SECONDS

    def _passes_premium_filters(self, eval_result: dict, info: dict) -> tuple:
        """
        Returns (passes: bool, reasons: list of why it failed).
        """
        reasons = []
        passes = True

        # Score filter
        if eval_result["score"] < config.PREMIUM_SIGNAL_THRESHOLD:
            passes = False
            reasons.append(f"Score {eval_result['score']:.0f} < {config.PREMIUM_SIGNAL_THRESHOLD}")

        # Liquidity filter
        liq = info.get("liquidity_usd", 0) or 0
        if liq < config.PREMIUM_MIN_LIQUIDITY_USD:
            passes = False
            reasons.append(f"Liquidity ${liq:,.0f} < ${config.PREMIUM_MIN_LIQUIDITY_USD:,.0f}")

        # Age filter
        age = info.get("age_minutes")
        if age is not None and age > config.PREMIUM_MAX_AGE_MINUTES:
            passes = False
            reasons.append(f"Age {age:.0f}m > {config.PREMIUM_MAX_AGE_MINUTES}m")

        # Safety filter - must pass RugCheck
        safety = eval_result.get("safety", {})
        if not safety.get("is_safe"):
            passes = False
            reasons.append("Failed safety check")

        # Bundle risk filter - cap if too suspicious
        if eval_result.get("breakdown", {}).get("bundle_risk", 100) < 40:
            passes = False
            reasons.append("High bundle risk detected")

        return passes, reasons

    def evaluate_and_alert(self, mint: str, live_record: dict = None) -> Optional[PremiumSignal]:
        """
        Full evaluation pipeline. Returns PremiumSignal if it passes all filters,
        None otherwise.
        """
        # Check cooldown first (fast exit)
        if self._is_on_cooldown(mint):
            return None

        # Get full evaluation
        eval_result = self.engine.evaluate(mint, live_record=live_record)

        # Get price info
        info = get_token_info(mint)
        if not info.get("ok"):
            return None

        # Run premium filters
        passes, reasons = self._passes_premium_filters(eval_result, info)
        if not passes:
            return None

        # Build premium signal
        signal = PremiumSignal(
            mint=mint,
            symbol=info.get("symbol", "?"),
            score=eval_result["score"],
            verdict=eval_result["verdict"],
            verdict_emoji=eval_result["verdict_emoji"],
            price_usd=info["price_usd"],
            liquidity_usd=info.get("liquidity_usd", 0),
            age_minutes=info.get("age_minutes"),
            bonding_progress=eval_result.get("bonding_progress"),
            initial_buy_sol=eval_result.get("initial_buy_sol"),
            dev_wallet=eval_result.get("dev_wallet"),
            timestamp=time.time(),
            notes=[n for notes in eval_result.get("notes", {}).values() for n in notes],
        )

        self.signals[mint] = signal
        self.cooldowns[mint] = time.time()
        self._save_state()
        return signal

    def scan_for_premium_signals(self, max_lines: int = 200) -> List[PremiumSignal]:
        """
        Scans live_discoveries.jsonl for premium-quality tokens.
        Returns list of signals sorted by score (highest first).
        """
        import os as _os
        import json as _json

        path = "live_discoveries.jsonl"
        if not _os.path.exists(path):
            return []

        results = []
        seen = set()

        try:
            with open(path) as f:
                lines = f.readlines()
        except IOError:
            return []

        for line in lines[-max_lines:]:
            if not line.strip():
                continue
            try:
                rec = _json.loads(line)
            except _json.JSONDecodeError:
                continue

            if rec.get("type") != "new_token":
                continue

            mint = rec.get("mint")
            if not mint or mint in seen:
                continue
            seen.add(mint)

            signal = self.evaluate_and_alert(mint, live_record=rec)
            if signal:
                results.append(signal)

        results.sort(key=lambda s: s.score, reverse=True)
        return results

    def format_premium_alert(self, signal: PremiumSignal) -> str:
        """Formats a premium signal for Telegram."""
        sol_price = get_sol_usd_price()
        trade_size_usd = config.DEFAULT_BUY_SIZE_SOL * sol_price if sol_price > 0 else 10.0

        lines = [
            f"⚡️ *SOUL PREMIUM SIGNAL*",
            f"",
            f"{signal.verdict_emoji} *{signal.verdict}* — Conviction: `{signal.score:.0f}/100`",
            f"*{signal.symbol}*",
            f"`{signal.mint}`",
            f"",
        ]

        # Key notes only (most important ones)
        key_notes = [n for n in signal.notes if any(
            emoji in n for emoji in ["🎯", "🐋", "🏆", "🚫", "🎭", "❌", "⚠️"]
        )]
        for note in key_notes[:5]:
            lines.append(note)

        lines.append("")

        # Price & liquidity
        lines.append(f"💰 Price: `${signal.price_usd:.8f}`")
        lines.append(f"💧 Liquidity: `${signal.liquidity_usd:,.0f}`")

        if signal.age_minutes is not None:
            if signal.age_minutes < 1:
                lines.append(f"⏱️ Age: *JUST LAUNCHED* ({signal.age_minutes:.0f}m)")
            else:
                lines.append(f"⏱️ Age: `{signal.age_minutes:.0f}m`")

        if signal.bonding_progress is not None:
            bar_filled = int(signal.bonding_progress / 10)
            bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
            lines.append(f"📊 Bonding: {bar} `{signal.bonding_progress:.0f}%`")

        if signal.initial_buy_sol:
            whale = " 🐋 WHALE LAUNCH" if signal.initial_buy_sol >= config.WHALE_LAUNCH_THRESHOLD_SOL else ""
            lines.append(f"💰 Initial buy: `{signal.initial_buy_sol:.2f} SOL`{whale}")

        lines.append("")
        lines.append(f"_Suggested size: {config.DEFAULT_BUY_SIZE_SOL} SOL (~${trade_size_usd:.0f})_")
        lines.append(f"_Auto TP: +{config.TAKE_PROFIT_PERCENT:.0f}% | Auto SL: {config.STOP_LOSS_PERCENT:.0f}%_")
        lines.append("")
        lines.append("⚡️ *Premium Signal — High conviction, timed entry*")

        return "\n".join(lines)

    def get_stats(self) -> dict:
        """Returns premium signal performance stats."""
        total = len(self.signals)
        wins = sum(1 for s in self.signals.values() if s.outcome == "WIN")
        losses = sum(1 for s in self.signals.values() if s.outcome == "LOSS")
        pending = sum(1 for s in self.signals.values() if s.outcome == "PENDING")
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

        return {
            "total_signals": total,
            "wins": wins,
            "losses": losses,
            "pending": pending,
            "win_rate": win_rate,
            "avg_score": sum(s.score for s in self.signals.values()) / total if total > 0 else 0,
        }

    def mark_outcome(self, mint: str, outcome: str):
        """Mark a signal as WIN or LOSS after trade closes."""
        if mint in self.signals:
            self.signals[mint].outcome = outcome
            self._save_state()


# Singleton
premium_engine = PremiumSignalEngine()
