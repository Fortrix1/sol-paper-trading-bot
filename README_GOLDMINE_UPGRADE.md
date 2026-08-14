# 🚀 Solana Goldmine Bot — Complete Upgrade Pack

## What You Just Got

This upgrade transforms your paper-trading bot into a **goldmine detection engine** with conviction scoring, autopilot auto-buy, **bonded curve sniping**, **bundle detection**, and **smart money tracking**.

### NEW Features in This Pack
- **🔥 Conviction Engine** — Scores every token 0-100 based on 7 signals
- **🚨 Goldmine Alerts** — Bot DMs you when it finds a token scoring ≥75/100
- **🤖 Autopilot Mode** — Toggle `/autopilot` ON and bot auto-buys ≥85/100 in paper mode
- **🔗 /bonded Command** — Shows tokens **still in the bonding curve** (earliest stage before graduation), sorted by progress (70-95% = snipe zone)
- **🎓 /graduated Command** — Shows tokens that **just left** the bonding curve (live PumpPortal data first, then Helius fallback)
- **🎭 Bundle/Sybil Detection** — Heuristic detection of suspicious holder patterns (free, no paid APIs)
- **🏆 Smart Money Tracker** — Auto-builds a database of dev wallets from your winning trades. Alerts when known winners launch again
- **📊 Bonding Curve Intelligence** — Calculates graduation progress from PumpPortal live data
- **🐋 Whale Launch Detection** — Flags tokens where dev bought >5 SOL on launch
- **🎯 Auto Exit** — Automatically paper-sells at +50% TP or -20% SL every 60 seconds
- **🚀 APE IN Button** — High-conviction tokens get a big red "APE IN" button

---

## 📁 Files to Replace

| File | Action | Notes |
|------|--------|-------|
| `config.py` | **Replace** | New settings for conviction thresholds, bonding curve, autopilot |
| `live_listener.py` | **Replace** | Captures bonding progress, initial buy, dev wallet, whale flags |
| `telegram_bot.py` | **Replace** | Major upgrade with all new commands and features |
| `conviction_engine.py` | **NEW** | The scoring brain + bundle detection |
| `auto_paper_trader.py` | **NEW** | Background scanner + auto-buy logic |
| `smart_money_tracker.py` | **NEW** | Self-building smart money database |
| `requirements.txt` | **Replace** | Same deps, cleaned up |

**Keep these as-is:** `honeypot_check.py`, `helius_check.py`, `price_feed.py`, `paper_wallet.py`, `risk_manager.py`, `new_scanner.py`, `launch_watcher.py`, `shadow_trader.py`, `bot_cli.py`, `run_bot.py`, `jsonbin_storage.py`

---

## ⚙️ Setup Steps

### 1. Replace the files
```bash
# Backup your old files first
cp config.py config.py.old
cp live_listener.py live_listener.py.old
cp telegram_bot.py telegram_bot.py.old

# Copy the new files from this pack
cp /path/to/new/config.py .
cp /path/to/new/live_listener.py .
cp /path/to/new/telegram_bot.py .
cp /path/to/new/conviction_engine.py .
cp /path/to/new/auto_paper_trader.py .
cp /path/to/new/smart_money_tracker.py .
cp /path/to/new/requirements.txt .
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure thresholds (optional)
Edit `config.py`:
```python
GOLDMINE_ALERT_THRESHOLD = 75      # Alert you at this score
AUTO_BUY_THRESHOLD = 85            # Autopilot buys at this score
BONDING_CURVE_START_SOL = 30.0     # PumpPortal bonding start
BONDING_CURVE_GRADUATE_SOL = 85.0  # PumpPortal graduation point
WHALE_LAUNCH_THRESHOLD_SOL = 5.0   # Flag as whale if initial buy > this
```

### 4. Run the bot
```bash
# Terminal 1: Live listener (catches tokens in real-time)
python live_listener.py

