"""
telegram_bot.py - FIXED VERSION v2
- Dual buttons (Paper + Real) on EVERY token card
- Fast /premium (no hanging)
- /holdings shows real wallet tokens + paper positions
- Buy/sell counts on all cards
- Non-overlapping background jobs
"""

import re
import io
import time
import asyncio
import datetime
import logging
import os
import json

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
from async_price_feed import get_token_info_async, get_sol_usd_price_async, batch_get_token_info
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
from premium_signals import premium_engine, PremiumSignal
from position_tracker import tracker
from whale_labeler import labeler
from trade_estimator import estimator
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

_pump_alert_tracker = {}

# ==================== ASYNC WRAPPERS ====================

async def _check_safety(mint: str) -> dict:
    checker = HoneypotChecker(
        api_endpoint=RUGCHECK_URL.format(mint),
        api_key=config.RUGCHECK_API_KEY,
        sell_tax_threshold=config.SELL_TAX_THRESHOLD,
    )
    return await asyncio.to_thread(checker.check_token_safety, mint)


async def _get_deployer_rep(wallet_addr: str, api_key: str) -> dict:
    return await asyncio.to_thread(get_deployer_reputation, wallet_addr, api_key)


async def _get_token_info_sync(mint: str) -> dict:
    return await asyncio.to_thread(get_token_info, mint)


async def _get_sol_price_sync() -> float:
    return await asyncio.to_thread(get_sol_usd_price)


async def _scan_goldmines(max_lines: int = 200) -> list:
    return await asyncio.to_thread(auto_trader.scan_for_goldmines, max_lines)


async def _scan_premium(max_lines: int = 300) -> list:
    return await asyncio.to_thread(premium_engine.scan_for_premium_signals, max_lines)


def _read_live_discoveries_sync(event_type: str, max_age_seconds: int = 3600) -> list:
    path = "live_discoveries.jsonl"
    if not os.path.exists(path):
        return []
    now = time.time()
    results = []
    try:
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("type") != event_type:
                    continue
                if now - rec.get("discovered_at", 0) > max_age_seconds:
                    continue
                results.append(rec)
    except (IOError, json.JSONDecodeError):
        return []
    results.sort(key=lambda r: r.get("discovered_at", 0), reverse=True)
    return results


async def _read_live_discoveries(event_type: str, max_age_seconds: int = 3600) -> list:
    return await asyncio.to_thread(_read_live_discoveries_sync, event_type, max_age_seconds)


def _get_raw_creation_record_sync(mint: str) -> dict:
    def _search(path):
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                for line in f:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    if rec.get("type") == "new_token" and rec.get("mint") == mint:
                        return rec
        except (IOError, json.JSONDecodeError):
            return None
        return None
    return _search("launch_index.jsonl") or _search("live_discoveries.jsonl")


async def _get_raw_creation_record(mint: str) -> dict:
    return await asyncio.to_thread(_get_raw_creation_record_sync, mint)


def _get_launch_price_sync(mint: str, sol_price_usd: float) -> dict:
    def _search(path):
        if not os.path.exists(path):
            return None
        earliest = None
        try:
            with open(path) as f:
                for line in f:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    if rec.get("type") != "new_token" or rec.get("mint") != mint:
                        continue
                    if earliest is None or rec.get("discovered_at", 0) < earliest.get("discovered_at", 0):
                        earliest = rec
        except (IOError, json.JSONDecodeError):
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


async def _get_launch_price(mint: str, sol_price_usd: float) -> dict:
    return await asyncio.to_thread(_get_launch_price_sync, mint, sol_price_usd)


# ==================== HELPERS ====================

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


def make_trade_buttons(mint: str, symbol: str) -> list:
    """Returns a row with both Paper and Real buy buttons."""
    return [
        InlineKeyboardButton("🚀 APE IN (Paper)", callback_data=f"buy:{mint}:{symbol}"),
        InlineKeyboardButton("🔴 REAL BUY", callback_data=f"realbuy_pct:{mint}:{symbol}"),
    ]

def make_pct_buttons(mint: str, symbol: str) -> list:
    """Returns percentage selection buttons for real buy."""
    return [
        InlineKeyboardButton("25%", callback_data=f"realbuy_exec:{mint}:{symbol}:0.25"),
        InlineKeyboardButton("50%", callback_data=f"realbuy_exec:{mint}:{symbol}:0.50"),
        InlineKeyboardButton("75%", callback_data=f"realbuy_exec:{mint}:{symbol}:0.75"),
        InlineKeyboardButton("100%", callback_data=f"realbuy_exec:{mint}:{symbol}:1.00"),
    ]



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
    ]

    # Buy/sell counts
    buys = info.get("buys_h1", 0)
    sells = info.get("sells_h1", 0)
    net = buys - sells
    net_emoji = "🟢" if net > 0 else "🔴" if net < 0 else "⚪"
    lines.append(f"📊 *Activity (1h):* Buys: `{buys}` | Sells: `{sells}` {net_emoji} Net: `{net:+d}`")
    lines.append("")

    lines += ["*-- Contract --*", contract_line, activity_line]

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

# ==================== ASYNC BUILDERS ====================

