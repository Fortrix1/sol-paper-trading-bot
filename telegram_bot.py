"""
telegram_bot.py - Paper-trading + REAL trading Telegram bot with GOLDMINE detection,
conviction scoring, autopilot, BONDED curve sniping, bundle detection,
smart money tracking, PREMIUM signals, and Phantom wallet integration.

NEW IN THIS VERSION:
  - /wallet -- Link Phantom wallet (bot generates dedicated wallet)
  - /realbuy -- Buy with REAL SOL
  - /realsell -- Sell for REAL SOL
  - /realpositions -- Live real P&L with whale activity
  - /whales -- Whale buy/sell alerts for held tokens
  - /premium -- Premium high-conviction signals
  - Faster async price fetching
  - Auto-refresh position updates every 20s
  - Early-stage coin alerts (< 60s old)
"""

import re
import io
import time
import asyncio
import datetime
import logging

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

import config
from price_feed import get_token_info, get_sol_usd_price
from honeypot_check import HoneypotChecker
from paper_wallet import PaperWallet
from new_scanner import get_boosted_solana_tokens, get_graduating_coins, get_latest_new_tokens
from helius_check import get_deployer_reputation
from launch_watcher import get_upcoming_pool_launches, debug_check_transaction, get_recent_migrations
from conviction_engine import ConvictionEngine
from auto_paper_trader import AutoPaperTrader
from risk_manager import RiskManager
from smart_money_tracker import SmartMoneyTracker
from phantom_connector import phantom
from real_trader import real_trader
from premium_signals import premium_engine
from position_tracker import tracker
import live_listener

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

wallet = PaperWallet(
    state_file=config.WALLET_STATE_FILE,
    starting_balance=config.STARTING_BALANCE_SOL,
    daily_topup=config.DAILY_TOPUP_SOL,
    cap=config.BALANCE_CAP_SOL,
)

risk_mgr = RiskManager()
conviction = ConvictionEngine()
auto_trader = AutoPaperTrader(wallet, risk_mgr)
smart_money = SmartMoneyTracker()

autopilot_states = {}
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report"
MINT_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
MINT_SEARCH_PATTERN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def check_safety(mint: str) -> dict:
    checker = HoneypotChecker(
        api_endpoint=RUGCHECK_URL.format(mint),
        api_key=config.RUGCHECK_API_KEY,
        sell_tax_threshold=config.SELL_TAX_THRESHOLD,
    )
    return checker.check_token_safety(mint)


def format_age(age_minutes):
    if age_minutes is None:
        return "unknown"
    if age_minutes < 60:
        return f"{age_minutes}m"
    hours = age_minutes // 60
    mins = age_minutes % 60
    if hours < 24:
        return f"{hours}h {mins}m"
    days = hours // 24
    hours = hours % 24
    return f"{days}d {hours}h"


def get_launch_price(mint: str, sol_price_usd: float) -> dict:
    import json as _json
    import os as _os

    def _search(path):
        if not _os.path.exists(path):
            return None
        earliest = None
        try:
            with open(path) as f:
                for line in f:
                    if not line.strip():
                        continue
                    rec = _json.loads(line)
                    if rec.get("type") != "new_token" or rec.get("mint") != mint:
                        continue
                    if earliest is None or rec.get("discovered_at", 0) < earliest.get("discovered_at", 0):
                        earliest = rec
        except (IOError, _json.JSONDecodeError):
            return None
        return earliest

    earliest = _search("launch_index.jsonl") or _search("live_discoveries.jsonl")
    if not earliest:
        return {"ok": False}

    raw = earliest.get("raw", {})
    v_sol = raw.get("vSolInBondingCurve")
    v_tokens = raw.get("vTokensInBondingCurve")
    if not v_sol or not v_tokens or sol_price_usd <= 0:
        return {"ok": False}

    price_sol = v_sol / v_tokens
    price_usd = price_sol * sol_price_usd
    return {"ok": True, "launch_price_usd": price_usd, "launch_timestamp": earliest.get("discovered_at")}


def format_token_card(mint: str, info: dict, safety: dict, dev_rep: dict = None,
                      launch: dict = None, eval_result: dict = None) -> str:
    age = format_age(info.get("age_minutes"))

    contract_line = "✅ Contract looks safe" if safety.get("is_safe") else f"❌ Contract UNSAFE ({safety.get('reason')})"
    activity_line = "💀 DEAD - very low liquidity/volume" if info.get("is_dead") else "✅ Active trading"

    check_failed = not safety.get("is_safe") and "API check failed" in str(safety.get("reason", ""))
    mint_active = bool(safety.get("mint_authority"))
    freeze_active = bool(safety.get("freeze_authority"))
    lp_locked = safety.get("lp_locked")
    creator_pct = safety.get("creator_holding_pct")

    if check_failed:
        risk_label = "⚫ UNKNOWN - safety check failed"
    elif mint_active or freeze_active:
        risk_label = "🔴 HIGH - dev can mint/freeze"
    elif lp_locked is False:
        risk_label = "🟠 MEDIUM-HIGH - liquidity unlocked"
    elif (creator_pct and creator_pct > 10) or (safety.get("top_holder_pct") and safety["top_holder_pct"] > 60):
        risk_label = "🟡 MEDIUM - supply concentrated"
    else:
        risk_label = "🟢 LOWER - no major red flags"

    lines = [
        f"*{info.get('name', '?')} ({info.get('symbol', '?')})*",
        f"`{mint}`",
        "",
    ]

    if eval_result:
        e = eval_result
        lines.append(f"{e['verdict_emoji']} *Conviction: {e['score']:.0f}/100* -- `{e['verdict']}`")
        if e.get("bonding_progress") is not None:
            bar_filled = int(e["bonding_progress"] / 10)
            bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
            lines.append(f"📊 Bonding: {bar} `{e['bonding_progress']:.0f}%`")
        if e.get("initial_buy_sol"):
            lines.append(f"💰 Initial buy: `{e['initial_buy_sol']:.2f} SOL`")
        lines.append("")

    lines += [
        f"⚠️ *Rug risk:* {risk_label}",
        "",
        "*-- Price --*",
        f"Now: `${info.get('price_usd', 0):.8f}`",
    ]

    if launch and launch.get("ok"):
        change_pct = ((info["price_usd"] - launch["launch_price_usd"]) / launch["launch_price_usd"] * 100) if launch["launch_price_usd"] > 0 else 0
        lines.append(f"Launched: `${launch['launch_price_usd']:.8f}` · Change: `{change_pct:+.1f}%`")

    lines += [
        f"Liq: `${info.get('liquidity_usd', 0):,.0f}` · 24h Vol: `${info.get('volume_24h_usd', 0):,.0f}`",
        f"MCap: `${info.get('mcap_usd', 0):,.0f}`" + (f" · FDV: `${info.get('fdv_usd', 0):,.0f}`" if info.get("fdv_usd") else ""),
        f"Age: `{age}` · Boosted: `{'Yes' if info.get('is_boosted') else 'No'}`",
        "",
        "*-- Contract --*",
        contract_line,
        activity_line,
    ]

    if check_failed:
        lines.append("Renounced: ⚫ Unknown")
    else:
        lines.append(f"Renounced: {'✅' if not mint_active else '❌ Mint active'}")
    if freeze_active:
        lines.append("❌ Freeze authority active")
    if lp_locked is not None:
        lines.append(f"LP locked: `{'Yes' if lp_locked else 'No - pullable'}`")

    lines += ["", "*-- Deployer --*"]
    if safety.get("creator"):
        lines.append(f"Wallet: `{safety['creator']}`")
        if creator_pct is not None:
            lines.append(f"Dev holds: `{creator_pct:.1f}%`")
        sm_summary = smart_money.get_summary(safety["creator"])
        if sm_summary:
            lines.append(sm_summary)
        elif dev_rep and dev_rep.get("ok"):
            if dev_rep["is_brand_new_wallet"]:
                lines.append("🆕 Brand new wallet")
            else:
                lines.append(f"History: {dev_rep['txn_count_sampled']} txns, ~{dev_rep['likely_tokens_created']} creations")

    if safety.get("top_holders_list"):
        lines += ["", "*-- Top Holders --*"]
        for h in safety["top_holders_list"][:5]:
            addr = h.get("address") or "unknown"
            short = f"{addr[:4]}...{addr[-4:]}" if len(addr) > 10 else addr
            tag = " 👤dev" if addr == safety.get("creator") else ""
            lines.append(f"`{short}` - `{h.get('pct', 0):.1f}%`{tag}")

    lines += ["", "*-- Socials --*"]
    lines.append(f"X: {'✅' if info.get('twitter_url') else '❌'} · TG: {'✅' if info.get('telegram_url') else '❌'} · Web: {'✅' if info.get('website_url') else '❌'}")
    lines.append("")
    lines.append("_This is a PAPER trade - no real funds involved._")
    return "\n".join(lines)


