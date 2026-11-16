"""AI 자동수거(수확) 로봇: 설계 대안 비교, 대수 산정, 작업 스케줄링.

자동수거 로봇이 하는 일 (배양 패널 1개 기준):
  1) 이동 → 2) AI 비전으로 농도·오염 판정 → 3) 퀵커플러 도킹 → 4) 초과 농도분 수확(흡입)
  5) 신선 배지 보충 → 6) 커플러 살균 → 7) 이탈.  연 1회 필름 교체도 보조합니다.

로봇 형식에 따라 달라지는 비용:
  · 로봇·레일·커플러 투자비, 감독·정비 인건비
  · **통로 폭 → 배양기/돔 면적비(packing) → 에어돔 투자비와 가온비**
  · 수확 빈도와 오염 조기 발견에 따른 생산성
모든 단가는 계획용 가정치이므로 견적을 받으면 HARVEST_SYSTEMS 만 고치면 됩니다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

from .model import Scenario, crf, simulate


# ---------------------------------------------------------------------------
# 패널 배치
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PanelLayout:
    panel_len_m: float = 2.0         # 패널 1개 길이 (레일 방향)
    panel_footprint_m2: float = 1.6  # 통로 제외, 패널+패널 간격 바닥면적
    row_len_m: float = 50.0          # 한 열(레일 1구간) 길이


@dataclass(frozen=True)
class HarvestSystem:
    key: str
    name: str
    robot_capex: float           # 로봇 1대 (USD)
    rail_per_m: float            # 레일 m 당 (USD)
    per_panel_capex: float       # 패널당 커플러·밸브·배관 (USD)
    fixed_capex: float           # 중앙 집수조·펌프·관제 (USD, 플랜트당)
    packing_ratio: float         # 배양기 바닥 / 돔 바닥 (통로 폭이 결정)
    travel_m_s: float
    dock_s: float                # 도킹 + 비전 판정 + 이탈
    sanitize_s: float
    pump_l_min: float            # 수확·보충 펌프 유량
    robot_hours_per_day: float   # 가동 가능 시간 (충전·정비 제외)
    crash_loss: float            # 오염·붕괴로 잃는 연간 생산 비율
    max_harvests_per_day: int    # 하루 최대 부분수확 횟수 (빈도↑ → 농도 최적 유지)
    labor_h_per_panel_day: float # 사람 작업 (수확·점검)
    film_h_per_panel_yr: float   # 필름 교체 사람 작업
    supervision_fte_per_robot: float
    maintenance_rate: float      # 로봇·레일 연 정비비 / CAPEX
    robot_life_yr: float = 8
    rows_per_robot_max: int = 999  # 한 대가 물리적으로 갈 수 있는 열 수 (0 = 플랜트 전체)
    rail_layout: str = "none"      # per_row(열마다 레일) | per_bay(베이 양쪽 2줄) | none
    row_change_s: float = 30.0     # 다른 열로 옮겨 가는 시간
    note: str = ""


HARVEST_SYSTEMS: Dict[str, HarvestSystem] = {
    "manual": HarvestSystem(
        "manual", "수작업 (호스 연결·수동 밸브)",
        robot_capex=0, rail_per_m=0, per_panel_capex=25, fixed_capex=30000,
        packing_ratio=0.75, travel_m_s=0, dock_s=0, sanitize_s=0, pump_l_min=60,
        robot_hours_per_day=0, crash_loss=0.07, max_harvests_per_day=1,
        labor_h_per_panel_day=0.05, film_h_per_panel_yr=0.5,
        supervision_fte_per_robot=0, maintenance_rate=0.02,
        note="통로 1.0 m, 하루 1회 주간 수확, 육안 점검"),
    "fixed_manifold": HarvestSystem(
        "fixed_manifold", "고정 배관 매니폴드 + 자동밸브",
        robot_capex=0, rail_per_m=0, per_panel_capex=150, fixed_capex=60000,
        packing_ratio=0.78, travel_m_s=0, dock_s=0, sanitize_s=0, pump_l_min=60,
        robot_hours_per_day=0, crash_loss=0.09, max_harvests_per_day=4,
        labor_h_per_panel_day=0.02, film_h_per_panel_yr=0.6,
        supervision_fte_per_robot=0, maintenance_rate=0.04,
        note="배관 공유 → 한 패널 오염이 매니폴드로 번짐, 필름 교체 시 배관 분리 작업"),
    "rail_shuttle": HarvestSystem(
        "rail_shuttle", "바닥 레일 셔틀 로봇 + AI 비전",
        robot_capex=28000, rail_per_m=45, per_panel_capex=18, fixed_capex=50000,
        packing_ratio=0.81, travel_m_s=0.5, dock_s=45, sanitize_s=15, pump_l_min=60,
        robot_hours_per_day=20, crash_loss=0.025, max_harvests_per_day=4,
        labor_h_per_panel_day=0.002, film_h_per_panel_yr=0.2,
        supervision_fte_per_robot=0.05, maintenance_rate=0.06, rows_per_robot_max=6,
        rail_layout="per_row", row_change_s=30,
        note="통로 0.7 m, 열 사이 이송대차로 여러 열 담당"),
    "overhead_gantry": HarvestSystem(
        "overhead_gantry", "천장 갠트리 로봇 + AI 비전 (에어돔 내부 무통로)",
        robot_capex=55000, rail_per_m=95, per_panel_capex=18, fixed_capex=50000,
        packing_ratio=0.87, travel_m_s=0.8, dock_s=40, sanitize_s=15, pump_l_min=80,
        robot_hours_per_day=21, crash_loss=0.025, max_harvests_per_day=4,
        labor_h_per_panel_day=0.002, film_h_per_panel_yr=0.15,
        supervision_fte_per_robot=0.05, maintenance_rate=0.05, rows_per_robot_max=12,
        rail_layout="per_bay", row_change_s=6,
        note="위에서 접근하므로 작업 통로 불필요 → 돔 면적·가온비 절감. 돔 안쪽 경량 트러스 필요"),
    "amr": HarvestSystem(
        "amr", "자율주행 AMR 로봇 + AI 비전",
        robot_capex=42000, rail_per_m=0, per_panel_capex=18, fixed_capex=60000,
        packing_ratio=0.70, travel_m_s=0.6, dock_s=70, sanitize_s=15, pump_l_min=50,
        robot_hours_per_day=18, crash_loss=0.025, max_harvests_per_day=4,
        labor_h_per_panel_day=0.002, film_h_per_panel_yr=0.2,
        supervision_fte_per_robot=0.05, maintenance_rate=0.07, rows_per_robot_max=0,
        rail_layout="none", row_change_s=40,
        note="레일 없음, 배치 유연 / 통로 1.2 m 필요 → 돔 면적 증가"),
}


@dataclass(frozen=True)
class RobotEconomics:
    labor_cost_h: float = 18.0        # 인건비 USD/h (부대비 포함)
    base_labor_per_m2_yr: float = 1.2 # 수확·필름 외 나머지 운전 인력 (배양기 m² 당)
    robot_kwh_per_h: float = 0.4
    fte_hours_yr: float = 2080.0


# ---------------------------------------------------------------------------
# 평가
# ---------------------------------------------------------------------------
@dataclass
class HarvestDesign:
    system: HarvestSystem
    reactor_m2: float
    dome_m2: float
    panels: int
    rows: int
    robots: int
    harvests_per_day: int
    cycle_s: float
    utilization: float
    capex: Dict[str, float]
    annual_cost: float
    cost_per_kg: float
    annual_kg: float
    scenario: Scenario = field(repr=False)
    layout: PanelLayout = field(default_factory=PanelLayout)
    rail_m: float = 0.0

    @property
    def capex_total(self) -> float:
        return sum(self.capex.values())


def cycle_time_s(hs: HarvestSystem, layout: PanelLayout, harvest_l: float) -> float:
    """패널 1개 수확 사이클 (이동+도킹+수확+보충+살균)."""
    travel = layout.panel_len_m / hs.travel_m_s if hs.travel_m_s else 0
    pump = 2 * harvest_l / hs.pump_l_min * 60   # 수확 + 같은 양 보충
    return travel + hs.dock_s + pump + hs.sanitize_s


def evaluate(base: Scenario, hs: HarvestSystem, target_tpy: float,
             layout: PanelLayout = PanelLayout(), reco: RobotEconomics = RobotEconomics()) -> HarvestDesign:
    # 통로 폭(packing)·기본 인건비를 반영해 생산·가온·돔 비용을 다시 계산
    econ = replace(base.econ, packing_ratio=hs.packing_ratio, labor_per_m2_yr=reco.base_labor_per_m2_yr)
    sc = simulate(base.reactor, base.enclosure, base.strain, base.climate, base.op, econ)

    peak = max(sc.months, key=lambda m: m.harvest_l)
    # 하루 수확 빈도: 로봇은 여러 번 나눠 부분수확 → 농도를 최적 근처로 유지
    def freq_factor(n: int) -> float:
        return 1.0 - 0.04 / n      # 하루 1회 수확 시 평균 농도 이탈로 4% 손실

    # 1차: 면적 → 패널 수 → 로봇 대수 (생산성 보정이 면적에 영향을 주므로 2회 반복)
    harvests = hs.max_harvests_per_day
    robots = 0
    reactor_m2 = target_tpy * 1000 / sc.annual_kg_per_m2
    cyc = util = 0.0
    for _ in range(3):
        crash = hs.crash_loss * (1 + 0.04 * (layout.panel_len_m - 2))   # 큰 패널 = 오염 시 손실 큼
        prod_mult = (1 - crash) * freq_factor(harvests)
        reactor_m2 = target_tpy * 1000 / (sc.annual_kg_per_m2 * prod_mult)
        panels = math.ceil(reactor_m2 / layout.panel_footprint_m2)
        per_row = max(1, int(layout.row_len_m // layout.panel_len_m))
        rows = math.ceil(panels / per_row)
        if hs.robot_capex == 0:
            robots, cyc, util = 0, 0.0, 0.0
            break
        # 로봇 대수: 하루 harvests 회 × 패널 수의 사이클을 소화할 수 있는 최소 대수, 부족하면 빈도를 낮춤
        best = None
        for n in range(hs.max_harvests_per_day, 0, -1):
            vol = peak.harvest_l * layout.panel_footprint_m2 / n
            cyc = cycle_time_s(hs, layout, vol)
            need_h = panels * n * cyc / 3600
            r = math.ceil(need_h / (hs.robot_hours_per_day * 0.9))
            if hs.rows_per_robot_max:
                r = max(r, math.ceil(rows / hs.rows_per_robot_max))
            cost = r * hs.robot_capex - n * 1e-6   # 동률이면 빈도 높은 쪽
            if best is None or cost < best[0]:
                best = (cost, r, n, cyc, need_h / (r * hs.robot_hours_per_day))
        _, robots, harvests, cyc, util = best

    panels = math.ceil(reactor_m2 / layout.panel_footprint_m2)
    per_row = max(1, int(layout.row_len_m // layout.panel_len_m))
    rows = math.ceil(panels / per_row)
    dome_m2 = reactor_m2 / hs.packing_ratio

    cb = sc.cost_breakdown()
    plant_capex = {k: v * reactor_m2 for k, v in cb["capex_per_m2"].items()}
    if hs.rail_layout == "per_row":          # 열마다 바닥 레일 + 열 끝 이송대차 구간
        rail_m = rows * (layout.row_len_m + 1.5)
    elif hs.rail_layout == "per_bay":        # 갠트리: 베이 양쪽 주행 레일 2줄
        bays = math.ceil(rows / hs.rows_per_robot_max)
        rail_m = bays * 2 * layout.row_len_m
    else:
        rail_m = 0.0
    plant_capex["harvest_robots"] = robots * hs.robot_capex
    plant_capex["harvest_rail"] = rail_m * hs.rail_per_m
    plant_capex["harvest_couplers_piping"] = panels * hs.per_panel_capex + hs.fixed_capex
    robot_capex = plant_capex["harvest_robots"] + plant_capex["harvest_rail"]

    dr = sc.econ.discount_rate
    annual = cb["annual_cost"] * reactor_m2
    annual += plant_capex["harvest_robots"] * crf(dr, hs.robot_life_yr)
    annual += (plant_capex["harvest_rail"] + plant_capex["harvest_couplers_piping"]) * crf(dr, 15)
    labor_h = (panels * hs.labor_h_per_panel_day * 365 + panels * hs.film_h_per_panel_yr
               + robots * hs.supervision_fte_per_robot * reco.fte_hours_yr)
    annual += labor_h * reco.labor_cost_h
    annual += robot_capex * hs.maintenance_rate + plant_capex["harvest_couplers_piping"] * 0.02
    annual += robots * hs.robot_hours_per_day * util * 365 * reco.robot_kwh_per_h * sc.econ.power_cost_kwh

    return HarvestDesign(hs, reactor_m2, dome_m2, panels, rows, robots, harvests, cyc, util,
                         plant_capex, annual, annual / (target_tpy * 1000), target_tpy * 1000, sc,
                         layout, rail_m)


PANEL_LENGTHS = [2.0, 4.0, 6.0, 8.0]
ROW_LENGTHS = [50.0, 100.0]
AISLE_DEPTH_M = 0.8   # 패널 1개가 차지하는 폭(패널 두께+패널 간 간격)


def layouts() -> List[PanelLayout]:
    return [PanelLayout(pl, pl * AISLE_DEPTH_M, rl) for pl in PANEL_LENGTHS for rl in ROW_LENGTHS]


def optimize(base: Scenario, target_tpy: float, reco: RobotEconomics = RobotEconomics(),
             systems: Optional[List[str]] = None) -> List[HarvestDesign]:
    """수거 방식 × 패널 길이 × 열 길이 전수 평가 → 방식별 최적 배치를 kg 당 원가 순으로 반환."""
    out = []
    for key in systems or HARVEST_SYSTEMS:
        hs = HARVEST_SYSTEMS[key]
        cands = [evaluate(base, hs, target_tpy, lay, reco) for lay in layouts()]
        out.append(min(cands, key=lambda d: d.cost_per_kg))
    return sorted(out, key=lambda d: d.cost_per_kg)


# ---------------------------------------------------------------------------
# AI 비전 판정 (패널 투과광 RGB → 농도·오염 지표)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VisionCalibration:
    g0: float = 235.0        # 빈 패널(배지만) 녹색 채널 투과 강도
    r0: float = 235.0
    k_green: float = 0.55    # 녹색 흡광도 → g/L 환산 (현장 건조중량으로 교정)
    healthy_gr_ratio: float = 1.35   # 건강한 녹조 투과광 G/R 비
    contam_drop: float = 0.20        # G/R 비가 이만큼 떨어지면 오염·붕괴 의심


def vision_estimate(r: float, g: float, b: float, cal: VisionCalibration = VisionCalibration()) -> dict:
    """백라이트 투과 이미지의 평균 RGB 로 농도와 오염 점수(0~1) 추정.

    Beer-Lambert: 흡광도 A = -log10(I/I0) ∝ 농도. 녹조는 적색을 흡수하고 녹색을 투과하므로
    G/R 비가 낮아지면(갈변·황변) 세균 오염, 섬모충 포식, 광붕괴를 의심한다.
    """
    g, r = max(g, 1.0), max(r, 1.0)
    a_g = -math.log10(g / cal.g0)
    biomass = max(0.0, cal.k_green * a_g * 10)
    gr = (g / cal.g0) / (r / cal.r0)
    drop = max(0.0, (cal.healthy_gr_ratio - gr) / cal.healthy_gr_ratio)
    score = min(1.0, drop / cal.contam_drop) if biomass > 0.3 else 0.0
    return {"biomass_g_l": biomass, "gr_ratio": gr, "contamination_score": score}


# ---------------------------------------------------------------------------
# AI 스케줄러 + 1일 시뮬레이션
# ---------------------------------------------------------------------------
@dataclass
class Panel:
    pid: int
    row: int
    pos: int
    x: float                     # g/L
    growth_g_l_h: float
    contamination: float = 0.0   # 0~1 (비전 점수)
    harvests: int = 0
    quarantined: bool = False


@dataclass
class Robot:
    rid: int
    row: int = 0
    pos: int = 0
    busy_until_s: float = 0.0
    work_s: float = 0.0


class HarvestScheduler:
    """우선순위 = (초과농도 × 부피) − 이동비용.  오염 의심 패널은 격리, 생산 라인으로 수확하지 않음."""

    def __init__(self, hs: HarvestSystem, layout: PanelLayout, x_opt: float,
                 volume_l: float, contam_threshold: float = 0.6, trigger: float = 1.04,
                 rows_per_robot: int = 0):
        self.hs, self.layout, self.x_opt, self.vol = hs, layout, x_opt, volume_l
        self.th, self.trigger = contam_threshold, trigger
        self.rows_per_robot = rows_per_robot

    def travel_s(self, rb: Robot, p: Panel) -> float:
        rows_cross = abs(rb.row - p.row) * self.hs.row_change_s
        along = abs(rb.pos - p.pos) * self.layout.panel_len_m / self.hs.travel_m_s
        return rows_cross + along

    def pick(self, rb: Robot, panels: List[Panel], taken: set) -> Optional[Panel]:
        best, best_score = None, 0.0
        for p in panels:
            if p.pid in taken or p.quarantined:
                continue
            if self.rows_per_robot and p.row // self.rows_per_robot != rb.rid:
                continue
            if p.contamination >= self.th:
                continue
            excess = (p.x - self.x_opt) / self.x_opt
            if p.x < self.x_opt * self.trigger:
                continue
            score = excess * self.vol / (60 + self.travel_s(rb, p))
            if score > best_score:
                best, best_score = p, score
        return best

    def simulate_day(self, panels: List[Panel], n_robots: int, dt_s: float = 60.0) -> dict:
        robots = [Robot(i, row=i * self.rows_per_robot if self.rows_per_robot else 0) for i in range(n_robots)]
        taken: set = set()
        assignment: Dict[int, int] = {}
        quarantined = []
        dev_sum, n_obs, t = 0.0, 0, 0.0
        while t < 24 * 3600:
            for p in panels:
                light = 6 * 3600 < t < 18 * 3600
                p.x += p.growth_g_l_h * (2.0 if light else 0.15) * dt_s / 3600
                if p.contamination >= self.th and not p.quarantined:
                    p.quarantined = True
                    quarantined.append(p.pid)
                dev_sum += abs(p.x - self.x_opt) / self.x_opt
                n_obs += 1
            for rb in robots:
                if rb.busy_until_s > t:
                    continue
                if rb.rid in assignment:          # 작업 완료 처리
                    p = panels[assignment.pop(rb.rid)]
                    p.x = self.x_opt
                    p.harvests += 1
                    taken.discard(p.pid)
                if t > self.hs.robot_hours_per_day * 3600:
                    continue                       # 충전·정비 시간
                p = self.pick(rb, panels, taken)
                if p is None:
                    continue
                harvest_l = self.vol * (1 - self.x_opt / p.x)
                dur = self.travel_s(rb, p) + cycle_time_s(self.hs, self.layout, harvest_l) - \
                    self.layout.panel_len_m / self.hs.travel_m_s
                rb.busy_until_s = t + dur
                rb.work_s += dur
                rb.row, rb.pos = p.row, p.pos
                taken.add(p.pid)
                assignment[rb.rid] = panels.index(p)
            t += dt_s
        return {
            "harvests": sum(p.harvests for p in panels),
            "mean_density_dev": dev_sum / max(1, n_obs),
            "robot_utilization": sum(r.work_s for r in robots) / (n_robots * 24 * 3600),
            "quarantined": quarantined,
            "max_x": max(p.x for p in panels if not p.quarantined),
        }


def build_bay(design: HarvestDesign, month_idx: int, contaminated: tuple = (), seed: int = 7) -> tuple:
    """로봇 1대 담당 구역의 패널 상태 생성 (검증용). 반환: (panels, scheduler)."""
    import random
    rnd = random.Random(seed)
    hs, lay, sc = design.system, design.layout, design.scenario
    m = sc.months[month_idx]
    vol = sc.reactor.volume_l_per_m2 * lay.panel_footprint_m2
    daily_g_l = m.total / sc.reactor.volume_l_per_m2
    growth_h = daily_g_l / (12 * 2.0 + 12 * 0.15)
    per_row = max(1, int(lay.row_len_m // lay.panel_len_m))
    rows = hs.rows_per_robot_max if hs.rows_per_robot_max else min(design.rows, 12)
    if design.robots:
        rows = min(rows, max(1, math.ceil(design.rows / design.robots)))
    x_opt = sc.reactor.x_opt
    panels = []
    for r in range(rows):
        for q in range(per_row):
            pid = len(panels)
            panels.append(Panel(pid, r, q, x_opt * rnd.uniform(0.97, 1.06),
                                growth_h * rnd.uniform(0.85, 1.15),
                                contamination=0.9 if pid in contaminated else 0.0))
    sched = HarvestScheduler(hs, lay, x_opt, vol, rows_per_robot=0)
    return panels, sched