async def build_token_card(mint: str):
    info = await get_token_info_async(mint)
    if not info["ok"]:
        info = await _get_token_info_sync(mint)

    if not info["ok"]:
        raw_record = await _get_raw_creation_record(mint)
        if raw_record:
            sol_price = await get_sol_usd_price_async()
            if sol_price <= 0:
                sol_price = await _get_sol_price_sync()
            safety = await _check_safety(mint)
            eval_result = await asyncio.to_thread(conviction.evaluate, mint, live_record=raw_record, safety=safety)
            card = format_minimal_card(mint, raw_record, sol_price, safety, eval_result)
            symbol = raw_record.get("symbol", "?")
            keyboard = InlineKeyboardMarkup([make_trade_buttons(mint, symbol)])
            return card, keyboard, None
        return None, None, info.get("error", "unknown error")

    safety = await _check_safety(mint)
    dev_rep = None
    if safety.get("creator"):
        dev_rep = await _get_deployer_rep(safety["creator"], config.HELIUS_API_KEY)

    live_rec = await _get_raw_creation_record(mint)
    eval_result = await asyncio.to_thread(
        conviction.evaluate, mint, live_record=live_rec, safety=safety, dev_rep=dev_rep, info=info
    )

    sol_price = await get_sol_usd_price_async()
    if sol_price <= 0:
        sol_price = await _get_sol_price_sync()
    launch = await _get_launch_price(mint, sol_price) if sol_price > 0 else {"ok": False}

    card = format_token_card(mint, info, safety, dev_rep, launch, eval_result)

    buttons = make_trade_buttons(mint, info.get("symbol", "?"))
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
    await asyncio.to_thread(wallet.apply_daily_topup, user_id)
    balance = await asyncio.to_thread(wallet.get_balance, user_id)
    await update.message.reply_text(
        "👋 *Solana Goldmine Bot* -- Paper + Real Trading\n\n"
        f"Fake balance: *{balance:.3f} SOL*\n"
        f"(Real wallet: `/wallet` to check)\n\n"
        "*How to use:*\n"
        "1. Run `python live_listener.py` in another terminal\n"
        "2. The bot alerts you when it finds a *goldmine* or when your coins *pump*\n"
        "3. Tap *APE IN (Paper)* for fake SOL, or *REAL BUY* for real SOL\n"
        "4. `/realbuy <CA>` to trade with REAL SOL\n"
        "5. `/estimate <CA>` for AI trade signals\n"
        "6. Sell manually or let auto-exit handle TP/SL\n\n"
        "*Commands:*\n"
        "Send a CA -- full analysis + conviction score\n"
        "/wallet -- Link/check your real wallet\n"
        "/holdings -- ALL your coins (paper + real wallet)\n"
        "/realbuy <CA> [SOL] -- Buy with REAL SOL\n"
        "/realsell <CA> -- Sell for REAL SOL\n"
        "/realpositions -- Live real P&L + whales\n"
        "/realstats -- Real trading performance\n"
        "/premium -- Premium high-conviction signals\n"
        "/whales -- Whale activity on your tokens\n"
        "/estimate <CA> -- AI buy/hold/sell signal\n"
        "/bonded -- tokens IN bonding curve\n"
        "/graduated -- tokens that just LEFT the curve\n"
        "/autopilot -- toggle auto paper-buying ON/OFF\n"
        "/conviction <CA> -- check goldmine score\n"
        "/smartmoney -- view tracked dev wallets\n"
        "/addsmart <wallet> <tag> -- manually track a wallet\n"
        "/setlabel <wallet> <category> <name> -- label a whale\n"
        "/labels -- browse whale label database\n"
        "/balance -- fake SOL balance\n"
        "/positions -- open paper trades with P&L\n"
        "/activity -- trade history with charts\n"
        "/stats -- win rate & total P&L\n"
        "/new -- new tokens\n"
        "/launching -- Raydium delayed pools\n"
        "/checktx <sig> -- debug transaction",
        parse_mode="Markdown",
    )


async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = phantom.get_status_text()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh Balance", callback_data="wallet_refresh"),
    ]])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def connectphantom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "🔑 *Connect Your Phantom Wallet*\n\n"
            "This lets the bot trade using SOL from your Phantom wallet.\n\n"
            "*How to get your private key:*\n"
            "1. Open Phantom → Settings → Security & Privacy\n"
            "2. Click 'Export Private Key'\n"
            "3. Copy the base58 string\n\n"
            "*Then run:*\n"
            "`/connectphantom YOUR_PRIVATE_KEY`\n\n"
            "⚠️ *SAFETY:* Only use a fresh wallet with small amounts. "
            "Never your main Phantom with everything in it.",
            parse_mode="Markdown",
        )
        return

    private_key = context.args[0]
    if len(private_key) < 40:
        await update.message.reply_text(
            "❌ That doesn't look like a valid private key.\n"
            "Phantom private keys are long base58 strings (usually 85+ chars).",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        "🔐 Importing your Phantom wallet...\n"
        "_Checking balance and verifying..._",
        parse_mode="Markdown",
    )

    try:
        result = phantom.import_from_phantom(private_key)
        if result["ok"]:
            await update.message.reply_text(
                f"✅ *Phantom Wallet Connected!*\n\n"
                f"Address: `{result['address']}`\n"
                f"Balance: `{result['balance_sol']:.4f} SOL`\n\n"
                f"You're ready to trade with `/realbuy <CA>`",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"❌ *Import failed*\n{result['error']}\n\n"
                f"Make sure you copied the full private key from Phantom.",
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"Phantom connect failed: {e}")
        await update.message.reply_text(f"❌ Error: {e}")


async def disconnectphantom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import os as _os
    if _os.path.exists(config.REAL_WALLET_FILE):
        _os.remove(config.REAL_WALLET_FILE)
    phantom.__init__()
    await update.message.reply_text(
        "🗑️ *Wallet disconnected.*\n\n"
        "A fresh bot wallet has been generated.\n"
        f"New address: `{phantom.public_key}`\n\n"
        "Use `/connectphantom <key>` to link Phantom again.",
        parse_mode="Markdown",
    )


