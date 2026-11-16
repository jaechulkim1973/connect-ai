"""미세조류 플랜트 디지털 트윈.

설계 단계의 모델(model · robot)과 운전 단계의 제어기(controller)를 하나의 가상 플랜트로 묶습니다.

  · 모듈(배양기 묶음)마다 물리·생물 상태를 시간 단위로 적분 (sim.react)
  · 모듈마다 실제와 같은 급이 제어기(FeedController)가 붙어 명령을 냄
  · 수확은 수거 로봇의 처리능력(L/h)을 넘지 못함 → 밀리면 backlog
  · 날씨(일별 흐림·맑음 변동)와 에어돔 온도를 반영
  · sync(): 현장 센서값으로 상태를 보정하고 혼합영양 성장속도(mu_het)를 온라인 학습
  · inject(): 오염·펌프 고장·히터 고장 같은 사고 주입
  · what_if(): 현재 상태를 복제해 설정을 바꾼 미래를 미리 돌려 보고 KPI 비교
  · to_dict()/dashboard_html(): 상태·이력·KPI 내보내기
"""
from __future__ import annotations

import copy
import json
import math
import random
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

from .controller import ControllerConfig, FeedController, Sensors, daily_glucose_budget
from .model import PAR_FRACTION, MOL_PER_MJ_PAR, Scenario
from .robot import HarvestDesign
from .sim import ReactorState, apply_actions, par_at, react, t_env_at

# 운전 소모 환산 (계획용 가정치)
CO2_KG_PER_ON_H_PER_M3 = 0.9    # CO2 밸브 ON 1시간당, 배양액 1 m³ 당 주입량
HEATER_KW_PER_M2 = 0.10         # 배양기 1 m² 당 가온 용량
COOLER_KW_PER_M2 = 0.04


@dataclass
class TwinModule:
    mid: int
    bay: int
    reactor_m2: float
    state: ReactorState
    ctl: FeedController
    mu_het_scale: float = 1.0          # 온라인 학습된 혼합영양 속도 보정
    harvest_backlog_l: float = 0.0
    glucose_pump_ok: bool = True
    heater_ok: bool = True
    alarms: List[str] = field(default_factory=list)
    produced_g: float = 0.0
    photo_g: float = 0.0
    mixo_g: float = 0.0
    harvested_l: float = 0.0
    harvested_g: float = 0.0
    co2_on_h: float = 0.0
    heater_on_h: float = 0.0
    cooler_on_h: float = 0.0
    glucose_sensor: bool = True        # 온라인 당 분석기 (오염 감지에 사용)
    _sync_pred_mixo: float = 0.0       # 마지막 동기화 이후 모델이 예측한 급이 성장 (g/L)
    _sync_pred_photo: float = 0.0
    _sync_x: Optional[float] = None
    _sync_harv_l: float = 0.0


