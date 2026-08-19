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

# --- Jupiter (Real Trading) ---
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
JUPITER_PRICE_URL = "https://api.jup.ag/price/v2"

# --- Persistent storage ---
JSONBIN_API_KEY = _env("JSONBIN_API_KEY", "$2a$10$xTIlkcWytMa.MtSe9MP1T.FCzHw1EHM90OGe3sl/RuiUSnFa9yVqK")
JSONBIN_BIN_ID = _env("JSONBIN_BIN_ID", "6a766e16da38895dfec84c4e")

# --- Paper wallet settings ---
STARTING_BALANCE_SOL = 1.0
DAILY_TOPUP_SOL = 0.2
BALANCE_CAP_SOL = 5.0
DEFAULT_BUY_SIZE_SOL = 0.1

# --- Real Wallet Settings ---
# The bot generates its OWN dedicated wallet. You fund it from Phantom.
# NEVER put your Phantom private key here.
BOT_WALLET_ENCRYPTED_KEY = _env("BOT_WALLET_ENCRYPTED_KEY", "")
# Minimum SOL to keep in bot wallet for fees
MIN_SOL_RESERVE = 0.01
# Max slippage % for real trades
MAX_SLIPPAGE_BPS = 250  # 2.5%

# --- Position tracking ---
PRICE_UPDATE_INTERVAL_SECONDS = 15

# --- Sell recommendation thresholds ---
TAKE_PROFIT_PERCENT = 50.0
STOP_LOSS_PERCENT = -20.0

# --- Safety ---
SELL_TAX_THRESHOLD = 0.10
LAUNCHING_LOOKBACK = 2000

# --- NEW: Goldmine / Conviction Engine Settings ---
GOLDMINE_ALERT_THRESHOLD = 75
AUTO_BUY_THRESHOLD = 85
BONDING_CURVE_START_SOL = 30.0
BONDING_CURVE_GRADUATE_SOL = 85.0
WHALE_LAUNCH_THRESHOLD_SOL = 5.0

# --- NEW: Premium Signals ---
PREMIUM_SIGNAL_THRESHOLD = 80      # Only signals >=80 get sent as premium
PREMIUM_MIN_LIQUIDITY_USD = 5000   # Premium tokens need at least $5k liq
PREMIUM_MAX_AGE_MINUTES = 30       # Only tokens < 30 min old for premium
PREMIUM_COOLDOWN_SECONDS = 300     # Don't spam same token

# --- NEW: Speed / Cache Settings ---
PRICE_CACHE_TTL_SECONDS = 8        # Cache prices for 8s to reduce API lag
SAFETY_CACHE_TTL_SECONDS = 60      # Cache RugCheck for 60s
MAX_CONCURRENT_REQUESTS = 10       # For async batch fetching

# --- NEW: Whale / Smart Money Alerts ---
WHALE_ALERT_MIN_USD = 1000         # Alert when someone buys/sells >$1k
WHALE_ALERT_MIN_SOL = 2.0          # Or >2 SOL
TRACK_TOP_HOLDERS_COUNT = 10       # How many top wallets to track

# --- NEW: Auto-refresh Settings ---
POSITION_AUTO_REFRESH_SECONDS = 20  # How often to push live position updates
EARLY_STAGE_ALERT_SECONDS = 60      # Alert on tokens < 60s old

# --- Files ---
WALLET_STATE_FILE = "paper_wallet.json"
REAL_WALLET_FILE = "real_wallet.json"
PREMIUM_STATE_FILE = "premium_state.json"
