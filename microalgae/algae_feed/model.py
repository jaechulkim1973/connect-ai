"""월별 생산성 · 급이량 · 비용 모델.

광합성(광독립) 생산 + 유기탄소 급이(혼합영양) 생산을 배양기 바닥 1 m² 기준으로 계산하고,
목표 연간 생산량에 필요한 배양기·에어돔 면적과 CAPEX/OPEX 를 산출한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List

from .config import Climate, Economics, Enclosure, Operation, Reactor, Strain

PAR_FRACTION = 0.45          # 전천일사 중 PAR 비율
MOL_PER_MJ_PAR = 4.57        # PAR 1 MJ ≈ 4.57 mol photons
HOURS = 24.0
CO2_PER_KG_BIOMASS = 1.83    # 광독립 1 kg 생산 시 고정 CO2 (C 50% 기준)


def ctmi(t: float, s: Strain) -> float:
    """Cardinal Temperature Model with Inflection (Rosso 1993). 0~1."""
    if t <= s.t_min or t >= s.t_max:
        return 0.0
    num = (t - s.t_max) * (t - s.t_min) ** 2
    den = (s.t_opt - s.t_min) * (
        (s.t_opt - s.t_min) * (t - s.t_opt)
        - (s.t_opt - s.t_max) * (s.t_opt + s.t_min - 2 * t))
    return max(0.0, min(1.0, num / den)) if den else 0.0


def crf(rate: float, years: float) -> float:
    """자본회수계수 (연 등가 비용 환산)."""
    if rate == 0:
        return 1.0 / years
    return rate * (1 + rate) ** years / ((1 + rate) ** years - 1)


@dataclass
class MonthResult:
    month: int
    days: int
    par_in: float          # 돔 내부 PAR mol/m²/day
    t_passive: float       # 무가온 배양액 온도
    t_culture: float
    p_auto: float          # 광합성 생산 g/m²/day (배양기 바닥 기준)
    p_mixo: float          # 급이에 의한 추가 생산 g/m²/day
    glucose: float         # 포도당 급이 g/m²/day
    co2: float             # CO2 주입 g/m²/day
    n_feed: float          # 질소 급이 g N/m²/day
    p_feed: float          # 인 급이 g P/m²/day
    heat_kwh: float        # 가온 열량 kWh/m²(배양기)/day
    harvest_l: float       # 수확(희석) 부피 L/m²/day

    @property
    def total(self) -> float:
        return self.p_auto + self.p_mixo


@dataclass
class Scenario:
    reactor: Reactor
    enclosure: Enclosure
    strain: Strain
    climate: Climate
    op: Operation
    econ: Economics
    months: List[MonthResult] = field(default_factory=list)

    # ---- 생산 집계 ----
    @property
    def annual_kg_per_m2(self) -> float:
        return sum(m.total * m.days for m in self.months) / 1000.0

    @property
    def min_month_ratio(self) -> float:
        tots = [m.total for m in self.months]
        return (min(tots) / max(tots)) if max(tots) > 0 else 0.0

    @property
    def mixo_share(self) -> float:
        tot = sum(m.total * m.days for m in self.months)
        return sum(m.p_mixo * m.days for m in self.months) / tot if tot else 0.0

    # ---- 비용 (배양기 바닥 1 m² 기준, USD/yr) ----
    def cost_breakdown(self) -> dict:
        r, e, ec = self.reactor, self.enclosure, self.econ
        dome_m2 = 1.0 / ec.packing_ratio if e.key != "none" else 0.0
        capex = {
            "reactor": r.capex_per_m2,
            "enclosure": e.capex_per_m2 * dome_m2,
            "bop": ec.bop_capex_per_m2,
            "seed": ec.seed_capex_per_m2,
        }
        ann = (r.capex_per_m2 * crf(ec.discount_rate, r.life_years)
               + e.capex_per_m2 * dome_m2 * crf(ec.discount_rate, e.life_years)
               + (ec.bop_capex_per_m2 + ec.seed_capex_per_m2) * crf(ec.discount_rate, 15))
        kg = self.annual_kg_per_m2
        days = sum(m.days for m in self.months)
        glu = sum(m.glucose * m.days for m in self.months) / 1000
        co2 = sum(m.co2 * m.days for m in self.months) / 1000
        n = sum(m.n_feed * m.days for m in self.months) / 1000
        p = sum(m.p_feed * m.days for m in self.months) / 1000
        heat = sum(m.heat_kwh * m.days for m in self.months)
        power = (r.energy_kwh_per_m3_day * r.volume_l_per_m2 / 1000 * days
                 + e.blower_kw_per_1000m2 / 1000 * dome_m2 * 24 * days
                 + ec.harvest_dry_kwh_per_kg * kg)
        opex = {
            "glucose": glu * ec.glucose_cost_kg,
            "co2": co2 * ec.co2_cost_kg,
            "nutrients": n * ec.n_cost_kg + p * ec.p_cost_kg + kg * ec.micro_cost_per_kg_biomass,
            "heat": heat * ec.heat_cost_kwh,
            "power": power * ec.power_cost_kwh,
            "consumables": r.consumable_per_m2_yr,
            "labor": ec.labor_per_m2_yr,
        }
        total = ann + sum(opex.values())
        return {
            "capex_per_m2": capex,
            "capex_total_per_m2": sum(capex.values()),
            "annualized_capex": ann,
            "opex": opex,
            "opex_total": sum(opex.values()),
            "annual_cost": total,
            "cost_per_kg": total / kg if kg > 0 else float("inf"),
            "capex_per_annual_kg": sum(capex.values()) / kg if kg > 0 else float("inf"),
            "margin_per_m2": kg * ec.biomass_value_kg - total,
        }


def _passive_temp(t_out: float, g: float, enc: Enclosure) -> float:
    return t_out + enc.solar_gain_k * g


def simulate(reactor: Reactor, enclosure: Enclosure, strain: Strain,
             climate: Climate, op: Operation, econ: Economics) -> Scenario:
    sc = Scenario(reactor, enclosure, strain, climate, op, econ)
    dome_m2 = 1.0 / econ.packing_ratio if enclosure.key != "none" else 0.0
    can_feed = strain.mixotrophic and reactor.closed_sterility >= 0.3 and op.feed_mode != "none"

    # 1차: 광독립 생산
    rows = []
    for i in range(12):
        g, t_out = climate.global_mj[i], climate.temp_c[i]
        par_in = g * PAR_FRACTION * MOL_PER_MJ_PAR * enclosure.transmission
        ik = strain.ik_daily * reactor.light_eff
        f_sat = (ik / par_in) * (1 - math.exp(-par_in / ik)) if par_in > 0 else 0
        t_pass = _passive_temp(t_out, g, enclosure)
        t_cul = t_pass
        heat = 0.0
        if enclosure.key != "none" and op.heat_setpoint_c is not None and t_pass < op.heat_setpoint_c:
            t_cul = op.heat_setpoint_c
            # 돔 외피 열손실 (배양기 1 m² 가 부담하는 돔 면적 기준)
            dt = op.heat_setpoint_c - t_pass
            heat = enclosure.u_value * enclosure.surface_ratio * dome_m2 * dt * HOURS / 1000
        if t_cul > op.cool_setpoint_c:
            t_cul = max(op.cool_setpoint_c, t_cul - enclosure.max_cooling_c)
        if enclosure.key == "none" and t_out < 2:
            t_cul = t_out  # 결빙 위험 — 운전 정지 간주
        f_t = ctmi(t_cul, strain)
        p_auto = strain.yield_photon * f_sat * par_in * reactor.light_eff * f_t
        rows.append([i, g, par_in, t_pass, t_cul, f_t, p_auto, heat])

    # gap_fill 목표치
    peak = max(r[6] for r in rows)
    if op.gap_target == "peak":
        target = peak
    else:
        target = float(op.gap_target)

    for i, g, par_in, t_pass, t_cul, f_t, p_auto, heat in rows:
        p_mixo = 0.0
        if can_feed:
            # 동역학 상한: 운전농도 × 부피 × mu_het × 온도보정
            kin = reactor.x_opt * reactor.volume_l_per_m2 * strain.mu_het_max * f_t
            # 산소 상한: 광합성 O2 가 혼합영양 호흡을 상쇄 + 탈기/폭기로 처리 가능한 여유
            o2 = p_auto * 1.0 + reactor.otr_g_per_l_day * reactor.volume_l_per_m2
            cap = min(kin, o2) * reactor.closed_sterility
            if op.feed_mode == "max":
                # 경제성 검증: 증분 바이오매스 가치 > 포도당 비용
                unit_cost = econ.glucose_cost_kg / strain.yield_glucose
                p_mixo = cap if econ.biomass_value_kg > unit_cost else 0.0
            else:  # gap_fill
                p_mixo = max(0.0, min(cap, target - p_auto))
        total = p_auto + p_mixo
        glucose = p_mixo / strain.yield_glucose
        co2 = p_auto * CO2_PER_KG_BIOMASS / econ.co2_efficiency
        n_feed = total * strain.n_frac
        p_feed = total * strain.p_frac
        harvest_l = total / reactor.x_opt
        sc.months.append(MonthResult(
            month=i + 1, days=climate.days[i], par_in=par_in, t_passive=t_pass, t_culture=t_cul,
            p_auto=p_auto, p_mixo=p_mixo, glucose=glucose, co2=co2,
            n_feed=n_feed, p_feed=p_feed, heat_kwh=heat, harvest_l=harvest_l))
    return sc


def sizing(sc: Scenario, target_tpy: float) -> dict:
    """연간 목표 생산량(t/yr)에 필요한 면적·투자비."""
    kgpm2 = sc.annual_kg_per_m2
    if kgpm2 <= 0:
        return {"feasible": False}
    reactor_m2 = target_tpy * 1000 / kgpm2
    dome_m2 = reactor_m2 / sc.econ.packing_ratio if sc.enclosure.key != "none" else 0
    cb = sc.cost_breakdown()
    capex = {k: v * reactor_m2 for k, v in cb["capex_per_m2"].items()}
    return {
        "feasible": True,
        "reactor_m2": reactor_m2,
        "dome_m2": dome_m2,
        "culture_volume_m3": reactor_m2 * sc.reactor.volume_l_per_m2 / 1000,
        "capex": capex,
        "capex_total": sum(capex.values()),
        "annual_cost": cb["annual_cost"] * reactor_m2,
        "cost_per_kg": cb["cost_per_kg"],
    }