def format_position_update(mint: str, pos: dict, current_price: float) -> str:
    entry = pos["entry_price_usd"]
    change_pct = ((current_price - entry) / entry * 100) if entry > 0 else 0.0
    arrow = "🟢" if change_pct >= 0 else "🔴"

    if change_pct >= config.TAKE_PROFIT_PERCENT:
        recommendation = f"🔔 *SELL - take profit* (target: {config.TAKE_PROFIT_PERCENT:+.0f}%)"
    elif change_pct <= config.STOP_LOSS_PERCENT:
        recommendation = f"🔔 *SELL - stop loss* (limit: {config.STOP_LOSS_PERCENT:+.0f}%)"
    else:
        recommendation = "⏳ Hold"

    return (
        f"{arrow} *{pos['symbol']}*\n"
        f"Entry: `${entry:.8f}` → Now: `${current_price:.8f}`\n"
        f"Change: `{change_pct:+.2f}%`\n"
        f"{recommendation}\n"
    )


def build_price_chart(symbol: str, price_history: list) -> io.BytesIO:
    times = [datetime.datetime.fromtimestamp(t) for t, _ in price_history]
    prices = [p for _, p in price_history]
    fig, ax = plt.subplots(figsize=(6, 3))
    ax.plot(times, prices, linewidth=2)
    ax.set_title(f"{symbol} price")
    ax.set_ylabel("Price (USD)")
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf


def format_activity_record(rec: dict) -> str:
    entry = rec["entry_price_usd"]
    peak = rec["peak_price_usd"]
    peak_time = datetime.datetime.fromtimestamp(rec["peak_timestamp"]).strftime("%H:%M") if rec.get("peak_timestamp") else "?"
    opened = datetime.datetime.fromtimestamp(rec["opened_at"]).strftime("%b %d, %H:%M") if rec.get("opened_at") else "?"
    lines = [f"*{rec['symbol']}* {'🟢 OPEN' if rec['is_open'] else '⚪ CLOSED'}"]
    lines.append(f"Bought: `{opened}` at `${entry:.8f}`")
    lines.append(f"Peak: `${peak:.8f}` (`{rec['peak_gain_pct']:+.1f}%`) at `{peak_time}`")
    if rec["is_open"]:
        lines.append("_Still open - check /positions_")
    else:
        lines.append(f"Sold: `${rec['exit_price_usd']:.8f}`")
        lines.append(f"Result: `{rec['pnl_sol']:+.4f} SOL` (`{rec['pnl_percent']:+.1f}%`)")
        if rec["peak_gain_pct"] > (rec["pnl_percent"] or 0) + 1:
            missed = rec["peak_gain_pct"] - (rec["pnl_percent"] or 0)
            lines.append(f"_If sold at peak: +{missed:.1f}% more_")
    return "\n".join(lines)


def read_live_discoveries(event_type: str, max_age_seconds: int = 3600) -> list:
    import json as _json
    import os as _os
    path = "live_discoveries.jsonl"
    if not _os.path.exists(path):
        return []
    now = time.time()
    results = []
    try:
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                rec = _json.loads(line)
                if rec.get("type") != event_type:
                    continue
                if now - rec.get("discovered_at", 0) > max_age_seconds:
                    continue
                results.append(rec)
    except (IOError, _json.JSONDecodeError):
        return []
    results.sort(key=lambda r: r.get("discovered_at", 0), reverse=True)
    return results


def get_raw_creation_record(mint: str) -> dict:
    import json as _json
    import os as _os
    def _search(path):
        if not _os.path.exists(path):
            return None
        try:
            with open(path) as f:
                for line in f:
                    if not line.strip():
                        continue
                    rec = _json.loads(line)
                    if rec.get("type") == "new_token" and rec.get("mint") == mint:
                        return rec
        except (IOError, _json.JSONDecodeError):
            return None
        return None
    return _search("launch_index.jsonl") or _search("live_discoveries.jsonl")


