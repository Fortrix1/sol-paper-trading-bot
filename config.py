# config.py
# Keep this file private - do NOT post it or commit it anywhere public.
# If this token is ever exposed, revoke it via @BotFather (/revoke) and
# generate a new one with /token, then paste the new one here.

TELEGRAM_BOT_TOKEN = "8945265024:AAEH6mMIMkNMi8xgBaK2-UJbQFEcVikGhJo"

# --- External API keys ---
RUGCHECK_API_KEY = "c0a45deb-810e-4c14-b50b-8aae8a0b758f"
HELIUS_API_KEY = "fe9608bf-2c9e-4825-8526-c77d667dac7e"

SOLANATRACKER_API_KEY = "b8fd4428-bf41-4810-8fc0-4f789dc8db9e"

# Solana RPC endpoint bundled with the RugCheck account - used for direct
# on-chain account reads (e.g. decoding Raydium pool state for /launching)
SOLANA_RPC_URL = f"https://eu.fluxrpc.com?key={RUGCHECK_API_KEY}"

# --- Persistent storage (survives Render redeploys) ---
# Leave both blank to just use local files (fine for running on your PC).
# To enable: get a free API key from jsonbin.io, paste it below, run the
# bot ONCE, check the logs for "Created new bin: <id>", then paste that
# ID into JSONBIN_BIN_ID and redeploy - after that it reuses the same
# bin forever instead of creating a new one each restart.
JSONBIN_API_KEY = "$2a$10$P47Cqc9MszALrN0QmSs6q.wlIHPi2QDfs9eW3j2Wo03avmytyeywO"
JSONBIN_BIN_ID = ""

# --- Paper wallet settings ---
STARTING_BALANCE_SOL = 1.0      # what a brand-new user starts with
DAILY_TOPUP_SOL = 0.2           # added once per day
BALANCE_CAP_SOL = 5.0           # topups stop once balance would exceed this
DEFAULT_BUY_SIZE_SOL = 0.1      # fake SOL spent per Buy tap

# --- Position tracking ---
PRICE_UPDATE_INTERVAL_SECONDS = 15    # how often to auto-push P&L updates
# NOTE: DexScreener and Telegram will start rate-limiting/blocking you if
# this goes much below ~10 seconds, especially with several positions open
# at once. 15s is a reasonably fast floor that should stay reliable.

# --- Sell recommendation thresholds ---
# Each auto-update tells you whether to consider selling based on these.
TAKE_PROFIT_PERCENT = 50.0    # suggest SELL if up this much or more
STOP_LOSS_PERCENT = -20.0     # suggest SELL if down this much or more

# --- Safety ---
SELL_TAX_THRESHOLD = 0.10       # matches honeypot_check.py default

# How many CPMM transactions /launching scans per run. Each 100 = one
# Helius API call, so this trades speed/rate-limit risk for a better
# chance of catching a genuine delayed-open pool creation (a naturally
# rare event). 2000 = ~20 sequential calls, will take noticeably longer
# to run than before. If you hit Helius rate limits, lower this.
LAUNCHING_LOOKBACK = 2000

# --- Files ---
WALLET_STATE_FILE = "paper_wallet.json"
