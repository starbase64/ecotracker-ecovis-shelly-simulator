import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import ecotracker_limiter as limiter  # noqa: E402


class ControllerTests(unittest.TestCase):
    def setUp(self):
        limiter.GRID = limiter.Reading()
        limiter.INVERTER = limiter.Reading()

    @staticmethod
    def set_readings(grid, inverter):
        limiter.GRID.set(
            grid,
            {"energyCounterIn": 1000.0, "energyCounterOut": 200.0, "agePower": 100.0},
        )
        limiter.INVERTER.set(inverter)

    def test_raises_output_below_target(self):
        self.set_readings(500.0, 0.0)
        result = limiter.Controller().step()
        self.assertEqual(result["limiterState"], "raise")
        self.assertGreater(result["limiterControlPower"], 0.0)
        self.assertEqual(result["limiterTargetPower"], 500.0)

    def test_hard_branch_reduces_excess_output(self):
        self.set_readings(-100.0, 850.0)
        result = limiter.Controller().step()
        self.assertEqual(result["limiterState"], "overshoot")
        self.assertLess(result["limiterControlPower"], 0.0)

    def test_signed_inverter_consumption_is_not_added_to_house_load(self):
        self.set_readings(500.0, -100.0)
        result = limiter.Controller().step()
        self.assertEqual(result["limiterHouseLoad"], 400.0)
        self.assertEqual(result["limiterInverterPower"], 0.0)
        self.assertEqual(result["limiterInverterFlow"], -100.0)

    def test_missing_sources_start_in_hard_failsafe(self):
        result = limiter.Controller().step()
        self.assertEqual(result["limiterState"], "failsafe_hard")
        self.assertEqual(result["limiterControlPower"], -800.0)


if __name__ == "__main__":
    unittest.main()
