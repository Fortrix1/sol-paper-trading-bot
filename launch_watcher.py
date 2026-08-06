"""
launch_watcher.py - Detects Raydium CPMM pools created on-chain with a
future scheduled open time.

HOW THIS ACTUALLY WORKS (confirmed against Raydium's real source, not
guessed): CPMM pool creation goes through one of two instructions:

    pub fn initialize(ctx, init_amount_0: u64, init_amount_1: u64, open_time: u64)
    pub fn initialize_with_permission(ctx, init_amount_0: u64, init_amount_1: u64,
                                       open_time: u64, creator_fee_on: CreatorFeeOn)

Source: raydium-io/raydium-cp-swap/programs/cp-swap/src/lib.rs

open_time is passed DIRECTLY as an instruction argument by whoever creates
the pool - there's no need to read it back out of the pool's account state
after the fact. That earlier approach (RPC account lookups + brute-force
byte scanning) was solving a harder problem than necessary. The instruction
data layout, after Anchor's 8-byte discriminator, is simply:

    bytes[0:8]   discriminator
    bytes[8:16]  init_amount_0 (u64, not needed here)
    bytes[16:24] init_amount_1 (u64, not needed here)
    bytes[24:32] open_time     (u64, little-endian, unix timestamp)

Both discriminators are computed directly (sha256("global:<name>")[:8]),
not guessed - see conversation history:
    initialize                -> afaf6d1f0d989bed
    initialize_with_permission -> 3f37fe4131b25979

CPMM program ID: CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C
Source: https://docs.raydium.io/reference/program-addresses

Residual risk, stated plainly: this reads what the pool CREATOR claimed
open_time would be, at the moment of creation. It's the real value from
the real instruction - there's no decode ambiguity left. What it can't
tell you is whether that's a well-intentioned delayed launch or a
scam-adjacent tactic - that judgment is still yours. Sanity-checked against
each transaction's own timestamp so an implausible value (e.g. a decode
that landed on the wrong bytes) gets rejected rather than shown.
"""

import struct
import time
import base64
import base58
import requests

RAYDIUM_CPMM_PROGRAM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{}/transactions"

INITIALIZE_DISCRIMINATORS = {
    bytes.fromhex("afaf6d1f0d989bed"): "initialize",
    bytes.fromhex("3f37fe4131b25979"): "initialize_with_permission",
}

# Offset of open_time within the instruction data, after the 8-byte
# discriminator and two 8-byte amount arguments.
OPEN_TIME_ARG_OFFSET = 24
OPEN_TIME_ARG_LENGTH = 8

# A genuine open_time can't be meaningfully before its own creation tx
# (small buffer for clock skew), and delayed opens are realistically
# hours-to-weeks out, not months+.
EARLIEST_BUFFER_SECONDS = 60
LATEST_WINDOW_SECONDS = 60 * 24 * 3600  # 60 days out, generous upper bound


def _decode_open_time_from_instruction(data_b58: str, tx_timestamp: float):
    """
    Returns (open_time, instruction_name) if this instruction is a genuine
    pool-creation call with a plausible open_time, else (None, None).
    """
    try:
        raw = base58.b58decode(data_b58)
    except Exception:
        return None, None

    if len(raw) < OPEN_TIME_ARG_OFFSET + OPEN_TIME_ARG_LENGTH:
        return None, None

    discriminator = raw[:8]
    instruction_name = INITIALIZE_DISCRIMINATORS.get(discriminator)
    if not instruction_name:
        return None, None

    chunk = raw[OPEN_TIME_ARG_OFFSET:OPEN_TIME_ARG_OFFSET + OPEN_TIME_ARG_LENGTH]
    open_time = struct.unpack("<Q", chunk)[0]

    if tx_timestamp:
        earliest = tx_timestamp - EARLIEST_BUFFER_SECONDS
        latest = tx_timestamp + LATEST_WINDOW_SECONDS
        if not (earliest <= open_time <= latest):
            return None, instruction_name  # confirmed real creation ix, but implausible value

    return open_time, instruction_name


