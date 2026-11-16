import os
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from algae_feed import robot as rb  # noqa: E402
from algae_feed.config import CLIMATES, ENCLOSURES, REACTORS, STRAINS, Economics, Operation  # noqa: E402
from algae_feed.model import simulate  # noqa: E402

BASE = simulate(REACTORS["film_panel"], ENCLOSURES["air_dome_pe2"], STRAINS["chlorella"],
                CLIMATES["korea_central"], replace(Operation(), heat_setpoint_c=22.0, feed_mode="max"),
                Economics())


class RobotDesignTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.designs = rb.optimize(BASE, 100)
        cls.by = {d.system.key: d for d in cls.designs}

    def test_automation_beats_manual(self):
        self.assertNotEqual(self.designs[0].system.key, "manual")
        self.assertLess(self.designs[0].cost_per_kg, self.by["manual"].cost_per_kg * 0.8)

    def test_gantry_shrinks_dome(self):
        self.assertLess(self.by["overhead_gantry"].dome_m2, self.by["manual"].dome_m2)
        self.assertGreater(self.by["amr"].dome_m2, self.by["overhead_gantry"].dome_m2)

    def test_fleet_covers_peak_workload(self):
        for d in self.designs:
            if d.robots:
                self.assertLessEqual(d.utilization, 1.0)
                self.assertGreaterEqual(d.harvests_per_day, 1)

    def test_scheduler_keeps_density_and_quarantines(self):
        d = self.by["overhead_gantry"]
        panels, sch = rb.build_bay(d, 6, contaminated=(3,))
        res = sch.simulate_day(panels, 1)
        self.assertIn(3, res["quarantined"])
        self.assertEqual(panels[3].harvests, 0)          # 오염 패널은 생산라인으로 수확 안 함
        self.assertLess(res["mean_density_dev"], 0.10)
        self.assertLess(res["max_x"], BASE.reactor.x_opt * 1.25)

    def test_vision(self):
        healthy = rb.vision_estimate(120, 150, 90)
        sick = rb.vision_estimate(150, 140, 90)
        self.assertGreater(healthy["biomass_g_l"], 0.5)
        self.assertLess(healthy["contamination_score"], 0.6)
        self.assertGreaterEqual(sick["contamination_score"], 0.6)


if __name__ == "__main__":
    unittest.main()
