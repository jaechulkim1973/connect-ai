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


@dataclass
class ReactorState:
    """배양기 1개(모듈)의 물리·생물 상태 — 시뮬레이터와 디지털 트윈이 공유."""
    v: float                  # 배양액 L
    x: float                  # 바이오매스 g/L
    g: float = 0.0            # 잔류 포도당 g/L
    ph: float = 7.5
    do: float = 100.0         # %sat
    t: float = 20.0           # °C
    n: float = 150.0          # N mg/L
    p: float = 20.0           # P mg/L
    bugs: float = 0.0         # 오염균 g/L


def apply_actions(s: ReactorState, act, cfg: ControllerConfig, harvest_l: float | None = None) -> float:
    """제어 명령을 상태에 반영. 실제 수확한 L 반환 (harvest_l 로 로봇 처리량 제한 가능)."""
    s.g += act.glucose_ml * cfg.glucose_stock_g_l / 1000 / s.v
    s.n += act.n_ml * cfg.n_stock_g_l / s.v
    s.p += act.p_ml * cfg.p_stock_g_l / s.v
    h = act.harvest_l if harvest_l is None else harvest_l
    if h > 0:
        frac = (s.v - h) / s.v
        s.x *= frac; s.g *= frac; s.bugs *= frac
        s.n = s.n * frac + cfg.n_target_mg_l * (1 - frac)
        s.p = s.p * frac + cfg.p_target_mg_l * (1 - frac)
    return h


def react(s: ReactorState, par: float, t_env: float, act, strain: Strain, reactor: Reactor,
          cfg: ControllerConfig, dt: float, mu_het_scale: float = 1.0) -> tuple:
    """dt(h) 동안 생물 반응·pH·DO·온도 적분. (광합성 g/L, 급이 g/L) 반환."""
    ik_inst = 350.0 * reactor.light_eff
    ft = ctmi(s.t, strain)
    nut = (s.n / (s.n + 5.0)) * (s.p / (s.p + 0.5))
    photo_inst = 0.0
    if par > 0:
        sat = (ik_inst / par) * (1 - math.exp(-par / ik_inst))
        photo_inst = strain.yield_photon * sat * par * 3600e-6 * reactor.light_eff * ft * nut  # g/m²/h
    photo_rate = photo_inst / reactor.volume_l_per_m2
    if s.do > 300:
        photo_rate *= 0.85                                            # 산소 저해
    mu_h = strain.mu_het_max * mu_het_scale * ft / 24 * (1.0 if par > 5 else cfg.dark_feed_factor)
    o2_lim = 1.0 if s.do > 20 else 0.4
    uptake = mu_h * s.x / strain.yield_glucose * s.g / (s.g + 0.05) * o2_lim * nut
    uptake = min(uptake, s.g / dt)
    bug_uptake = min(1.2 * s.bugs * s.g / (s.g + 0.02), max(0.0, s.g / dt - uptake)) if s.bugs else 0.0
    s.bugs += bug_uptake * 0.45 * dt
    mixo_rate = uptake * strain.yield_glucose
    s.x += (photo_rate + mixo_rate) * dt
    s.g = max(0.0, s.g - (uptake + bug_uptake) * dt)
    s.n = max(0.0, s.n - (photo_rate + mixo_rate) * dt * strain.n_frac * 1000)
    s.p = max(0.0, s.p - (photo_rate + mixo_rate) * dt * strain.p_frac * 1000)
    s.ph += (photo_rate * 40.0 - (2.0 if act.co2_on else 0.0) - uptake * 1.0 - 0.02) * dt
    s.ph = min(11.0, max(5.5, s.ph))
    o2_mg = (photo_rate * O2_PER_G_PHOTO - (uptake + bug_uptake) * O2_PER_G_GLUCOSE) * 1000
    s.do = max(0.0, s.do + (o2_mg / O2_SAT_MG_L * 100 - 6.0 * (s.do - 100)) * dt)
    s.t += (0.3 * (t_env - s.t) + (6.0 if act.heater_on else 0) - (6.0 if act.cooler_on else 0)) * dt
    return photo_rate * dt, mixo_rate * dt


def par_at(hour: float, par_mol_day: float) -> float:
    peak = par_mol_day * math.pi / (2 * 12 * 3600) * 1e6
    return peak * math.sin(math.pi * (hour - 6) / 12) if 6 < hour < 18 else 0.0


def t_env_at(hour: float, t_mean: float) -> float:
    return t_mean + 4 * math.sin(math.pi * (hour - 9) / 12)


def simulate_day(strain: Strain, reactor: Reactor, reactor_m2: float, par_mol_day: float,
                 t_env_mean: float, cfg: ControllerConfig | None = None,
                 x0: float | None = None, contaminate_at_h: float | None = None) -> DayResult:
    cfg = cfg or ControllerConfig(x_opt=reactor.x_opt)
    ctl = FeedController(strain, cfg)
    s = ReactorState(v=reactor.volume_l_per_m2 * reactor_m2,
                     x=x0 if x0 is not None else reactor.x_opt, ph=cfg.ph_setpoint,
                     t=max(t_env_mean, cfg.heat_on_c), n=cfg.n_target_mg_l, p=cfg.p_target_mg_l)
    dt = 1 / 60
    photo_g = mixo_g = 0.0
    do_sum, do_max, ph_min, ph_max = 0.0, 0.0, 99.0, 0.0
    alarms: list = []
    steps = int(24 / dt)
    for k in range(steps):
        h = k * dt
        par = par_at(h, par_mol_day)
        if contaminate_at_h is not None and h >= contaminate_at_h and s.bugs == 0:
            s.bugs = 0.02  # g/L 세균 접종(오염 사고)
        act = ctl.step(Sensors(hour=h, ph=s.ph, do_pct=s.do, temp_c=s.t, par_umol=par,
                               biomass_g_l=s.x, volume_l=s.v,
                               glucose_g_l=s.g if contaminate_at_h is not None else None), dt)
        for al in act.alarms:
            if al not in alarms:
                alarms.append(al)
        apply_actions(s, act, cfg)
        dp, dm = react(s, par, t_env_at(h, t_env_mean), act, strain, reactor, cfg, dt)
        photo_g += dp * s.v
        mixo_g += dm * s.v
        do_sum += s.do; do_max = max(do_max, s.do)
        ph_min = min(ph_min, s.ph); ph_max = max(ph_max, s.ph)

    return DayResult(
        biomass_produced_g=photo_g + mixo_g, photo_g=photo_g, mixo_g=mixo_g,
        glucose_fed_g=ctl.totals["glucose_g"], n_fed_g=ctl.totals["n_g"], p_fed_g=ctl.totals["p_g"],
        harvest_l=ctl.totals["harvest_l"], co2_on_min=ctl.totals["co2_on_min"],
        mean_do=do_sum / steps, max_do=do_max, min_ph=ph_min, max_ph=ph_max, alarms=alarms)