def format_minimal_card(mint: str, raw_record: dict, sol_price_usd: float, safety: dict = None, eval_result: dict = None) -> str:
    raw = raw_record.get("raw", {})
    name = raw.get("name") or raw_record.get("name") or "?"
    symbol = raw.get("symbol") or raw_record.get("symbol") or "?"
    lines = [f"*{name} ({symbol})*", f"`{mint}`", "", "_⚠️ Too new for DexScreener - showing creation data._", ""]
    if eval_result:
        e = eval_result
        lines.append(f"{e['verdict_emoji']} *Conviction: {e['score']:.0f}/100* -- `{e['verdict']}`")
        if e.get("bonding_progress") is not None:
            bar_filled = int(e["bonding_progress"] / 10)
            bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
            lines.append(f"📊 Bonding: {bar} `{e['bonding_progress']:.0f}%`")
        lines.append("")
    v_sol = raw.get("vSolInBondingCurve")
    v_tokens = raw.get("vTokensInBondingCurve")
    if v_sol and v_tokens and sol_price_usd > 0:
        launch_price = (v_sol / v_tokens) * sol_price_usd
        lines.append(f"Launch price: `${launch_price:.8f}`")
    if raw.get("marketCapSol") and sol_price_usd > 0:
        lines.append(f"MCap at creation: `${raw['marketCapSol'] * sol_price_usd:,.0f}`")
    if raw.get("solAmount"):
        lines.append(f"Initial buy: `{raw['solAmount']:.3f} SOL`")
    ago = int(time.time() - raw_record.get("discovered_at", time.time()))
    ago_str = f"{ago}s ago" if ago < 60 else format_age(ago // 60) + " ago"
    lines.append(f"Caught: `{ago_str}`")
    if safety:
        lines.append("")
        lines.append("*-- Contract --*")
        check_failed = not safety.get("is_safe") and "API check failed" in str(safety.get("reason", ""))
        contract_line = "✅ Safe" if safety.get("is_safe") else f"❌ UNSAFE ({safety.get('reason')})"
        lines.append(contract_line)
        if check_failed:
            lines.append("Renounced: ⚫ Unknown")
        else:
            lines.append(f"Renounced: {'✅' if not safety.get('mint_authority') else '❌ Mint active'}")
        if safety.get("freeze_authority"):
            lines.append("❌ Freeze active")
    lines.append("")
    lines.append("_This is a PAPER trade - no real funds involved._")
    return "\n".join(lines)


async def build_token_card(mint: str):
    info = get_token_info(mint)
    if not info["ok"]:
        raw_record = get_raw_creation_record(mint)
        if raw_record:
            sol_price = get_sol_usd_price()
            safety = check_safety(mint)
            eval_result = conviction.evaluate(mint, live_record=raw_record, safety=safety)
            card = format_minimal_card(mint, raw_record, sol_price, safety, eval_result)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 APE IN", callback_data=f"buy:{mint}:{raw_record.get('symbol', '?')}"),
                InlineKeyboardButton("🔄 Refresh", callback_data=f"cardrefresh:{mint}"),
            ]])
            return card, keyboard, None
        return None, None, info.get("error", "unknown error")

    safety = check_safety(mint)
    dev_rep = None
    if safety.get("creator"):
        dev_rep = get_deployer_reputation(safety["creator"], config.HELIUS_API_KEY)

    live_rec = get_raw_creation_record(mint)
    eval_result = conviction.evaluate(mint, live_record=live_rec, safety=safety, dev_rep=dev_rep, info=info)

    sol_price = get_sol_usd_price()
    launch = get_launch_price(mint, sol_price) if sol_price > 0 else {"ok": False}

    card = format_token_card(mint, info, safety, dev_rep, launch, eval_result)

    buttons = []
    if eval_result["score"] >= config.GOLDMINE_ALERT_THRESHOLD:
        buttons.append(InlineKeyboardButton(f"🚀 APE IN ({config.DEFAULT_BUY_SIZE_SOL} SOL)", callback_data=f"buy:{mint}:{info['symbol']}"))
    else:
        buttons.append(InlineKeyboardButton(f"Buy {config.DEFAULT_BUY_SIZE_SOL} SOL", callback_data=f"buy:{mint}:{info['symbol']}"))
    buttons.append(InlineKeyboardButton("🔄 Refresh", callback_data=f"cardrefresh:{mint}"))

    keyboard_rows = [buttons]
    link_row = []
    if info.get("twitter_url"):
        link_row.append(InlineKeyboardButton("𝕏", url=info["twitter_url"]))
    if info.get("telegram_url"):
        link_row.append(InlineKeyboardButton("Telegram", url=info["telegram_url"]))
    if info.get("website_url"):
        link_row.append(InlineKeyboardButton("Website", url=info["website_url"]))
    if link_row:
        keyboard_rows.append(link_row)

    return card, InlineKeyboardMarkup(keyboard_rows), None


# ==================== COMMAND HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    wallet.apply_daily_topup(user_id)
    balance = wallet.get_balance(user_id)
    await update.message.reply_text(
        "👋 *Solana Goldmine Bot* -- Paper + Real Trading\n\n"
        f"Fake balance: *{balance:.3f} SOL*\n"
        f"(Real wallet: `/wallet` to check)\n\n"
        "*How to use:*\n"
        "1. Run `python live_listener.py` in another terminal\n"
        "2. The bot alerts you when it finds a *goldmine*\n"
        "3. Tap *APE IN* to paper-buy, or /autopilot for auto-buy\n"
        "4. `/realbuy <CA>` to trade with REAL SOL\n"
        "5. Sell manually or let auto-exit handle TP/SL\n\n"
        "*Commands:*\n"
        "Send a CA -- full analysis + conviction score\n"
        "/wallet -- Link/check your real wallet\n"
        "/realbuy <CA> -- Buy with REAL SOL\n"
        "/realsell <CA> -- Sell for REAL SOL\n"
        "/realpositions -- Live real P&L + whales\n"
        "/premium -- Premium high-conviction signals\n"
        "/whales -- Whale activity on your tokens\n"
        "/bonded -- tokens IN bonding curve\n"
        "/graduated -- tokens that just LEFT the curve\n"
        "/autopilot -- toggle auto paper-buying ON/OFF\n"
        "/conviction <CA> -- check goldmine score\n"
        "/smartmoney -- view tracked dev wallets\n"
        "/addsmart <wallet> <tag> -- manually track a wallet\n"
        "/balance -- fake SOL balance\n"
        "/positions -- open paper trades with P&L\n"
        "/activity -- trade history with charts\n"
        "/stats -- win rate & total P&L\n"
        "/new -- new tokens\n"
        "/launching -- Raydium delayed pools\n"
        "/checktx <sig> -- debug transaction",
        parse_mode="Markdown",
    )


# --- NEW: WALLET COMMANDS ---

