#!/usr/bin/env python3
"""디지털 트윈 실행: 최적 설계로 가상 플랜트를 만들고, 사고를 주입하고, what-if 를 비교해 대시보드를 만듭니다.

사용 예:
    python3 twin_run.py                                  # 1월, 모듈 12개, 72시간
    python3 twin_run.py --month 7 --hours 48 --modules 24
    python3 twin_run.py --event 30:3:contamination --event 40:5:heater_fail --event 50:5:repair
결과: out/twin_dashboard.html (브라우저로 열기), out/twin_state.json
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace

from algae_feed import robot as rb
from algae_feed.config import CLIMATES, ENCLOSURES, REACTORS, STRAINS, Economics, Operation
from algae_feed.model import simulate
from algae_feed.optimizer import best, enumerate_scenarios
from algae_feed.twin import PlantTwin


def pick_design(strain, climate, econ, tpy):
    ranked = best(enumerate_scenarios(strain, climate, econ))
    best_cost = ranked[0].cost_breakdown()["cost_per_kg"]
    near = [s for s in ranked if s.cost_breakdown()["cost_per_kg"] <= best_cost * 1.05]
    top = min(near, key=lambda s: s.cost_breakdown()["capex_per_annual_kg"])
    return rb.optimize(top, tpy)[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--climate", default="korea_central", choices=CLIMATES)
    ap.add_argument("--strain", default="chlorella", choices=STRAINS)
    ap.add_argument("--tpy", type=float, default=100.0)
    ap.add_argument("--month", type=int, default=1)
    ap.add_argument("--modules", type=int, default=12)
    ap.add_argument("--module-m2", type=float, default=100.0)
    ap.add_argument("--hours", type=float, default=72.0)
    ap.add_argument("--whatif-hours", type=float, default=48.0)
    ap.add_argument("--event", action="append", default=None,
                    help="시각h:모듈:종류 (contamination|glucose_pump_fail|heater_fail|repair)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="out")
    a = ap.parse_args()

    strain, climate = STRAINS[a.strain], CLIMATES[a.climate]
    design = pick_design(strain, climate, Economics(), a.tpy)
    twin = PlantTwin(design.scenario, design, n_modules=a.modules, module_m2=a.module_m2,
                     month_idx=a.month - 1, seed=a.seed)

    events = a.event if a.event is not None else ["30:3:contamination", "40:5:heater_fail", "52:5:repair"]
    plan = sorted((float(h), int(m), kind) for h, m, kind in (e.split(":") for e in events))
    for h, mid, kind in plan:
        if h > twin.hour:
            twin.run(h - twin.hour)
        twin.inject(mid, kind)
    if a.hours > twin.hour:
        twin.run(a.hours - twin.hour)

    what_if = twin.what_if(a.whatif_hours, {
        "가온 18°C": {"heat_on_c": 18.0},
        "가온 25°C": {"heat_on_c": 25.0},
        "포도당 예산 ×1.5": {"glucose_budget_scale": 1.5},
        "포도당 예산 ×0.5": {"glucose_budget_scale": 0.5},
        "수확 기준 2.0 g/L": {"x_opt": 2.0},
    })

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "twin_dashboard.html"), "w", encoding="utf-8") as f:
        f.write(twin.dashboard_html(what_if))
    with open(os.path.join(a.out, "twin_state.json"), "w", encoding="utf-8") as f:
        json.dump(twin.to_dict(what_if), f, ensure_ascii=False, indent=1, default=lambda o: None)

    k = twin.kpis()
    print(f"[트윈] {design.scenario.strain.name} · {a.month}월 · 모듈 {a.modules}개 × {a.module_m2:.0f} m² · {k['hours']:.0f}시간")
    print(f"  생산 {k['produced_kg']:.1f} kg ({k['g_per_m2_day']:.1f} g/m²/d, 급이 기여 {k['mixo_kg'] / max(k['produced_kg'], 1e-9):.0%})"
          f" · 포도당 {k['glucose_kg']:.1f} kg · 가온 {k['heat_kwh']:,.0f} kWh · 원가 ${k['cost_per_kg']:.2f}/kg")
    print("  이벤트:")
    for e in twin.events:
        print("   ", e)
    print(f"  What-if ({a.whatif_hours:.0f}시간 앞):")
    for name, w in what_if.items():
        print(f"    {name:16s} 생산 {w['produced_kg']:7.1f} kg · 포도당 {w['glucose_kg']:6.1f} kg · "
              f"가온 {w['heat_kwh']:7,.0f} kWh · ${w['cost_per_kg']:.2f}/kg")
    print(f"[저장] {os.path.join(a.out, 'twin_dashboard.html')}, {os.path.join(a.out, 'twin_state.json')}")


if __name__ == "__main__":
    main()
