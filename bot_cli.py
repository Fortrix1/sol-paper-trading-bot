import argparse
import datetime
import sys
from risk_manager import RiskManager
from shadow_trader import ShadowTrader
from honeypot_check import HoneypotChecker

def main():
    parser = argparse.ArgumentParser(description="Solana Trading Bot Level 2 CLI Utility")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Honeypot Check Command
    honeypot_parser = subparsers.add_parser("check", help="Check a token for honeypot risks")
    honeypot_parser.add_argument("mint", help="The token mint address to check")
    honeypot_parser.add_argument("--api", default="https://api.rugcheck.xyz/v1/tokens/{}/report", help="Honeypot API endpoint")
    honeypot_parser.set_defaults(sell_tax=0.1)

    # Risk Manager Commands
    risk_parser = subparsers.add_parser("risk", help="Risk management operations")
    risk_subparsers = risk_parser.add_subparsers(dest="risk_command", help="Risk sub-commands")
    
    # Risk Check
    risk_check = risk_subparsers.add_parser("check", help="Check if a new position is safe to open")
    risk_check.add_argument("--deployer", required=True, help="Deployer wallet address")
    risk_check.add_argument("--size", type=float, required=True, help="Proposed trade size in USD")
    
    # Risk Add
    risk_add = risk_subparsers.add_parser("add", help="Record a newly opened position")
    risk_add.add_argument("--mint", required=True, help="Token mint address")
    risk_add.add_argument("--deployer", required=True, help="Deployer wallet address")
    risk_add.add_argument("--size", type=float, required=True, help="Position size in USD")
    
    # Risk Remove
    risk_remove = risk_subparsers.add_parser("remove", help="Record a closed position")
    risk_remove.add_argument("--mint", required=True, help="Token mint address")
    risk_remove.add_argument("--deployer", required=True, help="Deployer wallet address")
    risk_remove.add_argument("--pnl", type=float, required=True, help="Realized Profit/Loss in USD")
    risk_remove.add_argument("--win", action="store_true", help="Flag if the trade was a win")

    # Risk Status
    risk_subparsers.add_parser("status", help="Show current risk manager state")
    risk_init = risk_subparsers.add_parser("init", help="Initialize capital")
    risk_init.add_argument("capital", type=float, help="Initial capital amount")

    # Shadow Trader Command
    shadow_parser = subparsers.add_parser("log", help="Record a hypothetical trade in the shadow log")
    shadow_parser.add_argument("mint", help="Token mint address")
    shadow_parser.add_argument("action", choices=["BUY", "SELL"], help="Trade action")
    shadow_parser.add_argument("price", type=float, help="Token price")
    shadow_parser.add_argument("--amount", type=float, default=1.0, help="Trade amount")
    shadow_parser.add_argument("--outcome", choices=["WIN", "LOSS", "PENDING"], default="PENDING", help="Outcome for analysis")

    # Analysis Command
    analyze_parser = subparsers.add_parser("analyze", help="Analyze shadow trading performance")

    args = parser.parse_args()

    if args.command == "check":
        # RugCheck's real API is GET /v1/tokens/{mint}/report
        url = args.api.format(args.mint)
        checker = HoneypotChecker(api_endpoint=url, sell_tax_threshold=args.sell_tax)
        print(f"Querying security API for: {args.mint}...")
        result = checker.check_token_safety(args.mint)
        print(f"Result: {'✅ SAFE' if result['is_safe'] else '❌ UNSAFE'}")
        print(f"Reason: {result['reason']}")

    elif args.command == "risk":
        rm = RiskManager()
        
        if args.risk_command == "init":
            rm.set_initial_capital(args.capital)
            print(f"✅ Initial capital set to ${args.capital}")
            
        elif args.risk_command == "check":
            if rm.can_open_position(args.deployer, args.size):
                print(f"✅ Risk check PASSED for deployer {args.deployer} (${args.size})")
            else:
                print(f"❌ Risk check FAILED. Position blocked.")
                
        elif args.risk_command == "add":
            if rm.can_open_position(args.deployer, args.size):
                rm.add_position(args.mint, args.size, args.deployer)
                print(f"✅ Position recorded: {args.mint} (${args.size})")
            else:
                print("❌ Cannot add position: Risk limits exceeded.")
                
        elif args.risk_command == "remove":
            rm.update_capital(args.pnl)
            rm.remove_position(args.mint, args.deployer, args.win)
            print(f"✅ Position closed: {args.mint}. P/L: ${args.pnl}, Win: {args.win}")
            
        elif args.risk_command == "status":
            print(f"--- Risk Manager Status ---")
            print(f"Initial Capital: ${rm.initial_capital}")
            print(f"Current Capital: ${rm.current_capital}")
            print(f"Daily P/L: ${rm.daily_profit_loss}")
            print(f"Open Positions: {len(rm.open_positions)}")
            print(f"Active Deployers: {len(rm.deployer_exposure)}")
            print(f"Recent Win Rate: {rm.trade_outcomes.count(True)}/{len(rm.trade_outcomes)}")
            print(f"---------------------------")
        else:
            risk_parser.print_help()

    elif args.command == "log":
        st = ShadowTrader()
        st.record_potential_trade(
            timestamp=datetime.datetime.now(),
            token_mint=args.mint,
            action=args.action,
            proposed_amount=args.amount,
            proposed_price=args.price,
            decision_features={"source": "cli_test"},
            outcome=args.outcome,
            actual_price=args.price if args.outcome != "PENDING" else None
        )
        print(f"✅ {args.action} recorded for {args.mint} at ${args.price} (Outcome: {args.outcome})")

    elif args.command == "analyze":
        st = ShadowTrader()
        st.analyze_shadow_trades()

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
