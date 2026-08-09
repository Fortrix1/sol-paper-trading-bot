"""
telegram_bot.py - Paper-trading Telegram bot for practicing Solana token
decisions with fake SOL, before risking real capital.

Flow:
  - Send a token mint address (CA) -> bot replies with a live token card
    (price, liquidity, mcap, age, honeypot/safety verdict) + a Buy button.
  - Tap Buy -> opens a fake position sized at DEFAULT_BUY_SIZE_SOL.
  - Every PRICE_UPDATE_INTERVAL_SECONDS, the bot pushes a live P&L update
    for each open position, with a Sell button.
  - Tap Sell -> closes the position, credits/debits your fake balance.
  - Fake balance starts at STARTING_BALANCE_SOL and grows by
    DAILY_TOPUP_SOL per day, capped at BALANCE_CAP_SOL.

Run:
    pip install -r requirements.txt
    python telegram_bot.py
"""

import re
import io
import time
import asyncio
import datetime
import logging

import matplotlib
matplotlib.use("Agg")  # no display needed, just render to image bytes
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
import live_listener

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

wallet = PaperWallet(
    state_file=config.WALLET_STATE_FILE,
    starting_balance=config.STARTING_BALANCE_SOL,
    daily_topup=config.DAILY_TOPUP_SOL,
    cap=config.BALANCE_CAP_SOL,
)

RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report"

# Loose check for something that looks like a Solana base58 mint address
MINT_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
# Same character class, but for finding an address INSIDE a larger message
# (e.g. one of /new's feed messages) rather than matching the whole string.
MINT_SEARCH_PATTERN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def check_safety(mint: str) -> dict:
    checker = HoneypotChecker(
        api_endpoint=RUGCHECK_URL.format(mint),
        api_key=config.RUGCHECK_API_KEY,
        sell_tax_threshold=config.SELL_TAX_THRESHOLD,
    )
    return checker.check_token_safety(mint)


def format_age(age_minutes: int) -> str:
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
    """
    Looks up a token's price AT CREATION from launch_index.jsonl (a
    durable, more generously-sized store live_listener.py maintains
    specifically for this - separate from the busy general feed, which
    gets trimmed and can lose a token's record before you check it).
    Falls back to the general feed for anything logged before this
    separate index existed.
    Returns {ok, launch_price_usd, launch_timestamp} or {ok: False}.
    Approximation note: uses CURRENT sol_price_usd for the conversion
    (not the SOL price at the exact moment of launch), so this is close
    but not exact for the USD figure - fine for recently-launched coins
    where SOL's price hasn't moved much since.
    """
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


