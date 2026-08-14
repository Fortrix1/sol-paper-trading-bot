# config.py
# Keep this file private - do NOT post it or commit it anywhere public.
# SECURITY: Move API keys to environment variables for production:
#   export TELEGRAM_BOT_TOKEN="your_token"
#   export RUGCHECK_API_KEY="your_key"
#   etc.

import os

def _env(key, default=""):
    """Helper to read env vars with fallback."""
    return os.environ.get(key, default)

# --- Telegram ---
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "8945265024:AAEH6mMIMkNMi8xgBaK2-UJbQFEcVikGhJo")

# --- External API keys ---
RUGCHECK_API_KEY = _env("RUGCHECK_API_KEY", "c0a45deb-810e-4c14-b50b-8aae8a0b758f")
HELIUS_API_KEY = _env("HELIUS_API_KEY", "fe9608bf-2c9e-4825-8526-c77d667dac7e")
SOLANATRACKER_API_KEY = _env("SOLANATRACKER_API_KEY", "b8fd4428-bf41-4810-8fc0-4f789dc8db9e")

# Solana RPC endpoint bundled with the RugCheck account
SOLANA_RPC_URL = f"https://eu.fluxrpc.com?key={RUGCHECK_API_KEY}"

# --- Persistent storage ---
JSONBIN_API_KEY = _env("JSONBIN_API_KEY", "$2a$10$xTIlkcWytMa.MtSe9MP1T.FCzHw1EHM90OGe3sl/RuiUSnFa9yVqK")
JSONBIN_BIN_ID = _env("JSONBIN_BIN_ID", "6a766e16da38895dfec84c4e")

# --- Paper wallet settings ---
STARTING_BALANCE_SOL = 1.0
DAILY_TOPUP_SOL = 0.2
BALANCE_CAP_SOL = 5.0
DEFAULT_BUY_SIZE_SOL = 0.1

# --- Position tracking ---
PRICE_UPDATE_INTERVAL_SECONDS = 15

# --- Sell recommendation thresholds ---
TAKE_PROFIT_PERCENT = 50.0
STOP_LOSS_PERCENT = -20.0

# --- Safety ---
SELL_TAX_THRESHOLD = 0.10
LAUNCHING_LOOKBACK = 2000

# --- NEW: Goldmine / Conviction Engine Settings ---
# Score 0-100. Above this = "goldmine" alert sent to you
GOLDMINE_ALERT_THRESHOLD = 75

# Above this = bot AUTO-BUYS in paper mode (if autopilot is ON)
AUTO_BUY_THRESHOLD = 85

# Bonding curve graduation thresholds (PumpPortal data)
BONDING_CURVE_START_SOL = 30.0
BONDING_CURVE_GRADUATE_SOL = 85.0

# Whale launch detection: initial buy above this SOL = flagged
WHALE_LAUNCH_THRESHOLD_SOL = 5.0

# --- NEW: Real Trading Prep (for future use) ---
# NEVER put your Phantom private key here. The bot will generate its own
# dedicated trading wallet. You fund it from Phantom like a "trading allowance."
# If the bot gets compromised, only this wallet's funds are at risk.
BOT_WALLET_ENCRYPTED_KEY = _env("BOT_WALLET_ENCRYPTED_KEY", "")

# --- Files ---
WALLET_STATE_FILE = "paper_wallet.json"