async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Phantom-linked wallet status."""
    text = phantom.get_status_text()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh Balance", callback_data="wallet_refresh"),
    ]])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def realbuy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buy a token with REAL SOL."""
    if not context.args:
        await update.message.reply_text(
            "Usage: `/realbuy <token_address> [sol_amount]`\n"
            f"Example: `/realbuy ABC123... 0.05` (default: {config.DEFAULT_BUY_SIZE_SOL} SOL)",
            parse_mode="Markdown",
        )
        return

    mint = context.args[0]
    if not MINT_PATTERN.match(mint):
        await update.message.reply_text("Invalid Solana address.")
        return

    sol_amount = float(context.args[1]) if len(context.args) > 1 else config.DEFAULT_BUY_SIZE_SOL

    if not phantom.is_ready():
        await update.message.reply_text(
            "❌ *Real wallet not ready*\n"
            f"Send at least `{config.MIN_SOL_RESERVE + 0.05:.3f} SOL` to:\n"
            f"`{phantom.public_key}`\n\n"
            "_Copy the address and send from Phantom._",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(f"🔴 Executing REAL buy of `{sol_amount} SOL`...", parse_mode="Markdown")

    info = get_token_info(mint)
    symbol = info.get("symbol", "?") if info.get("ok") else "?"

    result = real_trader.buy(mint, symbol, sol_amount)
    if not result["ok"]:
        await update.message.reply_text(f"❌ Buy failed: {result['error']}")
        return

    await update.message.reply_text(
        f"🟢 *REAL BUY EXECUTED*\n"
        f"\n"
        f"Token: *{symbol}*\n"
        f"Mint: `{mint}`\n"
        f"Spent: `{result['sol_spent']:.4f} SOL`\n"
        f"Received: `{result['tokens_received']:,.2f}` tokens\n"
        f"Entry: `${result['price_usd']:.8f}`\n"
        f"Tx: `{result['tx_signature'][:20]}...`\n"
        f"\n"
        f"_Auto TP: +{config.TAKE_PROFIT_PERCENT:.0f}% | Auto SL: {config.STOP_LOSS_PERCENT:.0f}%_",
        parse_mode="Markdown",
    )


async def realsell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sell a token for REAL SOL."""
    if not context.args:
        await update.message.reply_text("Usage: `/realsell <token_address>`", parse_mode="Markdown")
        return

    mint = context.args[0]
    if not MINT_PATTERN.match(mint):
        await update.message.reply_text("Invalid Solana address.")
        return

    if mint not in real_trader.positions:
        await update.message.reply_text("❌ No open real position in this token.")
        return

    await update.message.reply_text("🔴 Executing REAL sell...")

    result = real_trader.sell(mint)
    if not result["ok"]:
        await update.message.reply_text(f"❌ Sell failed: {result['error']}")
        return

    emoji = "🟢" if result["pnl_sol"] >= 0 else "🔴"
    await update.message.reply_text(
        f"{emoji} *REAL SELL EXECUTED*\n"
        f"\n"
        f"Token: *{result['symbol']}*\n"
        f"Entry: `${result['entry_price_usd']:.8f}`\n"
        f"Received: `{result['sol_received']:.4f} SOL`\n"
        f"P&L: `{result['pnl_sol']:+.4f} SOL` (`{result['pnl_percent']:+.2f}%`)\n"
        f"Tx: `{result['tx_signature'][:20]}...`",
        parse_mode="Markdown",
    )


async def realpositions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all real positions with live P&L and whale activity."""
    if not real_trader.positions:
        await update.message.reply_text("🔴 No real positions open.")
        return

    await update.message.reply_text(f"🔴 Fetching live data for {len(real_trader.positions)} real position(s)...")

    for mint, pos in real_trader.positions.items():
        try:
            snapshot = await tracker.get_position_snapshot(mint, pos)
            if snapshot:
                text = tracker.format_position_update(snapshot, is_real=True)
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔴 SELL REAL", callback_data=f"realsell:{mint}")]])
                await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
            else:
                await update.message.reply_text(f"{pos['symbol']}: Could not fetch live data.")
        except Exception as e:
            logger.warning(f"Real position update failed for {mint}: {e}")
            await update.message.reply_text(f"Error updating {pos['symbol']}: {e}")


async def whales_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show whale activity for all held tokens."""
    user_id = str(update.effective_user.id)
    paper_positions = wallet.get_open_positions(user_id)
    all_positions = {**paper_positions}
    all_positions.update(real_trader.positions)

    if not all_positions:
        await update.message.reply_text("No open positions to track whales.")
        return

    await update.message.reply_text("🐋 Scanning whale activity...")

    for mint, pos in all_positions.items():
        try:
            info = get_token_info(mint)
            if not info.get("ok"):
                continue
            current_price = info["price_usd"]

            transfers = await tracker.fetch_token_transfers(mint, limit=30)
            activities = tracker.detect_whale_activity(mint, transfers, current_price)

            if activities:
                sentiment = tracker.analyze_whale_sentiment(activities)
                lines = [
                    f"🐋 *Whale Activity -- {pos['symbol']}*",
                    f"",
                    f"{sentiment['emoji']} *{sentiment['sentiment']}*",
                    f"Net flow: `${sentiment['net_flow_usd']:+.0f}`",
                    f"Buys: {sentiment['count_buy']} (${sentiment['total_buy_usd']:,.0f})",
                    f"Sells: {sentiment['count_sell']} (${sentiment['total_sell_usd']:,.0f})",
                    f"",
                ]
                for w in activities[:5]:
                    action_emoji = "🟢" if w.action == "BUY" else "🔴"
                    short = f"{w.wallet[:6]}...{w.wallet[-4:]}"
                    lines.append(f"{action_emoji} `{short}` -- `${w.amount_usd:,.0f}`")
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            else:
                await update.message.reply_text(f"🐋 {pos['symbol']}: No whale activity in last 30 txs.")
        except Exception as e:
            logger.warning(f"Whale scan failed for {mint}: {e}")


async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show premium high-conviction signals."""
    await update.message.reply_text("⚡️ Scanning for premium signals...")

    signals = premium_engine.scan_for_premium_signals(max_lines=300)
    if not signals:
        stats = premium_engine.get_stats()
        await update.message.reply_text(
            f"⚡️ *No premium signals right now.*\n"
            f"\n"
            f"_Premium filters: Score ≥{config.PREMIUM_SIGNAL_THRESHOLD}, Liq ≥${config.PREMIUM_MIN_LIQUIDITY_USD:,.0f}, Age <{config.PREMIUM_MAX_AGE_MINUTES}m_\n"
            f"\n"
            f"📊 *Premium Stats*\n"
            f"Total signals: {stats['total_signals']}\n"
            f"Wins: {stats['wins']} | Losses: {stats['losses']}\n"
            f"Win rate: {stats['win_rate']:.1f}%",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(f"⚡️ *{len(signals)} PREMIUM SIGNAL(S) FOUND*", parse_mode="Markdown")

    for signal in signals[:5]:
        text = premium_engine.format_premium_alert(signal)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 APE IN (Paper)", callback_data=f"buy:{signal.mint}:{signal.symbol}"),
            InlineKeyboardButton("🔴 REAL BUY", callback_data=f"realbuy:{signal.mint}:{signal.symbol}"),
        ]])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def premium_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show premium signal performance stats."""
    stats = premium_engine.get_stats()
    await update.message.reply_text(
        f"⚡️ *Premium Signal Stats*\n"
        f"\n"
        f"Total signals: `{stats['total_signals']}`\n"
        f"Wins: `{stats['wins']}` | Losses: `{stats['losses']}` | Pending: `{stats['pending']}`\n"
        f"Win rate: `{stats['win_rate']:.1f}%`\n"
        f"Avg score: `{stats['avg_score']:.1f}/100`\n"
        f"\n"
        f"_Premium = Score ≥{config.PREMIUM_SIGNAL_THRESHOLD}, Liq ≥${config.PREMIUM_MIN_LIQUIDITY_USD:,.0f}, Age <{config.PREMIUM_MAX_AGE_MINUTES}m_",
        parse_mode="Markdown",
    )


# --- EXISTING COMMANDS (kept from original) ---

async def bonded_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    live = read_live_discoveries("new_token", max_age_seconds=1800)
    if not live:
        await update.message.reply_text(
            "No live listener data. Run `python live_listener.py` first.\n"
            "Bonded tokens are caught at creation -- this is the earliest possible stage."
        )
        return

    bonded = [r for r in live if r.get("bonding_progress") is not None and r["bonding_progress"] < 100]
    if not bonded:
        await update.message.reply_text("No bonded tokens found in the last 30 min. Either everything graduated or the listener isn't capturing bonding data.")
        return

    bonded.sort(key=lambda x: x.get("bonding_progress", 0), reverse=True)
    shown = bonded[:10]
    header = (
        f"🔗 *Bonded Tokens ({len(bonded)} found)*\n"
        f"_Still in pump.fun bonding curve -- earliest stage before graduation_\n\n"
        f"Showing top {len(shown)} sorted by graduation progress (highest first):"
    )
    await update.message.reply_text(header, parse_mode="Markdown")

    for rec in shown:
        mint = rec.get("mint")
        if not mint:
            continue
        try:
            card, keyboard, error = await build_token_card(mint)
            if error:
                symbol = rec.get("symbol", "?")
                name = rec.get("name", "?")
                progress = rec.get("bonding_progress", 0)
                bar_filled = int(progress / 10)
                bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
                init_buy = rec.get("initial_buy_sol")
                is_whale = rec.get("is_whale_launch", False)
                ago = int(time.time() - rec.get("discovered_at", time.time()))
                ago_str = f"{ago}s ago" if ago < 60 else format_age(ago // 60) + " ago"

                lines = [
                    f"*{name} ({symbol})*",
                    f"`{mint}`",
                    f"📊 Bonding: {bar} `{progress:.0f}%`",
                ]
                if init_buy:
                    whale_tag = " 🐋" if is_whale else ""
                    lines.append(f"💰 Initial buy: `{init_buy:.2f} SOL`{whale_tag}")
                lines.append(f"⏱️ Caught: `{ago_str}`")
                lines.append("")
                lines.append("_Too new for full analysis -- tap Refresh in a minute._")

                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚀 APE IN", callback_data=f"buy:{mint}:{symbol}"),
                    InlineKeyboardButton("🔄 Refresh", callback_data=f"cardrefresh:{mint}"),
                ]])
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
                continue

            await update.message.reply_text(card, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            logger.warning(f"Failed bonded card for {mint}: {e}")


async def graduated_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    live = read_live_discoveries("migration")
    if live:
        shown = live[:8]
        header = f"🎓 *{len(shown)} graduation(s) caught live*"
        await update.message.reply_text(header, parse_mode="Markdown")
        for mig in shown:
            try:
                mint = mig.get("mint")
                if not mint:
                    continue
                card, keyboard, error = await build_token_card(mint)
                if error:
                    continue
                ago = format_age(int((time.time() - mig["discovered_at"]) // 60))
                card = f"🎓 *Graduated ~{ago} ago (live)*\n\n" + card
                await update.message.reply_text(card, parse_mode="Markdown", reply_markup=keyboard)
            except Exception as e:
                logger.warning(f"Failed graduation card: {e}")
        return

    await update.message.reply_text(
        "No live migration data yet. Falling back to on-demand Helius scan...\n"
        "🎓 Scanning for recent pump.fun → PumpSwap graduations... (20-30s)",
    )

    result = get_recent_migrations(config.HELIUS_API_KEY, config.SOLANA_RPC_URL, limit=8, lookback=config.LAUNCHING_LOOKBACK)
    if not result["ok"]:
        await update.message.reply_text(f"Scan failed: {result['error']}")
        return
    if not result["tokens"]:
        await update.message.reply_text(result.get("note") or "No graduations found in this window.")
        return

    for mig in result["tokens"]:
        ago = format_age(mig["seconds_ago"] // 60) if mig.get("seconds_ago") and mig["seconds_ago"] >= 60 else f"{mig.get('seconds_ago', '?')}s"
        lines = [f"🎓 *Graduated {ago} ago*"]
        if mig.get("mint") and mig.get("mint_confirmed"):
            lines.append(f"Mint: `{mig['mint']}`")
            safety = check_safety(mig["mint"])
            if safety.get("mint_authority"):
                lines.append("⚠️ Mint authority active")
            if safety.get("freeze_authority"):
                lines.append("⚠️ Freeze authority active")
            if not safety.get("mint_authority") and not safety.get("freeze_authority"):
                lines.append("✅ No mint/freeze red flags")
        elif mig.get("mint"):
            lines.append(f"Mint: `{mig['mint']}` ⚠️ unconfirmed")
        else:
            lines.append("Mint: unknown")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def smartmoney_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not smart_money.wallets:
        await update.message.reply_text(
            "📭 *Smart Money Tracker* -- No wallets tracked yet.\n\n"
            "The bot auto-tracks dev wallets from your winning trades.\n"
            "You can also manually add wallets with `/addsmart <wallet> <tag>`",
            parse_mode="Markdown",
        )
        return

    lines = ["🏆 *Smart Money Tracker*", ""]
    for wallet_addr, data in sorted(smart_money.wallets.items(), key=lambda x: x[1]["wins"], reverse=True)[:15]:
        total = data["wins"] + data["losses"]
        wr = (data["wins"] / total * 100) if total > 0 else 0
        tags = ", ".join(data.get("tags", []))
        short = f"{wallet_addr[:6]}...{wallet_addr[-6:]}"
        lines.append(f"`{short}` -- {data['wins']}W/{data['losses']}L ({wr:.0f}%) -- `{tags}`")

    lines.append("")
    lines.append(f"_Total tracked: {len(smart_money.wallets)} wallets_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def addsmart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: `/addsmart <wallet_address> [tag]`\n"
            "Example: `/addsmart ABC123... whale_dev`",
            parse_mode="Markdown",
        )
        return

    wallet_addr = context.args[0]
    tag = context.args[1] if len(context.args) > 1 else "watched"
    smart_money.add_manual_wallet(wallet_addr, tag)
    await update.message.reply_text(
        f"✅ Added `{wallet_addr[:10]}...` with tag `{tag}`",
        parse_mode="Markdown",
    )


async def autopilot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    current = autopilot_states.get(user_id, False)
    autopilot_states[user_id] = not current
    status = "🟢 ON" if autopilot_states[user_id] else "🔴 OFF"
    await update.message.reply_text(
        f"🤖 *Autopilot: {status}*\n\n"
        f"Bot will auto-buy any token scoring ≥{config.AUTO_BUY_THRESHOLD}/100 in paper mode.\n"
        f"Risk limits: max {risk_mgr.max_concurrent_positions} positions, "
        f"max ${risk_mgr.max_exposure_per_deployer} per deployer.\n\n"
        "⚠️ *Still paper trading.* No real money at risk.",
        parse_mode="Markdown",
    )


async def conviction_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /conviction <token_address>")
        return
    mint = context.args[0]
    if not MINT_PATTERN.match(mint):
        await update.message.reply_text("Invalid Solana address.")
        return
    await update.message.reply_text(f"🔎 Analyzing `{mint}`...", parse_mode="Markdown")
    try:
        result = conviction.evaluate(mint)
        card = conviction.format_card(result)
        await update.message.reply_text(card, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Conviction check failed: {e}")
        await update.message.reply_text(f"Analysis failed: {e}")


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    wallet.apply_daily_topup(user_id)
    balance = wallet.get_balance(user_id)
    await update.message.reply_text(f"💰 Paper Balance: *{balance:.3f} SOL*", parse_mode="Markdown")


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    positions = wallet.get_open_positions(user_id)
    if not positions:
        await update.message.reply_text("No open paper positions.")
        return
    for mint, pos in positions.items():
        info = get_token_info(mint)
        if not info["ok"]:
            await update.message.reply_text(f"{pos['symbol']}: price lookup failed")
            continue
        text = format_position_update(mint, pos, info["price_usd"])
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Sell Paper", callback_data=f"sell:{mint}")]])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    s = wallet.get_stats(user_id)
    await update.message.reply_text(
        f"📊 *Paper Stats*\n"
        f"Trades: {s['total_trades']} | Wins: {s['wins']} | Losses: {s['losses']}\n"
        f"Win rate: {s['win_rate']:.1f}%\n"
        f"Total P&L: {s['total_pnl_sol']:+.4f} SOL",
        parse_mode="Markdown",
    )


async def activity_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    records = wallet.get_activity_records(user_id)
    if not records:
        await update.message.reply_text("No activity yet.")
        return
    for rec in records[:10]:
        text = format_activity_record(rec)
        await update.message.reply_text(text, parse_mode="Markdown")
        if len(rec.get("price_history", [])) >= 2:
            try:
                chart = build_price_chart(rec["symbol"], rec["price_history"])
                await update.message.reply_photo(photo=chart)
            except Exception as e:
                logger.warning(f"Chart failed: {e}")


async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    live_new = read_live_discoveries("new_token", max_age_seconds=600)
    if live_new:
        shown = live_new[:8]
        await update.message.reply_text(f"🆕 *{len(shown)} brand-new token(s) caught live*", parse_mode="Markdown")
        for t in shown:
            try:
                card, keyboard, error = await build_token_card(t["mint"])
                if error:
                    continue
                await update.message.reply_text(card, parse_mode="Markdown", reply_markup=keyboard)
            except Exception as e:
                logger.warning(f"Failed new-token card: {e}")
    else:
        await update.message.reply_text("No live listener data. Falling back to on-demand scan...")

    if not live_new:
        latest = get_latest_new_tokens(config.SOLANATRACKER_API_KEY, limit=8)
        if latest["ok"] and latest["tokens"]:
            window = f"last {latest['window_used_minutes']} min" if latest.get('window_used_minutes') else "age unknown"
            lines = [f"🆕 *New Tokens ({window})*", ""]
            if latest.get("note"):
                lines.append(f"_{latest['note']}_")
                lines.append("")
            for t in latest["tokens"]:
                age_str = format_age(int(t["age_minutes"])) if t.get("age_minutes") is not None else "unknown"
                lines.append(f"*{t['symbol']}* - Age: `{age_str}`")
                lines.append(f"`{t['mint']}`")
                lines.append("")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        else:
            await update.message.reply_text(f"Feed unavailable ({latest.get('error', 'no results')}).")

    boosted = get_boosted_solana_tokens(limit=5)
    if boosted["ok"] and boosted["tokens"]:
        lines = ["📢 *Promoted (paid placement)*", ""]
        for t in boosted["tokens"]:
            desc = (t["description"][:60] + "...") if len(t.get("description", "")) > 60 else t.get("description", "")
            lines.append(f"`{t['mint']}`")
            if desc:
                lines.append(f"_{desc}_")
            lines.append("")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    graduating = get_graduating_coins(api_key=config.SOLANATRACKER_API_KEY, limit=8)
    if graduating["ok"] and graduating["tokens"]:
        lines = ["⏳ *Nearing Graduation*", ""]
        for t in graduating["tokens"]:
            bar_filled = int(t["graduation_progress_pct"] / 10)
            bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
            age_str = format_age(t["age_minutes"]) if t.get("age_minutes") is not None else "unknown"
            lines.append(f"*{t['symbol']}* - `${t['mcap_usd']:,.0f}` mcap · Age: `{age_str}`")
            lines.append(f"{bar} `{t['graduation_progress_pct']:.0f}%` to PumpSwap")
            lines.append(f"`{t['mint']}`")
            lines.append("")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def checktx_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /checktx <signature>")
        return
    signature = context.args[0]
    await update.message.reply_text(f"🔎 Checking `{signature}`...", parse_mode="Markdown")
    result = debug_check_transaction(signature, config.HELIUS_API_KEY)
    if not result["ok"]:
        await update.message.reply_text(f"Couldn't fetch: {result['error']}")
        return
    if result["matching_instructions_found"] == 0:
        await update.message.reply_text("No CPMM or pump.fun instructions found.")
        return
    lines = [f"Tx type: `{result['tx_type']}`", f"Matches: {result['matching_instructions_found']}", ""]
    for i, f in enumerate(result["findings"], 1):
        lines.append(f"*Instruction {i} ({f.get('program', '?')}):*")
        lines.append(f"  Discriminator: `{f.get('discriminator_hex', 'n/a')}`")
        lines.append(f"  Matched: `{f.get('matched_instruction', 'n/a')}`")
        if "decoded_open_time_readable" in f:
            lines.append(f"  Open time: `{f['decoded_open_time_readable']}`")
        if "extracted_mint" in f:
            lines.append(f"  Mint: `{f.get('extracted_mint', 'n/a')}`")
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def launching_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = get_upcoming_pool_launches(config.HELIUS_API_KEY, config.SOLANA_RPC_URL, limit=8, lookback=config.LAUNCHING_LOOKBACK)
    if not result["ok"]:
        await update.message.reply_text(f"Scan failed: {result['error']}")
        return
    if not result["tokens"]:
        await update.message.reply_text(result.get("note") or "No upcoming launches found.")
        return
    for pool in result["tokens"]:
        secs = pool["seconds_until_open"]
        countdown = format_age(secs // 60) if secs >= 60 else f"{secs}s"
        lines = [f"⏰ *Opens in ~{countdown}*", f"Mint: `{pool['mint'] or 'unknown'}`", f"Pool: `{pool['pool_address'] or 'unknown'}`"]
        if pool.get("creator"):
            dev_rep = get_deployer_reputation(pool["creator"], config.HELIUS_API_KEY)
            if dev_rep.get("ok"):
                if dev_rep["is_brand_new_wallet"]:
                    lines.append("👤 Dev: brand new wallet")
                else:
                    lines.append(f"👤 Dev: {dev_rep['txn_count_sampled']} txns, ~{dev_rep['likely_tokens_created']} creations")
        if pool.get("mint"):
            safety = check_safety(pool["mint"])
            if safety.get("mint_authority"):
                lines.append("⚠️ Mint authority active")
            if safety.get("freeze_authority"):
                lines.append("⚠️ Freeze authority active")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def analyse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    replied = update.message.reply_to_message
    if not replied or not replied.text:
        await update.message.reply_text("Reply to a message containing a token address with /analyse.")
        return
    match = MINT_SEARCH_PATTERN.search(replied.text)
    if not match:
        await update.message.reply_text("No token address found in that message.")
        return
    mint = match.group(0)
    await update.message.reply_text(f"🔎 Analysing `{mint}`...", parse_mode="Markdown")
    card, keyboard, error = await build_token_card(mint)
    if error:
        await update.message.reply_text(f"Couldn't analyse: {error}")
        return
    await update.message.reply_text(card, parse_mode="Markdown", reply_markup=keyboard)


async def handle_ca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not MINT_PATTERN.match(text):
        await update.message.reply_text("That doesn't look like a token address. Send a valid Solana CA.")
        return
    mint = text
    await update.message.reply_text("🔎 Checking token + calculating conviction...")
    card, keyboard, error = await build_token_card(mint)
    if error:
        await update.message.reply_text(f"Couldn't find data: {error}")
        return
    await update.message.reply_text(card, parse_mode="Markdown", reply_markup=keyboard)


# ==================== CALLBACK HANDLERS ====================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    data = query.data

    if data == "wallet_refresh":
        text = phantom.get_status_text()
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh Balance", callback_data="wallet_refresh"),
        ]])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    if data.startswith("cardrefresh:"):
        _, mint = data.split(":", 1)
        card, keyboard, error = await build_token_card(mint)
        if error:
            await query.answer(f"Refresh failed: {error}", show_alert=True)
            return
        await query.edit_message_text(card, parse_mode="Markdown", reply_markup=keyboard)
        return

    if data.startswith("buy:"):
        _, mint, symbol = data.split(":", 2)
        info = get_token_info(mint)
        sol_price = get_sol_usd_price()
        if not info["ok"] or sol_price <= 0:
            await query.message.reply_text("Price data unavailable, try again shortly.")
            return
        result = wallet.buy(
            user_id, mint, symbol,
            sol_amount=config.DEFAULT_BUY_SIZE_SOL,
            token_price_usd=info["price_usd"],
            sol_price_usd=sol_price,
        )
        if not result["ok"]:
            await query.message.reply_text(f"❌ {result['error']}")
            return
        await query.message.reply_text(
            f"✅ Bought *{symbol}* with {config.DEFAULT_BUY_SIZE_SOL} SOL\n"
            f"Entry: `${info['price_usd']:.8f}`\n"
            f"New paper balance: {wallet.get_balance(user_id):.3f} SOL",
            parse_mode="Markdown",
        )

    elif data.startswith("realbuy:"):
        _, mint, symbol = data.split(":", 2)
        if not phantom.is_ready():
            await query.message.reply_text(
                "❌ *Real wallet not funded*\n"
                f"Send SOL to: `{phantom.public_key}`",
                parse_mode="Markdown",
            )
            return
        result = real_trader.buy(mint, symbol, config.DEFAULT_BUY_SIZE_SOL)
        if not result["ok"]:
            await query.message.reply_text(f"❌ Real buy failed: {result['error']}")
            return
        await query.message.reply_text(
            f"🔴 *REAL BUY EXECUTED*\n"
            f"{symbol} -- `{result['sol_spent']:.4f} SOL` → `{result['tokens_received']:,.0f}` tokens",
            parse_mode="Markdown",
        )

    elif data.startswith("sell:"):
        _, mint = data.split(":", 1)
        info = get_token_info(mint)
        sol_price = get_sol_usd_price()
        if not info["ok"] or sol_price <= 0:
            await query.message.reply_text("Price data unavailable, try again shortly.")
            return
        result = wallet.sell(user_id, mint, info["price_usd"], sol_price)
        if not result["ok"]:
            await query.message.reply_text(f"❌ {result['error']}")
            return

        safety = check_safety(mint)
        dev_wallet = safety.get("creator")
        if dev_wallet:
            smart_money.record_trade(dev_wallet, result["symbol"], result["pnl_sol"], mint)

        emoji = "🟢" if result["pnl_sol"] >= 0 else "🔴"
        await query.message.reply_text(
            f"{emoji} Sold *{result['symbol']}*\n"
            f"Entry: `${result['entry_price_usd']:.8f}` → Exit: `${result['exit_price_usd']:.8f}`\n"
            f"P&L: `{result['pnl_sol']:+.4f} SOL` (`{result['pnl_percent']:+.2f}%`)\n"
            f"New paper balance: {wallet.get_balance(user_id):.3f} SOL",
            parse_mode="Markdown",
        )

    elif data.startswith("realsell:"):
        _, mint = data.split(":", 1)
        result = real_trader.sell(mint)
        if not result["ok"]:
            await query.message.reply_text(f"❌ Real sell failed: {result['error']}")
            return
        emoji = "🟢" if result["pnl_sol"] >= 0 else "🔴"
        await query.message.reply_text(
            f"{emoji} *REAL SELL EXECUTED*\n"
            f"{result['symbol']} -- P&L: `{result['pnl_sol']:+.4f} SOL` (`{result['pnl_percent']:+.2f}%`)",
            parse_mode="Markdown",
        )


# ==================== BACKGROUND JOBS ====================

async def push_position_updates(context: ContextTypes.DEFAULT_TYPE):
    """Push live position updates with full details including whales."""
    sol_price = get_sol_usd_price()
    if sol_price <= 0:
        return

    # Paper positions
    for user_id, user_data in wallet.users.items():
        positions = user_data.get("open_positions", {})
        if not positions:
            continue
        for mint, pos in positions.items():
            try:
                snapshot = await tracker.get_position_snapshot(mint, pos)
                if snapshot:
                    text = tracker.format_position_update(snapshot, is_real=False)
                    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Sell Paper", callback_data=f"sell:{mint}")]])
                    await context.bot.send_message(
                        chat_id=int(user_id), text=text, parse_mode="Markdown", reply_markup=keyboard
                    )
                else:
                    info = get_token_info(mint)
                    if info["ok"]:
                        wallet.record_price_snapshot(user_id, mint, info["price_usd"])
                        text = format_position_update(mint, pos, info["price_usd"])
                        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Sell Paper", callback_data=f"sell:{mint}")]])
                        await context.bot.send_message(
                            chat_id=int(user_id), text=text, parse_mode="Markdown", reply_markup=keyboard
                        )
            except Exception as e:
                logger.warning(f"Failed push update to {user_id}: {e}")

    # Real positions
    for mint, pos in real_trader.positions.items():
        try:
            snapshot = await tracker.get_position_snapshot(mint, pos)
            if snapshot:
                text = tracker.format_position_update(snapshot, is_real=True)
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔴 SELL REAL", callback_data=f"realsell:{mint}")]])
                for user_id in wallet.users.keys():
                    try:
                        await context.bot.send_message(
                            chat_id=int(user_id), text=text, parse_mode="Markdown", reply_markup=keyboard
                        )
                    except Exception as e:
                        logger.warning(f"Failed real push to {user_id}: {e}")
        except Exception as e:
            logger.warning(f"Failed real position update for {mint}: {e}")


async def auto_exit_checker(context: ContextTypes.DEFAULT_TYPE):
    """Auto TP/SL for both paper and real positions."""
    sol_price = get_sol_usd_price()
    if sol_price <= 0:
        return

    # Paper auto-exit
    for user_id, user_data in list(wallet.users.items()):
        positions = user_data.get("open_positions", {})
        if not positions:
            continue
        for mint, pos in list(positions.items()):
            info = get_token_info(mint)
            if not info["ok"]:
                continue
            entry = pos["entry_price_usd"]
            current = info["price_usd"]
            change = (current - entry) / entry * 100 if entry > 0 else 0
            if change >= config.TAKE_PROFIT_PERCENT:
                result = wallet.sell(user_id, mint, current, sol_price)
                if result["ok"]:
                    safety = check_safety(mint)
                    dev_wallet = safety.get("creator")
                    if dev_wallet:
                        smart_money.record_trade(dev_wallet, result["symbol"], result["pnl_sol"], mint)
                    await context.bot.send_message(
                        chat_id=int(user_id),
                        text=(f"🎯 *AUTO TAKE PROFIT* -- {result['symbol']}\n"
                              f"Sold at `{change:+.1f}%` (+{config.TAKE_PROFIT_PERCENT}%)\n"
                              f"P&L: `{result['pnl_sol']:+.4f} SOL`"),
                        parse_mode="Markdown",
                    )
            elif change <= config.STOP_LOSS_PERCENT:
                result = wallet.sell(user_id, mint, current, sol_price)
                if result["ok"]:
                    safety = check_safety(mint)
                    dev_wallet = safety.get("creator")
                    if dev_wallet:
                        smart_money.record_trade(dev_wallet, result["symbol"], result["pnl_sol"], mint)
                    await context.bot.send_message(
                        chat_id=int(user_id),
                        text=(f"🛑 *AUTO STOP LOSS* -- {result['symbol']}\n"
                              f"Sold at `{change:+.1f}%` ({config.STOP_LOSS_PERCENT}%)\n"
                              f"P&L: `{result['pnl_sol']:+.4f} SOL`"),
                        parse_mode="Markdown",
                    )

    # Real auto-exit
    for mint, pos in list(real_trader.positions.items()):
        info = get_token_info(mint)
        if not info["ok"]:
            continue
        exit_type = real_trader.check_auto_exit(mint, info["price_usd"])
        if exit_type:
            result = real_trader.sell(mint)
            if result["ok"]:
                emoji = "🎯" if exit_type == "tp" else "🛑"
                label = "AUTO TAKE PROFIT" if exit_type == "tp" else "AUTO STOP LOSS"
                for user_id in wallet.users.keys():
                    try:
                        await context.bot.send_message(
                            chat_id=int(user_id),
                            text=(f"{emoji} *{label} (REAL)* -- {result['symbol']}\n"
                                  f"P&L: `{result['pnl_sol']:+.4f} SOL` (`{result['pnl_percent']:+.2f}%`)\n"
                                  f"Tx: `{result['tx_signature'][:20]}...`"),
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning(f"Failed real auto-exit alert to {user_id}: {e}")


async def goldmine_scanner(context: ContextTypes.DEFAULT_TYPE):
    """Scan for goldmines and premium signals."""
    goldmines = auto_trader.scan_for_goldmines(max_lines=200)
    if not goldmines:
        return
    for eval_result in goldmines:
        score = eval_result["score"]
        mint = eval_result["mint"]
        for user_id in list(wallet.users.keys()):
            try:
                alert_lines = [
                    f"🚨 *GOLDMINE DETECTED* 🚨",
                    f"",
                    f"{eval_result['verdict_emoji']} *{eval_result['verdict']}* -- Score: `{score:.0f}/100`",
                    f"`{mint}`",
                    f"",
                ]
                for cat, notes in eval_result["notes"].items():
                    for note in notes:
                        if any(x in note for x in ["🎯", "❌", "🐋", "🔴", "🟠", "🎭"]):
                            alert_lines.append(note)
                if eval_result.get("bonding_progress") is not None:
                    bar_filled = int(eval_result["bonding_progress"] / 10)
                    bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
                    alert_lines.append(f"📊 Bonding: {bar} `{eval_result['bonding_progress']:.0f}%`")
                info = eval_result.get("info", {})
                if info and info.get("ok"):
                    alert_lines.append(f"💰 Price: `${info['price_usd']:.8f}` | Liq: `${info['liquidity_usd']:,.0f}`")
                alert_lines.append("")
                alert_lines.append("_Paper trading only. Tap APE IN to buy with fake SOL._")
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚀 APE IN", callback_data=f"buy:{mint}:{info.get('symbol', '?') if info else '?'}")
                ]])
                await context.bot.send_message(
                    chat_id=int(user_id),
                    text="\n".join(alert_lines),
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
                if autopilot_states.get(user_id, False) and score >= config.AUTO_BUY_THRESHOLD:
                    buy_result = auto_trader.attempt_paper_buy(user_id, eval_result)
                    if buy_result["ok"]:
                        await context.bot.send_message(
                            chat_id=int(user_id),
                            text=(f"🤖 *AUTO-PILOT BUY* -- {info.get('symbol', '?') if info else '?'}\n"
                                  f"Score: `{score:.0f}/100` ≥ {config.AUTO_BUY_THRESHOLD} threshold\n"
                                  f"Spent: `{config.DEFAULT_BUY_SIZE_SOL} SOL` (paper)"),
                            parse_mode="Markdown",
                        )
            except Exception as e:
                logger.warning(f"Goldmine alert failed for user {user_id}: {e}")


async def premium_scanner(context: ContextTypes.DEFAULT_TYPE):
    """Background job to scan for premium signals and alert users."""
    signals = premium_engine.scan_for_premium_signals(max_lines=300)
    if not signals:
        return
    for signal in signals:
        for user_id in list(wallet.users.keys()):
            try:
                text = premium_engine.format_premium_alert(signal)
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚀 APE IN (Paper)", callback_data=f"buy:{signal.mint}:{signal.symbol}"),
                    InlineKeyboardButton("🔴 REAL BUY", callback_data=f"realbuy:{signal.mint}:{signal.symbol}"),
                ]])
                await context.bot.send_message(
                    chat_id=int(user_id),
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.warning(f"Premium alert failed for user {user_id}: {e}")


async def early_stage_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Alert on brand new coins (< 60s old) with conviction score."""
    live = read_live_discoveries("new_token", max_age_seconds=config.EARLY_STAGE_ALERT_SECONDS)
    if not live:
        return
    for rec in live:
        mint = rec.get("mint")
        if not mint:
            continue
        try:
            eval_result = conviction.evaluate(mint, live_record=rec)
            if eval_result["score"] >= config.GOLDMINE_ALERT_THRESHOLD:
                for user_id in list(wallet.users.keys()):
                    try:
                        lines = [
                            f"⚡ *EARLY STAGE ALERT*",
                            f"",
                            f"{eval_result['verdict_emoji']} *{eval_result['verdict']}* -- `{eval_result['score']:.0f}/100`",
                            f"`{mint}`",
                            f"",
                        ]
                        if eval_result.get("bonding_progress") is not None:
                            bar_filled = int(eval_result["bonding_progress"] / 10)
                            bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
                            lines.append(f"📊 Bonding: {bar} `{eval_result['bonding_progress']:.0f}%`")
                        if eval_result.get("initial_buy_sol"):
                            lines.append(f"💰 Initial buy: `{eval_result['initial_buy_sol']:.2f} SOL`")
                        lines.append("")
                        lines.append("_Just launched! High risk, high reward._")
                        keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton("🚀 APE IN", callback_data=f"buy:{mint}:{rec.get('symbol', '?')}"),
                            InlineKeyboardButton("🔴 REAL BUY", callback_data=f"realbuy:{mint}:{rec.get('symbol', '?')}"),
                        ]])
                        await context.bot.send_message(
                            chat_id=int(user_id),
                            text="\n".join(lines),
                            parse_mode="Markdown",
                            reply_markup=keyboard,
                        )
                    except Exception as e:
                        logger.warning(f"Early stage alert failed for user {user_id}: {e}")
        except Exception as e:
            logger.warning(f"Early stage eval failed for {mint}: {e}")


