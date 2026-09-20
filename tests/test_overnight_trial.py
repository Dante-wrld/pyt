import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solana_launch_guard.overnight_trial import watch


class Monitor:
    returncode = None

    def __init__(self):
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return 0


class OvernightTrialTests(unittest.TestCase):
    def test_triggers_at_most_one_canary_and_stops_monitor(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "recommendations.json"
            snapshot.write_text(json.dumps({"candidates": []}))
            journal = Path(folder) / "canary.json"
            monitor = Monitor()
            counter = []
            clock = iter((0, 1, 3600))

            async def run_live_canary(**kwargs):
                counter.append(kwargs)
                return {"broadcast": True, "status": "CONFIRMED"}

            with patch.dict(os.environ, {
                "RECOMMENDATION_SNAPSHOT_PATH": str(snapshot),
                "AGENT_LIVE_CANARY_PATH": str(journal),
                "AUTO_BUY_LIVE": "false",
            }), patch("solana_launch_guard.overnight_trial.validate_live_environment"), \
                    patch("solana_launch_guard.overnight_trial.subprocess.Popen", return_value=monitor), \
                    patch("solana_launch_guard.overnight_trial.run_live_canary", side_effect=run_live_canary):
                result = asyncio.run(watch(hours=0.1, clock=lambda: next(clock)))
            self.assertEqual(result["status"], "CONFIRMED")
            self.assertEqual(len(counter), 1)
            self.assertTrue(monitor.terminated)

    def test_rejects_unsafe_duration_before_starting_monitor(self):
        with self.assertRaisesRegex(ValueError, "at most 8 hours"):
            asyncio.run(watch(hours=9))


if __name__ == "__main__":
    unittest.main()
