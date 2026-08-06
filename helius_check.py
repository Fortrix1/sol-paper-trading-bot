"""
helius_check.py - Best-effort deployer wallet reputation check via Helius's
Enhanced Transactions API.

HONESTY NOTE: this environment has no network access to Helius, so the
exact response schema below (the "type" field values Helius uses for
token-creation transactions, etc.) is based on general recollection of
their API shape, not a verified live response. It's written defensively -
if a field name is wrong, it degrades to "couldn't determine" rather than
crashing. If the counts look off once you run this for real, send me one
real JSON response from this endpoint and I'll correct the field mapping.
"""

import requests

HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{}/transactions"

# Transaction "type" values that plausibly indicate this wallet created a
# token/pool. Matched loosely (substring) since exact enum values can vary
# by Helius API version.
CREATION_TYPE_HINTS = ["TOKEN_MINT", "CREATE", "INITIALIZE"]


def get_deployer_reputation(wallet_address: str, api_key: str, sample_size: int = 100) -> dict:
    """
    Returns a best-effort read on a deployer wallet:
      { ok, txn_count_sampled, likely_tokens_created, is_brand_new_wallet }
    or { ok: False, error }
    """
    if not wallet_address:
        return {"ok": False, "error": "No deployer address available"}

    try:
        resp = requests.get(
            HELIUS_TX_URL.format(wallet_address),
            params={"api-key": api_key, "limit": sample_size},
            timeout=8,
        )
        resp.raise_for_status()
        txns = resp.json()
        if not isinstance(txns, list):
            return {"ok": False, "error": "Unexpected response shape from Helius"}

        creation_count = 0
        for tx in txns:
            tx_type = str(tx.get("type", "")).upper()
            if any(hint in tx_type for hint in CREATION_TYPE_HINTS):
                creation_count += 1

        txn_count = len(txns)
        # A deployer wallet with almost no history is itself a signal -
        # likely spun up fresh just for this launch.
        is_brand_new_wallet = txn_count < 5

        return {
            "ok": True,
            "txn_count_sampled": txn_count,
            "likely_tokens_created": creation_count,
            "is_brand_new_wallet": is_brand_new_wallet,
        }
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": str(e)}
