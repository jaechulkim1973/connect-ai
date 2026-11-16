import os
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from algae_feed.config import CLIMATES, ENCLOSURES, REACTORS, STRAINS, Economics, Operation  # noqa: E402
from algae_feed.controller import ControllerConfig, FeedController, Sensors  # noqa: E402
from algae_feed.model import ctmi, simulate  # noqa: E402
from algae_feed.optimizer import best, enumerate_scenarios  # noqa: E402
from algae_feed.sim import simulate_day  # noqa: E402

S = STRAINS["chlorella"]
KR = CLIMATES["korea_central"]


def sc(reactor, enc, heat=18.0, feed="none"):
    return simulate(REACTORS[reactor], ENCLOSURES[enc], S, KR,
                    replace(Operation(), heat_setpoint_c=heat, feed_mode=feed), Economics())


class ModelTest(unittest.TestCase):
    def test_ctmi_bounds(self):
        self.assertEqual(ctmi(S.t_min, S), 0)
        self.assertEqual(ctmi(S.t_max, S), 0)
        self.assertAlmostEqual(ctmi(S.t_opt, S), 1.0, places=6)

    def test_outdoor_winter_collapses_dome_keeps_running(self):
        open_air = sc("raceway", "none", heat=None)
        dome = sc("film_panel", "air_dome_pe2", heat=18.0)
        self.assertLess(open_air.months[0].total, 0.5)        # 1월 노지 = 사실상 0
        self.assertGreater(dome.months[0].total, 5.0)          # 돔+가온 → 겨울 생산 유지

    def test_raceway_cannot_be_fed(self):
        s = sc("raceway", "air_dome_pe2", feed="max")
        self.assertEqual(sum(m.glucose for m in s.months), 0)

    def test_feeding_raises_productivity_and_cuts_reactor_area(self):
        auto = sc("film_panel", "air_dome_pe2", feed="none")
        mixo = sc("film_panel", "air_dome_pe2", feed="max")
        self.assertGreater(mixo.annual_kg_per_m2, 2 * auto.annual_kg_per_m2)
        self.assertGreater(mixo.min_month_ratio, auto.min_month_ratio)

    def test_gap_fill_flattens_year(self):
        s = sc("flat_panel_rigid", "air_dome_pe2", heat=22.0, feed="gap_fill")
        self.assertGreater(s.min_month_ratio, 0.75)

    def test_optimizer_prefers_low_cost_reactor_in_dome(self):
        top = best(enumerate_scenarios(S, KR, Economics()))[0]
        self.assertIn(top.reactor.key, ("film_panel", "flat_panel_rigid"))
        self.assertTrue(top.enclosure.key.startswith("air_dome"))
        self.assertNotEqual(top.op.feed_mode, "none")


class ControllerTest(unittest.TestCase):
    def base(self, **kw):
        d = dict(hour=12, ph=7.5, do_pct=120, temp_c=25, par_umol=800, biomass_g_l=2.5, volume_l=1000)
        d.update(kw)
        return Sensors(**d)

    def test_ph_stat_hysteresis(self):
        c = FeedController(S)
        self.assertTrue(c.step(self.base(ph=7.7), 1 / 60).co2_on)
        self.assertTrue(c.step(self.base(ph=7.5), 1 / 60).co2_on)    # 밴드 안 → 유지
        self.assertFalse(c.step(self.base(ph=7.4), 1 / 60).co2_on)

    def test_glucose_pulse_respects_residual_cap_and_budget(self):
        cfg = ControllerConfig(daily_glucose_budget_g=300)
        c = FeedController(S, cfg)
        a = c.step(self.base(glucose_g_l=0.0), 1 / 60)
        dosed_g = a.glucose_ml * cfg.glucose_stock_g_l / 1000
        self.assertLessEqual(dosed_g / 1000, cfg.glucose_max + 1e-9)
        self.assertLessEqual(dosed_g, 300 + 1e-9)

    def test_no_glucose_when_oxygen_limited(self):
        c = FeedController(S)
        a = c.step(self.base(do_pct=20, glucose_g_l=0.0), 1 / 60)
        self.assertEqual(a.glucose_ml, 0)

    def test_harvest_when_dense(self):
        c = FeedController(S, ControllerConfig(x_opt=2.5))
        a = c.step(self.base(biomass_g_l=3.5), 1 / 60)
        self.assertAlmostEqual(a.harvest_l, 1000 * (1 - 2.5 / 3.5), places=3)

    def test_nitrogen_topup(self):
        c = FeedController(S)
        a = c.step(self.base(no3_n_mg_l=30), 1 / 60)
        self.assertAlmostEqual(a.n_ml, (150 - 30) * 1000 / 100.0, places=3)

    def test_contamination_lockout(self):
        r = REACTORS["film_panel"]
        d = simulate_day(S, r, 10, 20, 22, ControllerConfig(x_opt=r.x_opt, heat_on_c=22),
                         contaminate_at_h=8)
        self.assertTrue(any("오염" in al for al in d.alarms))

    def test_day_sim_feeding_adds_biomass_ph_stays_in_band(self):
        r = REACTORS["film_panel"]
        base = simulate_day(S, r, 10, 12, 22, ControllerConfig(x_opt=r.x_opt, daily_glucose_budget_g=0, heat_on_c=22))
        fed = simulate_day(S, r, 10, 12, 22, ControllerConfig(x_opt=r.x_opt, daily_glucose_budget_g=500, heat_on_c=22))
        self.assertGreater(fed.biomass_produced_g, base.biomass_produced_g + 150)
        self.assertGreater(fed.min_ph, 6.5)
        self.assertLess(fed.max_ph, 8.5)


if __name__ == "__main__":
    unittest.main()