async def daily_topup_job(context: ContextTypes.DEFAULT_TYPE):
    for user_id in list(wallet.users.keys()):
        wallet.apply_daily_topup(user_id)


def start_keepalive_server():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import os
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Solana goldmine bot is alive")
        def log_message(self, format, *args):
            pass
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Keep-alive HTTP server on port {port}")


async def start_live_listener_task(application):
    asyncio.create_task(live_listener.listen())
    logger.info("Started PumpPortal live listener as background task.")


def main():
    start_keepalive_server()
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(start_live_listener_task).build()

    # Core commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("realbuy", realbuy_cmd))
    app.add_handler(CommandHandler("realsell", realsell_cmd))
    app.add_handler(CommandHandler("realpositions", realpositions_cmd))
    app.add_handler(CommandHandler("whales", whales_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(CommandHandler("premiumstats", premium_stats_cmd))

    # Existing commands
    app.add_handler(CommandHandler("bonded", bonded_cmd))
    app.add_handler(CommandHandler("graduated", graduated_cmd))
    app.add_handler(CommandHandler("autopilot", autopilot_cmd))
    app.add_handler(CommandHandler("conviction", conviction_cmd))
    app.add_handler(CommandHandler("smartmoney", smartmoney_cmd))
    app.add_handler(CommandHandler("addsmart", addsmart_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("positions", positions_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("activity", activity_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("launching", launching_cmd))
    app.add_handler(CommandHandler("checktx", checktx_cmd))
    app.add_handler(CommandHandler(["analyse", "analysis"], analyse_cmd))

    # Callbacks & messages
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ca))

    # Background jobs
    job_queue = app.job_queue
    job_queue.run_repeating(push_position_updates, interval=config.POSITION_AUTO_REFRESH_SECONDS, first=30)
    job_queue.run_repeating(auto_exit_checker, interval=60, first=60)
    job_queue.run_repeating(goldmine_scanner, interval=30, first=15)
    job_queue.run_repeating(premium_scanner, interval=45, first=20)
    job_queue.run_repeating(early_stage_alerts, interval=30, first=10)
    job_queue.run_repeating(daily_topup_job, interval=3600, first=10)

    logger.info("Goldmine bot starting with REAL trading support...")
    app.run_polling()


if __name__ == "__main__":
    main()