class PlantTwin:
    def __init__(self, scenario: Scenario, design: Optional[HarvestDesign] = None,
                 n_modules: int = 12, module_m2: float = 100.0, month_idx: int = 0,
                 seed: int = 1, cloud_sigma: float = 0.25):
        self.sc, self.design = scenario, design
        self.month_idx = month_idx
        self.module_m2 = module_m2
        self.rnd = random.Random(seed)
        self.cloud_sigma = cloud_sigma
        self.hour = 0.0          # 누적 시간 (h)
        self.history: List[dict] = []
        self.events: List[str] = []
        r, m = scenario.reactor, scenario.months[month_idx]
        self.x_opt = r.x_opt
        cfg = ControllerConfig(
            x_opt=r.x_opt,
            daily_glucose_budget_g=daily_glucose_budget(m.glucose, module_m2),
            heat_on_c=scenario.op.heat_setpoint_c if scenario.op.heat_setpoint_c is not None else 5.0,
            cool_on_c=scenario.op.cool_setpoint_c)
        self.cfg = cfg
        # 수거 로봇: 로봇 1대가 맡는 모듈 수와 처리능력
        if design and design.robots:
            m2_per_robot = design.reactor_m2 / design.robots
            self.modules_per_robot = max(1, round(m2_per_robot / module_m2))
            self.robot_l_h = design.system.pump_l_min * 60 / 2 * 0.9   # 수확+보충 → 절반
        else:
            self.modules_per_robot = n_modules
            self.robot_l_h = 60 * 60 / 2 * 0.5                          # 수작업 기준
        self.modules: List[TwinModule] = []
        for i in range(n_modules):
            st = ReactorState(v=r.volume_l_per_m2 * module_m2, x=r.x_opt * self.rnd.uniform(0.95, 1.05),
                              ph=cfg.ph_setpoint, t=cfg.heat_on_c, n=cfg.n_target_mg_l, p=cfg.p_target_mg_l)
            self.modules.append(TwinModule(i, i // self.modules_per_robot, module_m2, st,
                                           FeedController(scenario.strain, copy.deepcopy(cfg))))
        self._new_day_weather()

    # ------------------------------------------------------------ 날씨
    def _new_day_weather(self):
        cl, enc, i = self.sc.climate, self.sc.enclosure, self.month_idx
        cloud = min(1.5, max(0.3, self.rnd.gauss(1.0, self.cloud_sigma)))
        g = cl.global_mj[i] * cloud
        self.today_par = g * PAR_FRACTION * MOL_PER_MJ_PAR * enc.transmission
        self.today_t_env = cl.temp_c[i] + enc.solar_gain_k * g      # 돔 내부 일평균 (무가온)
        self.today_cloud = cloud

    # ------------------------------------------------------------ 시뮬레이션
    def step(self, dt: float = 1 / 30):
        hod = self.hour % 24
        par = par_at(hod, self.today_par)
        t_env = t_env_at(hod, self.today_t_env)
        # 로봇별 이번 tick 처리능력 (L)
        cap = {b: self.robot_l_h * dt for b in {m.bay for m in self.modules}}
        for mod in self.modules:
            s = mod.state
            act = mod.ctl.step(Sensors(hour=hod, ph=s.ph, do_pct=s.do, temp_c=s.t, par_umol=par,
                                       biomass_g_l=s.x, volume_l=s.v,
                                       glucose_g_l=s.g if mod.glucose_sensor else None), dt)
            if not mod.glucose_pump_ok:
                act.glucose_ml = 0.0
            if not mod.heater_ok:
                act.heater_on = False
            for al in act.alarms:
                if al not in mod.alarms:
                    mod.alarms.append(al)
                    self.events.append(f"{self.hour:6.1f}h 모듈{mod.mid}: {al}")
            # 수확 요청 → 로봇 처리능력 한도 안에서 수행, 나머지는 backlog
            mod.harvest_backlog_l = max(mod.harvest_backlog_l, act.harvest_l)
            done = min(mod.harvest_backlog_l, cap[mod.bay])
            cap[mod.bay] -= done
            mod.harvest_backlog_l -= done
            x_before = s.x
            apply_actions(s, act, mod.ctl.cfg, harvest_l=done)
            # 제어기는 요청 즉시 수확됐다고 가정하므로, 로봇이 실제로 처리한 결과로 되돌려 알림
            mod.ctl.last_biomass_g = s.x * s.v
            if done > 0:
                mod.harvested_l += done
                mod.harvested_g += done * x_before
                mod.ctl.last_glucose_meas = None
            dp, dm = react(s, par, t_env, act, self.sc.strain, self.sc.reactor, self.cfg, dt, mod.mu_het_scale)
            mod.photo_g += dp * s.v
            mod.mixo_g += dm * s.v
            mod.produced_g += (dp + dm) * s.v
            mod._sync_pred_mixo += dm
            mod._sync_pred_photo += dp
            if act.co2_on:
                mod.co2_on_h += dt
            if act.heater_on:
                mod.heater_on_h += dt
            if act.cooler_on:
                mod.cooler_on_h += dt
        self.hour += dt
        if int(self.hour / 24) != int((self.hour - dt) / 24):
            self._new_day_weather()
        if abs(self.hour - round(self.hour)) < dt / 2:
            self._record(par)

    def run(self, hours: float, dt: float = 1 / 30):
        for _ in range(int(round(hours / dt))):
            self.step(dt)
        return self

    def _record(self, par: float):
        ms = self.modules
        n = len(ms)
        self.history.append({
            "h": round(self.hour, 2),
            "par": round(par, 1),
            "x": round(sum(m.state.x for m in ms) / n, 3),
            "ph": round(sum(m.state.ph for m in ms) / n, 3),
            "do": round(sum(m.state.do for m in ms) / n, 1),
            "t": round(sum(m.state.t for m in ms) / n, 2),
            "glu": round(sum(m.state.g for m in ms) / n, 3),
            "produced_kg": round(sum(m.produced_g for m in ms) / 1000, 3),
            "backlog_l": round(sum(m.harvest_backlog_l for m in ms), 1),
            "x_mod": [round(m.state.x, 3) for m in ms],
        })

    # ------------------------------------------------------------ 현장 동기화
    def sync(self, mid: int, gain: float = 0.8, learn_rate: float = 0.5,
             harvested_l: Optional[float] = None, **meas) -> dict:
        """현장 측정값으로 트윈 상태를 보정 (가중 평균). biomass_g_l 을 주면 mu_het 도 학습.

        meas 키: biomass_g_l, ph, do_pct, temp_c, glucose_g_l, no3_n_mg_l, po4_p_mg_l
        harvested_l: 지난 동기화 이후 현장 수확량(로봇 유량계). 없으면 트윈의 수확량으로 대신함.
        """
        mod = self.modules[mid]
        s = mod.state
        keymap = {"biomass_g_l": "x", "ph": "ph", "do_pct": "do", "temp_c": "t",
                  "glucose_g_l": "g", "no3_n_mg_l": "n", "po4_p_mg_l": "p"}
        before = {k: getattr(s, a) for k, a in keymap.items() if k in meas}
        if "biomass_g_l" in meas and mod._sync_x is not None and mod._sync_pred_mixo > 0.02:
            # 현장 실측 성장 = 농도 변화 + 수확으로 빠져나간 몫 → 광합성 예측분을 빼면 급이 성장 실측치
            h_l = harvested_l if harvested_l is not None else mod.harvested_l - mod._sync_harv_l
            x_avg = (meas["biomass_g_l"] + mod._sync_x) / 2
            obs_growth = meas["biomass_g_l"] - mod._sync_x + h_l / s.v * x_avg * 1.07
            ratio = (obs_growth - mod._sync_pred_photo) / mod._sync_pred_mixo
            ratio = max(0.2, min(2.5, ratio))
            # mu_het_scale 가 반영된 예측 대비 비율이므로 곱셈 보정
            mod.mu_het_scale = max(0.3, min(2.0, mod.mu_het_scale * ratio ** learn_rate))
        for k, a in keymap.items():
            if k in meas:
                setattr(s, a, (1 - gain) * getattr(s, a) + gain * meas[k])
        if "biomass_g_l" in meas:
            mod._sync_pred_mixo = mod._sync_pred_photo = 0.0
            mod._sync_x = s.x
            mod._sync_harv_l = mod.harvested_l
            mod.ctl.last_biomass_g = s.x * s.v
        return {"module": mid, "before": before, "after": {k: getattr(s, keymap[k]) for k in before},
                "mu_het_scale": mod.mu_het_scale}

    # ------------------------------------------------------------ 사고 주입
    def inject(self, mid: int, event: str):
        mod = self.modules[mid]
        if event == "contamination":
            mod.state.bugs = max(mod.state.bugs, 0.03)
        elif event == "glucose_pump_fail":
            mod.glucose_pump_ok = False
        elif event == "heater_fail":
            mod.heater_ok = False
        elif event == "repair":
            mod.glucose_pump_ok = mod.heater_ok = True
        else:
            raise ValueError(event)
        self.events.append(f"{self.hour:6.1f}h 모듈{mid}: 사고 주입 '{event}'")

    # ------------------------------------------------------------ What-if
    def clone(self) -> "PlantTwin":
        return copy.deepcopy(self)

    def apply_settings(self, **kw):
        """제어 설정 변경: ControllerConfig 필드명 그대로 (예: heat_on_c=22, ph_setpoint=7.3),
        glucose_budget_scale=1.5 (일일 포도당 예산 배율)."""
        scale = kw.pop("glucose_budget_scale", None)
        for mod in self.modules:
            cfg = replace(mod.ctl.cfg, **kw)
            if scale is not None:
                cfg = replace(cfg, daily_glucose_budget_g=cfg.daily_glucose_budget_g * scale)
            mod.ctl.cfg = cfg
        self.cfg = replace(self.cfg, **kw)

    def what_if(self, hours: float, scenarios: Dict[str, dict]) -> Dict[str, dict]:
        """현재 상태에서 시나리오별 미래를 미리 돌려 KPI 비교. '현재 설정'은 항상 포함."""
        out = {}
        for name, settings in {"현재 설정": {}, **scenarios}.items():
            tw = self.clone()
            base = tw.kpis()
            tw.apply_settings(**dict(settings))
            tw.run(hours)
            out[name] = tw.kpis(since=base)
        return out

    # ------------------------------------------------------------ KPI
    def _totals(self) -> dict:
        ms = self.modules
        vol_m3 = sum(m.state.v for m in ms) / 1000
        return {
            "hours": self.hour,
            "produced_kg": sum(m.produced_g for m in ms) / 1000,
            "photo_kg": sum(m.photo_g for m in ms) / 1000,
            "mixo_kg": sum(m.mixo_g for m in ms) / 1000,
            "harvested_kg": sum(m.harvested_g for m in ms) / 1000,
            "harvested_m3": sum(m.harvested_l for m in ms) / 1000,
            "glucose_kg": sum(m.ctl.totals["glucose_g"] for m in ms) / 1000,
            "n_kg": sum(m.ctl.totals["n_g"] for m in ms) / 1000,
            "p_kg": sum(m.ctl.totals["p_g"] for m in ms) / 1000,
            "co2_kg": sum(m.co2_on_h for m in ms) * CO2_KG_PER_ON_H_PER_M3 * vol_m3 / len(ms),
            "heat_kwh": sum(m.heater_on_h * HEATER_KW_PER_M2 * m.reactor_m2 for m in ms),
            "cool_kwh": sum(m.cooler_on_h * COOLER_KW_PER_M2 * m.reactor_m2 for m in ms),
        }

    def _fixed_cost_per_m2_day(self) -> float:
        """감가상각·인건비·소모품 등 운전량과 무관한 비용 (USD/m²/day)."""
        if self.design:
            cb = self.design.scenario.cost_breakdown()
            var = sum(cb["opex"][k] for k in ("glucose", "co2", "nutrients", "heat", "power"))
            fixed_annual = self.design.annual_cost / self.design.reactor_m2 - var
        else:
            cb = self.sc.cost_breakdown()
            fixed_annual = cb["annual_cost"] - sum(cb["opex"][k] for k in ("glucose", "co2", "nutrients", "heat", "power"))
        return fixed_annual / 365

    def kpis(self, since: Optional[dict] = None) -> dict:
        tot = self._totals()
        if since:
            tot = {k: tot[k] - since.get(k, 0.0) for k in tot}
        ec = self.sc.econ
        days = max(tot["hours"], 1e-9) / 24
        area = sum(m.reactor_m2 for m in self.modules)
        var_cost = (tot["glucose_kg"] * ec.glucose_cost_kg + tot["co2_kg"] * ec.co2_cost_kg
                    + tot["n_kg"] * ec.n_cost_kg + tot["p_kg"] * ec.p_cost_kg
                    + tot["produced_kg"] * ec.micro_cost_per_kg_biomass
                    + tot["heat_kwh"] * ec.heat_cost_kwh
                    + (tot["cool_kwh"] + tot["produced_kg"] * ec.harvest_dry_kwh_per_kg) * ec.power_cost_kwh)
        fixed = self._fixed_cost_per_m2_day() * area * days
        kg = tot["produced_kg"]
        out = dict(tot)
        out.update({
            "days": days,
            "kg_per_day": kg / days,
            "g_per_m2_day": kg * 1000 / area / days,
            "variable_cost": var_cost,
            "fixed_cost": fixed,
            "cost_per_kg": (var_cost + fixed) / kg if kg > 0 else float("inf"),
            "mean_x": sum(m.state.x for m in self.modules) / len(self.modules),
            "backlog_l": sum(m.harvest_backlog_l for m in self.modules),
            "alarms": sum(len(m.alarms) for m in self.modules),
        })
        return out

    # ------------------------------------------------------------ 내보내기
    def to_dict(self, what_if: Optional[Dict[str, dict]] = None) -> dict:
        sc = self.sc
        return {
            "plant": {
                "strain": sc.strain.name, "climate": sc.climate.name, "month": self.month_idx + 1,
                "reactor": sc.reactor.name, "enclosure": sc.enclosure.name,
                "harvest_system": self.design.system.name if self.design else "-",
                "modules": len(self.modules), "module_m2": self.module_m2,
                "modules_per_robot": self.modules_per_robot, "robot_l_h": self.robot_l_h,
                "x_opt": self.x_opt,
            },
            "kpis": self.kpis(),
            "modules": [{
                "id": m.mid, "bay": m.bay, "x": m.state.x, "ph": m.state.ph, "do": m.state.do,
                "t": m.state.t, "glucose": m.state.g, "n": m.state.n, "p": m.state.p,
                "mu_het_scale": m.mu_het_scale, "backlog_l": m.harvest_backlog_l,
                "produced_kg": m.produced_g / 1000, "glucose_locked": m.ctl.glucose_locked,
                "pump_ok": m.glucose_pump_ok, "heater_ok": m.heater_ok, "alarms": m.alarms,
            } for m in self.modules],
            "history": self.history,
            "events": self.events,
            "what_if": what_if or {},
        }

    def dashboard_html(self, what_if: Optional[Dict[str, dict]] = None) -> str:
        data = json.dumps(self.to_dict(what_if), ensure_ascii=False, default=lambda o: None)
        data = data.replace("Infinity", "null").replace("</", "<\\/")
        return _DASHBOARD.replace("__DATA__", data)


