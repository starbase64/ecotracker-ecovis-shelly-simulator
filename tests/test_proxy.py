import math
import time
import unittest

import ecotracker_shelly_proxy as proxy_module


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            name: getattr(proxy_module.Config, name)
            for name in (
                "REPORT_GAIN_UP", "REPORT_GAIN_DOWN", "REPORT_DEADBAND_W",
                "REPORT_HOLD_S", "GRID_SMOOTH_S", "FAST_EXPORT_W",
                "MAX_CORRECTION_UP_W", "MAX_CORRECTION_DOWN_W",
                "STALE_S", "STALE_HARD_S", "SAFE_REDUCE_W",
                "SAFE_REDUCE_HARD_W",
            )
        }
        proxy_module.Config.REPORT_GAIN_UP = 0.4
        proxy_module.Config.REPORT_GAIN_DOWN = 0.6
        proxy_module.Config.REPORT_DEADBAND_W = 10.0
        proxy_module.Config.REPORT_HOLD_S = 5.0
        proxy_module.Config.GRID_SMOOTH_S = 0.0
        proxy_module.Config.FAST_EXPORT_W = 30.0
        proxy_module.Config.MAX_CORRECTION_UP_W = 800.0
        proxy_module.Config.MAX_CORRECTION_DOWN_W = 2000.0
        proxy_module.Config.STALE_S = 5.0
        proxy_module.Config.STALE_HARD_S = 15.0
        proxy_module.Config.SAFE_REDUCE_W = -300.0
        proxy_module.Config.SAFE_REDUCE_HARD_W = -800.0
        proxy_module.GRID = proxy_module.Reading()

    def tearDown(self):
        for name, value in self.config.items():
            setattr(proxy_module.Config, name, value)

    def set_grid(self, value, **extra):
        defaults = {
            "energyCounterIn": 1.0,
            "energyCounterOut": 2.0,
            "agePower": 250.0,
            "agePowerAvailable": True,
        }
        defaults.update(extra)
        proxy_module.GRID.set(value, defaults)

    def age_grid(self, seconds):
        with proxy_module.GRID.lock:
            proxy_module.GRID.timestamp = time.monotonic() - seconds

    def test_gain_for_import(self):
        reported, gain, direction, state = proxy_module.DampedProxy.damp(100.0)
        self.assertEqual(reported, 40.0)
        self.assertEqual(gain, 0.4)
        self.assertEqual(direction, "raise")
        self.assertEqual(state, "damped_raise")

    def test_gain_for_export(self):
        reported, gain, direction, state = proxy_module.DampedProxy.damp(-100.0)
        self.assertEqual(reported, -60.0)
        self.assertEqual(gain, 0.6)
        self.assertEqual(direction, "reduce")
        self.assertEqual(state, "damped_reduce")

    def test_deadband(self):
        reported, gain, direction, state = proxy_module.DampedProxy.damp(9.9)
        self.assertEqual((reported, gain, direction, state),
                         (0.0, 0.0, "hold", "deadband"))

    def test_correction_limit_is_only_single_step(self):
        reported, _, _, _ = proxy_module.DampedProxy.damp(5000.0)
        self.assertEqual(reported, 800.0)

    def test_hold_window(self):
        instance = proxy_module.DampedProxy()
        self.set_grid(100.0)
        first = instance.step()
        self.set_grid(200.0)
        second = instance.step()
        self.assertEqual(first["reportedPower"], 40.0)
        self.assertEqual(second["reportedPower"], 40.0)
        self.assertEqual(second["proxyState"], "damped_raise_hold")

    def test_urgent_export_bypasses_hold(self):
        instance = proxy_module.DampedProxy()
        self.set_grid(100.0)
        self.assertEqual(instance.step()["reportedPower"], 40.0)
        self.set_grid(-100.0)
        result = instance.step()
        self.assertEqual(result["reportedPower"], -60.0)
        self.assertEqual(result["proxyState"], "damped_reduce")

    def test_soft_failsafe_never_holds_positive_value(self):
        instance = proxy_module.DampedProxy()
        self.set_grid(100.0)
        self.assertEqual(instance.step()["reportedPower"], 40.0)
        self.age_grid(6.0)
        result = instance.step()
        self.assertEqual(result["reportedPower"], -300.0)
        self.assertEqual(result["proxyState"], "failsafe_soft")
        self.assertFalse(result["dataHealthy"])

    def test_hard_failsafe(self):
        instance = proxy_module.DampedProxy()
        self.set_grid(100.0)
        self.age_grid(20.0)
        result = instance.step()
        self.assertEqual(result["reportedPower"], -800.0)
        self.assertEqual(result["proxyState"], "failsafe_hard")

    def test_missing_age_power_is_visible_but_accepted(self):
        power, extra = proxy_module.parse_ecotracker({"power": 100.0})
        proxy_module.GRID.set(power, extra)
        instance = proxy_module.DampedProxy()
        result = instance.step()
        self.assertTrue(result["dataHealthy"])
        self.assertIsNone(result["ecoTrackerAgePowerMs"])
        self.assertFalse(result["ecoTrackerAgePowerAvailable"])

    def test_stale_age_power_is_rejected(self):
        with self.assertRaises(RuntimeError):
            proxy_module.parse_ecotracker({"power": 100.0, "agePower": 4000})

    def test_non_finite_reading_is_rejected(self):
        with self.assertRaises(ValueError):
            proxy_module.GRID.set(math.nan)


if __name__ == "__main__":
    unittest.main()