def format_token_card(mint: str, info: dict, safety: dict, dev_rep: dict = None, launch: dict = None) -> str:
    age = format_age(info["age_minutes"]) if info.get("age_minutes") is not None else "unknown"

    contract_line = "✅ Contract looks safe" if safety["is_safe"] else f"❌ Contract UNSAFE ({safety['reason']})"
    activity_line = "💀 DEAD - very low liquidity/volume" if info.get("is_dead") else "✅ Active trading"

    # --- Overall rug-risk summary: consolidates several signals into one
    # line so you don't have to mentally combine 5 different data points
    # yourself every time. ---
    #
    # CRITICAL: if the safety check itself FAILED (API error, not an
    # actual clean result), every field below is None - treating that as
    # "no red flags found" would be a real bug (looks safe when we simply
    # have no data). Must fail unsafe/unknown here, matching the honeypot
    # check's own fail-safe design.
    check_failed = not safety.get("is_safe") and "API check failed" in str(safety.get("reason", ""))

    mint_active = bool(safety.get("mint_authority"))
    freeze_active = bool(safety.get("freeze_authority"))
    lp_locked = safety.get("lp_locked")
    creator_pct = safety.get("creator_holding_pct")

    if check_failed:
        risk_label = "⚫ UNKNOWN - safety check failed, could NOT verify this token. Treat as risky until confirmed."
    elif mint_active or freeze_active:
        risk_label = "🔴 HIGH - dev can still mint more supply or freeze your funds"
    elif lp_locked is False:
        risk_label = "🟠 MEDIUM-HIGH - liquidity isn't locked, can be pulled anytime"
    elif (creator_pct and creator_pct > 10) or (safety.get("top_holder_pct") and safety["top_holder_pct"] > 60):
        risk_label = "🟡 MEDIUM - supply concentrated in few wallets"
    else:
        risk_label = "🟢 LOWER - no major red flags found"

    lines = [
        f"*{info['name']} ({info['symbol']})*",
        f"`{mint}`",
        "",
        f"⚠️ *Rug risk:* {risk_label}",
        "",
        "*— Price —*",
        f"Now: `${info['price_usd']:.8f}`",
    ]

    if launch and launch.get("ok"):
        change_pct = ((info["price_usd"] - launch["launch_price_usd"]) / launch["launch_price_usd"] * 100) if launch["launch_price_usd"] > 0 else 0
        lines.append(f"Launched at: `${launch['launch_price_usd']:.8f}`  ·  Change: `{change_pct:+.1f}%`")

    lines += [
        f"Liquidity: `${info['liquidity_usd']:,.0f}`  ·  24h Vol: `${info['volume_24h_usd']:,.0f}`",
        f"Market Cap: `${info['mcap_usd']:,.0f}`" + (f"  ·  FDV: `${info['fdv_usd']:,.0f}`" if info.get("fdv_usd") else ""),
    ]
    if info.get("fdv_usd") and info["fdv_usd"] > 0:
        circ_pct = info["mcap_usd"] / info["fdv_usd"] * 100
        lines.append(f"Circulating supply: `{circ_pct:.0f}%` of total is in public hands")

    lines += [
        f"Age: `{age}`  ·  Dex boosted: `{'Yes' if info.get('is_boosted') else 'No'}`",
        "",
        "*— Contract —*",
        contract_line,
        activity_line,
        f"Renounced: {'⚫ Unknown (check failed)' if check_failed else ('✅' if not mint_active else '❌ Mint authority still active')}",
    ]
    if freeze_active:
        lines.append("❌ Freeze authority still active")
    if lp_locked is not None:
        lines.append(f"Liquidity locked: `{'Yes' if lp_locked else 'No - can be pulled anytime'}`")

    # --- Deployer / dev wallet ---
    lines += ["", "*— Deployer —*"]
    if safety.get("creator"):
        lines.append(f"Wallet: `{safety['creator']}`")
        if creator_pct is not None:
            lines.append(f"Dev directly holds: `{creator_pct:.1f}%` of supply")
        else:
            lines.append("Dev holding: not in top 10 holders (or unknown)")
    if dev_rep and dev_rep.get("ok"):
        if dev_rep["is_brand_new_wallet"]:
            lines.append("Wallet history: brand new (little/no prior activity)")
        else:
            lines.append(
                f"Wallet history: {dev_rep['txn_count_sampled']} txns seen, "
                f"~{dev_rep['likely_tokens_created']} look like token creations"
            )
    lines.append("_Note: full track record (past rugs vs successful launches) isn't reliably "
                  "trackable yet - this is a rough proxy from recent wallet activity only, not a real history._")

    # --- Top holders, shown raw so you can eyeball sybil patterns yourself ---
    if safety.get("top_holders_list"):
        lines += ["", "*— Top Holders —*"]
        for h in safety["top_holders_list"][:5]:
            addr = h.get("address") or "unknown"
            short_addr = f"{addr[:4]}...{addr[-4:]}" if addr and len(addr) > 10 else addr
            tag = " 👤(dev)" if addr == safety.get("creator") else ""
            lines.append(f"`{short_addr}` - `{h.get('pct', 0):.1f}%`{tag}")

    # --- Socials, stated explicitly not just as buttons ---
    lines += ["", "*— Socials —*"]
    lines.append(f"X: {'✅' if info.get('twitter_url') else '❌ none found'}  ·  "
                 f"Telegram: {'✅' if info.get('telegram_url') else '❌ none found'}  ·  "
                 f"Website: {'✅' if info.get('website_url') else '❌ none found'}")

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
        f"Entry: `${entry:.8f}`  →  Now: `${current_price:.8f}`\n"
        f"Change: `{change_pct:+.2f}%`\n"
        f"{recommendation}\n"
    )


