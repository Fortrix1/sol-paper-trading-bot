import requests

class HoneypotChecker:
    """
    Interfaces with third-party security APIs to gate trading on safety-critical checks.
    Pragmatic Level 2 approach to avoid complex in-house DEX simulation.
    """
    def __init__(self, api_endpoint: str, api_key: str = None, sell_tax_threshold: float = 0.1):
        self.api_endpoint = api_endpoint
        self.api_key = api_key
        self.sell_tax_threshold = sell_tax_threshold

    def check_token_safety(self, token_mint: str) -> dict:
        """
        Queries security API for honeypot, tax, and broader risk signals.
        Defaults to NOT SAFE if the API is unreachable or returns an error.

        NOTE: RugCheck's exact JSON field names can differ by API version.
        Every extra field below is pulled defensively with .get() so a
        missing/renamed field degrades to "unknown" instead of crashing.
        If your live responses use different key names, tell me the actual
        JSON and I'll adjust the field paths.
        """
        headers = {}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key

        try:
            # Mint is already embedded in the URL path (api_endpoint) - no
            # extra query param needed. Set a strict timeout to avoid
            # blocking the trading loop.
            response = requests.get(self.api_endpoint, headers=headers, timeout=5)
            response.raise_for_status()
            result = response.json()
            
            # Core honeypot/tax indicators
            is_honeypot = result.get("is_honeypot", False)
            sell_tax = result.get("sell_tax", 0.0)

            # Broader risk signals, extracted defensively from the same response
            token_meta = result.get("token", {}) if isinstance(result.get("token"), dict) else {}
            mint_authority = token_meta.get("mintAuthority") or result.get("mintAuthority")
            freeze_authority = token_meta.get("freezeAuthority") or result.get("freezeAuthority")

            top_holders = result.get("topHolders") or result.get("top_holders") or []
            top_holder_pct = None
            top_holders_list = []
            creator = result.get("creator") or result.get("deployer")
            creator_holding_pct = None

            if top_holders:
                try:
                    top_holder_pct = sum(float(h.get("pct", 0)) for h in top_holders[:10])
                    for h in top_holders[:10]:
                        addr = h.get("address") or h.get("owner")
                        pct = float(h.get("pct", 0))
                        top_holders_list.append({"address": addr, "pct": pct})
                        if creator and addr == creator:
                            creator_holding_pct = pct
                except (TypeError, ValueError):
                    top_holder_pct = None

            markets = result.get("markets") or []
            lp_locked = None
            if markets:
                lp_locked = any(m.get("lpLocked") or m.get("liquidityLocked") for m in markets if isinstance(m, dict))

            risk_flags = []
            if mint_authority not in (None, "", "null"):
                risk_flags.append("mint authority still active (supply can be inflated)")
            if freeze_authority not in (None, "", "null"):
                risk_flags.append("freeze authority active (funds can be frozen)")
            if top_holder_pct is not None and top_holder_pct > 50:
                risk_flags.append(f"top 10 holders control {top_holder_pct:.0f}% of supply")
            if lp_locked is False:
                risk_flags.append("liquidity not locked")
            if creator_holding_pct is not None and creator_holding_pct > 10:
                risk_flags.append(f"deployer wallet directly holds {creator_holding_pct:.1f}% of supply")

            extra = {
                "mint_authority": mint_authority,
                "freeze_authority": freeze_authority,
                "top_holder_pct": top_holder_pct,
                "top_holders_list": top_holders_list,
                "lp_locked": lp_locked,
                "creator": creator,
                "creator_holding_pct": creator_holding_pct,
                "risk_flags": risk_flags,
            }

            # Logic: If it's a known honeypot or has too-high sell tax, it's unsafe.
            if is_honeypot or sell_tax > self.sell_tax_threshold:
                return {
                    "is_safe": False,
                    "reason": "Honeypot detected or high sell tax",
                    "details": result,
                    **extra,
                }

            return {"is_safe": True, "reason": "Passed API check", "details": result, **extra}
            
        except requests.exceptions.RequestException as e:
            # SAFETY CRITICAL: Default to False if the check cannot be completed.
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            detail = f"HTTP {status_code}" if status_code else type(e).__name__
            print(f"ERROR: Honeypot API check failed for {token_mint}: {e}")
            return {
                "is_safe": False, 
                "reason": f"API check failed ({detail})", 
                "details": {},
                "mint_authority": None, "freeze_authority": None,
                "top_holder_pct": None, "top_holders_list": [], "lp_locked": None,
                "creator": None, "creator_holding_pct": None, "risk_flags": [],
            }