async def holdings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    sol_price = await get_sol_usd_price_async()
    if sol_price <= 0:
        sol_price = await _get_sol_price_sync()

    lines = ["💼 *Your Holdings*", ""]
    total_value_usd = 0.0
    has_any = False

    # Paper positions
    paper_positions = await asyncio.to_thread(wallet.get_open_positions, user_id)
    if paper_positions:
        has_any = True
        lines.append("*🔵 Paper Positions:*")
        mints = list(paper_positions.keys())
        prices = await batch_get_token_info(mints)
        for mint, pos in paper_positions.items():
            info = prices.get(mint, {})
            price = info.get("price_usd", 0) if info.get("ok") else 0
            tokens = pos.get("tokens_held", 0)
            value_usd = tokens * price
            entry = pos["entry_price_usd"]
            change = ((price - entry) / entry * 100) if entry > 0 else 0
            arrow = "🟢" if change >= 0 else "🔴"
            lines.append(
                f"{arrow} *{pos['symbol']}*\n"
                f"  Tokens: `{tokens:,.2f}`\n"
                f"  Value: `${value_usd:.2f}`\n"
                f"  Entry: `${entry:.8f}` → Now: `${price:.8f}`\n"
                f"  P&L: `{change:+.1f}%`"
            )
            total_value_usd += value_usd
        lines.append("")

    # Real trading positions (open trades)
    if real_trader.positions:
        has_any = True
        lines.append("*🔴 Real Trading Positions:*")
        mints = list(real_trader.positions.keys())
        prices = await batch_get_token_info(mints)
        for mint, pos in real_trader.positions.items():
            info = prices.get(mint, {})
            price = info.get("price_usd", 0) if info.get("ok") else 0
            tokens = pos.get("tokens_held", 0)
            value_usd = tokens * price
            entry = pos["entry_price_usd"]
            invested_usd = pos["sol_spent"] * sol_price
            change = ((price - entry) / entry * 100) if entry > 0 else 0
            pnl_usd = value_usd - invested_usd
            arrow = "🟢" if change >= 0 else "🔴"
            lines.append(
                f"{arrow} *{pos['symbol']}*\n"
                f"  Tokens: `{tokens:,.2f}`\n"
                f"  Value: `${value_usd:.2f}`\n"
                f"  Invested: `${invested_usd:.2f}`\n"
                f"  P&L: `${pnl_usd:+.2f}` (`{change:+.1f}%`)"
            )
            total_value_usd += value_usd
        lines.append("")

    # Real wallet holdings (ALL tokens in wallet, not just tracked trades)
    real_holdings = phantom.get_holdings()
    if real_holdings:
        has_any = True
        lines.append("*👛 Real Wallet Holdings:*")
        mints = list(real_holdings.keys())
        prices = await batch_get_token_info(mints)
        for mint, data in real_holdings.items():
            info = prices.get(mint, {})
            price = info.get("price_usd", 0) if info.get("ok") else 0
            ui_amount = data.get("ui_amount", 0)
            value_usd = ui_amount * price
            symbol = info.get("symbol", data.get("symbol", "UNKNOWN")) if info.get("ok") else data.get("symbol", "UNKNOWN")
            lines.append(
                f"• *{symbol}*\n"
                f"  Tokens: `{ui_amount:,.4f}`\n"
                f"  Value: `${value_usd:.2f}`\n"
                f"  `{mint[:8]}...{mint[-4:]}`"
            )
            total_value_usd += value_usd
        lines.append("")

    if not has_any:
        lines.append("No open positions or wallet holdings.\n")
        lines.append("Use `/realbuy <CA>` or tap *APE IN (Paper)* on a token.")
    else:
        lines.append(f"*Total value: `${total_value_usd:.2f}`*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def realbuy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/realbuy <token_address> [amount]`
"
            f"Amount can be:
"
            f"  • A SOL value: `0.05`
"
            f"  • A percentage: `25%`, `50%`, `75%`, `100%`
"
            f"Example: `/realbuy ABC123... 50%`",
            parse_mode="Markdown",
        )
        return

    mint = context.args[0]
    if not MINT_PATTERN.match(mint):
        await update.message.reply_text("Invalid Solana address.")
        return

    if not phantom.is_ready():
        await update.message.reply_text(
            "❌ *Real wallet not ready*
"
            f"Send at least `{config.MIN_SOL_RESERVE + 0.05:.3f} SOL` to:
"
            f"`{phantom.public_key}`

"
            "_Copy the address and send from Phantom._",
            parse_mode="Markdown",
        )
        return

    balance = phantom.get_balance_sol()
    available = max(0, balance - config.MIN_SOL_RESERVE)

    # Parse amount
    amount_arg = context.args[1] if len(context.args) > 1 else str(config.DEFAULT_BUY_SIZE_SOL)
    amount_arg = amount_arg.strip()

    if amount_arg.endswith("%"):
        try:
            pct = float(amount_arg[:-1]) / 100.0
        except ValueError:
            await update.message.reply_text("❌ Invalid percentage. Use: `25%`, `50%`, `75%`, or `100%`")
            return
        sol_amount = available * pct
        pct_display = int(pct * 100)
    else:
        try:
            sol_amount = float(amount_arg)
            pct_display = None
        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Use a number like `0.05` or a percentage like `50%`")
            return

    if sol_amount > available:
        await update.message.reply_text(
            f"❌ Not enough SOL. You asked for `{sol_amount:.4f}` but only `{available:.4f}` is available (reserve: {config.MIN_SOL_RESERVE}).",
            parse_mode="Markdown",
        )
        return

    if sol_amount <= 0:
        await update.message.reply_text("❌ Amount must be greater than 0.")
        return

    pct_text = f" ({pct_display}% of available)" if pct_display else ""
    await update.message.reply_text(f"🔴 Executing REAL buy of `{sol_amount:.4f} SOL`{pct_text}...", parse_mode="Markdown")

    info = await _get_token_info_sync(mint)
    symbol = info.get("symbol", "?") if info.get("ok") else "?"

    result = await asyncio.to_thread(real_trader.buy, mint, symbol, sol_amount)
    if not result["ok"]:
        await update.message.reply_text(f"❌ Buy failed: {result['error']}")
        return

    await update.message.reply_text(
        f"🟢 *REAL BUY EXECUTED*
"
        f"
"
        f"Token: *{symbol}*
"
        f"Mint: `{mint}`
"
        f"Spent: `{result['sol_spent']:.4f} SOL`{pct_text}
"
        f"Received: `{result['tokens_received']:,.2f}` tokens
"
        f"Entry: `${result['price_usd']:.8f}`
"
        f"Tx: `{result['tx_signature'][:20]}...`
"
        f"
"
        f"_Auto TP: +{config.TAKE_PROFIT_PERCENT:.0f}% | Auto SL: {config.STOP_LOSS_PERCENT:.0f}%_",
        parse_mode="Markdown",
    )