# ---------------- command handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    wallet.apply_daily_topup(user_id)
    balance = wallet.get_balance(user_id)
    await update.message.reply_text(
        "👋 Welcome to your Solana paper-trading practice bot.\n\n"
        f"Fake balance: *{balance:.3f} SOL*\n"
        f"(+{config.DAILY_TOPUP_SOL} SOL/day, capped at {config.BALANCE_CAP_SOL} SOL)\n\n"
        "*For instant new-coin discovery:* run `python live_listener.py` in a separate "
        "terminal (free, uses PumpPortal) - then /new and /graduated show real-time results. "
        "Without it, they fall back to slower on-demand scans.\n\n"
        "*Commands:*\n"
        "Send a contract address (CA) — check a token's safety, price, and get a Buy button\n"
        "/balance — see your fake SOL balance\n"
        "/positions — see open trades with live P&L and a Sell button\n"
        "/activity — history of every coin you've bought, with peak price and a chart\n"
        "/stats — your overall win rate and total P&L\n"
        "/new — brand-new tokens (live if listener running, else on-demand)\n"
        "/graduated — tokens that just migrated to PumpSwap (live if listener running)\n"
        "/launching — Raydium CPMM pools with a future open time (rare, separate on-demand check)\n"
        "/checktx <signature> — debug tool for verifying on-chain detection against a known transaction\n"
        "/analyse — reply to any message showing a token (from /new, /graduated, etc.) to get the full breakdown\n",
        parse_mode="Markdown",
    )


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    wallet.apply_daily_topup(user_id)
    balance = wallet.get_balance(user_id)
    await update.message.reply_text(f"💰 Balance: *{balance:.3f} SOL*", parse_mode="Markdown")


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    positions = wallet.get_open_positions(user_id)
    if not positions:
        await update.message.reply_text("No open positions.")
        return
    for mint, pos in positions.items():
        info = get_token_info(mint)
        if not info["ok"]:
            await update.message.reply_text(f"{pos['symbol']}: price lookup failed ({info['error']})")
            continue
        text = format_position_update(mint, pos, info["price_usd"])
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Sell", callback_data=f"sell:{mint}")]])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    s = wallet.get_stats(user_id)
    await update.message.reply_text(
        f"📊 *Stats*\n"
        f"Trades closed: {s['total_trades']}\n"
        f"Wins: {s['wins']}  Losses: {s['losses']}\n"
        f"Win rate: {s['win_rate']:.1f}%\n"
        f"Total P&L: {s['total_pnl_sol']:+.4f} SOL",
        parse_mode="Markdown",
    )


def build_price_chart(symbol: str, price_history: list) -> io.BytesIO:
    """Renders a simple price-over-time line chart to an in-memory PNG."""
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
    peak_time = (
        datetime.datetime.fromtimestamp(rec["peak_timestamp"]).strftime("%H:%M")
        if rec.get("peak_timestamp") else "?"
    )
    opened = (
        datetime.datetime.fromtimestamp(rec["opened_at"]).strftime("%b %d, %H:%M")
        if rec.get("opened_at") else "?"
    )

    lines = [f"*{rec['symbol']}* {'🟢 OPEN' if rec['is_open'] else '⚪ CLOSED'}"]
    lines.append(f"Bought: `{opened}` at `${entry:.8f}`")
    lines.append(f"Peak: `${peak:.8f}` (`{rec['peak_gain_pct']:+.1f}%`) at `{peak_time}`")

    if rec["is_open"]:
        lines.append("_Still open - check /positions for live P&L_")
    else:
        lines.append(f"Sold at: `${rec['exit_price_usd']:.8f}`")
        lines.append(f"Result: `{rec['pnl_sol']:+.4f} SOL` (`{rec['pnl_percent']:+.1f}%`)")
        # The "what if you'd sold at the peak instead" comparison
        if rec["peak_gain_pct"] > (rec["pnl_percent"] or 0) + 1:
            missed = rec["peak_gain_pct"] - (rec["pnl_percent"] or 0)
            lines.append(f"_If sold at peak instead: +{missed:.1f}% more_")

    return "\n".join(lines)


