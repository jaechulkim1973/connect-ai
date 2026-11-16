"""시나리오 전수 탐색 — 연중 생산 조건에서 kg 당 원가 최소 조합 선택."""
from __future__ import annotations

from dataclasses import replace
from itertools import product
from typing import Iterable, List, Optional

from .config import ENCLOSURES, REACTORS, Climate, Economics, Operation, Strain
from .model import Scenario, simulate

HEAT_OPTIONS: List[Optional[float]] = [None, 12.0, 18.0, 22.0]
FEED_OPTIONS = ["none", "gap_fill", "max"]


def enumerate_scenarios(strain: Strain, climate: Climate, econ: Economics,
                        reactors: Iterable[str] | None = None,
                        enclosures: Iterable[str] | None = None) -> List[Scenario]:
    out = []
    for rk, ek, heat, feed in product(reactors or REACTORS, enclosures or ENCLOSURES,
                                      HEAT_OPTIONS, FEED_OPTIONS):
        if ek == "none" and heat is not None:
            continue
        op = replace(Operation(), heat_setpoint_c=heat, feed_mode=feed)
        out.append(simulate(REACTORS[rk], ENCLOSURES[ek], strain, climate, op, econ))
    return out


def best(scenarios: List[Scenario], min_month_ratio: float = 0.5) -> List[Scenario]:
    """연중 생산 조건(최저월 ≥ 최고월 × ratio) 을 만족하는 시나리오를 kg 원가 순 정렬."""
    ok = [s for s in scenarios if s.min_month_ratio >= min_month_ratio and s.annual_kg_per_m2 > 0]
    return sorted(ok, key=lambda s: s.cost_breakdown()["cost_per_kg"])


def label(s: Scenario) -> str:
    h = "무가온" if s.op.heat_setpoint_c is None else f"{s.op.heat_setpoint_c:.0f}°C"
    return f"{s.reactor.key} + {s.enclosure.key} · 가온 {h} · 급이 {s.op.feed_mode}"
