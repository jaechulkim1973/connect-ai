"""설계 파라미터 · 기술 카탈로그.

모든 비용 값은 **계획용 가정치(USD, 2025~2026 기준 문헌·업계 범위의 중간값)** 입니다.
실제 견적(배양기/에어돔 업체 견적서)을 받으면 이 파일만 고치면 전체 최적화가 다시 계산됩니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List


# ---------------------------------------------------------------------------
# 균주 (기본: Chlorella vulgaris — 광독립·혼합영양(mixotrophic) 모두 가능)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Strain:
    name: str = "Chlorella vulgaris"
    t_min: float = 5.0          # CTMI 최저 성장온도 (°C)
    t_opt: float = 28.0         # 최적 온도
    t_max: float = 40.0         # 최고 온도
    yield_photon: float = 1.2   # g 건조중량 / mol 광자(PAR), 옥외 실효 최대치
    ik_daily: float = 25.0      # 광포화 일적산 기준 (mol/m²/day)
    mixotrophic: bool = True    # 유기탄소 급이 가능 여부
    mu_het_max: float = 0.6     # 유기탄소 급이 시 추가 비성장속도 (1/day, at t_opt)
    yield_glucose: float = 0.50 # g 바이오매스 / g 포도당
    # 바이오매스 원소 조성 (질량분율, 건조중량 기준)
    n_frac: float = 0.080
    p_frac: float = 0.012
    mg_frac: float = 0.005
    s_frac: float = 0.006
    fe_frac: float = 0.0010
    c_frac: float = 0.50


STRAINS: Dict[str, Strain] = {
    "chlorella": Strain(),
    "nannochloropsis": Strain(
        name="Nannochloropsis oceanica", t_min=8, t_opt=25, t_max=35,
        yield_photon=1.0, mixotrophic=True, mu_het_max=0.25, yield_glucose=0.40,
        n_frac=0.065, p_frac=0.010),
    "haematococcus": Strain(
        name="Haematococcus pluvialis", t_min=5, t_opt=23, t_max=30,
        yield_photon=0.8, mixotrophic=True, mu_het_max=0.20, yield_glucose=0.35,
        n_frac=0.060, p_frac=0.010),
    "spirulina": Strain(
        name="Arthrospira platensis (Spirulina)", t_min=15, t_opt=35, t_max=42,
        yield_photon=1.0, mixotrophic=True, mu_het_max=0.25, yield_glucose=0.40,
        n_frac=0.105, p_frac=0.010),
}


# ---------------------------------------------------------------------------
# 기후 (기본: 한국 중부 내륙 — 대전 부근 평년값, 월별)
# global_mj: 수평면 일사량 MJ/m²/day, temp_c: 월평균 기온
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Climate:
    name: str
    global_mj: List[float]
    temp_c: List[float]
    days: List[int] = field(default_factory=lambda: [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31])


CLIMATES: Dict[str, Climate] = {
    "korea_central": Climate(
        "Korea central (Daejeon-like)",
        [7.6, 10.7, 13.9, 17.3, 19.0, 17.6, 15.3, 15.6, 13.9, 11.7, 8.2, 6.7],
        [-1.0, 1.3, 6.6, 12.9, 18.3, 22.6, 25.6, 26.0, 21.2, 14.6, 7.4, 1.0]),
    "korea_jeju": Climate(
        "Korea Jeju",
        [7.0, 9.6, 13.0, 16.6, 18.5, 17.0, 17.9, 17.5, 14.0, 11.6, 8.0, 6.3],
        [6.1, 7.0, 9.8, 14.3, 18.4, 22.0, 26.4, 27.3, 23.6, 18.6, 13.4, 8.4]),
    "almeria_spain": Climate(
        "Almeria, Spain (benchmark)",
        [10.0, 13.5, 18.0, 22.5, 26.0, 28.0, 28.0, 25.0, 20.0, 15.0, 11.0, 9.0],
        [12.5, 13.0, 14.8, 16.6, 19.5, 23.0, 26.0, 26.5, 24.0, 20.0, 16.0, 13.5]),
}


# ---------------------------------------------------------------------------
# 배양기(PBR) 기술 카탈로그 — 1 m² '배양기 설치 바닥면적' 기준
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Reactor:
    key: str
    name: str
    capex_per_m2: float         # 배양기 본체+배관+순환펌프 (USD / m² 바닥)
    life_years: float
    consumable_per_m2_yr: float # 필름 백 교체 등 소모성 (USD/m²/yr)
    volume_l_per_m2: float      # 바닥 1 m² 당 배양액 (L)
    light_eff: float            # 광 이용 효율 보정 (광희석·혼합 효과, 튜브=1.0)
    x_opt: float                # 운전 최적 세포농도 (g/L)
    closed_sterility: float     # 0=개방(유기탄소 급이 불가) ~ 1=완전 폐쇄
    otr_g_per_l_day: float      # 혼합영양 시 공기·탈기로 감당 가능한 추가 산소 처리능 (바이오매스 g/L/day 환산)
    energy_kwh_per_m3_day: float  # 순환·탈기 동력
    note: str = ""


REACTORS: Dict[str, Reactor] = {
    "tubular_glass": Reactor(
        "tubular_glass", "유리 튜브형 PBR (독일·스페인 상업 플랜트형)",
        capex_per_m2=320, life_years=15, consumable_per_m2_yr=0,
        volume_l_per_m2=40, light_eff=1.00, x_opt=2.5, closed_sterility=1.0,
        otr_g_per_l_day=0.35, energy_kwh_per_m3_day=2.5,
        note="최고 위생·고가 제품용, 가장 비쌈. 탈기 구간 필요."),
    "flat_panel_rigid": Reactor(
        "flat_panel_rigid", "경질 평판형 PBR (PC/PMMA 패널)",
        capex_per_m2=210, life_years=10, consumable_per_m2_yr=0,
        volume_l_per_m2=50, light_eff=1.08, x_opt=3.0, closed_sterility=0.95,
        otr_g_per_l_day=0.40, energy_kwh_per_m3_day=1.6,
        note="수직 패널 광희석 → 광효율 높음, 에어리프트 혼합."),
    "film_panel": Reactor(
        "film_panel", "필름(LDPE/PE) 평판·행잉백 PBR — 에어돔 내부 전용",
        capex_per_m2=55, life_years=10, consumable_per_m2_yr=9,
        volume_l_per_m2=45, light_eff=1.03, x_opt=2.5, closed_sterility=0.90,
        otr_g_per_l_day=0.30, energy_kwh_per_m3_day=1.4,
        note="구조 강도를 에어돔이 대신 → 프레임+일회용 필름으로 최저가. 필름 매년 교체."),
    "thin_layer_cascade": Reactor(
        "thin_layer_cascade", "박막 캐스케이드 (Thin-layer, 체코·스페인형)",
        capex_per_m2=75, life_years=15, consumable_per_m2_yr=0,
        volume_l_per_m2=12, light_eff=0.95, x_opt=12.0, closed_sterility=0.35,
        otr_g_per_l_day=0.10, energy_kwh_per_m3_day=4.0,
        note="고농도·저부피, 반개방이라 유기탄소 급이 제한."),
    "raceway": Reactor(
        "raceway", "레이스웨이 개방지 (Raceway pond)",
        capex_per_m2=18, life_years=20, consumable_per_m2_yr=0,
        volume_l_per_m2=250, light_eff=0.50, x_opt=0.6, closed_sterility=0.0,
        otr_g_per_l_day=0.0, energy_kwh_per_m3_day=0.15,
        note="최저가지만 개방형 → 유기탄소 급이 불가, 겨울 생산 붕괴."),
}


# ---------------------------------------------------------------------------
# 외피(Enclosure) — 에어돔 / 온실 / 노지
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Enclosure:
    key: str
    name: str
    capex_per_m2: float     # USD / m² 바닥
    life_years: float
    transmission: float     # PAR 투과율
    u_value: float          # 열관류율 W/m²K (외피 표면 기준)
    surface_ratio: float    # 외피 표면적 / 바닥면적
    solar_gain_k: float     # 패시브 온도상승 계수 (°C per MJ/m²/day 일사)
    blower_kw_per_1000m2: float
    max_cooling_c: float    # 차광·환기·증발냉각으로 낮출 수 있는 상한
    note: str = ""


ENCLOSURES: Dict[str, Enclosure] = {
    "none": Enclosure("none", "노지(외피 없음)", 0, 99, 1.00, 25.0, 1.0, 0.0, 0, 0,
                      note="겨울 결빙 → 연중 생산 불가"),
    "air_dome_pe2": Enclosure(
        "air_dome_pe2", "에어돔 이중 PE 공기막 (팽창형)",
        capex_per_m2=48, life_years=12, transmission=0.78, u_value=4.0,
        surface_ratio=1.55, solar_gain_k=0.75, blower_kw_per_1000m2=0.8, max_cooling_c=8,
        note="필름은 4~5년 교체(비용에 포함). 무기둥 대경간 → 배양기 배치 자유."),
    "air_dome_etfe": Enclosure(
        "air_dome_etfe", "에어돔 ETFE 쿠션 (고투과)",
        capex_per_m2=140, life_years=25, transmission=0.90, u_value=2.9,
        surface_ratio=1.55, solar_gain_k=0.85, blower_kw_per_1000m2=0.6, max_cooling_c=8,
        note="투과율·수명 최고, 초기비용 큼."),
    "glass_greenhouse": Enclosure(
        "glass_greenhouse", "유리 온실 (벤로형)",
        capex_per_m2=150, life_years=25, transmission=0.72, u_value=6.0,
        surface_ratio=1.35, solar_gain_k=0.70, blower_kw_per_1000m2=0.0, max_cooling_c=10,
        note="골조 그림자로 투과 저하, 단열 약함."),
}


# ---------------------------------------------------------------------------
# 운전 · 경제 파라미터
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Economics:
    discount_rate: float = 0.07
    bop_capex_per_m2: float = 45.0    # 배지조제·수확(원심/막)·건조·계측·배관 등 (배양기 바닥 m² 당)
    seed_capex_per_m2: float = 12.0   # 접종용 소형 PBR/발효기 시드 트레인
    packing_ratio: float = 0.75       # 배양기 바닥면적 / 돔 바닥면적 (통로·설비 공간)
    heat_cost_kwh: float = 0.045      # 열 단가 USD/kWh (폐열/히트펌프 시 하향)
    power_cost_kwh: float = 0.11
    co2_cost_kg: float = 0.12         # 정제 CO2 (배가스 직접 사용 시 0.02~0.05)
    co2_efficiency: float = 0.70      # pH-stat 주입 CO2 이용률
    glucose_cost_kg: float = 0.45     # 공업용 포도당(또는 당밀/전분당화액 환산)
    n_cost_kg: float = 1.10           # N 1 kg 당 (요소 기준)
    p_cost_kg: float = 6.50           # P 1 kg 당 (KH2PO4/인산 기준)
    micro_cost_per_kg_biomass: float = 0.04
    labor_per_m2_yr: float = 4.0
    biomass_value_kg: float = 12.0    # 판매 단가 (식품·사료용 건조분말 가정)
    harvest_dry_kwh_per_kg: float = 1.2


@dataclass(frozen=True)
class Operation:
    heat_setpoint_c: float | None = 18.0    # 최저 배양액 온도 (None = 무가온)
    cool_setpoint_c: float = 32.0
    feed_mode: str = "gap_fill"             # none | gap_fill | max
    gap_target: str = "peak"                # gap_fill 목표: peak(연중 최대월 수준) 또는 숫자(g/m²/day)
    residual_glucose_max_g_l: float = 0.5   # 잔류 당 상한 (오염 방지)


def with_overrides(obj, **kw):
    return replace(obj, **kw)