async def activity_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    records = wallet.get_activity_records(user_id)

    if not records:
        await update.message.reply_text("No activity yet. Send a CA and tap Buy to get started.")
        return

    for rec in records[:10]:  # most recent 10, so this doesn't turn into a wall of messages
        text = format_activity_record(rec)
        await update.message.reply_text(text, parse_mode="Markdown")

        if len(rec.get("price_history", [])) >= 2:
            try:
                chart = build_price_chart(rec["symbol"], rec["price_history"])
                await update.message.reply_photo(photo=chart)
            except Exception as e:
                logger.warning(f"Chart render failed for {rec['symbol']}: {e}")


async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Live listener (PumpPortal, free) is the best source when running -
    # genuinely real-time, not a REST snapshot that can lag.
    live_new = read_live_discoveries("new_token", max_age_seconds=600)
    if live_new:
        shown = live_new[:8]
        if len(live_new) > len(shown):
            await update.message.reply_text(
                f"🆕 *{len(live_new)} brand-new token(s) caught live - showing the {len(shown)} most recent (PumpPortal)*",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(f"🆕 *{len(shown)} brand-new token(s) caught live (PumpPortal)*", parse_mode="Markdown")
        for t in shown:
            try:
                card, keyboard, error = await build_token_card(t["mint"])
                if error:
                    logger.warning(f"Auto-analysis failed for {t.get('mint')}: {error}")
                    continue
                await update.message.reply_text(card, parse_mode="Markdown", reply_markup=keyboard)
            except Exception as e:
                logger.warning(f"Failed to send new-token entry for {t.get('mint')}: {e}")
    else:
        await update.message.reply_text(
            "No live listener data yet (run live_listener.py for instant results) - "
            "falling back to on-demand scan.\n"
            "🔎 Scanning for genuinely new Solana tokens...",
        )

    # PRIMARY (fallback) section: only runs if the live listener has
    # nothing yet - avoids showing two different "new tokens" lists at once.
    if not live_new:
        latest = get_latest_new_tokens(config.SOLANATRACKER_API_KEY, limit=8)
        if latest["ok"] and latest["tokens"]:
            window_label = f"last {latest['window_used_minutes']} min" if latest.get("window_used_minutes") else "age unknown"
            lines = [f"🆕 *Genuinely New Tokens ({window_label})*", ""]
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
            err = latest.get("error", "no results")
            await update.message.reply_text(f"New-token feed unavailable right now ({err}).")

    # SEPARATE section: boosted/promoted listings - these are PAID
    # placements, not necessarily new. Kept distinct on purpose so it's
    # never confused with the section above again.
    boosted = get_boosted_solana_tokens(limit=5)
    if boosted["ok"] and boosted["tokens"]:
        lines = ["📢 *Currently Promoted (paid placement, NOT necessarily new)*", ""]
        for t in boosted["tokens"]:
            desc = (t["description"][:60] + "...") if len(t.get("description", "")) > 60 else t.get("description", "")
            lines.append(f"`{t['mint']}`")
            if desc:
                lines.append(f"_{desc}_")
            lines.append("")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    graduating = get_graduating_coins(api_key=config.SOLANATRACKER_API_KEY, limit=8)
    if graduating["ok"] and graduating["tokens"]:
        lines = ["⏳ *Nearing Graduation to PumpSwap (SolanaTracker)*", ""]
        for t in graduating["tokens"]:
            bar_filled = int(t["graduation_progress_pct"] / 10)
            bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
            age_str = format_age(t["age_minutes"]) if t.get("age_minutes") is not None else "unknown"
            lines.append(f"*{t['symbol']}* - `${t['mcap_usd']:,.0f}` mcap  ·  Age: `{age_str}`")
            lines.append(f"{bar} `{t['graduation_progress_pct']:.0f}%` to PumpSwap")
            lines.append(f"`{t['mint']}`")
            lines.append("")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    else:
        logger.info(f"Graduating-coins scan unavailable: {graduating.get('error')}")


async def checktx_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /checktx <transaction_signature>\n\n"
            "For a Raydium pool creation: search CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C on "
            "Solscan, find a transaction labeled Initialize.\n"
            "For a pump.fun graduation: search 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P on "
            "Solscan, find a transaction labeled Migrate.\n"
            "Copy either one's signature and send it here."
        )
        return

    signature = context.args[0]
    await update.message.reply_text(f"🔎 Checking transaction `{signature}`...", parse_mode="Markdown")

    result = debug_check_transaction(signature, config.HELIUS_API_KEY)
    if not result["ok"]:
        await update.message.reply_text(f"Couldn't fetch that transaction: {result['error']}")
        return

    if result["matching_instructions_found"] == 0:
        await update.message.reply_text(
            f"Tx type (per Helius): `{result['tx_type']}`\n"
            "No CPMM or pump.fun program instructions found in this transaction at all - "
            "make sure this signature actually touches one of those programs."
        )
        return

    lines = [f"Tx type: `{result['tx_type']}`", f"Matching instructions found: {result['matching_instructions_found']}", ""]
    for i, f in enumerate(result["findings"], 1):
        lines.append(f"*Instruction {i} ({f.get('program', '?')}):*")
        lines.append(f"  Discriminator: `{f.get('discriminator_hex', 'n/a')}`")
        lines.append(f"  Matched: `{f.get('matched_instruction', 'n/a')}`")
        if "decoded_open_time_readable" in f:
            lines.append(f"  Decoded open_time: `{f['decoded_open_time_readable']}`")
            lines.append(f"  Tx happened at: `{f.get('tx_timestamp_readable', '?')}`")
            lines.append(f"  Gap (open_time - tx_time): `{f.get('seconds_from_tx_to_open_time', '?')}s`")
        if "extracted_mint" in f:
            lines.append(f"  Extracted mint: `{f.get('extracted_mint', 'n/a')}`")
            lines.append(f"  Mint confirmed (ends in 'pump'): `{f.get('mint_confirmed')}`")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def read_live_discoveries(event_type: str, max_age_seconds: int = 3600) -> list:
    """
    Reads recent entries from live_discoveries.jsonl (written by
    live_listener.py, if it's running). Returns [] if the file doesn't
    exist or has nothing recent - callers should fall back to the
    on-demand scan in that case.
    """
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


