"""
health_check.py - Startup diagnostics. Logs which modules loaded successfully
and catches import errors before they crash the deploy.
"""

import logging
logger = logging.getLogger(__name__)

def run_startup_checks():
    modules = [
        ("config", False),
        ("price_feed", False),
        ("async_price_feed", True),
        ("honeypot_check", False),
        ("paper_wallet", False),
        ("new_scanner", False),
        ("helius_check", False),
        ("launch_watcher", False),
        ("conviction_engine", False),
        ("auto_paper_trader", False),
        ("risk_manager", False),
        ("smart_money_tracker", False),
        ("phantom_connector", False),
        ("real_trader", False),
        ("premium_signals", False),
        ("position_tracker", False),
        ("whale_labeler", False),
        ("trade_estimator", False),
        ("live_listener", False),
    ]

    ok = []
    failed = []

    for name, optional in modules:
        try:
            __import__(name)
            ok.append(name)
        except Exception as e:
            if optional:
                failed.append(f"{name} (optional: {e})")
            else:
                failed.append(f"{name} (CRITICAL: {e})")

    logger.info(f"=== STARTUP HEALTH CHECK ===")
    logger.info(f"OK ({len(ok)}): {', '.join(ok)}")
    if failed:
        logger.warning(f"FAILED ({len(failed)}): {', '.join(failed)}")
    else:
        logger.info("All modules loaded successfully")
    logger.info(f"============================")

    return len(failed) == 0 or all("(optional:" in f for f in failed)