async def realsell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    result = await asyncio.to_thread(real_trader.sell, mint)
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


async def realstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txs = await asyncio.to_thread(real_trader.get_recent_transactions, 100)
    if not txs:
        await update.message.reply_text("🔴 No real trades yet. Use `/realbuy <CA>` to start.")
        return

    buys = [t for t in txs if t.get("type") == "BUY"]
    sells = [t for t in txs if t.get("type") == "SELL"]
    total_pnl = sum(t.get("pnl_sol", 0) for t in sells)
    wins = sum(1 for t in sells if t.get("pnl_sol", 0) > 0)
    losses = sum(1 for t in sells if t.get("pnl_sol", 0) <= 0)
    win_rate = (wins / len(sells) * 100) if sells else 0

    sol_price = await get_sol_usd_price_async()
    if sol_price <= 0:
        sol_price = await _get_sol_price_sync()
    open_pnl = 0.0
    for mint, pos in real_trader.positions.items():
        info = await get_token_info_async(mint)
        if info.get("ok"):
            current = info["price_usd"]
            entry = pos["entry_price_usd"]
            tokens = pos["tokens_held"]
            invested = pos["sol_spent"] * sol_price
            value = tokens * current
            open_pnl += (value - invested)

    lines = [
        "🔴 *Real Trading Stats*",
        "",
        f"Total trades: `{len(buys)}` buys, `{len(sells)}` sells",
        f"Win rate: `{win_rate:.1f}%` ({wins}W / {losses}L)",
        f"Realized P&L: `{total_pnl:+.4f} SOL`",
        f"Open unrealized: `${open_pnl:+.2f}`",
        "",
        f"Open positions: `{len(real_trader.positions)}`",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def estimate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/estimate <token_address>`\n"
            "Get an AI-style signal with confidence, risk level, and reasoning.",
            parse_mode="Markdown",
        )
        return

    mint = context.args[0]
    if not MINT_PATTERN.match(mint):
        await update.message.reply_text("Invalid Solana address.")
        return

    await update.message.reply_text(f"🧠 Analyzing `{mint}`...", parse_mode="Markdown")

    try:
        safety = await _check_safety(mint)
        dev_rep = None
        if safety.get("creator"):
            dev_rep = await _get_deployer_rep(safety["creator"], config.HELIUS_API_KEY)
        live_rec = await _get_raw_creation_record(mint)

        signal = await estimator.estimate(mint, live_record=live_rec, safety=safety, dev_rep=dev_rep)
        text = estimator.format_signal(signal)

        buttons = []
        if signal.verdict in ("STRONG_BUY", "BUY"):
            buttons = make_trade_buttons(mint, signal.symbol)
        elif signal.verdict in ("SELL", "STRONG_SELL"):
            buttons = [InlineKeyboardButton("🔴 SELL REAL", callback_data=f"realsell:{mint}")]
        keyboard = InlineKeyboardMarkup([buttons]) if buttons else None

        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Estimate failed: {e}")
        await update.message.reply_text(f"Analysis failed: {e}")


async def setlabel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        cats = ", ".join(labeler.CATEGORIES.keys())
        await update.message.reply_text(
            f"Usage: `/setlabel <wallet> <category> <name>`\n"
            f"Categories: `{cats}`\n"
            f"Example: `/setlabel ABC123... known_dev MoonshotDev`",
            parse_mode="Markdown",
        )
        return

    wallet_addr = context.args[0]
    category = context.args[1]
    name = " ".join(context.args[2:])

    if category not in labeler.CATEGORIES:
        cats = ", ".join(labeler.CATEGORIES.keys())
        await update.message.reply_text(f"Invalid category. Use one of: `{cats}`", parse_mode="Markdown")
        return

    labeler.add_label(wallet_addr, name, category, source="user")
    emoji = labeler.CATEGORIES.get(category, "👤")
    await update.message.reply_text(
        f"✅ Labeled `{wallet_addr[:10]}...`\n"
        f"{emoji} *{name}* (`{category}`)",
        parse_mode="Markdown",
    )


async def labels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = labeler.get_summary_text()
    await update.message.reply_text(text, parse_mode="Markdown")


async def whales_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    paper_positions = await asyncio.to_thread(wallet.get_open_positions, user_id)
    all_positions = {**paper_positions}
    all_positions.update(real_trader.positions)

    if not all_positions:
        await update.message.reply_text("No open positions to track whales.")
        return

    await update.message.reply_text("🐋 Scanning whale activity with labels...")

    for mint, pos in all_positions.items():
        try:
            info = await get_token_info_async(mint)
            if not info.get("ok"):
                info = await _get_token_info_sync(mint)
            if not info.get("ok"):
                continue
            current_price = info["price_usd"]

            transfers = await tracker.fetch_token_transfers(mint, limit=30)
            activities = tracker.detect_whale_activity(mint, transfers, current_price)

            for act in activities:
                labeler.auto_label_whale(act.wallet, act.amount_usd)

            if activities:
                sentiment = tracker.analyze_whale_sentiment(activities)
                lines = [
                    f"🐋 *Whale Activity — {pos['symbol']}*",
                    "",
                    f"{sentiment['emoji']} *{sentiment['sentiment']}*",
                    f"Net flow: `${sentiment['net_flow_usd']:+.0f}`",
                    f"Buys: {sentiment['count_buy']} (${sentiment['total_buy_usd']:,.0f})",
                    f"Sells: {sentiment['count_sell']} (${sentiment['total_sell_usd']:,.0f})",
                    "",
                ]
                for i, w in enumerate(activities[:5], 1):
                    action_emoji = "🟢" if w.action == "BUY" else "🔴"
                    display = labeler.get_display_name(w.wallet)
                    lines.append(f"{i}. {action_emoji} {display} — `{w.amount_tokens:,.0f}` tokens (`${w.amount_usd:,.0f}`)")

                try:
                    safety = await _check_safety(mint)
                    signal = await estimator.estimate(mint, safety=safety)
                    e_emoji = {"STRONG_BUY": "🚀", "BUY": "🟢", "HOLD": "🟡", "SELL": "🔴", "STRONG_SELL": "🛑", "AVOID": "🚫"}
                    lines.append("")
                    lines.append(f"*Bot Estimate:* {e_emoji.get(signal.verdict, '❓')} `{signal.verdict}` ({signal.confidence}% confidence)")
                except Exception:
                    pass

                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            else:
                await update.message.reply_text(f"🐋 {pos['symbol']}: No whale activity in last 30 txs.")
        except Exception as e:
            logger.warning(f"Whale scan failed for {mint}: {e}")


async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fast premium command - shows cached signals, doesn't scan."""
    signals = []
    if os.path.exists(config.PREMIUM_STATE_FILE):
        try:
            with open(config.PREMIUM_STATE_FILE) as f:
                data = json.load(f)
            for mint, sig_dict in data.get("signals", {}).items():
                if time.time() - sig_dict.get("timestamp", 0) < 1800:
                    signals.append(PremiumSignal(**sig_dict))
        except (json.JSONDecodeError, TypeError):
            pass

    signals.sort(key=lambda s: s.score, reverse=True)
    signals = [s for s in signals if s.outcome == "PENDING"]

    if not signals:
        stats = await asyncio.to_thread(premium_engine.get_stats)
        await update.message.reply_text(
            f"⚡️ *No active premium signals right now.*\n"
            f"\n"
            f"_The bot scans every 60s in the background and alerts you instantly._\n"
            f"_Premium filters: Score ≥{config.PREMIUM_SIGNAL_THRESHOLD}, Liq ≥${config.PREMIUM_MIN_LIQUIDITY_USD:,.0f}, Age <{config.PREMIUM_MAX_AGE_MINUTES}m_\n"
            f"\n"
            f"📊 *Premium Stats*\n"
            f"Total signals: {stats['total_signals']}\n"
            f"Wins: {stats['wins']} | Losses: {stats['losses']}\n"
            f"Win rate: {stats['win_rate']:.1f}%",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(f"⚡️ *{len(signals)} ACTIVE PREMIUM SIGNAL(S)*", parse_mode="Markdown")

    for signal in signals[:5]:
        text = await asyncio.to_thread(premium_engine.format_premium_alert, signal)
        keyboard = InlineKeyboardMarkup([make_trade_buttons(signal.mint, signal.symbol)])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def premium_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await asyncio.to_thread(premium_engine.get_stats)
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


async def bonded_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    live = await _read_live_discoveries("new_token", 1800)
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

                btns = make_trade_buttons(mint, symbol)
                btns.append(InlineKeyboardButton("🔄 Refresh", callback_data=f"cardrefresh:{mint}"))
                keyboard = InlineKeyboardMarkup([btns])
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
                continue

            await update.message.reply_text(card, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            logger.warning(f"Failed bonded card for {mint}: {e}")


async def graduated_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    live = await _read_live_discoveries("migration", 3600)
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

    result = await asyncio.to_thread(
        get_recent_migrations, config.HELIUS_API_KEY, config.SOLANA_RPC_URL, 8, config.LAUNCHING_LOOKBACK
    )
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
            mint = mig["mint"]
            lines.append(f"Mint: `{mint}`")
            safety = await _check_safety(mint)
            if safety.get("mint_authority"):
                lines.append("⚠️ Mint authority active")
            if safety.get("freeze_authority"):
                lines.append("⚠️ Freeze authority active")
            if not safety.get("mint_authority") and not safety.get("freeze_authority"):
                lines.append("✅ No mint/freeze red flags")
            btns = make_trade_buttons(mint, "?")
            keyboard = InlineKeyboardMarkup([btns])
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
        elif mig.get("mint"):
            lines.append(f"Mint: `{mig['mint']}` ⚠️ unconfirmed")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
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
        result = await asyncio.to_thread(conviction.evaluate, mint)
        card = conviction.format_card(result)
        await update.message.reply_text(card, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Conviction check failed: {e}")
        await update.message.reply_text(f"Analysis failed: {e}")


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    await asyncio.to_thread(wallet.apply_daily_topup, user_id)
    balance = await asyncio.to_thread(wallet.get_balance, user_id)
    await update.message.reply_text(f"💰 Paper Balance: *{balance:.3f} SOL*", parse_mode="Markdown")


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    positions = await asyncio.to_thread(wallet.get_open_positions, user_id)
    if not positions:
        await update.message.reply_text("No open paper positions.")
        return
    for mint, pos in positions.items():
        info = await _get_token_info_sync(mint)
        if not info["ok"]:
            await update.message.reply_text(f"{pos['symbol']}: price lookup failed")
            continue
        text = format_position_update(mint, pos, info["price_usd"])
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Sell Paper", callback_data=f"sell:{mint}")]])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    s = await asyncio.to_thread(wallet.get_stats, user_id)
    await update.message.reply_text(
        f"📊 *Paper Stats*\n"
        f"Trades: {s['total_trades']} | Wins: {s['wins']} | Losses: {s['losses']}\n"
        f"Win rate: {s['win_rate']:.1f}%\n"
        f"Total P&L: {s['total_pnl_sol']:+.4f} SOL",
        parse_mode="Markdown",
    )


async def activity_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    records = await asyncio.to_thread(wallet.get_activity_records, user_id)
    if not records:
        await update.message.reply_text("No activity yet.")
        return
    for rec in records[:10]:
        text = format_activity_record(rec)
        await update.message.reply_text(text, parse_mode="Markdown")
        if len(rec.get("price_history", [])) >= 2:
            try:
                chart = await asyncio.to_thread(build_price_chart, rec["symbol"], rec["price_history"])
                await update.message.reply_photo(photo=chart)
            except Exception as e:
                logger.warning(f"Chart failed: {e}")


async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    live_new = await _read_live_discoveries("new_token", 600)
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
        latest = await asyncio.to_thread(get_latest_new_tokens, config.SOLANATRACKER_API_KEY, 8)
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

    boosted = await asyncio.to_thread(get_boosted_solana_tokens, 5)
    if boosted["ok"] and boosted["tokens"]:
        lines = ["📢 *Promoted (paid placement)*", ""]
        for t in boosted["tokens"]:
            desc = (t["description"][:60] + "...") if len(t.get("description", "")) > 60 else t.get("description", "")
            lines.append(f"`{t['mint']}`")
            if desc:
                lines.append(f"_{desc}_")
            lines.append("")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    graduating = await asyncio.to_thread(get_graduating_coins, config.SOLANATRACKER_API_KEY, 8)
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
    result = await asyncio.to_thread(debug_check_transaction, signature, config.HELIUS_API_KEY)
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
    result = await asyncio.to_thread(
        get_upcoming_pool_launches, config.HELIUS_API_KEY, config.SOLANA_RPC_URL, 8, config.LAUNCHING_LOOKBACK
    )
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
            dev_rep = await _get_deployer_rep(pool["creator"], config.HELIUS_API_KEY)
            if dev_rep.get("ok"):
                if dev_rep["is_brand_new_wallet"]:
                    lines.append("👤 Dev: brand new wallet")
                else:
                    lines.append(f"👤 Dev: {dev_rep['txn_count_sampled']} txns, ~{dev_rep['likely_tokens_created']} creations")
        if pool.get("mint"):
            safety = await _check_safety(pool["mint"])
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
        info = await get_token_info_async(mint)
        if not info.get("ok"):
            info = await _get_token_info_sync(mint)
        sol_price = await get_sol_usd_price_async()
        if sol_price <= 0:
            sol_price = await _get_sol_price_sync()
        if not info.get("ok") or sol_price <= 0:
            await query.message.reply_text("Price data unavailable, try again shortly.")
            return
        result = await asyncio.to_thread(
            wallet.buy, user_id, mint, symbol,
            sol_amount=config.DEFAULT_BUY_SIZE_SOL,
            token_price_usd=info["price_usd"],
            sol_price_usd=sol_price,
        )
        if not result["ok"]:
            await query.message.reply_text(f"❌ {result['error']}")
            return
        balance = await asyncio.to_thread(wallet.get_balance, user_id)
        await query.message.reply_text(
            f"✅ Bought *{symbol}* with {config.DEFAULT_BUY_SIZE_SOL} SOL\n"
            f"Entry: `${info['price_usd']:.8f}`\n"
            f"New paper balance: {balance:.3f} SOL",
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
        result = await asyncio.to_thread(real_trader.buy, mint, symbol, config.DEFAULT_BUY_SIZE_SOL)
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
        info = await get_token_info_async(mint)
        if not info.get("ok"):
            info = await _get_token_info_sync(mint)
        sol_price = await get_sol_usd_price_async()
        if sol_price <= 0:
            sol_price = await _get_sol_price_sync()
        if not info.get("ok") or sol_price <= 0:
            await query.message.reply_text("Price data unavailable, try again shortly.")
            return
        result = await asyncio.to_thread(wallet.sell, user_id, mint, info["price_usd"], sol_price)
        if not result["ok"]:
            await query.message.reply_text(f"❌ {result['error']}")
            return

        safety = await _check_safety(mint)
        dev_wallet = safety.get("creator")
        if dev_wallet:
            smart_money.record_trade(dev_wallet, result["symbol"], result["pnl_sol"], mint)

        emoji = "🟢" if result["pnl_sol"] >= 0 else "🔴"
        balance = await asyncio.to_thread(wallet.get_balance, user_id)
        await query.message.reply_text(
            f"{emoji} Sold *{result['symbol']}*\n"
            f"Entry: `${result['entry_price_usd']:.8f}` → Exit: `${result['exit_price_usd']:.8f}`\n"
            f"P&L: `{result['pnl_sol']:+.4f} SOL` (`{result['pnl_percent']:+.2f}%`)\n"
            f"New paper balance: {balance:.3f} SOL",
            parse_mode="Markdown",
        )

    elif data.startswith("realsell:"):
        _, mint = data.split(":", 1)
        result = await asyncio.to_thread(real_trader.sell, mint)
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
    sol_price = await get_sol_usd_price_async()
    if sol_price <= 0:
        sol_price = await _get_sol_price_sync()
    if sol_price <= 0:
        return

    all_mints = set()
    users_copy = await asyncio.to_thread(lambda: dict(wallet.users))
    for user_data in users_copy.values():
        all_mints.update(user_data.get("open_positions", {}).keys())
    all_mints.update(real_trader.positions.keys())

    if all_mints:
        await batch_get_token_info(list(all_mints))

    for user_id, user_data in users_copy.items():
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
                    info = await get_token_info_async(mint)
                    if not info.get("ok"):
                        info = await _get_token_info_sync(mint)
                    if info.get("ok"):
                        await asyncio.to_thread(wallet.record_price_snapshot, user_id, mint, info["price_usd"])
                        text = format_position_update(mint, pos, info["price_usd"])
                        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Sell Paper", callback_data=f"sell:{mint}")]])
                        await context.bot.send_message(
                            chat_id=int(user_id), text=text, parse_mode="Markdown", reply_markup=keyboard
                        )
            except Exception as e:
                logger.warning(f"Failed push update to {user_id}: {e}")

    for mint, pos in real_trader.positions.items():
        try:
            snapshot = await tracker.get_position_snapshot(mint, pos)
            if snapshot:
                text = tracker.format_position_update(snapshot, is_real=True)
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔴 SELL REAL", callback_data=f"realsell:{mint}")]])
                for user_id in users_copy.keys():
                    try:
                        await context.bot.send_message(
                            chat_id=int(user_id), text=text, parse_mode="Markdown", reply_markup=keyboard
                        )
                    except Exception as e:
                        logger.warning(f"Failed real push to {user_id}: {e}")
        except Exception as e:
            logger.warning(f"Failed real position update for {mint}: {e}")


async def auto_exit_checker(context: ContextTypes.DEFAULT_TYPE):
    sol_price = await get_sol_usd_price_async()
    if sol_price <= 0:
        sol_price = await _get_sol_price_sync()
    if sol_price <= 0:
        return

    users_copy = await asyncio.to_thread(lambda: dict(wallet.users))
    for user_id, user_data in list(users_copy.items()):
        positions = user_data.get("open_positions", {})
        if not positions:
            continue
        for mint, pos in list(positions.items()):
            info = await get_token_info_async(mint)
            if not info.get("ok"):
                info = await _get_token_info_sync(mint)
            if not info.get("ok"):
                continue
            entry = pos["entry_price_usd"]
            current = info["price_usd"]
            change = (current - entry) / entry * 100 if entry > 0 else 0
            if change >= config.TAKE_PROFIT_PERCENT:
                result = await asyncio.to_thread(wallet.sell, user_id, mint, current, sol_price)
                if result["ok"]:
                    safety = await _check_safety(mint)
                    dev_wallet = safety.get("creator")
                    if dev_wallet:
                        smart_money.record_trade(dev_wallet, result["symbol"], result["pnl_sol"], mint)
                    try:
                        await context.bot.send_message(
                            chat_id=int(user_id),
                            text=(f"🎯 *AUTO TAKE PROFIT* -- {result['symbol']}\n"
                                  f"Sold at `{change:+.1f}%` (+{config.TAKE_PROFIT_PERCENT}%)\n"
                                  f"P&L: `{result['pnl_sol']:+.4f} SOL`"),
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning(f"TP alert failed: {e}")
            elif change <= config.STOP_LOSS_PERCENT:
                result = await asyncio.to_thread(wallet.sell, user_id, mint, current, sol_price)
                if result["ok"]:
                    safety = await _check_safety(mint)
                    dev_wallet = safety.get("creator")
                    if dev_wallet:
                        smart_money.record_trade(dev_wallet, result["symbol"], result["pnl_sol"], mint)
                    try:
                        await context.bot.send_message(
                            chat_id=int(user_id),
                            text=(f"🛑 *AUTO STOP LOSS* -- {result['symbol']}\n"
                                  f"Sold at `{change:+.1f}%` ({config.STOP_LOSS_PERCENT}%)\n"
                                  f"P&L: `{result['pnl_sol']:+.4f} SOL`"),
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning(f"SL alert failed: {e}")

    for mint, pos in list(real_trader.positions.items()):
        info = await get_token_info_async(mint)
        if not info.get("ok"):
            info = await _get_token_info_sync(mint)
        if not info.get("ok"):
            continue
        exit_type = real_trader.check_auto_exit(mint, info["price_usd"])
        if exit_type:
            result = await asyncio.to_thread(real_trader.sell, mint)
            if result["ok"]:
                emoji = "🎯" if exit_type == "tp" else "🛑"
                label = "AUTO TAKE PROFIT" if exit_type == "tp" else "AUTO STOP LOSS"
                for user_id in users_copy.keys():
                    try:
                        await context.bot.send_message(
                            chat_id=int(user_id),
                            text=(f"{emoji} *{label} (REAL)* -- {result['symbol']}\n"
                                  f"P&L: `{result['pnl_sol']:+.4f} SOL` (`{result['pnl_percent']:+.2f}%`)\n"
                                  f"Tx: `{result['tx_signature'][:20]}...`"),
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning(f"Real auto-exit alert failed: {e}")


async def goldmine_scanner(context: ContextTypes.DEFAULT_TYPE):
    try:
        goldmines = await _scan_goldmines(200)
        if not goldmines:
            return
        for eval_result in goldmines:
            score = eval_result["score"]
            mint = eval_result["mint"]
            users_copy = await asyncio.to_thread(lambda: dict(wallet.users))
            for user_id in list(users_copy.keys()):
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
                        buys = info.get("buys_h1", 0)
                        sells = info.get("sells_h1", 0)
                        net = buys - sells
                        net_emoji = "🟢" if net > 0 else "🔴" if net < 0 else "⚪"
                        alert_lines.append(f"📊 Activity (1h): Buys: `{buys}` | Sells: `{sells}` {net_emoji} Net: `{net:+d}`")
                    alert_lines.append("")
                    alert_lines.append("_Paper trading only. Tap APE IN (Paper) or REAL BUY._")
                    symbol = info.get("symbol", "?") if info else "?"
                    keyboard = InlineKeyboardMarkup([make_trade_buttons(mint, symbol)])
                    await context.bot.send_message(
                        chat_id=int(user_id),
                        text="\n".join(alert_lines),
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                    )
                    if autopilot_states.get(user_id, False) and score >= config.AUTO_BUY_THRESHOLD:
                        buy_result = await asyncio.to_thread(auto_trader.attempt_paper_buy, user_id, eval_result)
                        if buy_result["ok"]:
                            await context.bot.send_message(
                                chat_id=int(user_id),
                                text=(f"🤖 *AUTO-PILOT BUY* -- {symbol}\n"
                                      f"Score: `{score:.0f}/100` ≥ {config.AUTO_BUY_THRESHOLD} threshold\n"
                                      f"Spent: `{config.DEFAULT_BUY_SIZE_SOL} SOL` (paper)"),
                                parse_mode="Markdown",
                            )
                except Exception as e:
                    logger.warning(f"Goldmine alert failed for user {user_id}: {e}")
    except Exception as e:
        logger.error(f"Goldmine scanner crashed: {e}")


async def premium_scanner(context: ContextTypes.DEFAULT_TYPE):
    try:
        signals = await _scan_premium(300)
        if not signals:
            return
        users_copy = await asyncio.to_thread(lambda: dict(wallet.users))
        for signal in signals:
            for user_id in list(users_copy.keys()):
                try:
                    text = await asyncio.to_thread(premium_engine.format_premium_alert, signal)
                    keyboard = InlineKeyboardMarkup([make_trade_buttons(signal.mint, signal.symbol)])
                    await context.bot.send_message(
                        chat_id=int(user_id),
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                    )
                except Exception as e:
                    logger.warning(f"Premium alert failed for user {user_id}: {e}")
    except Exception as e:
        logger.error(f"Premium scanner crashed: {e}")


async def early_stage_alerts(context: ContextTypes.DEFAULT_TYPE):
    try:
        live = await _read_live_discoveries("new_token", config.EARLY_STAGE_ALERT_SECONDS)
        if not live:
            return
        users_copy = await asyncio.to_thread(lambda: dict(wallet.users))
        for rec in live:
            mint = rec.get("mint")
            if not mint:
                continue
            try:
                eval_result = await asyncio.to_thread(conviction.evaluate, mint, live_record=rec)
                if eval_result["score"] >= config.GOLDMINE_ALERT_THRESHOLD:
                    for user_id in list(users_copy.keys()):
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
                            keyboard = InlineKeyboardMarkup([make_trade_buttons(mint, rec.get("symbol", "?"))])
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
    except Exception as e:
        logger.error(f"Early stage scanner crashed: {e}")


async def pump_alerts(context: ContextTypes.DEFAULT_TYPE):
    try:
        sol_price = await get_sol_usd_price_async()
        if sol_price <= 0:
            sol_price = await _get_sol_price_sync()
        if sol_price <= 0:
            return

        users_copy = await asyncio.to_thread(lambda: dict(wallet.users))
        all_positions = {}
        for user_id, user_data in users_copy.items():
            for mint, pos in user_data.get("open_positions", {}).items():
                all_positions.setdefault(mint, {})[user_id] = pos
        for mint, pos in real_trader.positions.items():
            for user_id in users_copy.keys():
                all_positions.setdefault(mint, {})[user_id] = pos

        if not all_positions:
            return

        prices = await batch_get_token_info(list(all_positions.keys()))
        now = time.time()

        for mint, user_positions in all_positions.items():
            info = prices.get(mint, {})
            if not info.get("ok"):
                continue
            current_price = info["price_usd"]

            for user_id, pos in user_positions.items():
                entry = pos.get("entry_price_usd", 0)
                if entry <= 0:
                    continue
                change_pct = ((current_price - entry) / entry) * 100

                alert_key = (user_id, mint)
                last_alert = _pump_alert_tracker.get(alert_key, {})
                last_time = last_alert.get("time", 0)

                should_alert = False
                alert_tier = 0

                if change_pct >= 100 and (now - last_time) > 1800:
                    should_alert = True
                    alert_tier = 3
                elif change_pct >= 50 and (now - last_time) > 1200:
                    should_alert = True
                    alert_tier = 2
                elif change_pct >= 25 and (now - last_time) > 600:
                    should_alert = True
                    alert_tier = 1

                if should_alert:
                    _pump_alert_tracker[alert_key] = {"price": current_price, "time": now}
                    emoji = "🚀" if alert_tier >= 3 else "🔥" if alert_tier == 2 else "📈"
                    try:
                        is_real = mint in real_trader.positions
                        mode = "🔴 REAL" if is_real else "🔵 PAPER"
                        await context.bot.send_message(
                            chat_id=int(user_id),
                            text=(
                                f"{emoji} *YOUR COIN IS PUMPING!* {mode}\n\n"
                                f"*{pos['symbol']}*\n"
                                f"Entry: `${entry:.8f}`\n"
                                f"Now: `${current_price:.8f}`\n"
                                f"Up: `{change_pct:+.1f}%` 🚀\n\n"
                                f"_This is your position doing well!_"
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning(f"Pump alert failed for {user_id}/{mint}: {e}")
    except Exception as e:
        logger.error(f"Pump alerts crashed: {e}")


async def daily_topup_job(context: ContextTypes.DEFAULT_TYPE):
    users_copy = await asyncio.to_thread(lambda: dict(wallet.users))
    for user_id in list(users_copy.keys()):
        await asyncio.to_thread(wallet.apply_daily_topup, user_id)


async def sync_smart_money_labels(context: ContextTypes.DEFAULT_TYPE):
    try:
        labeler.auto_label_from_smart_money(smart_money.wallets)
    except Exception as e:
        logger.warning(f"Smart money label sync failed: {e}")


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
    app.add_handler(CommandHandler("holdings", holdings_cmd))
    app.add_handler(CommandHandler("realbuy", realbuy_cmd))
    app.add_handler(CommandHandler("realsell", realsell_cmd))
    app.add_handler(CommandHandler("realpositions", realpositions_cmd))
    app.add_handler(CommandHandler("realstats", realstats_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(CommandHandler("premiumstats", premium_stats_cmd))
    app.add_handler(CommandHandler("whales", whales_cmd))
    app.add_handler(CommandHandler("estimate", estimate_cmd))
    app.add_handler(CommandHandler("setlabel", setlabel_cmd))
    app.add_handler(CommandHandler("labels", labels_cmd))
    app.add_handler(CommandHandler("connectphantom", connectphantom_cmd))
    app.add_handler(CommandHandler("disconnectphantom", disconnectphantom_cmd))

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

    # Background jobs — staggered intervals so they never overlap
    job_queue = app.job_queue
    job_queue.run_repeating(push_position_updates, interval=60, first=30)
    job_queue.run_repeating(auto_exit_checker, interval=60, first=45)
    job_queue.run_repeating(goldmine_scanner, interval=60, first=15)
    job_queue.run_repeating(premium_scanner, interval=60, first=25)
    job_queue.run_repeating(early_stage_alerts, interval=60, first=10)
    job_queue.run_repeating(pump_alerts, interval=45, first=5)
    job_queue.run_repeating(daily_topup_job, interval=3600, first=10)
    job_queue.run_repeating(sync_smart_money_labels, interval=300, first=60)

    logger.info("Goldmine bot FIXED v2 starting with dual buttons + fast premium + real holdings...")
    app.run_polling()


if __name__ == "__main__":
    main()
