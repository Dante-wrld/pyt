import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from solana_launch_guard.agent_trial_sell import extra_exit_eligible


class ExtraSellTests(unittest.TestCase):
    def test_one_exit_only_for_small_risk_signal(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "extra.json")
            flags = {"AGENT_LIVE_CANARY_ONLY": "true", "AGENT_LIVE_EXTRA_SMALL_SELL": "true",
                     "AGENT_LIVE_TEST_ENABLED": "true", "AGENT_LIVE_KILL_SWITCH": "false",
                     "AGENT_LIVE_EXTRA_SELL_PATH": path}
            signal = SimpleNamespace(decision="EXIT WARNING", current_value_usd=4)
            balance = SimpleNamespace(raw_amount=100)
            with patch.dict(os.environ, flags):
                self.assertTrue(extra_exit_eligible(signal, balance, minimum_usd=2))
                for value in (1.99, 5.01, float("nan"), None):
                    self.assertFalse(extra_exit_eligible(
                        SimpleNamespace(decision="EXIT WARNING", current_value_usd=value), balance,
                        minimum_usd=2))
                self.assertFalse(extra_exit_eligible(
                    SimpleNamespace(decision="HOLD", current_value_usd=4), balance,
                    minimum_usd=2))
                self.assertFalse(extra_exit_eligible(signal, balance, minimum_usd=4.5))
                Path(path + ".claim").touch()
                self.assertFalse(extra_exit_eligible(signal, balance, minimum_usd=2))

    def test_kill_switch_and_missing_opt_in_block(self):
        signal = SimpleNamespace(decision="EXIT WARNING", current_value_usd=4)
        balance = SimpleNamespace(raw_amount=100)
        with patch.dict(os.environ, {"AGENT_LIVE_CANARY_ONLY": "true",
                                    "AGENT_LIVE_EXTRA_SMALL_SELL": "true",
                                    "AGENT_LIVE_TEST_ENABLED": "true",
                                    "AGENT_LIVE_KILL_SWITCH": "true"}):
            self.assertFalse(extra_exit_eligible(signal, balance, minimum_usd=2))


if __name__ == "__main__":
    unittest.main()
