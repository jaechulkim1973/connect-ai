import os
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from algae_feed import robot as rb  # noqa: E402
from algae_feed.config import CLIMATES, ENCLOSURES, REACTORS, STRAINS, Economics, Operation  # noqa: E402
from algae_feed.model import simulate  # noqa: E402
from algae_feed.twin import PlantTwin  # noqa: E402

BASE = simulate(REACTORS["film_panel"], ENCLOSURES["air_dome_pe2"], STRAINS["chlorella"],
                CLIMATES["korea_central"], replace(Operation(), heat_setpoint_c=22.0, feed_mode="max"),
                Economics())
DESIGN = rb.optimize(BASE, 100, systems=["overhead_gantry"])[0]


def twin(n=6, month=0):
    return PlantTwin(DESIGN.scenario, DESIGN, n_modules=n, month_idx=month, cloud_sigma=0.0)


class TwinTest(unittest.TestCase):
    def test_matches_design_model(self):
        tw = twin().run(48)
        model = DESIGN.scenario.months[0].total
        self.assertAlmostEqual(tw.kpis()["g_per_m2_day"], model, delta=model * 0.2)

    def test_incidents_raise_specific_alarms(self):
        tw = twin().run(10)
        tw.inject(1, "contamination")
        tw.inject(2, "glucose_pump_fail")
        tw.inject(4, "heater_fail")
        tw.run(30)
        ev = "\n".join(tw.events)
        self.assertIn("모듈1: 오염 의심", ev)
        self.assertIn("모듈2: 포도당 펌프 이상", ev)
        self.assertIn("모듈4: 온도 저하", ev)
        self.assertNotIn("모듈2: 오염", ev)          # 펌프 고장을 오염으로 오인하지 않음
        self.assertTrue(tw.modules[1].ctl.glucose_locked)
        for m in (0, 3, 5):                           # 정상 모듈엔 경보 없음
            self.assertEqual(tw.modules[m].alarms, [])

    def test_sync_learns_true_growth_rate(self):
        for true_scale in (0.7, 1.3):
            tw = twin(4).run(30)
            real = tw.clone()
            for m in real.modules:
                m.mu_het_scale = true_scale
            marks = [m.harvested_l for m in real.modules]
            for m in real.modules:
                tw.sync(m.mid, biomass_g_l=m.state.x)
            for _ in range(16):
                real.run(3)
                tw.run(3)
                for m in real.modules:
                    tw.sync(m.mid, biomass_g_l=m.state.x, harvested_l=m.harvested_l - marks[m.mid])
                    marks[m.mid] = m.harvested_l
            learned = [m.mu_het_scale for m in tw.modules]
            self.assertAlmostEqual(sum(learned) / len(learned), true_scale, delta=0.15)

    def test_what_if_does_not_touch_live_twin(self):
        tw = twin().run(12)
        before = tw.kpis()["produced_kg"]
        wi = tw.what_if(24, {"more glucose": {"glucose_budget_scale": 1.5}, "cold": {"heat_on_c": 15.0}})
        self.assertEqual(tw.kpis()["produced_kg"], before)
        self.assertGreater(wi["more glucose"]["glucose_kg"], wi["현재 설정"]["glucose_kg"])
        self.assertLess(wi["cold"]["heat_kwh"], wi["현재 설정"]["heat_kwh"])

    def test_robot_capacity_limits_harvest(self):
        tw = twin(4).run(6)
        tw.robot_l_h = 1.0                             # 로봇 처리능력을 거의 0으로
        tw.run(24)
        self.assertGreater(tw.kpis()["backlog_l"], 100)

    def test_dashboard_renders_data(self):
        tw = twin(3).run(6)
        html = tw.dashboard_html({"현재 설정": tw.kpis()})
        self.assertIn("<!doctype html>", html)
        self.assertNotIn("__DATA__", html)


if __name__ == "__main__":
    unittest.main()
