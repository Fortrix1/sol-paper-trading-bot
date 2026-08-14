"""
conviction_engine.py - The "brain" that scores tokens 0-100 for goldmine potential.
NOW WITH:
  - Bundle / sybil detection heuristic (free, no paid APIs)
  - Smart money dev tracking integration
  - Bonding curve intelligence
  - Initial buy / whale launch detection
"""

import time
from typing import Dict, Optional

from honeypot_check import HoneypotChecker
from helius_check import get_deployer_reputation
from price_feed import get_token_info
from smart_money_tracker import SmartMoneyTracker
import config


class ConvictionEngine:
    """
    Heuristic scoring system. Weights tuned for early-entry sniping.
    """

    WEIGHTS = {
        "safety": 0.22,
        "dev_rep": 0.13,
        "bonding": 0.18,
        "initial_buy": 0.10,
        "socials": 0.10,
        "age": 0.09,
        "liquidity": 0.09,
        "bundle_risk": 0.09,  # NEW: penalize suspicious patterns
    }

    def __init__(self):
        self.rugcheck_url = "https://api.rugcheck.xyz/v1/tokens/{}/report"
        self.smart_money = SmartMoneyTracker()

    def score_safety(self, safety: dict) -> tuple:
        if not safety:
            return 50, ["No safety data"]

        check_failed = not safety.get("is_safe") and "API check failed" in str(safety.get("reason", ""))
        if check_failed:
            return 40, ["⚠️ Safety check failed - treating as risky"]

        if not safety.get("is_safe"):
            return 20, [f"❌ Unsafe: {safety.get('reason', 'unknown')}"]

        score = 100
        notes = ["✅ Contract looks safe"]

        if safety.get("mint_authority"):
            score -= 25
            notes.append("❌ Mint authority active (-25)")
        if safety.get("freeze_authority"):
            score -= 25
            notes.append("❌ Freeze authority active (-25)")

        creator_pct = safety.get("creator_holding_pct")
        if creator_pct and creator_pct > 20:
            score -= 20
            notes.append(f"⚠️ Dev holds {creator_pct:.1f}% (-20)")
        elif creator_pct and creator_pct > 10:
            score -= 10
            notes.append(f"⚠️ Dev holds {creator_pct:.1f}% (-10)")

        top_pct = safety.get("top_holder_pct")
        if top_pct and top_pct > 70:
            score -= 15
            notes.append(f"⚠️ Top 10 hold {top_pct:.0f}% (-15)")
        elif top_pct and top_pct > 50:
            score -= 5
            notes.append(f"⚠️ Top 10 hold {top_pct:.0f}% (-5)")

        if safety.get("lp_locked") is False:
            score -= 15
            notes.append("⚠️ Liquidity not locked (-15)")

        return max(0, score), notes

    def score_dev_rep(self, dev_rep: dict, dev_wallet: str = None) -> tuple:
        # Check smart money tracker first
        if dev_wallet:
            sm = self.smart_money.check_wallet(dev_wallet)
            if sm:
                total = sm["wins"] + sm["losses"]
                wr = (sm["wins"] / total * 100) if total > 0 else 0
                if "smart_money" in sm.get("tags", []):
                    return 95, [f"🏆 SMART MONEY DEV — {sm['wins']}/{total} wins ({wr:.0f}%)"]
                if "serial_rugger" in sm.get("tags", []):
                    return 15, [f"🚫 SERIAL RUGGER — {sm['losses']}/{total} losses ({wr:.0f}%)"]

        if not dev_rep or not dev_rep.get("ok"):
            return 50, ["Dev rep unknown"]

        if dev_rep.get("is_brand_new_wallet"):
            return 35, ["🆕 Brand new dev wallet - possible rug risk"]

        txns = dev_rep.get("txn_count_sampled", 0)
        creations = dev_rep.get("likely_tokens_created", 0)

        if creations == 0:
            if txns < 10:
                return 45, [f"👤 Low activity wallet ({txns} txns)"]
            return 65, [f"👤 Active wallet, no prior tokens ({txns} txns)"]

        if creations >= 3:
            return 75, [f"🏗️ Experienced dev: ~{creations} tokens created"]

        return 60, [f"🏗️ Some creation history: ~{creations} tokens"]

    def score_bonding(self, progress_pct: Optional[float]) -> tuple:
        if progress_pct is None:
            return 50, ["Bonding progress unknown"]

        if 70 <= progress_pct <= 95:
            return 95, [f"🎯 Bonding at {progress_pct:.0f}% — PRE-GRADUATION SWEET SPOT!"]
        elif 50 <= progress_pct < 70:
            return 85, [f"📈 Bonding at {progress_pct:.0f}% — building momentum"]
        elif 30 <= progress_pct < 50:
            return 75, [f"🌱 Bonding at {progress_pct:.0f}% — early but promising"]
        elif 0 <= progress_pct < 30:
            return 60, [f"🌱 Bonding at {progress_pct:.0f}% — very early, high risk"]
        elif 95 < progress_pct <= 100:
            return 70, [f"🎓 Bonding at {progress_pct:.0f}% — graduating now, may be late"]
        else:
            return 50, [f"❓ Bonding at {progress_pct:.0f}% — unusual value"]

    def score_initial_buy(self, sol_amount: Optional[float]) -> tuple:
        if sol_amount is None:
            return 50, ["Initial buy unknown"]

        if sol_amount >= 5.0:
            return 90, [f"🐋 Whale launch: {sol_amount:.2f} SOL initial buy"]
        elif sol_amount >= 1.0:
            return 75, [f"💰 Serious launch: {sol_amount:.2f} SOL initial buy"]
        elif sol_amount >= 0.1:
            return 55, [f"👍 Normal launch: {sol_amount:.2f} SOL initial buy"]
        else:
            return 30, [f"😴 Low effort launch: {sol_amount:.2f} SOL initial buy"]

    def score_socials(self, info: dict) -> tuple:
        score = 0
        notes = []

        if info.get("twitter_url"):
            score += 30
            notes.append("🐦 Twitter (+30)")
        if info.get("telegram_url"):
            score += 25
            notes.append("✈️ Telegram (+25)")
        if info.get("website_url"):
            score += 25
            notes.append("🌐 Website (+25)")
        if info.get("is_boosted"):
            score += 20
            notes.append("📢 Boosted listing (+20)")

        if score == 0:
            return 20, ["🚫 No socials found — major red flag"]

        return score, notes

    def score_age(self, age_minutes: Optional[int]) -> tuple:
        if age_minutes is None:
            return 50, ["Age unknown"]

        if age_minutes < 5:
            return 90, [f"⚡ Just launched ({age_minutes}m ago) — earliest entry"]
        elif age_minutes < 30:
            return 80, [f"⚡ Very fresh ({age_minutes}m ago)"]
        elif age_minutes < 60:
            return 70, [f"⏱️ Fresh ({age_minutes}m ago)"]
        elif age_minutes < 360:
            return 60, [f"⏱️ {age_minutes//60}h old — still early"]
        else:
            return 40, [f"⏱️ {age_minutes//60}h+ old — may have pumped already"]

    def score_liquidity(self, info: dict) -> tuple:
        liq = info.get("liquidity_usd", 0) or 0
        vol = info.get("volume_24h_usd", 0) or 0

        if liq >= 50000 and vol >= 10000:
            return 90, [f"💧 Strong liquidity ${liq:,.0f} + vol ${vol:,.0f}"]
        elif liq >= 10000 and vol >= 5000:
            return 75, [f"💧 Good liquidity ${liq:,.0f} + vol ${vol:,.0f}"]
        elif liq >= 1000 and vol >= 500:
            return 60, [f"💧 Moderate liquidity ${liq:,.0f}"]
        elif liq < 1000:
            return 30, [f"💧 Low liquidity ${liq:,.0f} — risky"]
        else:
            return 50, ["💧 Liquidity data unclear"]

    def detect_bundle_risk(self, safety: dict, live_record: dict = None) -> tuple:
        """
        Heuristic bundle/sybil detection. NOT foolproof — this is the best
        we can do without paid transaction-parsing APIs.

        Returns (score_0_100, notes_list).
        Score = 100 means no bundle signs. Lower = more suspicious.
        """
        notes = []
        penalties = 0

        # 1. Check top holder distribution for sybil pattern
        # Sybil = multiple wallets with nearly identical small percentages
        holders = safety.get("top_holders_list", []) if safety else []
        if len(holders) >= 5:
            pcts = [h.get("pct", 0) for h in holders[:10]]
            if len(pcts) >= 5:
                # If top 5 holders all have 1-5% exactly, that's suspicious
                similar_small = sum(1 for p in pcts[:5] if 1.0 <= p <= 5.0)
                if similar_small >= 4:
                    penalties += 25
                    notes.append("🎭 SYBIL PATTERN: Top 5 holders all have 1-5% — possible bundle")

        # 2. Dev holding vs initial buy mismatch
        # If dev bought huge but isn't in top holders, they used multiple wallets
        initial_buy = (live_record or {}).get("initial_buy_sol") if live_record else None
        creator_pct = safety.get("creator_holding_pct") if safety else None
        if initial_buy and initial_buy >= 3.0:
            if creator_pct is not None and creator_pct < 2.0:
                penalties += 20
                notes.append(f"🎭 BUNDLE SIGN: Dev bought {initial_buy:.2f} SOL but only holds {creator_pct:.1f}% — likely split across wallets")

        # 3. Brand new dev + large initial buy = high bundle probability
        dev_wallet = (live_record or {}).get("dev_wallet") if live_record else None
        if dev_wallet:
            dev_rep = get_deployer_reputation(dev_wallet, config.HELIUS_API_KEY)
            if dev_rep.get("ok") and dev_rep.get("is_brand_new_wallet") and initial_buy and initial_buy >= 2.0:
                penalties += 15
                notes.append("🎭 Fresh dev wallet + large initial buy — classic bundle setup")

        if not notes:
            notes.append("✅ No obvious bundle/sybil patterns detected")
            return 100, notes

        return max(0, 100 - penalties), notes

    def evaluate(self, mint: str, live_record: dict = None,
                 safety: dict = None, dev_rep: dict = None,
                 info: dict = None) -> dict:
        if safety is None:
            checker = HoneypotChecker(
                api_endpoint=self.rugcheck_url.format(mint),
                api_key=config.RUGCHECK_API_KEY,
                sell_tax_threshold=config.SELL_TAX_THRESHOLD,
            )
            safety = checker.check_token_safety(mint)

        dev_wallet = (live_record or {}).get("dev_wallet") if live_record else (safety or {}).get("creator")

        if dev_rep is None and dev_wallet:
            dev_rep = get_deployer_reputation(dev_wallet, config.HELIUS_API_KEY)

        if info is None:
            info = get_token_info(mint)

        raw = (live_record or {}).get("raw", {})
        v_sol = raw.get("vSolInBondingCurve")

        bonding_progress = None
        if v_sol is not None:
            bonding_progress = max(0, min(100,
                (v_sol - config.BONDING_CURVE_START_SOL) /
                (config.BONDING_CURVE_GRADUATE_SOL - config.BONDING_CURVE_START_SOL) * 100
            ))

        initial_buy = (live_record or {}).get("initial_buy_sol") if live_record else raw.get("solAmount")
        age_minutes = info.get("age_minutes") if info and info.get("ok") else None

        scores = {}
        notes = {}

        scores["safety"], notes["safety"] = self.score_safety(safety)
        scores["dev_rep"], notes["dev_rep"] = self.score_dev_rep(dev_rep, dev_wallet)
        scores["bonding"], notes["bonding"] = self.score_bonding(bonding_progress)
        scores["initial_buy"], notes["initial_buy"] = self.score_initial_buy(initial_buy)
        scores["socials"], notes["socials"] = self.score_socials(info if info and info.get("ok") else {})
        scores["age"], notes["age"] = self.score_age(age_minutes)
        scores["liquidity"], notes["liquidity"] = self.score_liquidity(info if info and info.get("ok") else {})
        scores["bundle_risk"], notes["bundle_risk"] = self.detect_bundle_risk(safety, live_record)

        total = sum(scores[k] * self.WEIGHTS[k] for k in self.WEIGHTS)

        # Override: if bundle risk is very low (<40), cap total score
        if scores["bundle_risk"] < 40:
            total = min(total, 60)
            notes["bundle_risk"].append("🔒 Score capped due to high bundle risk")

        # Override: if smart money dev, boost score
        if dev_wallet and self.smart_money.is_smart_money(dev_wallet):
            total = min(100, total + 10)
            notes["dev_rep"].append("🏆 +10 boost from smart money track record")

        # Override: if serial rugger, hard cap
        if dev_wallet and self.smart_money.is_serial_rugger(dev_wallet):
            total = min(total, 30)
            notes["dev_rep"].append("🚫 HARD CAP: Known serial rugger")

        if total >= 85:
            verdict = "APE"
            verdict_emoji = "🚀"
        elif total >= 70:
            verdict = "STRONG_BUY"
            verdict_emoji = "🟢"
        elif total >= 55:
            verdict = "WATCH"
            verdict_emoji = "🟡"
        elif total >= 40:
            verdict = "WEAK"
            verdict_emoji = "🟠"
        else:
            verdict = "SKIP"
            verdict_emoji = "🔴"

        return {
            "mint": mint,
            "score": round(total, 1),
            "verdict": verdict,
            "verdict_emoji": verdict_emoji,
            "breakdown": scores,
            "notes": notes,
            "bonding_progress": bonding_progress,
            "initial_buy_sol": initial_buy,
            "dev_wallet": dev_wallet,
            "safety": safety,
            "dev_rep": dev_rep,
            "info": info,
        }

    def format_card(self, eval_result: dict) -> str:
        e = eval_result
        lines = [
            f"{e['verdict_emoji']} *{e['verdict']}* — Conviction: `{e['score']:.0f}/100`",
            f"`{e['mint']}`",
            "",
        ]

        for category, note_list in e["notes"].items():
            for note in note_list:
                lines.append(note)

        if e["bonding_progress"] is not None:
            bar_filled = int(e["bonding_progress"] / 10)
            bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
            lines.append(f"\n📊 Bonding Progress: {bar} `{e['bonding_progress']:.0f}%`")

        info = e.get("info")
        if info and info.get("ok"):
            lines.append(f"💰 Price: `${info['price_usd']:.8f}` | Liq: `${info['liquidity_usd']:,.0f}`")

        lines.append("\n_This is a PAPER trade - no real funds involved._")
        return "\n".join(lines)