# Terminal 2: Telegram bot (alerts, trading, autopilot)
python telegram_bot.py
```

---

## 🎮 How to Use

### /bonded — The Earliest Stage
Shows tokens **still in the pump.fun bonding curve** — this is BEFORE they graduate to PumpSwap. Sorted by graduation progress (highest first).

- **70-95%** = 🎯 Pre-graduation sweet spot. Buy here, sell at the graduation pump.
- **< 30%** = Very early, high risk
- **> 95%** = Graduating now, might be too late

### /graduated — Just Left the Curve
Shows tokens that **just graduated** from pump.fun to PumpSwap. Uses live PumpPortal data first (catches within seconds), then falls back to Helius scanning.

### Manual Mode
1. Start both scripts
2. Bot sends `🚨 GOLDMINE DETECTED` when it finds something hot
3. Tap **APE IN** to paper-buy with fake SOL
4. Watch `/positions` for live P&L
5. Sell manually or let auto-exit handle it

### Autopilot Mode
1. Send `/autopilot` in Telegram
2. Bot toggles ON
3. Any token scoring ≥85 auto-bought in paper mode
4. You get confirmation: "🤖 AUTO-PILOT BUY"

### Smart Money Tracking
- The bot **automatically** tracks dev wallets from your winning trades
- Use `/smartmoney` to see the leaderboard
- Use `/addsmart <wallet> <tag>` to manually add wallets from Solscan/GMGN
- When a known "smart money" dev launches a new token, the bot boosts its score and flags it

### Check Any Token
Send a CA directly — shows conviction score, verdict, bonding progress, bundle risk, and smart money status.

---

## 📊 Understanding the Score

| Score | Verdict | Meaning |
|-------|---------|---------|
| 85-100 | 🚀 APE | High conviction, multiple green flags |
| 70-84 | 🟢 STRONG_BUY | Good setup, worth considering |
| 55-69 | 🟡 WATCH | Mixed signals, monitor only |
| 40-54 | 🟠 WEAK | Several red flags |
| 0-39 | 🔴 SKIP | Unsafe or no data |

### Signal Breakdown
- **Safety (22%)** — RugCheck results, mint/freeze authorities, LP lock
- **Bonding Progress (18%)** — 70-95% = about to graduate (best entry)
- **Dev Rep (13%)** — Wallet history, smart money tags, serial rugger detection
- **Initial Buy (10%)** — >5 SOL = whale/cabal launch
- **Socials (10%)** — Twitter, Telegram, Website presence
- **Age (9%)** — <5 min = earliest entry
- **Liquidity (9%)** — High liq + volume = safer
- **Bundle Risk (9%)** — Penalizes sybil patterns and suspicious holder distributions

---

## 🔍 Honest Accuracy Report

### RugCheck (Safety)
- ✅ **Best free option** for contract-level checks
- ⚠️ Can lag on tokens < 2 minutes old
- ❌ Does NOT detect bundles, MEV, or insider wallets
- **My code defaults to UNSAFE if the API fails** — never gives false confidence

### Price Accuracy
| Token Age | Source | Accuracy |
|-----------|--------|----------|
| > 5 min | DexScreener | ✅ Real market price |
| < 2 min | PumpPortal creation data | ⚠️ Approximation (launch price from bonding curve) |
| Not indexed | Fallback | ⚠️ "Best guess" — clearly labeled as such |

### Bundle Detection
- **What I built:** Heuristic based on top holder patterns + dev buy vs dev holding mismatch
- **What this catches:** Sybil attacks where dev splits supply across multiple wallets
- **What this misses:** Sophisticated bundles that use wildly different wallet sizes
- **To do it properly:** You need gRPC/Geyser streaming or Bitquery (paid)

### Graduated Command
- ✅ **Live PumpPortal data** catches graduations within seconds
- ✅ **Helius fallback** scans the correct migration wrapper program (verified against 968-star reference bot)
- ⚠️ Helius scan takes 20-30s and can miss very fresh ones if pagination is slow

---

## 🔄 When Ready for Real Money

**DO NOT send your Phantom private key to the bot.**

Instead:
1. I'll write you a `real_trader.py` module
2. The bot generates its **own dedicated wallet**
3. You fund it from Phantom (like a "trading allowance")
4. If compromised, only the bot's funds are lost
5. Swap `PaperWallet` → `RealTrader` in `telegram_bot.py` (2 lines)

**The risk_manager, conviction_engine, and auto-exit logic stay exactly the same.**

---

## 🗺️ Roadmap

| Phase | Feature | Status |
|-------|---------|--------|
| ✅ Now | Paper trading + conviction scoring | DONE |
| ✅ Now | Goldmine alerts + autopilot | DONE |
| ✅ Now | Auto TP/SL exit | DONE |
| ✅ Now | /bonded + /graduated | DONE |
| ✅ Now | Bundle detection (heuristic) | DONE |
| ✅ Now | Smart money tracking | DONE |
| 🔜 Next | Real trading via Jupiter API | Ready when you are |
| 🔜 Next | True bundle detection (tx parsing) | Paid tier |
| 🔜 Next | MEV protection (Jito bundles) | Paid tier |

---

**Test this for 2 weeks with paper SOL. Track your win rate in `/stats`. When you're consistently green, we'll flip the switch to real trading.**