_DASHBOARD = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>미세조류 디지털 트윈</title>
<style>
:root{--bg:#f6f7f5;--panel:#fff;--ink:#1d2420;--muted:#5d6a62;--line:#dfe4e0;--accent:#2f8f5b;--accent2:#3b6fb6;--warn:#c2410c;--ok:#2f8f5b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#111513;--panel:#1a201c;--ink:#e6ece8;--muted:#9aa8a0;--line:#2c3530;--accent:#5cc28a;--accent2:#7aa6e6;--warn:#f08a4b;--ok:#5cc28a}}
:root[data-theme="dark"]{--bg:#111513;--panel:#1a201c;--ink:#e6ece8;--muted:#9aa8a0;--line:#2c3530;--accent:#5cc28a;--accent2:#7aa6e6;--warn:#f08a4b;--ok:#5cc28a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:0 0 10px}.sub{color:var(--muted);margin:0 0 18px}
.grid{display:grid;gap:12px}.kpis{grid-template-columns:repeat(auto-fit,minmax(130px,1fr));margin-bottom:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.kpi .v{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}.kpi .l{color:var(--muted);font-size:12px}
.charts{grid-template-columns:repeat(auto-fit,minmax(340px,1fr));margin-bottom:12px}
svg{width:100%;height:auto;display:block}.axis{stroke:var(--line)}.lbl{fill:var(--muted);font-size:10px}
.mods{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:8px}
.mod{border:1px solid var(--line);border-radius:8px;padding:8px;font-size:12px;font-variant-numeric:tabular-nums}
.mod b{display:block;font-size:13px}.bad{border-color:var(--warn);box-shadow:inset 0 0 0 1px var(--warn)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--line)}th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600;font-size:12px}.best{color:var(--accent);font-weight:650}
ul.ev{margin:0;padding-left:18px;max-height:220px;overflow:auto;font-size:12px}.warn{color:var(--warn)}
.wrap{overflow-x:auto}
</style></head><body><main>
<h1>미세조류 플랜트 디지털 트윈</h1><p class="sub" id="sub"></p>
<section class="grid kpis" id="kpis"></section>
<section class="grid charts" id="charts"></section>
<section class="card" style="margin-bottom:12px"><h2>모듈 상태 (색 = 최적농도 대비 세포농도)</h2><div class="mods" id="mods"></div></section>
<section class="grid charts">
 <div class="card"><h2>What-if 비교 (현재 상태에서 미래 예측)</h2><div class="wrap" id="wi"></div></div>
 <div class="card"><h2>이벤트 · 경보</h2><ul class="ev" id="ev"></ul></div>
</section>
</main>
<script>
const D=__DATA__;const $=id=>document.getElementById(id);const f=(v,d=1)=>v==null||!isFinite(v)?'–':Number(v).toLocaleString('ko-KR',{maximumFractionDigits:d,minimumFractionDigits:d});
const P=D.plant,K=D.kpis,H=D.history;
$('sub').textContent=`${P.strain} · ${P.climate} · ${P.month}월 · ${P.reactor} · ${P.enclosure} · 수거: ${P.harvest_system} · 모듈 ${P.modules}개 × ${P.module_m2} m² · 경과 ${f(K.hours,0)} h`;
[['생산량',f(K.produced_kg,1)+' kg'],['일 생산',f(K.kg_per_day,1)+' kg/d'],['면적 생산성',f(K.g_per_m2_day,1)+' g/m²/d'],['급이 기여',f(K.produced_kg?K.mixo_kg/K.produced_kg*100:0,0)+' %'],['포도당 투입',f(K.glucose_kg,1)+' kg'],['실시간 원가',(K.cost_per_kg==null?'–':'$'+f(K.cost_per_kg,2))+'/kg'],['수확 대기',f(K.backlog_l,0)+' L'],['경보',String(K.alarms)]]
.forEach(([l,v])=>{const c=document.createElement('div');c.className='card kpi';c.innerHTML=`<div class="l">${l}</div><div class="v">${v}</div>`;$('kpis').appendChild(c)});
function chart(title,key,unit,color,lo,hi){const W=520,Hh=180,pl=40,pr=10,pt=10,pb=24;const xs=H.map(r=>r.h),ys=H.map(r=>r[key]);if(!xs.length)return;
const x0=Math.min(...xs),x1=Math.max(...xs);let y0=lo!=null?lo:Math.min(...ys),y1=hi!=null?hi:Math.max(...ys);if(y1-y0<1e-9){y1+=1;y0-=1}
const X=v=>pl+(v-x0)/(x1-x0||1)*(W-pl-pr),Y=v=>pt+(1-(v-y0)/(y1-y0))*(Hh-pt-pb);
let s=`<svg viewBox="0 0 ${W} ${Hh}" role="img" aria-label="${title}">`;
for(let i=0;i<=4;i++){const v=y0+(y1-y0)*i/4;s+=`<line class="axis" x1="${pl}" x2="${W-pr}" y1="${Y(v)}" y2="${Y(v)}"/><text class="lbl" x="${pl-4}" y="${Y(v)+3}" text-anchor="end">${f(v,y1-y0<5?2:0)}</text>`}
for(let d=0;d<=x1;d+=24){if(d<x0)continue;s+=`<text class="lbl" x="${X(d)}" y="${Hh-6}" text-anchor="middle">${d}h</text>`}
s+=`<polyline fill="none" stroke="${color}" stroke-width="2" points="${H.map(r=>X(r.h)+','+Y(r[key])).join(' ')}"/></svg>`;
const c=document.createElement('div');c.className='card';c.innerHTML=`<h2>${title}${unit?' <span class="lbl">('+unit+')</span>':''}</h2>`+s;$('charts').appendChild(c)}
chart('평균 세포농도','x','g/L','var(--accent)');chart('누적 생산량','produced_kg','kg','var(--accent2)');
chart('평균 pH','ph','','var(--accent)');chart('평균 용존산소','do','%sat','var(--accent2)');
chart('평균 배양온도','t','°C','var(--warn)');chart('수확 대기량(로봇 backlog)','backlog_l','L','var(--warn)',0);
D.modules.forEach(m=>{const r=m.x/P.x_opt;const a=Math.max(.15,Math.min(1,(r-.6)/.8));const bad=m.glucose_locked||!m.pump_ok||!m.heater_ok||m.alarms.length;
const e=document.createElement('div');e.className='mod'+(bad?' bad':'');e.style.background=`color-mix(in srgb, var(--accent) ${Math.round(a*45)}%, transparent)`;
e.innerHTML=`<b>M${m.id} · 베이${m.bay}</b>${f(m.x,2)} g/L<br>pH ${f(m.ph,2)} · DO ${f(m.do,0)}%<br>μ보정 ${f(m.mu_het_scale,2)}${bad?'<br><span class="warn">'+(m.glucose_locked?'당 급이 차단':!m.pump_ok?'펌프 고장':!m.heater_ok?'히터 고장':'경보 이력')+'</span>':''}`;$('mods').appendChild(e)});
const W=D.what_if,names=Object.keys(W);if(names.length){const best=names.reduce((a,b)=>(W[a].cost_per_kg<=W[b].cost_per_kg?a:b));
let t='<table><tr><th>시나리오</th><th>생산 kg</th><th>포도당 kg</th><th>가온 kWh</th><th>원가 $/kg</th></tr>';
names.forEach(n=>{const w=W[n];t+=`<tr class="${n===best?'best':''}"><td>${n}</td><td>${f(w.produced_kg,1)}</td><td>${f(w.glucose_kg,1)}</td><td>${f(w.heat_kwh,0)}</td><td>${f(w.cost_per_kg,2)}</td></tr>`});
$('wi').innerHTML=t+'</table>'}else $('wi').textContent='what-if 결과 없음';
(D.events.length?D.events:['이벤트 없음']).forEach(e=>{const li=document.createElement('li');li.textContent=e;if(/오염|고장|이상|과포화/.test(e))li.className='warn';$('ev').appendChild(li)});
</script></body></html>
"""
