"""간이 플랜트(배양기) 동역학 — 제어기 검증용 1일 시뮬레이션 (1분 tick)."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Reactor, Strain
from .controller import ControllerConfig, FeedController, Sensors
from .model import ctmi

O2_SAT_MG_L = 8.0
O2_PER_G_PHOTO = 1.3      # g O2 발생 / g 광합성 바이오매스
O2_PER_G_GLUCOSE = 0.5    # g O2 소비 / g 포도당 (혼합영양 동화)


@dataclass
class DayResult:
    biomass_produced_g: float
    photo_g: float
    mixo_g: float
    glucose_fed_g: float
    n_fed_g: float
    p_fed_g: float
    harvest_l: float
    co2_on_min: float
    mean_do: float
    max_do: float
    min_ph: float
    max_ph: float
    alarms: list


def simulate_day(strain: Strain, reactor: Reactor, reactor_m2: float, par_mol_day: float,
                 t_env_mean: float, cfg: ControllerConfig | None = None,
                 x0: float | None = None, contaminate_at_h: float | None = None) -> DayResult:
    cfg = cfg or ControllerConfig(x_opt=reactor.x_opt)
    ctl = FeedController(strain, cfg)
    v = reactor.volume_l_per_m2 * reactor_m2
    x = x0 if x0 is not None else reactor.x_opt
    g, ph, do, t = 0.0, cfg.ph_setpoint, 100.0, max(t_env_mean, cfg.heat_on_c)
    n, p = cfg.n_target_mg_l, cfg.p_target_mg_l
    dt = 1 / 60
    peak_umol = par_mol_day * math.pi / (2 * 12 * 3600) * 1e6
    ik_inst = 350.0 * reactor.light_eff
    photo_g = mixo_g = 0.0
    do_sum, do_max, ph_min, ph_max = 0.0, 0.0, 99.0, 0.0
    alarms: list = []
    bugs = 0.0
    steps = int(24 / dt)
    for k in range(steps):
        h = k * dt
        par = peak_umol * math.sin(math.pi * (h - 6) / 12) if 6 < h < 18 else 0.0
        if contaminate_at_h is not None and h >= contaminate_at_h and bugs == 0:
            bugs = 0.02  # g/L 세균 접종(오염 사고)

        act = ctl.step(Sensors(hour=h, ph=ph, do_pct=do, temp_c=t, par_umol=par,
                               biomass_g_l=x, volume_l=v, glucose_g_l=g if contaminate_at_h is not None else None,
                               no3_n_mg_l=None, po4_p_mg_l=None), dt)
        for al in act.alarms:
            if al not in alarms:
                alarms.append(al)
        # --- 액추에이터 반영
        g += act.glucose_ml * cfg.glucose_stock_g_l / 1000 / v
        n += act.n_ml * cfg.n_stock_g_l / v
        p += act.p_ml * cfg.p_stock_g_l / v
        if act.harvest_l > 0:
            frac = (v - act.harvest_l) / v
            x *= frac; g *= frac; bugs *= frac
            n = n * frac + cfg.n_target_mg_l * (1 - frac)
            p = p * frac + cfg.p_target_mg_l * (1 - frac)
        # --- 생물 반응
        ft = ctmi(t, strain)
        nut = (n / (n + 5.0)) * (p / (p + 0.5))
        i_mol_h = par * 3600e-6
        photo_inst = 0.0
        if par > 0:
            sat = (ik_inst / par) * (1 - math.exp(-par / ik_inst))
            photo_inst = strain.yield_photon * sat * i_mol_h * reactor.light_eff * ft * nut  # g/m²/h
        photo_rate = photo_inst / reactor.volume_l_per_m2           # g/L/h
        if do > 300:
            photo_rate *= 0.85                                        # 산소 저해
        mu_h = strain.mu_het_max * ft / 24 * (1.0 if par > 5 else cfg.dark_feed_factor)
        o2_lim = 1.0 if do > 20 else 0.4
        uptake = mu_h * x / strain.yield_glucose * g / (g + 0.05) * o2_lim * nut   # g glu/L/h
        uptake = min(uptake, g / dt)
        bug_uptake = min(1.2 * bugs * g / (g + 0.02), max(0.0, g / dt - uptake)) if bugs else 0.0
        bugs += bug_uptake * 0.45 * dt
        mixo_rate = uptake * strain.yield_glucose
        x += (photo_rate + mixo_rate) * dt
        g = max(0.0, g - (uptake + bug_uptake) * dt)
        photo_g += photo_rate * dt * v
        mixo_g += mixo_rate * dt * v
        n = max(0.0, n - (photo_rate + mixo_rate) * dt * strain.n_frac * 1000)
        p = max(0.0, p - (photo_rate + mixo_rate) * dt * strain.p_frac * 1000)
        # pH: 광합성 탄소흡수 ↑, CO2 주입·호흡 ↓
        ph += (photo_rate * 40.0 - (2.0 if act.co2_on else 0.0) - uptake * 1.0 - 0.02) * dt
        ph = min(11.0, max(5.5, ph))
        # DO (%sat): 광합성 생성 – 호흡 소비 – 탈기
        o2_mg = (photo_rate * O2_PER_G_PHOTO - (uptake + bug_uptake) * O2_PER_G_GLUCOSE) * 1000
        do += (o2_mg / O2_SAT_MG_L * 100 - 6.0 * (do - 100)) * dt
        do = max(0.0, do)
        # 온도
        t_env = t_env_mean + 4 * math.sin(math.pi * (h - 9) / 12)
        t += (0.3 * (t_env - t) + (6.0 if act.heater_on else 0) - (6.0 if act.cooler_on else 0)) * dt
        do_sum += do; do_max = max(do_max, do)
        ph_min = min(ph_min, ph); ph_max = max(ph_max, ph)

    return DayResult(
        biomass_produced_g=photo_g + mixo_g, photo_g=photo_g, mixo_g=mixo_g,
        glucose_fed_g=ctl.totals["glucose_g"], n_fed_g=ctl.totals["n_g"], p_fed_g=ctl.totals["p_g"],
        harvest_l=ctl.totals["harvest_l"], co2_on_min=ctl.totals["co2_on_min"],
        mean_do=do_sum / steps, max_do=do_max, min_ph=ph_min, max_ph=ph_max, alarms=alarms)
