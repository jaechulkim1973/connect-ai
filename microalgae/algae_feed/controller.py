"""실시간 급이(Feeding) 제어기.

센서값(pH, DO, 온도, PAR, 세포농도, 잔류당/질산염 선택)을 받아 매 tick 마다
  · CO2 밸브 (pH-stat, 광합성 탄소원)
  · 포도당 펄스 (DO-stat + 잔류당 상한, 혼합영양 탄소원)
  · 질소/인 영양염 (측정 기반 보충 + 바이오매스 증가량 비례 feed-forward)
  · 반연속 수확(희석) · 가온/냉각
명령을 산출한다. PLC/Modbus·MQTT 등 어떤 I/O 계층에도 붙일 수 있도록 순수 로직만 담았다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .config import Strain
from .model import ctmi


@dataclass
class Sensors:
    hour: float                      # 0~24 현지 시각
    ph: float
    do_pct: float                    # 용존산소 % 공기포화
    temp_c: float
    par_umol: float                  # 배양기 표면 PAR µmol/m²/s
    biomass_g_l: float               # OD/탁도 환산 건조중량
    volume_l: float
    glucose_g_l: Optional[float] = None  # 온라인 당 센서(없으면 None → 모델 추정)
    no3_n_mg_l: Optional[float] = None
    po4_p_mg_l: Optional[float] = None


@dataclass
class ControllerConfig:
    ph_setpoint: float = 7.5
    ph_band_hi: float = 0.10         # pH > sp+hi → CO2 ON
    ph_band_lo: float = 0.05         # pH < sp-lo → CO2 OFF
    ph_alarm_lo: float = 6.3
    ph_alarm_hi: float = 9.5
    do_min_feed: float = 40.0        # 이 이하에서는 포도당 금지 (산소 제한 → 발효·오염 유발)
    do_high: float = 250.0           # 광합성 O2 과잉 → 급이 우선 (혼합영양 시너지)
    do_alarm_hi: float = 350.0
    glucose_low: float = 0.10        # g/L, 이 밑으로 떨어지면 펄스
    glucose_target: float = 0.35
    glucose_max: float = 0.50        # 잔류당 절대 상한 (세균 오염 방지)
    allow_dark_feeding: bool = True
    dark_feed_factor: float = 0.6    # 야간 종속영양 속도 비율
    glucose_stock_g_l: float = 500.0
    n_stock_g_l: float = 100.0       # N 기준 (요소/질산염 혼합액)
    p_stock_g_l: float = 20.0        # P 기준
    n_low_mg_l: float = 60.0
    n_target_mg_l: float = 150.0
    p_low_mg_l: float = 8.0
    p_target_mg_l: float = 20.0
    x_opt: float = 2.5               # 수확 기준 농도 g/L
    harvest_trigger: float = 1.15    # x_opt × 이 배수 초과 시 희석
    heat_on_c: float = 18.0
    cool_on_c: float = 32.0
    hysteresis_c: float = 0.5
    daily_glucose_budget_g: float = float("inf")   # 일일 계획(Planner)에서 내려옴
    contamination_ratio: float = 2.5  # 실측 당 소비가 예측의 N배 이상 → 오염 의심


@dataclass
class Actions:
    co2_on: bool = False
    glucose_ml: float = 0.0
    n_ml: float = 0.0
    p_ml: float = 0.0
    harvest_l: float = 0.0
    heater_on: bool = False
    cooler_on: bool = False
    alarms: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class FeedController:
    def __init__(self, strain: Strain, cfg: ControllerConfig | None = None):
        self.s = strain
        self.cfg = cfg or ControllerConfig()
        self.co2_on = False
        self.heater_on = False
        self.cooler_on = False
        self.glucose_est: float = 0.0
        self.n_est: float = self.cfg.n_target_mg_l
        self.p_est: float = self.cfg.p_target_mg_l
        self.glucose_used_today_g = 0.0
        self.last_biomass_g: Optional[float] = None
        self.last_glucose_meas: Optional[float] = None
        self.last_hour: Optional[float] = None
        self.glucose_locked = False
        self._last_dose_g_l = 0.0
        # 누적 통계
        self.totals = {"glucose_g": 0.0, "n_g": 0.0, "p_g": 0.0, "harvest_l": 0.0, "co2_on_min": 0.0}

    # -------------------------------------------------------------- helpers
    def _q_glucose(self, x: float, t: float, light: bool) -> float:
        """예상 포도당 소비속도 g/L/h."""
        mu = self.s.mu_het_max * ctmi(t, self.s)
        if not light:
            mu *= self.cfg.dark_feed_factor
        return mu * x / self.s.yield_glucose / 24.0

    def new_day(self):
        self.glucose_used_today_g = 0.0

    # ----------------------------------------------------------------- step
    def step(self, z: Sensors, dt_h: float) -> Actions:
        c, a = self.cfg, Actions()
        if self.last_hour is not None and z.hour < self.last_hour:
            self.new_day()
        self.last_hour = z.hour
        light = z.par_umol > 5.0

        # ---- 1. 온도 ----
        if z.temp_c < c.heat_on_c - c.hysteresis_c:
            self.heater_on = True
        elif z.temp_c > c.heat_on_c + c.hysteresis_c:
            self.heater_on = False
        if z.temp_c > c.cool_on_c + c.hysteresis_c:
            self.cooler_on = True
        elif z.temp_c < c.cool_on_c - c.hysteresis_c:
            self.cooler_on = False
        a.heater_on, a.cooler_on = self.heater_on, self.cooler_on

        # ---- 2. CO2 pH-stat ----
        if z.ph > c.ph_setpoint + c.ph_band_hi:
            self.co2_on = True
        elif z.ph < c.ph_setpoint - c.ph_band_lo:
            self.co2_on = False
        a.co2_on = self.co2_on
        if self.co2_on:
            self.totals["co2_on_min"] += dt_h * 60
        if z.ph < c.ph_alarm_lo or z.ph > c.ph_alarm_hi:
            a.alarms.append(f"pH 이상 {z.ph:.2f}")

        # ---- 3. 포도당 (혼합영양) ----
        q = self._q_glucose(z.biomass_g_l, z.temp_c, light)
        if z.glucose_g_l is not None:
            # 오염 감시: 측정된 소비속도 vs 예측
            if self.last_glucose_meas is not None and dt_h > 0:
                consumed = (self.last_glucose_meas + self._last_dose_g_l - z.glucose_g_l) / dt_h
                if q > 0 and consumed > c.contamination_ratio * q and consumed > 0.05:
                    self.glucose_locked = True
                    a.alarms.append(
                        f"오염 의심: 당 소비 {consumed:.3f} g/L/h > 예측 {q:.3f} — 포도당 급이 차단")
            self.glucose_est = z.glucose_g_l
            self.last_glucose_meas = z.glucose_g_l
        else:
            self.glucose_est = max(0.0, self.glucose_est - q * dt_h)
        self._last_dose_g_l = 0.0

        feed_ok = (self.s.mixotrophic and not self.glucose_locked
                   and z.do_pct >= c.do_min_feed
                   and (light or c.allow_dark_feeding)
                   and ctmi(z.temp_c, self.s) > 0.2)
        if z.do_pct < c.do_min_feed:
            a.notes.append("DO 낮음 → 포도당 보류, 폭기량 증가 권장")
        if feed_ok and self.glucose_est < c.glucose_low:
            target = c.glucose_max if z.do_pct > c.do_high else c.glucose_target
            dose_g_l = max(0.0, min(target, c.glucose_max) - self.glucose_est)
            dose_g = dose_g_l * z.volume_l
            remaining = c.daily_glucose_budget_g - self.glucose_used_today_g
            dose_g = max(0.0, min(dose_g, remaining))
            if dose_g > 0:
                a.glucose_ml = dose_g / c.glucose_stock_g_l * 1000
                self.glucose_used_today_g += dose_g
                self.totals["glucose_g"] += dose_g
                self._last_dose_g_l = dose_g / z.volume_l
                self.glucose_est += self._last_dose_g_l
            elif remaining <= 0:
                a.notes.append("일일 포도당 예산 소진")

        # ---- 4. 영양염 (N, P) ----
        x_g = z.biomass_g_l * z.volume_l
        growth_g = 0.0
        if self.last_biomass_g is not None:
            growth_g = max(0.0, x_g - self.last_biomass_g)
        # feed-forward 소비 추정
        self.n_est -= growth_g * self.s.n_frac * 1000 / z.volume_l
        self.p_est -= growth_g * self.s.p_frac * 1000 / z.volume_l
        if z.no3_n_mg_l is not None:
            self.n_est = z.no3_n_mg_l
        if z.po4_p_mg_l is not None:
            self.p_est = z.po4_p_mg_l
        if self.n_est < c.n_low_mg_l:
            add_mg = (c.n_target_mg_l - self.n_est) * z.volume_l
            a.n_ml = add_mg / c.n_stock_g_l
            self.totals["n_g"] += add_mg / 1000
            self.n_est = c.n_target_mg_l
        if self.p_est < c.p_low_mg_l:
            add_mg = (c.p_target_mg_l - self.p_est) * z.volume_l
            a.p_ml = add_mg / c.p_stock_g_l
            self.totals["p_g"] += add_mg / 1000
            self.p_est = c.p_target_mg_l

        # ---- 5. 반연속 수확 ----
        if z.biomass_g_l > c.x_opt * c.harvest_trigger:
            a.harvest_l = z.volume_l * (1 - c.x_opt / z.biomass_g_l)
            self.totals["harvest_l"] += a.harvest_l
            # 신선 배지로 보충 → 농도 희석 반영 (당·영양염도 희석)
            frac = c.x_opt / z.biomass_g_l
            self.glucose_est *= frac
            self.n_est = self.n_est * frac + c.n_target_mg_l * (1 - frac)
            self.p_est = self.p_est * frac + c.p_target_mg_l * (1 - frac)
            x_g = c.x_opt * z.volume_l
        self.last_biomass_g = x_g

        if z.do_pct > c.do_alarm_hi:
            a.alarms.append(f"DO 과포화 {z.do_pct:.0f}% — 탈기/유속 증가 필요 (광저해 위험)")
        return a



def daily_glucose_budget(month_glucose_g_per_m2: float, reactor_m2: float, safety: float = 1.1) -> float:
    """월별 계획(model.simulate)의 포도당 g/m²/day → 실 배양기 일일 예산."""
    return month_glucose_g_per_m2 * reactor_m2 * safety