async def graduated_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Live feed of tokens that just graduated from pump.fun to PumpSwap (the real, current graduation mechanism)."""
    live = read_live_discoveries("migration")
    if live:
        shown = live[:8]
        if len(live) > len(shown):
            await update.message.reply_text(
                f"🎓 {len(live)} graduation(s) caught live - showing the {len(shown)} most recent:"
            )
        else:
            await update.message.reply_text(f"🎓 {len(shown)} graduation(s) caught live by the listener:")
        for mig in shown:
            try:
                mint = mig.get("mint")
                if not mint:
                    continue
                card, keyboard, error = await build_token_card(mint)
                if error:
                    logger.warning(f"Auto-analysis failed for {mint}: {error}")
                    continue
                ago = format_age(int((time.time() - mig["discovered_at"]) // 60))
                card = f"🎓 *Graduated ~{ago} ago (live)*\n\n" + card
                await update.message.reply_text(card, parse_mode="Markdown", reply_markup=keyboard)
            except Exception as e:
                logger.warning(f"Failed to send graduation entry for {mig.get('mint')}: {e}")
        return

    await update.message.reply_text(
        "No live listener running (or nothing caught yet) - falling back to on-demand scan.\n"
        "🎓 Scanning for recent pump.fun -> PumpSwap graduations... (this may take 20-30s)",
    )

    result = get_recent_migrations(config.HELIUS_API_KEY, config.SOLANA_RPC_URL, limit=8, lookback=config.LAUNCHING_LOOKBACK)

    if not result["ok"]:
        await update.message.reply_text(f"Couldn't scan for graduations: {result['error']}")
        return

    if not result["tokens"]:
        await update.message.reply_text(result.get("note") or "No graduations found in this window.")
        return

    for mig in result["tokens"]:
        ago = format_age(mig["seconds_ago"] // 60) if mig["seconds_ago"] and mig["seconds_ago"] >= 60 else f"{mig.get('seconds_ago', '?')}s"
        lines = [f"🎓 *Graduated {ago} ago*"]

        if mig.get("mint") and mig.get("mint_confirmed"):
            lines.append(f"Mint: `{mig['mint']}`")
            safety = check_safety(mig["mint"])
            if safety.get("mint_authority"):
                lines.append("⚠️ Mint authority still active")
            if safety.get("freeze_authority"):
                lines.append("⚠️ Freeze authority still active")
            if not safety.get("mint_authority") and not safety.get("freeze_authority"):
                lines.append("✅ No mint/freeze authority red flags")
        elif mig.get("mint"):
            lines.append(f"Mint: `{mig['mint']}` ⚠️ _unconfirmed - doesn't match pump.fun's usual address pattern, verify on Solscan before trusting_")
        else:
            lines.append("Mint: unknown (couldn't extract from this transaction)")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def launching_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    live = read_live_discoveries("cpmm_creation")
    live_future = [r for r in live if r.get("is_future")]
    if live_future:
        await update.message.reply_text(f"⏰ {len(live_future)} delayed-open pool(s) caught live:")
        for pool in live_future[:8]:
            when = datetime.datetime.fromtimestamp(pool["open_timestamp"]).strftime("%Y-%m-%d %H:%M")
            await update.message.reply_text(
                f"⏰ *Opens at {when}* (caught live, not a decode guess)\n"
                f"Signature: `{pool.get('signature', 'unknown')}`",
                parse_mode="Markdown",
            )
        return

    await update.message.reply_text(
        "No live listener running (or nothing caught yet) - falling back to on-demand scan.\n"
        "🔎 Scanning for Raydium CPMM pools with a future open time... "
        "(this may take 20-30s - NOTE: since March 2025 most pump.fun graduations go to "
        "PumpSwap, not Raydium, so this specifically only catches manual/direct Raydium "
        "launches, which are less common now. Try /graduated for the more common path.)\n",
    )

    result = get_upcoming_pool_launches(config.HELIUS_API_KEY, config.SOLANA_RPC_URL, limit=8, lookback=config.LAUNCHING_LOOKBACK)

    if not result["ok"]:
        await update.message.reply_text(f"Couldn't scan for upcoming launches: {result['error']}")
        return

    if not result["tokens"]:
        await update.message.reply_text(result.get("note") or "No upcoming launches found right now.")
        return

    for pool in result["tokens"]:
        secs = pool["seconds_until_open"]
        countdown = format_age(secs // 60) if secs >= 60 else f"{secs}s"

        lines = [
            f"⏰ *Opens in ~{countdown}* ⚠️ _unverified decode - check manually before trusting_",
            f"Mint: `{pool['mint'] or 'unknown'}`",
            f"Pool: `{pool['pool_address'] or 'unknown'}`",
        ]

        # Pull the same research you'd want before buying anything else
        if pool.get("creator"):
            dev_rep = get_deployer_reputation(pool["creator"], config.HELIUS_API_KEY)
            if dev_rep.get("ok"):
                if dev_rep["is_brand_new_wallet"]:
                    lines.append("👤 Dev wallet: brand new (little/no history)")
                else:
                    lines.append(
                        f"👤 Dev wallet: {dev_rep['txn_count_sampled']} txns, "
                        f"~{dev_rep['likely_tokens_created']} look like token creations"
                    )

        if pool.get("mint"):
            safety = check_safety(pool["mint"])
            if safety.get("mint_authority"):
                lines.append("⚠️ Mint authority still active")
            if safety.get("freeze_authority"):
                lines.append("⚠️ Freeze authority still active")
            if not safety.get("mint_authority") and not safety.get("freeze_authority"):
                lines.append("✅ No mint/freeze authority red flags")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def get_raw_creation_record(mint: str) -> dict:
    """Returns the raw PumpPortal 'new_token' record our own listener
    captured at creation, if any - used as a fallback when DexScreener
    hasn't indexed a trading pair yet (very common for tokens seconds old,
    exactly the case /analyse is often used for)."""
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


def format_minimal_card(mint: str, raw_record: dict, sol_price_usd: float, safety: dict = None) -> str:
    """Fallback card built from our own captured creation data, for when
    DexScreener hasn't indexed this token yet (common for very fresh
    tokens - which is exactly when /analyse tends to get used)."""
    raw = raw_record.get("raw", {})
    name = raw.get("name") or raw_record.get("name") or "?"
    symbol = raw.get("symbol") or raw_record.get("symbol") or "?"

    lines = [
        f"*{name} ({symbol})*",
        f"`{mint}`",
        "",
        "_⚠️ Too new for DexScreener yet - showing what was captured at creation. "
        "Try /analyse again in a minute for full price/liquidity data._",
        "",
    ]

    v_sol = raw.get("vSolInBondingCurve")
    v_tokens = raw.get("vTokensInBondingCurve")
    if v_sol and v_tokens and sol_price_usd > 0:
        launch_price = (v_sol / v_tokens) * sol_price_usd
        lines.append(f"Launch price: `${launch_price:.8f}`")
    if raw.get("marketCapSol") and sol_price_usd > 0:
        lines.append(f"Market Cap at creation: `${raw['marketCapSol'] * sol_price_usd:,.0f}`")
    if raw.get("solAmount"):
        lines.append(f"Initial buy: `{raw['solAmount']:.3f} SOL`")

    ago = int(time.time() - raw_record.get("discovered_at", time.time()))
    ago_str = f"{ago}s ago" if ago < 60 else format_age(ago // 60) + " ago"
    lines.append(f"Caught: `{ago_str}`")

    if safety:
        lines.append("")
        lines.append("*— Contract —*")
        min_check_failed = not safety.get("is_safe") and "API check failed" in str(safety.get("reason", ""))
        contract_line = "✅ Contract looks safe" if safety.get("is_safe") else f"❌ Contract UNSAFE ({safety.get('reason')})"
        lines.append(contract_line)
        if min_check_failed:
            lines.append("Renounced: ⚫ Unknown (check failed)")
        else:
            renounced = not safety.get("mint_authority")
            lines.append(f"Renounced: {'✅' if renounced else '❌ Mint authority still active'}")
        if safety.get("freeze_authority"):
            lines.append("❌ Freeze authority still active")

    lines.append("")
    lines.append("_This is a PAPER trade - no real funds involved._")
    return "\n".join(lines)


async def build_token_card(mint: str):
    """Builds the (card_text, keyboard) pair for a token - shared by
    handle_ca and the Refresh button so both stay in sync."""
    info = get_token_info(mint)
    if not info["ok"]:
        raw_record = get_raw_creation_record(mint)
        if raw_record:
            sol_price = get_sol_usd_price()
            safety = check_safety(mint)  # doesn't depend on DexScreener, try it anyway
            card = format_minimal_card(mint, raw_record, sol_price, safety)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data=f"cardrefresh:{mint}"),
            ]])
            return card, keyboard, None
        return None, None, info.get("error", "unknown error")

    safety = check_safety(mint)
    dev_rep = None
    if safety.get("creator"):
        dev_rep = get_deployer_reputation(safety["creator"], config.HELIUS_API_KEY)

    sol_price = get_sol_usd_price()
    launch = get_launch_price(mint, sol_price) if sol_price > 0 else {"ok": False}

    card = format_token_card(mint, info, safety, dev_rep, launch)

    keyboard_rows = [[
        InlineKeyboardButton(f"Buy {config.DEFAULT_BUY_SIZE_SOL} SOL", callback_data=f"buy:{mint}:{info['symbol']}"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"cardrefresh:{mint}"),
    ]]

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


async def analyse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    replied = update.message.reply_to_message
    if not replied or not replied.text:
        await update.message.reply_text(
            "Reply to a message that shows a token (e.g. one from /new or /graduated) "
            "with /analyse to get the full breakdown for that coin."
        )
        return

    match = MINT_SEARCH_PATTERN.search(replied.text)
    if not match:
        await update.message.reply_text("Couldn't find a token address in that message.")
        return

    mint = match.group(0)
    await update.message.reply_text(f"🔎 Analysing `{mint}`...", parse_mode="Markdown")

    card, keyboard, error = await build_token_card(mint)
    if error:
        await update.message.reply_text(f"Couldn't analyse that token: {error}")
        return

    await update.message.reply_text(card, parse_mode="Markdown", reply_markup=keyboard)


async def handle_ca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not MINT_PATTERN.match(text):
        await update.message.reply_text("That doesn't look like a token address. Send a valid Solana CA.")
        return

    mint = text
    await update.message.reply_text("🔎 Checking token...")

    card, keyboard, error = await build_token_card(mint)
    if error:
        await update.message.reply_text(f"Couldn't find price data: {error}")
        return

    await update.message.reply_text(card, parse_mode="Markdown", reply_markup=keyboard)


# ---------------- button handlers ----------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    data = query.data

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
            await query.message.reply_text("Price data unavailable right now, try again shortly.")
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
            f"Entry price: `${info['price_usd']:.8f}`\n"
            f"New balance: {wallet.get_balance(user_id):.3f} SOL",
            parse_mode="Markdown",
        )

    elif data.startswith("sell:"):
        _, mint = data.split(":", 1)
        info = get_token_info(mint)
        sol_price = get_sol_usd_price()
        if not info["ok"] or sol_price <= 0:
            await query.message.reply_text("Price data unavailable right now, try again shortly.")
            return

        result = wallet.sell(user_id, mint, info["price_usd"], sol_price)
        if not result["ok"]:
            await query.message.reply_text(f"❌ {result['error']}")
            return

        emoji = "🟢" if result["pnl_sol"] >= 0 else "🔴"
        await query.message.reply_text(
            f"{emoji} Sold *{result['symbol']}*\n"
            f"Entry: `${result['entry_price_usd']:.8f}`  →  Exit: `${result['exit_price_usd']:.8f}`\n"
            f"P&L: `{result['pnl_sol']:+.4f} SOL` (`{result['pnl_percent']:+.2f}%`)\n"
            f"New balance: {wallet.get_balance(user_id):.3f} SOL",
            parse_mode="Markdown",
        )


# ---------------- background job: live position updates ----------------

async def push_position_updates(context: ContextTypes.DEFAULT_TYPE):
    sol_price = get_sol_usd_price()
    if sol_price <= 0:
        return
    for user_id, user_data in wallet.users.items():
        positions = user_data.get("open_positions", {})
        if not positions:
            continue
        for mint, pos in positions.items():
            info = get_token_info(mint)
            if not info["ok"]:
                continue
            wallet.record_price_snapshot(user_id, mint, info["price_usd"])
            text = format_position_update(mint, pos, info["price_usd"])
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Sell", callback_data=f"sell:{mint}")]])
            try:
                await context.bot.send_message(
                    chat_id=int(user_id), text=text, parse_mode="Markdown", reply_markup=keyboard
                )
            except Exception as e:
                logger.warning(f"Failed to push update to {user_id}: {e}")


async def daily_topup_job(context: ContextTypes.DEFAULT_TYPE):
    for user_id in list(wallet.users.keys()):
        wallet.apply_daily_topup(user_id)


def start_keepalive_server():
    """
    Minimal HTTP server so Render's free tier treats this as a 'web
    service' (which is free) instead of a 'background worker' (which
    isn't). Runs in a background thread; an external pinger (e.g.
    UptimeRobot, free) hits this URL every few minutes to keep the
    service from sleeping. Does nothing on your own PC - harmless there.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import os

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Solana paper bot is alive")

        def log_message(self, format, *args):
            pass  # suppress noisy request logs

    port = int(os.environ.get("PORT", 8080))  # Render sets $PORT automatically
    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Keep-alive HTTP server listening on port {port}")


async def start_live_listener_task(application):
    """
    Runs live_listener.listen() as a background task IN THIS SAME PROCESS,
    so it shares memory/disk with the bot - fixes the two-separate-Render-
    services problem, where live_discoveries.jsonl written by one service
    was invisible to the other. Now there's only one process, one disk,
    same as running both scripts locally in the same folder.
    """
    asyncio.create_task(live_listener.listen())
    logger.info("Started PumpPortal live listener as a background task.")


def main():
    start_keepalive_server()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(start_live_listener_task).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("positions", positions_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("activity", activity_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("launching", launching_cmd))
    app.add_handler(CommandHandler("graduated", graduated_cmd))
    app.add_handler(CommandHandler("checktx", checktx_cmd))
    app.add_handler(CommandHandler(["analyse", "analysis"], analyse_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ca))

    job_queue = app.job_queue
    job_queue.run_repeating(push_position_updates, interval=config.PRICE_UPDATE_INTERVAL_SECONDS, first=30)
    job_queue.run_repeating(daily_topup_job, interval=3600, first=10)  # checked hourly, only applies once/day

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
