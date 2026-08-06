import time
import json
import os
from typing import Dict, List

class RiskManager:
    """
    Manages capital risk and trading circuit breakers for a Solana trading bot.
    Includes state persistence to disk to maintain context across CLI invocations.
    """
    def __init__(self, 
                 state_file: str = "risk_state.json",
                 max_daily_loss_percent: float = 10.0, 
                 max_concurrent_positions: int = 5, 
                 max_exposure_per_deployer: float = 100.0, 
                 min_win_rate: float = 40.0, 
                 win_rate_lookback_trades: int = 20):
        self.state_file = state_file
        self.max_daily_loss_percent = max_daily_loss_percent
        self.max_concurrent_positions = max_concurrent_positions
        self.max_exposure_per_deployer = max_exposure_per_deployer
        self.min_win_rate = min_win_rate
        self.win_rate_lookback_trades = win_rate_lookback_trades

        # Default internal state
        self.initial_capital = 0.0
        self.current_capital = 0.0
        self.daily_profit_loss = 0.0
        self.last_reset_day = time.strftime("%Y-%m-%d")
        self.open_positions: Dict[str, float] = {} # token_mint -> position_size
        self.deployer_exposure: Dict[str, float] = {} # deployer_wallet -> total_exposure
        self.trade_outcomes: List[bool] = [] # True for win, False for loss

        self._load_state()
        self._check_daily_reset()

    def _save_state(self):
        """Serializes current risk state to a JSON file."""
        state = {
            "initial_capital": self.initial_capital,
            "current_capital": self.current_capital,
            "daily_profit_loss": self.daily_profit_loss,
            "last_reset_day": self.last_reset_day,
            "open_positions": self.open_positions,
            "deployer_exposure": self.deployer_exposure,
            "trade_outcomes": self.trade_outcomes
        }
        with open(self.state_file, "w") as f:
            json.dump(state, f)

    def _load_state(self):
        """Loads risk state from disk if available."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    state = json.load(f)
                    self.initial_capital = state.get("initial_capital", 0.0)
                    self.current_capital = state.get("current_capital", 0.0)
                    self.daily_profit_loss = state.get("daily_profit_loss", 0.0)
                    self.last_reset_day = state.get("last_reset_day", time.strftime("%Y-%m-%d"))
                    self.open_positions = state.get("open_positions", {})
                    self.deployer_exposure = state.get("deployer_exposure", {})
                    self.trade_outcomes = state.get("trade_outcomes", [])
            except (json.JSONDecodeError, IOError) as e:
                print(f"WARNING: Could not load risk state: {e}. Starting fresh.")

    def _check_daily_reset(self):
        """Resets daily metrics if a new day has started."""
        current_day = time.strftime("%Y-%m-%d")
        if current_day != self.last_reset_day:
            self.daily_profit_loss = 0.0
            self.last_reset_day = current_day
            self._save_state()

    def set_initial_capital(self, capital: float):
        """Initializes the bot's capital baseline if not already set."""
        if self.initial_capital == 0.0:
            self.initial_capital = capital
            self.current_capital = capital
            self._save_state()

    def update_capital(self, trade_profit_loss: float):
        """Updates capital and daily P/L after a trade is closed."""
        self._check_daily_reset()
        self.current_capital += trade_profit_loss
        self.daily_profit_loss += trade_profit_loss
        self._save_state()

    def add_position(self, token_mint: str, position_size: float, deployer_wallet: str):
        """Records a new open position and updates deployer exposure."""
        self.open_positions[token_mint] = position_size
        self.deployer_exposure[deployer_wallet] = self.deployer_exposure.get(deployer_wallet, 0.0) + position_size
        self._save_state()

    def remove_position(self, token_mint: str, deployer_wallet: str, trade_was_win: bool):
        """Closes a position and safely decrements exposures."""
        if token_mint in self.open_positions:
            position_size = self.open_positions.pop(token_mint)
            
            # Safely decrement deployer exposure with epsilon comparison to handle float drift
            current_exp = self.deployer_exposure.get(deployer_wallet, 0.0)
            new_exp = current_exp - position_size
            
            if abs(new_exp) < 1e-9:
                if deployer_wallet in self.deployer_exposure:
                    del self.deployer_exposure[deployer_wallet]
            elif new_exp < 0:
                print(f"WARNING: Deployer exposure for {deployer_wallet} drifted negative ({new_exp}). Force clearing.")
                if deployer_wallet in self.deployer_exposure:
                    del self.deployer_exposure[deployer_wallet]
            else:
                self.deployer_exposure[deployer_wallet] = new_exp
            
            self.trade_outcomes.append(trade_was_win)
            if len(self.trade_outcomes) > self.win_rate_lookback_trades:
                self.trade_outcomes.pop(0)
            self._save_state()

    def _check_max_daily_loss(self) -> bool:
        if self.initial_capital == 0:
            return False
        loss_percentage = -self.daily_profit_loss / self.initial_capital * 100
        if loss_percentage >= self.max_daily_loss_percent:
            print(f"CRITICAL: Max daily loss reached! Loss: {loss_percentage:.2f}%")
            return True
        return False

    def _check_max_concurrent_positions(self) -> bool:
        if len(self.open_positions) >= self.max_concurrent_positions:
            print(f"WARNING: Max concurrent positions reached ({len(self.open_positions)}).")
            return True
        return False

    def _check_max_exposure_per_deployer(self, deployer_wallet: str, proposed_trade_size: float) -> bool:
        current_exposure = self.deployer_exposure.get(deployer_wallet, 0.0)
        if (current_exposure + proposed_trade_size) > self.max_exposure_per_deployer:
            print(f"WARNING: Max exposure for deployer {deployer_wallet} exceeded.")
            return True
        return False

    def _check_min_win_rate(self) -> bool:
        if len(self.trade_outcomes) < self.win_rate_lookback_trades:
            return False
        
        wins = self.trade_outcomes.count(True)
        win_rate = (wins / len(self.trade_outcomes)) * 100
        if win_rate < self.min_win_rate:
            print(f"CRITICAL: Win rate ({win_rate:.2f}%) below threshold ({self.min_win_rate}%).")
            return True
        return False

    def should_pause_trading(self) -> bool:
        """
        Checks for catastrophic circuit breakers (Daily Loss, Win Rate).
        Note: Concurrent positions and per-deployer exposure are gated in can_open_position.
        """
        return self._check_max_daily_loss() or self._check_min_win_rate()

    def can_open_position(self, deployer_wallet: str, proposed_trade_size: float) -> bool:
        """
        Consolidated entry point for all pre-trade risk checks.
        Returns True if safe to trade, False otherwise.
        """
        if self.should_pause_trading():
            return False
        if self._check_max_concurrent_positions():
            return False
        if self._check_max_exposure_per_deployer(deployer_wallet, proposed_trade_size):
            return False
        return True
