#!/usr/bin/env python3
"""미세조류 연중생산 최적화 + 급이 계획 생성기.

사용 예:
    python3 run.py                         # 한국 중부, Chlorella, 연 100 t
    python3 run.py --tpy 300 --climate korea_jeju --strain nannochloropsis
    python3 run.py --glucose-cost 0.30 --heat-cost 0.015   # 폐열·저가 당 확보 시
결과: 콘솔 리포트 + out/feeding_plan.csv + out/report.md
"""
from __future__ import annotations

import argparse
import csv
import os
from dataclasses import replace

from algae_feed.config import CLIMATES, STRAINS, Economics
from algae_feed.controller import ControllerConfig, daily_glucose_budget
from algae_feed.model import sizing
from algae_feed.optimizer import best, enumerate_scenarios, label
from algae_feed.sim import simulate_day
from algae_feed import robot as rb

UREA_N = 0.466
KH2PO4_P = 0.228
MON = ["1월", "2월", "3월", "4월", "5월", "6월", "7월", "8월", "9월", "10월", "11월", "12월"]


def fmt_usd(v: float) -> str:
    return f"${v:,.0f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tpy", type=float, default=100.0, help="연간 목표 생산량 (건조 t/yr)")
    ap.add_argument("--climate", default="korea_central", choices=CLIMATES)
    ap.add_argument("--strain", default="chlorella", choices=STRAINS)
    ap.add_argument("--min-ratio", type=float, default=0.5, help="연중생산 조건: 최저월/최고월 생산 비율")
    ap.add_argument("--glucose-cost", type=float)
    ap.add_argument("--heat-cost", type=float)
    ap.add_argument("--co2-cost", type=float)
    ap.add_argument("--price", type=float, help="바이오매스 판매가 USD/kg")
    ap.add_argument("--out", default="out")
    a = ap.parse_args()

    econ = Economics()
    for arg, fld in [("glucose_cost", "glucose_cost_kg"), ("heat_cost", "heat_cost_kwh"),
                     ("co2_cost", "co2_cost_kg"), ("price", "biomass_value_kg")]:
        if getattr(a, arg) is not None:
            econ = replace(econ, **{fld: getattr(a, arg)})
    strain, climate = STRAINS[a.strain], CLIMATES[a.climate]

    scen = enumerate_scenarios(strain, climate, econ)
    ranked = best(scen, a.min_ratio)
    if not ranked:
        raise SystemExit("연중생산 조건을 만족하는 조합이 없습니다. --min-ratio 를 낮춰 보세요.")

    lines: list[str] = []
    p = lines.append
    p(f"# 미세조류 연중생산 최적화 리포트\n")
    p(f"- 균주: **{strain.name}** · 입지: **{climate.name}** · 목표: **{a.tpy:,.0f} t/yr (건조)**")
    p(f"- 연중생산 조건: 최저월 생산 ≥ 최고월 × {a.min_ratio:.0%}")
    p(f"- 탐색 조합 {len(scen)}개 중 조건 충족 {len(ranked)}개\n")

    p("## 1. 상위 조합 (kg 당 총원가 순)\n")
    p("| 순위 | 조합 | 연생산 kg/m² | 급이 기여 | 최저/최고월 | CAPEX/연간kg | 원가 $/kg |")
    p("|---|---|---|---|---|---|---|")
    for i, s in enumerate(ranked[:10], 1):
        cb = s.cost_breakdown()
        p(f"| {i} | {label(s)} | {s.annual_kg_per_m2:.1f} | {s.mixo_share:.0%} | {s.min_month_ratio:.2f} "
          f"| ${cb['capex_per_annual_kg']:.1f} | ${cb['cost_per_kg']:.2f} |")

    # 비교 기준: 기존 방식(유리튜브 + 유리온실, 급이 없음)
    base = [s for s in scen if s.reactor.key == "tubular_glass" and s.enclosure.key == "glass_greenhouse"
            and s.op.feed_mode == "none" and s.op.heat_setpoint_c == 18.0][0]
    # 추천안: 최저원가 대비 5% 이내 조합 중 '연간 kg 당 투자비'가 가장 낮은 것
    #  (배양기·에어돔 구축비가 가장 큰 부담이므로 원가가 비슷하면 CAPEX 최소를 택함)
    best_cost = ranked[0].cost_breakdown()["cost_per_kg"]
    near = [s for s in ranked if s.cost_breakdown()["cost_per_kg"] <= best_cost * 1.05]
    top = min(near, key=lambda s: s.cost_breakdown()["capex_per_annual_kg"])
    p(f"\n**추천안 (원가 최저 대비 +5% 이내 중 투자비 최소):** {label(top)}\n")
    sz_top, sz_base = sizing(top, a.tpy), sizing(base, a.tpy)
    p("## 2. 투자비 비교 — 기존 방식 vs 추천안\n")
    p("| 항목 | 기존: 유리튜브+유리온실, 광독립 | 추천안 |")
    p("|---|---|---|")
    p(f"| 조합 | {label(base)} | {label(top)} |")
    p(f"| 배양기 설치면적 | {sz_base['reactor_m2']:,.0f} m² | {sz_top['reactor_m2']:,.0f} m² |")
    p(f"| 돔/온실 바닥면적 | {sz_base['dome_m2']:,.0f} m² | {sz_top['dome_m2']:,.0f} m² |")
    p(f"| 배양액 총량 | {sz_base['culture_volume_m3']:,.0f} m³ | {sz_top['culture_volume_m3']:,.0f} m³ |")
    for k, nm in [("reactor", "배양기 CAPEX"), ("enclosure", "에어돔/온실 CAPEX"),
                  ("bop", "수확·건조·배관 CAPEX"), ("seed", "시드 트레인 CAPEX")]:
        p(f"| {nm} | {fmt_usd(sz_base['capex'][k])} | {fmt_usd(sz_top['capex'][k])} |")
    p(f"| **총 CAPEX** | **{fmt_usd(sz_base['capex_total'])}** | **{fmt_usd(sz_top['capex_total'])}** |")
    p(f"| 생산원가 | ${sz_base['cost_per_kg']:.2f}/kg | ${sz_top['cost_per_kg']:.2f}/kg |")
    red = 1 - sz_top["capex"]["reactor"] / sz_base["capex"]["reactor"]
    red_d = 1 - sz_top["capex"]["enclosure"] / sz_base["capex"]["enclosure"] if sz_base["capex"]["enclosure"] else 0
    p(f"\n→ 배양기 투자비 **{red:.0%} 절감**, 외피(에어돔) 투자비 **{red_d:.0%} 절감**, "
      f"총 CAPEX **{1 - sz_top['capex_total'] / sz_base['capex_total']:.0%} 절감**.\n")

    # 원가 구성
    cb = top.cost_breakdown()
    kg = top.annual_kg_per_m2
    p("## 3. 추천안 원가 구성 ($/kg)\n")
    p("| 항목 | $/kg |")
    p("|---|---|")
    p(f"| 감가상각(CAPEX 연등가) | {cb['annualized_capex'] / kg:.2f} |")
    for k, v in cb["opex"].items():
        p(f"| {k} | {v / kg:.2f} |")
    p(f"| **합계** | **{cb['cost_per_kg']:.2f}** |\n")

    # 월별 급이 계획 (플랜트 전체, 1일 기준)
    area = sz_top["reactor_m2"]
    os.makedirs(a.out, exist_ok=True)
    csv_path = os.path.join(a.out, "feeding_plan.csv")
    p("## 4. 월별 급이 계획 (플랜트 전체 · 1일 기준)\n")
    p("| 월 | 배양온도 | 광합성 kg/d | 급이생산 kg/d | 포도당 kg/d | CO2 kg/d | 요소 kg/d | KH2PO4 kg/d | 수확 m³/d | 가온 MWh/d |")
    p("|---|---|---|---|---|---|---|---|---|---|")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["month", "culture_temp_c", "photo_kg_d", "mixo_kg_d", "total_kg_d", "glucose_kg_d",
                    "co2_kg_d", "urea_kg_d", "kh2po4_kg_d", "harvest_m3_d", "heat_mwh_d"])
        for m in top.months:
            row = [m.month, round(m.t_culture, 1), m.p_auto * area / 1000, m.p_mixo * area / 1000,
                   m.total * area / 1000, m.glucose * area / 1000, m.co2 * area / 1000,
                   m.n_feed * area / 1000 / UREA_N, m.p_feed * area / 1000 / KH2PO4_P,
                   m.harvest_l * area / 1000, m.heat_kwh * area / 1000]
            w.writerow([row[0], row[1]] + [round(v, 2) for v in row[2:]])
            p(f"| {MON[m.month - 1]} | {row[1]}°C | {row[2]:.0f} | {row[3]:.0f} | {row[5]:.0f} | {row[6]:.0f} "
              f"| {row[7]:.1f} | {row[8]:.1f} | {row[9]:.0f} | {row[10]:.1f} |")

    # 실시간 제어기 검증 (1월·7월, 배양기 100 m² 모듈)
    p("\n## 5. 실시간 급이 제어기 시뮬레이션 (배양기 100 m² 모듈, 1분 제어주기)\n")
    p("| 월 | 일일 포도당 예산 | 광합성 생산 | 급이 생산 | 투입 포도당 | CO2 밸브 ON | 수확 | DO 평균/최대 | pH 범위 | 경보 |")
    p("|---|---|---|---|---|---|---|---|---|---|")
    mod_m2 = 100.0
    for mi in (0, 6):
        m = top.months[mi]
        budget = daily_glucose_budget(m.glucose, mod_m2)
        cfg = ControllerConfig(x_opt=top.reactor.x_opt, daily_glucose_budget_g=budget,
                               heat_on_c=top.op.heat_setpoint_c or 5.0, cool_on_c=top.op.cool_setpoint_c)
        d = simulate_day(strain, top.reactor, mod_m2, m.par_in, climate.temp_c[mi] + 5, cfg)
        p(f"| {MON[mi]} | {budget / 1000:.1f} kg | {d.photo_g / 1000:.2f} kg | {d.mixo_g / 1000:.2f} kg "
          f"| {d.glucose_fed_g / 1000:.1f} kg | {d.co2_on_min:.0f} 분 | {d.harvest_l:,.0f} L "
          f"| {d.mean_do:.0f}/{d.max_do:.0f}% | {d.min_ph:.2f}~{d.max_ph:.2f} | {len(d.alarms)} |")

    # AI 자동수거 로봇 최적 설계
    p("\n## 6. AI 자동수거 로봇 최적 설계\n")
    p("수거 방식 × 패널 길이(2·4·6·8 m) × 열 길이(50·100 m)를 전수 평가하고, 방식마다 가장 싼 배치를 골랐습니다. "
      "인건비는 수확·점검·필름교체 작업시간으로 직접 계산합니다(위 1~5장은 인건비를 m²당 일괄값으로 단순화).\n")
    p("| 순위 | 수거 방식 | 패널/열 길이 | 배양기 m² | 돔 m² | 로봇 | 하루 수확 | 가동률 | 총 CAPEX | 원가 $/kg |")
    p("|---|---|---|---|---|---|---|---|---|---|")
    designs = rb.optimize(top, a.tpy)
    for i, d in enumerate(designs, 1):
        p(f"| {i} | {d.system.name} | {d.layout.panel_len_m:.0f} m / {d.layout.row_len_m:.0f} m | {d.reactor_m2:,.0f} "
          f"| {d.dome_m2:,.0f} | {d.robots}대 | {d.harvests_per_day}회 | {d.utilization:.0%} "
          f"| {fmt_usd(d.capex_total)} | **${d.cost_per_kg:.2f}** |")
    win = designs[0]
    manual = [d for d in designs if d.system.key == "manual"][0]
    p(f"\n**선정: {win.system.name}** — {win.system.note}")
    p(f"- 패널 {win.panels:,}개 ({win.layout.panel_len_m:.0f} m), {win.rows}열, 로봇 {win.robots}대, 레일 {win.rail_m:,.0f} m, "
      f"패널당 사이클 {win.cycle_s:.0f}초, 하루 {win.harvests_per_day}회 부분수확")
    p(f"- 수작업 대비: 원가 ${manual.cost_per_kg:.2f} → **${win.cost_per_kg:.2f}/kg** "
      f"({1 - win.cost_per_kg / manual.cost_per_kg:.0%} 절감), 돔 면적 {manual.dome_m2:,.0f} → {win.dome_m2:,.0f} m² "
      f"({1 - win.dome_m2 / manual.dome_m2:.0%} 감소)")
    p("\n| CAPEX 항목 | 금액 |")
    p("|---|---|")
    for k, v in win.capex.items():
        p(f"| {k} | {fmt_usd(v)} |")
    p(f"| **합계** | **{fmt_usd(win.capex_total)}** |")
    if win.robots:
        p("\n**AI 스케줄러 1일 검증 (로봇 1대 담당 구역, 패널 1개를 일부러 오염 상태로 둠)**\n")
        p("| 월 | 패널 수 | 수확 횟수 | 최적농도 평균 이탈 | 로봇 가동률 | 격리 패널 |")
        p("|---|---|---|---|---|---|")
        for mi in (0, 6):
            panels, sch = rb.build_bay(win, mi, contaminated=(5,))
            res = sch.simulate_day(panels, 1)
            p(f"| {MON[mi]} | {len(panels)} | {res['harvests']} | {res['mean_density_dev']:.1%} "
              f"| {res['robot_utilization']:.0%} | {res['quarantined']} |")

    report = "\n".join(lines)
    with open(os.path.join(a.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(report)
    print(f"\n[저장] {csv_path}, {os.path.join(a.out, 'report.md')}")


if __name__ == "__main__":
    main()