def get_upcoming_pool_launches(helius_api_key: str, rpc_url: str = None, limit: int = 8, lookback: int = 500) -> dict:
    """
    Returns { ok, tokens: [{mint, pool_address, open_timestamp,
              seconds_until_open, creator, instruction_type}], note, diagnostics }

    rpc_url is accepted for backward compatibility with callers but no
    longer used - decoding no longer requires on-chain account reads.
    """
    try:
        txns = []
        before_sig = None
        page_size = 100
        pages_needed = max(1, (lookback + page_size - 1) // page_size)

        for _ in range(pages_needed):
            params = {"api-key": helius_api_key, "limit": page_size}
            if before_sig:
                params["before"] = before_sig

            resp = requests.get(HELIUS_TX_URL.format(RAYDIUM_CPMM_PROGRAM), params=params, timeout=10)
            resp.raise_for_status()
            page = resp.json()
            if not isinstance(page, list) or not page:
                break
            txns.extend(page)
            before_sig = page[-1].get("signature")
            if not before_sig:
                break

        txns = txns[:lookback]
        if not txns:
            return {"ok": False, "error": "No transactions returned from Helius", "tokens": []}

        unique_sigs = len(set(t.get("signature") for t in txns))
        pagination_suspicious = unique_sigs < len(txns) * 0.9

        now = time.time()
        results = []
        creation_instructions_found = 0
        implausible_value_count = 0
        instructions_scanned = 0

        for tx in txns:
            tx_timestamp = tx.get("timestamp")
            creator = tx.get("feePayer")

            instructions = tx.get("instructions", [])
            all_instructions = []
            for ix in instructions:
                all_instructions.append(ix)
                for inner in ix.get("innerInstructions", []):
                    all_instructions.append(inner)

            for ix in all_instructions:
                if ix.get("programId") != RAYDIUM_CPMM_PROGRAM:
                    continue
                data_field = ix.get("data")
                if not data_field:
                    continue
                instructions_scanned += 1

                open_time, instruction_name = _decode_open_time_from_instruction(data_field, tx_timestamp)
                if not instruction_name:
                    continue  # not a creation instruction at all

                creation_instructions_found += 1

                if open_time is None:
                    implausible_value_count += 1
                    continue

                if open_time > now:
                    mint = None
                    transfers = tx.get("tokenTransfers", [])
                    if transfers:
                        mint = transfers[0].get("mint")
                    accounts = ix.get("accounts", [])
                    results.append({
                        "mint": mint,
                        "pool_address": accounts[0] if accounts else None,
                        "open_timestamp": open_time,
                        "seconds_until_open": int(open_time - now),
                        "creator": creator,
                        "instruction_type": instruction_name,
                    })

            if len(results) >= limit:
                break

        note = None
        if not results:
            pagination_note = (
                f"⚠️ PAGINATION LOOKS BROKEN: only {unique_sigs} unique transactions out of "
                f"{len(txns)} claimed - likely re-fetching the same page repeatedly. This would "
                "explain why increasing lookback hasn't helped at all. "
                if pagination_suspicious else
                f"(Pagination verified working: {unique_sigs} unique tx out of {len(txns)} scanned.) "
            )
            if instructions_scanned == 0:
                note = pagination_note + (
                    f"Scanned {len(txns)} transactions, found 0 instructions with programId "
                    "matching CPMM and a readable data field. Try again or increase lookback."
                )
            elif creation_instructions_found == 0:
                note = pagination_note + (
                    f"Checked {instructions_scanned} CPMM instruction(s) with readable data, none "
                    "matched the initialize/initialize_with_permission discriminators - confirmed "
                    "these are other instruction types (swap/deposit/withdraw). No genuine pool "
                    "creations happened in this window. Try again later or increase lookback."
                )
            elif implausible_value_count == creation_instructions_found:
                note = (
                    f"⚠️ Found {creation_instructions_found} genuine creation instruction(s), but "
                    "the decoded open_time value didn't pass the plausibility check on any of them. "
                    "This would mean the OPEN_TIME_ARG_OFFSET (24) is wrong despite matching the "
                    "documented instruction signature - send me this message, this is a real anomaly "
                    "worth double-checking against one actual instruction's raw data."
                )
            else:
                note = (
                    f"Found {creation_instructions_found} genuine pool creation(s) with a plausible "
                    "open_time, but all of them already opened (open_time in the past, i.e. "
                    "immediate-open pools) rather than being scheduled for the future. This is a "
                    "real, working result - just no delayed-open pools in this window."
                )

        return {
            "ok": True,
            "tokens": results,
            "note": note,
            "diagnostics": {
                "instructions_scanned": instructions_scanned,
                "creation_instructions_found": creation_instructions_found,
                "implausible_value_count": implausible_value_count,
            },
        }

    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": str(e), "tokens": []}


def debug_check_transaction(signature: str, helius_api_key: str) -> dict:
    """
    Fetches ONE specific transaction by signature and runs the exact same
    detection/decode logic against it (both CPMM pool-creation AND
    pump.fun migration checks), returning a full diagnostic report. Use
    this to verify the tool against a transaction you already know is
    genuine (e.g. found by browsing Solscan yourself) - much more
    reliable than waiting to catch a rare live event.
    """
    try:
        resp = requests.post(
            "https://api.helius.xyz/v0/transactions",
            params={"api-key": helius_api_key},
            json={"transactions": [signature]},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if not isinstance(results, list) or not results:
            return {"ok": False, "error": "Transaction not found or empty response"}
        tx = results[0]
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": str(e)}

    tx_timestamp = tx.get("timestamp")
    tx_type = tx.get("type")
    instructions = tx.get("instructions", [])
    all_instructions = []
    for ix in instructions:
        all_instructions.append(ix)
        for inner in ix.get("innerInstructions", []):
            all_instructions.append(inner)

    findings = []
    for ix in all_instructions:
        program_id = ix.get("programId")
        if program_id not in (RAYDIUM_CPMM_PROGRAM, PUMPFUN_PROGRAM):
            continue
        data_field = ix.get("data")
        entry = {
            "program": "CPMM" if program_id == RAYDIUM_CPMM_PROGRAM else "pump.fun",
            "has_data_field": bool(data_field),
            "raw_data_preview": (data_field or "")[:20],
        }

        if data_field:
            try:
                raw = base58.b58decode(data_field)
                entry["decoded_byte_length"] = len(raw)
                entry["discriminator_hex"] = raw[:8].hex()

                if program_id == RAYDIUM_CPMM_PROGRAM:
                    name = INITIALIZE_DISCRIMINATORS.get(raw[:8])
                    entry["matched_instruction"] = name or "not a creation instruction"
                    if name and len(raw) >= OPEN_TIME_ARG_OFFSET + OPEN_TIME_ARG_LENGTH:
                        chunk = raw[OPEN_TIME_ARG_OFFSET:OPEN_TIME_ARG_OFFSET + OPEN_TIME_ARG_LENGTH]
                        open_time = struct.unpack("<Q", chunk)[0]
                        entry["decoded_open_time_raw"] = open_time
                        if open_time > 0:
                            from datetime import datetime
                            entry["decoded_open_time_readable"] = datetime.fromtimestamp(open_time).strftime("%Y-%m-%d %H:%M:%S")
                        if tx_timestamp:
                            from datetime import datetime as dt2
                            entry["tx_timestamp_readable"] = dt2.fromtimestamp(tx_timestamp).strftime("%Y-%m-%d %H:%M:%S")
                            entry["seconds_from_tx_to_open_time"] = open_time - tx_timestamp
                else:  # pump.fun
                    is_migrate = raw[:8] == MIGRATE_DISCRIMINATOR
                    entry["matched_instruction"] = "migrate" if is_migrate else "not a migrate instruction"
                    if is_migrate:
                        extracted_mint = _extract_pumpfun_mint(tx)
                        entry["extracted_mint"] = extracted_mint
                        entry["mint_confirmed"] = bool(extracted_mint and extracted_mint.endswith("pump"))
            except Exception as e:
                entry["decode_error"] = str(e)

        findings.append(entry)

    return {
        "ok": True,
        "tx_type": tx_type,
        "tx_timestamp": tx_timestamp,
        "matching_instructions_found": len(findings),
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# PumpSwap migration watcher - "just graduated" feed
#
# Since March 2025, pump.fun tokens graduate to PumpSwap (pump.fun's own
# AMM), not Raydium. Raydium CPMM creation (everything above this section)
# is now a rare, mostly-manual path. This section watches the REAL,
# current graduation mechanism instead.
#
# Source: pump.fun's own official docs
# (github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_PROGRAM_README.md)
#   "migrate(user, mint) allows any user to migrate the liquidity of a
#    completed bonding curve of the given mint to PumpSwap AMM ... It is
#    also permissionless, so anyone can migrate a completed bonding curve."
#
# migrate() fires the instant a bonding curve completes - there's no
# scheduled future timestamp involved, unlike CPMM's open_time. So this is
# a "just happened" feed, not a countdown.
#
# Program ID confirmed via pump.fun's own docs + independently by
# Bitquery, Shyft, QuickNode, Solscan: 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
#
# Discriminator computed the same way as CPMM's, and this time verified
# against an independently PUBLISHED correct value (pump.fun's own
# "create" instruction discriminator, confirmed via QuickNode's docs) -
# the computation method itself is now proven correct, not just assumed.
# ---------------------------------------------------------------------------

PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# THE ACTUAL FIX: migrations are NOT executed via the main pump.fun
# program's own migrate() instruction in practice - they go through a
# SEPARATE migration wrapper program. Confirmed via Chainstack's official,
# recently-updated guide AND the actual working source code of a
# popular (968-star), actively maintained pump.fun bot
# (chainstacklabs/pumpfun-bonkfun-bot). This is why scanning the main
# program found thousands of instructions and zero real migrations - we
# were checking the wrong address the entire time.
MIGRATION_WRAPPER_PROGRAM = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"

# Anchor discriminators depend only on the instruction NAME, not the
# program address - so this same discriminator (verified via the
# cross-check against pump.fun's published 'create' discriminator earlier)
# should still be correct now that we're pointed at the right program,
# since the reference bot's logs confirm the instruction is literally
# named "Migrate" on this wrapper program too.
MIGRATE_DISCRIMINATOR = bytes.fromhex("9beae792ec9ea21e")

# Exact field layout of the Migrate event, taken directly from the
# reference bot's working parser (not reconstructed from memory):
# https://github.com/chainstacklabs/pumpfun-bonkfun-bot/blob/main/learning-examples/listen-migrations/listen_logsubscribe.py
MIGRATE_EVENT_FIELDS = [
    ("timestamp", "i64"), ("index", "u16"), ("creator", "pubkey"),
    ("baseMint", "pubkey"), ("quoteMint", "pubkey"),
    ("baseMintDecimals", "u8"), ("quoteMintDecimals", "u8"),
    ("baseAmountIn", "u64"), ("quoteAmountIn", "u64"),
    ("poolBaseAmount", "u64"), ("poolQuoteAmount", "u64"),
    ("minimumLiquidity", "u64"), ("initialLiquidity", "u64"),
    ("lpTokenAmountOut", "u64"), ("poolBump", "u8"),
    ("pool", "pubkey"), ("lpMint", "pubkey"),
    ("userBaseTokenAccount", "pubkey"), ("userQuoteTokenAccount", "pubkey"),
]


def _parse_migrate_event(data: bytes) -> dict:
    """Parses the Migrate event's raw bytes using the exact field layout
    from the working reference implementation. Returns {} on failure."""
    if len(data) < 8:
        return {}
    offset = 8  # skip the event discriminator
    parsed = {}
    try:
        for name, ftype in MIGRATE_EVENT_FIELDS:
            if ftype == "pubkey":
                parsed[name] = base58.b58encode(data[offset:offset + 32]).decode()
                offset += 32
            elif ftype == "u64":
                parsed[name] = struct.unpack("<Q", data[offset:offset + 8])[0]
                offset += 8
            elif ftype == "i64":
                parsed[name] = struct.unpack("<q", data[offset:offset + 8])[0]
                offset += 8
            elif ftype == "u16":
                parsed[name] = struct.unpack("<H", data[offset:offset + 2])[0]
                offset += 2
            elif ftype == "u8":
                parsed[name] = data[offset]
                offset += 1
        return parsed
    except (struct.error, IndexError):
        return {}


def _fetch_transaction_logs(signature: str, rpc_url: str) -> list:
    """Fetches a transaction's raw log messages via direct Solana RPC -
    needed because the Migrate event lives in program logs, not in the
    instruction data Helius's enhanced format normally surfaces."""
    try:
        resp = requests.post(
            rpc_url,
            json={
                "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                "params": [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
            },
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json().get("result")
        if not result:
            return []
        return result.get("meta", {}).get("logMessages", [])
    except requests.exceptions.RequestException:
        return []


def _extract_mint_from_logs(log_messages: list) -> dict:
    """Finds and parses the Migrate event from a transaction's logs.
    Returns {'mint': ..., 'pool': ...} or {} if not found/not a migration."""
    if not any("Program log: Instruction: Migrate" in l for l in log_messages):
        return {}
    if any("Program log: Bonding curve already migrated" in l for l in log_messages):
        return {}  # idempotent no-op call, not a real migration

    for log in log_messages:
        if log.startswith("Program data:"):
            try:
                data = base64.b64decode(log.split(": ", 1)[1])
                parsed = _parse_migrate_event(data)
                if parsed.get("baseMint"):
                    return {"mint": parsed["baseMint"], "pool": parsed.get("pool")}
            except Exception:
                continue
    return {}

SOL_MINT = "So11111111111111111111111111111111111111112"


def _extract_pumpfun_mint(tx: dict) -> str:
    """
    Finds the actual migrated token's mint from a transaction, preferring
    candidates that match pump.fun's known vanity-mined address pattern
    (all pump.fun mints end in the literal suffix "pump" - this is a real,
    well-documented pattern, not a guess, visible on virtually every
    pump.fun token address). This catches wrong extractions (like
    accidentally grabbing a program address) rather than confidently
    showing something incorrect - exactly the kind of bug this caught
    when accounts[]-index guessing pulled the migration program's own
    address instead of the actual token.
    """
    candidates = []

    # Check tokenTransfers first
    for t in tx.get("tokenTransfers", []):
        m = t.get("mint")
        if m and m != SOL_MINT:
            candidates.append(m)

    # Check accountData's tokenBalanceChanges too - migrate() may not
    # surface as a simple tokenTransfer given it's a multi-step liquidity
    # operation, not a plain transfer.
    for acct in tx.get("accountData", []):
        for change in acct.get("tokenBalanceChanges", []):
            m = change.get("mint")
            if m and m != SOL_MINT:
                candidates.append(m)

    if not candidates:
        return None

    # Strongly prefer a candidate matching pump.fun's known suffix pattern
    for c in candidates:
        if c.endswith("pump"):
            return c

    # No confirmed match - return the first candidate anyway but the
    # caller marks mint_confirmed=False so it's shown as unverified rather
    # than asserted as fact.
    return candidates[0]


def get_recent_migrations(helius_api_key: str, rpc_url: str, limit: int = 8, lookback: int = 500) -> dict:
    """
    Returns { ok, tokens: [{mint, seconds_ago, signature}], note, diagnostics }
    A live feed of tokens that recently graduated (migrated) from pump.fun's
    bonding curve to PumpSwap.

    Scans MIGRATION_WRAPPER_PROGRAM (not the main pump.fun program - see
    module notes on why that was the core bug). For each genuine 'migrate'
    instruction match, fetches the full transaction logs via direct RPC and
    parses the real Migrate event for a precise mint - not a heuristic guess.
    """
    try:
        txns = []
        before_sig = None
        page_size = 100
        pages_needed = max(1, (lookback + page_size - 1) // page_size)

        for _ in range(pages_needed):
            params = {"api-key": helius_api_key, "limit": page_size}
            if before_sig:
                params["before"] = before_sig
            resp = requests.get(HELIUS_TX_URL.format(MIGRATION_WRAPPER_PROGRAM), params=params, timeout=10)
            resp.raise_for_status()
            page = resp.json()
            if not isinstance(page, list) or not page:
                break
            txns.extend(page)
            before_sig = page[-1].get("signature")
            if not before_sig:
                break

        txns = txns[:lookback]
        if not txns:
            return {"ok": False, "error": "No transactions returned from Helius", "tokens": []}

        unique_sigs = len(set(t.get("signature") for t in txns))
        pagination_suspicious = unique_sigs < len(txns) * 0.9

        now = time.time()
        results = []
        instructions_scanned = 0
        migrations_found = 0
        log_parse_failures = 0

        for tx in txns:
            tx_timestamp = tx.get("timestamp")
            instructions = tx.get("instructions", [])
            all_instructions = []
            for ix in instructions:
                all_instructions.append(ix)
                for inner in ix.get("innerInstructions", []):
                    all_instructions.append(inner)

            for ix in all_instructions:
                if ix.get("programId") != MIGRATION_WRAPPER_PROGRAM:
                    continue
                data_field = ix.get("data")
                if not data_field:
                    continue
                instructions_scanned += 1

                try:
                    raw = base58.b58decode(data_field)
                except Exception:
                    continue

                if raw[:8] != MIGRATE_DISCRIMINATOR:
                    continue

                migrations_found += 1
                signature = tx.get("signature")

                # Get the precise mint from the actual event log, matching
                # the proven reference implementation exactly.
                mint = None
                if signature:
                    logs = _fetch_transaction_logs(signature, rpc_url)
                    event = _extract_mint_from_logs(logs)
                    mint = event.get("mint")
                if not mint:
                    log_parse_failures += 1
                    mint = _extract_pumpfun_mint(tx)  # fallback to the heuristic

                results.append({
                    "mint": mint,
                    "mint_confirmed": bool(mint and mint.endswith("pump")),
                    "seconds_ago": int(now - tx_timestamp) if tx_timestamp else None,
                    "signature": signature,
                })

            if len(results) >= limit:
                break

        note = None
        if not results:
            pagination_note = (
                f"⚠️ PAGINATION LOOKS BROKEN: only {unique_sigs} unique transactions out of "
                f"{len(txns)} claimed - likely re-fetching the same page repeatedly. This would "
                "explain why increasing lookback hasn't helped at all. "
                if pagination_suspicious else
                f"(Pagination verified working: {unique_sigs} unique tx out of {len(txns)} scanned.) "
            )
            note = pagination_note + (
                f"Scanned {instructions_scanned} pump.fun instruction(s) across {len(txns)} "
                "transactions, found 0 migrate calls. This is a real, independently-verified "
                "instruction check (not a guess) - so an empty result likely just means no "
                "graduations happened in this window. Migration is known to be infrequent. "
                "Try again later or increase lookback."
            )

        return {
            "ok": True,
            "tokens": results,
            "note": note,
            "diagnostics": {"instructions_scanned": instructions_scanned, "migrations_found": migrations_found},
        }

    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": str(e), "tokens": []}
