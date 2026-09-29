"""Build the Deye (Solarman) controller blueprint and its companion package.

This file is the only source. The blueprint and package in the repository are
generated from it; edit here, then run `python tools/deye_controller/build.py`.
`--check` fails when the committed files differ from the generator output.
"""

import argparse
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BLUEPRINT_PATH = (
    REPO / "blueprints/automation/energy_compass/deye_solarman_controller.yaml"
)
PACKAGE_PATH = REPO / "packages/energy_compass_deye.yaml"
DEFAULT_PROGRAM_PREFIX = "inverter_deye_program_"


class Input:
    """A blueprint `!input` reference; rendered by the YAML dumper."""

    def __init__(self, name):
        self.name = name


# Blueprint inputs, grouped into collapsible sections in the automation editor.
# Every key here must be documented in docs/guide.en.md and docs/guide.pl.md
# (enforced by tests/test_deye_controller_docs.py).
def entity(name, description, domain, integration=None, multiple=False, default=None):
    selector = {"domain": domain}
    if integration:
        selector["integration"] = integration
    out = {
        "name": name,
        "description": description,
        "selector": {
            "entity": {"filter": selector} | ({"multiple": True} if multiple else {})
        },
    }
    if default is not None:
        out["default"] = default
    return out


def number(name, description, default, minimum, maximum, step, unit=None):
    selector = {"min": minimum, "max": maximum, "step": step, "mode": "box"}
    if unit:
        selector["unit_of_measurement"] = unit
    return {
        "name": name,
        "description": description,
        "default": default,
        "selector": {"number": selector},
    }


INPUTS = {
    "energy_compass": {
        "name": "Energy Compass",
        "icon": "mdi:compass",
        "input": {
            "plan_entity": entity(
                "Plan",
                "The Plan sensor of the Energy Compass installation to execute.",
                "sensor",
                "energy_compass",
            ),
            "optimizer_entity": entity(
                "Optimizer status",
                "The Optimizer status sensor of the same installation.",
                "sensor",
                "energy_compass",
            ),
            "valid_entity": entity(
                "Forecast valid",
                "The Forecast valid binary sensor of the same installation.",
                "binary_sensor",
                "energy_compass",
            ),
            "alert_entity": entity(
                "Alert",
                "The Alert binary sensor of the same installation.",
                "binary_sensor",
                "energy_compass",
            ),
            "compass_entity": entity(
                "Consumption compass",
                "The Consumption compass sensor; its changes re-run the controller.",
                "sensor",
                "energy_compass",
            ),
        },
    },
    "inverter": {
        "name": "Deye inverter (Solarman)",
        "icon": "mdi:solar-power",
        "input": {
            "solarman_device": {
                "name": "Solarman device",
                "description": "Inverter device used to read back holding registers 108-177 after each write.",
                "selector": {"device": {"filter": {"integration": "solarman"}}},
            },
            "charge_entity": entity(
                "Battery max charging current", "Register 108.", "number", "solarman"
            ),
            "discharge_entity": entity(
                "Battery max discharging current", "Register 109.", "number", "solarman"
            ),
            "grid_entity": entity(
                "Battery grid charging current", "Register 128.", "number", "solarman"
            ),
            "operation_entity": entity(
                "Battery operation mode",
                "Select with Capacity / Voltage.",
                "select",
                "solarman",
            ),
            "soc_entity": entity(
                "Battery SOC", "Battery state of charge in %.", "sensor", "solarman"
            ),
            "voltage_entity": entity(
                "Battery voltage",
                "Battery pack voltage in V; 400-610 V is accepted.",
                "sensor",
                "solarman",
            ),
            "telemetry_entities": entity(
                "Telemetry heartbeat",
                "Sensors that must be numeric and reported within 30 s, for example SOC, voltage and the update interval.",
                "sensor",
                multiple=True,
            ),
        },
    },
    "tuning": {
        "name": "Limits and takeover",
        "icon": "mdi:tune",
        "collapsed": True,
        "input": {
            "commissioned_battery_modes": {
                "name": "Commissioned battery modes",
                "description": "Battery operation modes whose physical SOC/voltage thresholds were verified. Other modes block control.",
                "default": ["Capacity"],
                "selector": {
                    "select": {"multiple": True, "options": ["Capacity", "Voltage"]}
                },
            },
            "discharge_energy_entity": {
                "name": "Battery discharge energy counter",
                "description": "Optional cumulative battery discharge energy in kWh (for example Total Battery Discharge). Solarman reports it only on change; add a heartbeat of the same inverter to Telemetry heartbeat. In Voltage mode a DISCHARGE_GRID row then ends after its planned discharge energy, not at a target voltage that the sag under load reaches within seconds.",
                "default": "",
                "selector": {"entity": {"filter": {"domain": "sensor"}}},
            },
            "hold_grid_current": number(
                "HOLD grid current",
                "Grid charging current written in HOLD.",
                1,
                0,
                50,
                1,
                "A",
            ),
            "balance_grid_current": number(
                "Balance grid current",
                "Minimum grid charging current in an LFP balance row.",
                2,
                0,
                50,
                1,
                "A",
            ),
            "capacity_kwh": number(
                "Battery capacity",
                "Usable capacity the Energy Compass plan uses; converts planned end SOC kWh into %.",
                25,
                1,
                200,
                0.1,
                "kWh",
            ),
            "max_power_w": number(
                "Maximum battery power",
                "DC charge/discharge power cap and the TOU program power.",
                8000,
                500,
                30000,
                10,
                "W",
            ),
            "max_current": number(
                "Maximum battery current",
                "Cap on the charge and discharge current written in every forced state.",
                18,
                1,
                350,
                1,
                "A",
            ),
            "max_grid_current": number(
                "Maximum grid charging current",
                "Cap on the grid charging current in CHARGE_GRID.",
                16,
                0,
                350,
                1,
                "A",
            ),
            "relinquish_current": number(
                "Relinquish current",
                "Charge and discharge current restored on release (BASE).",
                18,
                0,
                200,
                1,
                "A",
            ),
            "eta": number(
                "One-way battery efficiency",
                "Used to convert planned kWh into DC current.",
                0.9746794344808963,
                0.5,
                1,
                "any",
            ),
            "old_writers": entity(
                "Previous battery automations",
                "Automations that also write these registers. Control is blocked until each is off and not running.",
                "automation",
                multiple=True,
                default=[],
            ),
        },
    },
}
PLAN, OPTIMIZER, VALID, ALERT = (
    Input(k)
    for k in ["plan_entity", "optimizer_entity", "valid_entity", "alert_entity"]
)
CHARGE, DISCHARGE, GRID = (
    Input(k) for k in ["charge_entity", "discharge_entity", "grid_entity"]
)
MODE = "input_select.energy_compass_deye_mode"
CACHE = "sensor.energy_compass_deye_plan"
RUNTIME = "sensor.energy_compass_deye_runtime"
PENDING = "input_boolean.energy_compass_deye_restore_pending"
SESSION = "input_boolean.energy_compass_deye_session"
START = "input_datetime.energy_compass_deye_session_start"
TOU_SETTINGS = "sensor.energy_compass_deye_tou_settings"
PROGRAM_FIELDS = [
    ("power", 153, 10),
    ("voltage", 159, 0.01),
    ("soc", 165, 1),
    ("charging", 171, 1),
]
# Entity -> Solarman holding register. The TOU program entities come from the
# prefix the package publishes, so one package edit re-targets all 24 of them.
REGISTERS = (
    "{% set ns = namespace(r=dict([(charge_entity, {'address':108,'scale':1}), (discharge_entity, {'address':109,'scale':1}), (grid_entity, {'address':128,'scale':1})])) %}"
    "{% for i in range(1,7) %}"
    + "".join(
        f"{{% set ns.r = dict(ns.r, **dict([('{'select' if field == 'charging' else 'number'}.' ~ program_prefix ~ i ~ '_{field}', {{'address':{base}+i,'scale':{scale}}})])) %}}"
        for field, base, scale in PROGRAM_FIELDS
    )
    + "{% endfor %}{{ ns.r }}"
)

CANDIDATE = r"""
{% set p = states[plan_entity].attributes if states[plan_entity] is not none else {} %}
{% set cache = state_attr(cache_entity, 'snapshot') or {} %}
{% set rt = state_attr(runtime_entity, 'runtime') or {} %}
{% set t = as_timestamp(now()) %}
{% set g = p.get('generated_at') %}
{% set raw_rows = p.get('intervals', []) %}
{% set rows = raw_rows if raw_rows is sequence and raw_rows is not string else [] %}
{% set ns = namespace(ok=rows is sequence and rows is not string and rows|length > 0, end=none, covered=false) %}
{% for r in rows if r is mapping %}
  {% set start = as_timestamp(r.get('start'), 0) %}
  {% set end = as_timestamp(r.get('end'), 0) %}
  {% if start <= 0 or end <= start or (ns.end is not none and start != ns.end) %}{% set ns.ok = false %}{% endif %}
  {% set ns.end = end %}
  {% if start <= t < end %}{% set ns.covered = true %}{% endif %}
  {% if r.get('state') not in ['CHARGE_GRID','CHARGE_PV','SELF_CONSUME','DISCHARGE_GRID','HOLD','CURTAIL'] %}{% set ns.ok = false %}{% endif %}
  {% if r.get('balance_hold', false) is not boolean %}{% set ns.ok = false %}{% endif %}
  {% for key in ['pv_kwh','load_kwh','charge_kwh','discharge_kwh','curtail_kwh','end_soc_kwh','grid_import_kwh','grid_export_kwh','buy_per_kwh','sell_per_kwh'] %}
    {% if not is_number(r.get(key)) %}{% set ns.ok = false %}
    {% elif key not in ['buy_per_kwh','sell_per_kwh'] and r[key]|float < -0.000001 %}{% set ns.ok = false %}{% endif %}
  {% endfor %}
{% endfor %}
{% if rows|select('mapping')|list|length != rows|length %}{% set ns.ok = false %}{% endif %}
{% set session = state_attr(session_start_entity, 'timestamp')|float(0) %}
{# Optimizer restarts on every input change and each solve takes minutes, so
   'ready' can last milliseconds. A completed, published generation retained
   during the next solve is equally valid; plans computed before the last
   revocation are not. #}
{% set ok = ns.ok and ns.covered and p.get('dispatch_policy') is mapping
  and is_state(session_entity, 'on') and as_timestamp(g,0) >= session
  and as_timestamp(g,0) <= t and as_timestamp(g,0) > as_timestamp(cache.get('generated_at'),0)
  and g != rt.get('revoked_generation') and as_timestamp(g,0) > as_timestamp(rt.get('revoked_at'),0)
  and is_state(valid_entity,'on') and is_state(alert_entity,'off')
  and ((states(optimizer_entity) == 'ready' and p.get('refreshing') == false and p.get('plan_retained') == false)
    or (states(optimizer_entity) == 'calculating' and p.get('refreshing') == true and p.get('plan_retained') == true))
  and g == state_attr(optimizer_entity,'generated_at') == state_attr(valid_entity,'generated_at')
  and as_timestamp(p.get('valid_until'),0) > t %}
{{ dict(schema=1, session=session, accepted_at=now().isoformat(), generated_at=g,
  valid_until=p.get('valid_until'), coverage_end=ns.end, intervals=rows, dispatch_policy=p.get('dispatch_policy')) if ok else {} }}
"""

DECISION = r"""
{% set t = as_timestamp(now()) %}
{% set cache = state_attr(cache_entity,'snapshot') or {} %}
{% set rt = state_attr(runtime_entity,'runtime') or {} %}
{% set p = states[plan_entity].attributes if states[plan_entity] is not none else {} %}
{% set ns = namespace(telemetry=true, writers=true, times=[], row={}, active=0, desired={}, reason='ok', valid=true, reached=false) %}
{% for e in telemetry_entities %}
  {% if not is_number(states(e)) or not (0 <= t - as_timestamp(states[e].last_reported,0) <= 30) %}{% set ns.telemetry=false %}{% endif %}
{% endfor %}
{% set v = states(voltage_entity)|float(0) %}
{% set soc = states(soc_entity)|float(-1) %}
{% if not (400 <= v <= 610 and 0 <= soc <= 100) %}{% set ns.telemetry=false %}{% endif %}
{% for e in old_writers %}
  {% if not is_state(e,'off') or state_attr(e,'current') != 0 %}{% set ns.writers=false %}{% endif %}
{% endfor %}
{% for i in range(1,7) %}
  {% set s = states('time.' ~ program_prefix ~ i ~ '_time') %}
  {% set parts = s.split(':') %}
  {% if parts|length == 3 and is_number(parts[0]) and is_number(parts[1]) and is_number(parts[2]) and 0 <= parts[0]|int < 24 and 0 <= parts[1]|int < 60 and 0 <= parts[2]|int < 60 %}
    {% set ns.times=ns.times + [parts[0]|int * 3600 + parts[1]|int * 60 + parts[2]|int] %}
  {% endif %}
{% endfor %}
{% set times_ok = ns.times|length == 6 and ns.times|unique|list|length == 6 and ns.times == ns.times|sort %}
{% set clock = now().hour * 3600 + now().minute * 60 + now().second %}
{% if times_ok %}
  {% set ns.active=6 %}
  {% for s in ns.times %}{% if s <= clock %}{% set ns.active=loop.index %}{% endif %}{% endfor %}
{% endif %}
{% for r in cache.get('intervals',[]) %}
  {% if as_timestamp(r.start,0) <= t < (as_timestamp(r.end,0)|round(0,'floor')) %}{% set ns.row=r %}{% endif %}
{% endfor %}
{% set retained = states(optimizer_entity)=='calculating' %}
{% set coherent = p.get('generated_at') == cache.get('generated_at') == state_attr(optimizer_entity,'generated_at') == state_attr(valid_entity,'generated_at') %}
{% set live_ok = is_state(alert_entity,'off') and (retained or (states(optimizer_entity)=='ready' and is_state(valid_entity,'on') and coherent and p.get('plan_retained') == false and p.get('refreshing') == false)) %}
{% if not ns.telemetry %}{% set ns.reason='telemetria: wymagany świeży SOC, napięcie i heartbeat (30 s)' %}
{% elif not times_ok %}{% set ns.reason='TOU: wymagane sześć różnych rosnących godzin' %}
{% elif not is_state(session_entity,'on') or cache.get('session') != state_attr(session_start_entity,'timestamp')|float(0) %}{% set ns.reason='sesja: oczekiwanie na nowy poprawny plan po uruchomieniu' %}
{% elif cache.get('generated_at') == rt.get('revoked_generation') %}{% set ns.reason='plan: generacja unieważniona po błędzie, wymagany nowy plan' %}
{% elif not live_ok %}{% set ns.reason='plan: błąd źródeł, brak gotowości lub niespójne generacje' %}
{% elif t >= (as_timestamp(cache.get('valid_until'),0)|round(0,'floor')) or t >= (cache.get('coverage_end',0)|float(0)|round(0,'floor')) or not ns.row %}{% set ns.reason='plan: oryginalny termin ważności minął lub brak przedziału' %}
{% elif states(operation_entity) not in ['Capacity','Voltage'] %}{% set ns.reason='bateria: wymagany tryb Capacity albo Voltage' %}{% endif %}
{% set ns.valid = ns.reason == 'ok' %}
{# HA publishes plan/status/validity separately. Retry a newer publication before
   restoring BASE; never extend the accepted snapshot's original lifetime. #}
{% set handoff_pending = ns.reason == 'plan: błąd źródeł, brak gotowości lub niespójne generacje'
  and not coherent and states(optimizer_entity) == 'ready' and is_state(alert_entity,'off')
  and states(mode_entity) == 'Auto' and ns.row
  and t < (as_timestamp(cache.get('valid_until'),0)|round(0,'floor'))
  and t < (cache.get('coverage_end',0)|float(0)|round(0,'floor'))
  and (as_timestamp(p.get('generated_at'),0) > as_timestamp(cache.get('generated_at'),0)
    or as_timestamp(state_attr(optimizer_entity,'generated_at'),0) > as_timestamp(cache.get('generated_at'),0)) %}
{% set state = ns.row.get('state','BASE') if ns.valid else 'BASE' %}
{% if states(mode_entity) == 'Off' %}{% set state='BASE' %}{% endif %}
{% set balance = ns.row.get('balance_hold', false) is sameas true and state in ['CHARGE_PV','CHARGE_GRID'] %}
{% set charge_cap = ([max_current, max_power_w / v]|min if v > 0 and ns.telemetry else 0) %}
{% set discharge_cap = ([max_current, max_power_w * eta / v]|min if v > 0 and ns.telemetry else 0) %}
{% set reached_key=[cache.get('generated_at'),ns.row.get('start'),state,states(operation_entity)] %}
{# Tryb Voltage: napiecie pod obciazeniem spada o ~0,7 V przy 7 kW, a krzywa LFP
   jest plaska (51,5-52,8 V to ~30-70 %), wiec cel napieciowy DISCHARGE_GRID
   zatrzaskiwal sie po kilkunastu sekundach (29.09). Ze swiezym licznikiem
   energii rozladowania wiersz konczy sie po oddaniu zaplanowanej energii,
   liczonej od pierwszego przebiegu wiersza (runtime slot_energy). Licznik
   Solarman zglasza sie tylko przy zmianie (w spoczynku nawet godzinami), wiec
   jego swiezosc gwarantuje heartbeat telemetrii tego samego falownika. #}
{% set energy_mode = states(operation_entity) == 'Voltage' and discharge_energy_entity is string and discharge_energy_entity != ''
  and is_number(states(discharge_energy_entity)) %}
{% set counted = states(discharge_energy_entity)|float(0) if energy_mode else 0 %}
{% set slot = rt.get('slot_energy') %}
{% set slot_start = slot.get('discharge')|float if energy_mode and slot is mapping and slot.get('key') == reached_key and is_number(slot.get('discharge')) else counted %}
{% set discharged = [0, counted - slot_start]|max %}
{% set target_soc = 10.0 %}
{% set target_voltage = 49.6 %}
{% set charge = relinquish_current if state == 'BASE' else (charge_cap if state in ['CHARGE_PV','SELF_CONSUME'] else 0) %}
{% set discharge = relinquish_current if state == 'BASE' else (discharge_cap if state in ['CHARGE_PV','SELF_CONSUME'] else 0) %}
{% set grid = 0 %}
{% set direction = 'Disabled' %}
{% set power = max_power_w %}
{# SELF_CONSUME nie ustawia progu SOC ani nie zatrzaskuje celu: falownik sam
   pokrywa dom z baterii do rezerwy 10%. Prog SOC rowny biezacemu SOC blokowal
   rozladowanie i wymuszal drogi zakup z sieci. Ladowanie ma cap jak CHARGE_PV:
   chwilowa nadwyzka PV idzie do baterii zamiast na tani eksport. Kierunek
   Disabled i prad sieciowy 0 A, wiec zrodlem ladowania jest wylacznie PV.
   Swiadome odejscie od planu (plan liczy SOC bez ladowania); replanning to
   wchlania. #}
{% if state in ['CHARGE_GRID','DISCHARGE_GRID'] %}
  {% set h = (as_timestamp(ns.row.end) - as_timestamp(ns.row.start)) / 3600 %}
  {% set target_soc = [100, [10, ns.row.end_soc_kwh|float / capacity_kwh * 100]|max]|min %}
  {% set charging = state == 'CHARGE_GRID' %}
  {% set target_soc = target_soc|round(0,'floor' if charging else 'ceil') %}
  {# Balansowanie LFP: cel zawsze 100 %, plan moze konczyc slot na progu 99 %. #}
  {% if balance and charging %}{% set target_soc = 100 %}{% endif %}
  {% set knots = [496,512,515,520,522,523,528,531,536,584 if charging else 544] %}
  {% set lo = [8, (target_soc / 10)|int - 1]|min %}
  {% set physical = knots[lo] + (target_soc - (lo + 1)*10)/10 * (knots[lo+1]-knots[lo]) %}
  {% set target_voltage = (physical|round(0,'floor' if charging else 'ceil'))/10 %}
  {% if charging %}
    {# Tylko udzial sieci idzie za planem; calkowite ladowanie ma limit jak
       CHARGE_PV, wiec PV ponad prognoze laduje baterie zamiast eksportu.
       Po osiagnieciu celu SOC siec 0 A i TOU Disabled, PV dalej laduje. #}
    {% set planned = [charge_cap, [0,ns.row.charge_kwh|float]|max / h * 1000 * eta / v]|min %}
    {% set surplus = [0, ns.row.pv_kwh|float - ns.row.curtail_kwh|float - ns.row.load_kwh|float]|max %}
    {% set grid = [max_grid_current,planned,[0,ns.row.charge_kwh|float-surplus]|max / h * 1000 * eta / v]|min %}
    {% set charge = charge_cap %}
    {% set direction = 'Grid' if grid >= 1 and planned >= 1 else 'Disabled' %}
    {# Balansowanie: pelna bateria ma w planie charge_kwh ~ 0, wiec udzial sieci
       wyszedlby 0 A. Co najmniej maly prad z sieci (2 A ~ 1 kW przy 530 V)
       pozwala BMS pobierac prad absorpcji; faktyczny prad ogranicza BMS.
       Wiekszy planowany udzial sieci (dojscie do progu) zostaje. #}
    {% if balance %}{% set grid = [balance_grid_current, grid]|max %}{% set direction = 'Grid' %}{% endif %}
    {% if not balance and ((states(operation_entity)=='Capacity' and soc >= target_soc) or (states(operation_entity)=='Voltage' and v >= target_voltage*10)) %}{% set grid=0 %}{% set direction='Disabled' %}{% set ns.reached=true %}{% endif %}
  {% else %}
    {% set discharge = [discharge_cap, [0,ns.row.discharge_kwh|float]|max / h * 1000 / eta / v]|min %}
    {% set power = [max_power_w, [0,ns.row.discharge_kwh|float]|max / h * 1000]|min %}
    {% set direction = 'Sell' if state=='DISCHARGE_GRID' and discharge >= 1 else 'Disabled' %}
    {% if energy_mode %}
      {# Falownik zatrzymuje sie sam dopiero na progu 49,6 V; koniec wiersza wyznacza licznik. #}
      {% set target_voltage = 49.6 %}
      {% if discharged >= [0, ns.row.discharge_kwh|float]|max - 0.05 %}{% set discharge=0 %}{% set direction='Disabled' %}{% set ns.reached=true %}{% endif %}
    {% elif (states(operation_entity)=='Capacity' and soc <= target_soc) or (states(operation_entity)=='Voltage' and v <= target_voltage*10) %}{% set discharge=0 %}{% set direction='Disabled' %}{% set ns.reached=true %}{% endif %}
  {% endif %}
{% endif %}
{# HOLD/CURTAIL: rozladowanie 0 A, ladowanie i siec hold_grid_current (1 A).
   Rozladowanie 0 A nie zatrzymuje poboru wlasnego falownika: pierwszy HOLD na
   zywo (21.09 22:00) przy trzech pradach 0 A oddawal z baterii 140 W, dom szedl
   z sieci. Kierunek Grid, prog TOU SOC i 1 A ladowania z sieci pozwalaja
   falownikowi pokryc ten pobor z sieci, gdy SOC dojdzie do progu. Prog
   zaokraglony W DOL: przy progu >= SOC falownik dokupowal z sieci (530 W przy
   1 A, 21.09); przy progu <= SOC dryf w dol ograniczony do jednego kroku.
   Krzywa rozladowania (544 V przy 100%) trzyma cel w zatwierdzonym 495-560 V.
   Bez zatrzasku reached_key: cel sledzi plan w kazdym przebiegu. #}
{% if state in ['HOLD','CURTAIL'] and ns.row %}
  {% set target_soc = [100, [10, ns.row.end_soc_kwh|float / capacity_kwh * 100]|max]|min|round(0,'floor') %}
  {% set knots = [496,512,515,520,522,523,528,531,536,544] %}
  {% set lo = [8, (target_soc / 10)|int - 1]|min %}
  {% set physical = knots[lo] + (target_soc - (lo + 1)*10)/10 * (knots[lo+1]-knots[lo]) %}
  {% set target_voltage = (physical|round(0,'floor'))/10 %}
  {% set charge = hold_grid_current %}{% set discharge = 0 %}
  {% set grid = hold_grid_current %}{% set direction = 'Grid' %}
{% endif %}
{% if state in ['CHARGE_GRID','DISCHARGE_GRID'] and rt.get('reached_key') == reached_key and not balance %}{% set charge=charge_cap if state == 'CHARGE_GRID' else 0 %}{% set grid=0 %}{% set discharge=0 %}{% set direction='Disabled' %}{% set ns.reached=true %}{% endif %}
{# Przy 100 % ladowanie 0 A, poza oknem balansowania: tam BMS musi dostac prad absorpcji. #}
{% if state in ['CHARGE_PV','SELF_CONSUME','CHARGE_GRID'] and soc >= 100 and not balance %}{% set charge=0 %}{% endif %}
{% if states(operation_entity)=='Voltage' and (target_voltage < 49.5 or target_voltage > 56.0) %}
  {% set ns.valid=false %}{% set ns.reason='napięcie: cel przekracza zatwierdzony zakres 495–560 V; nie zmieniaj limitów BMS' %}
  {% set state='BASE' %}{% set target_soc=10 %}{% set target_voltage=49.6 %}{% set direction='Disabled' %}{% set charge=relinquish_current %}{% set discharge=relinquish_current %}{% set grid=0 %}{% set power=max_power_w %}
{% endif %}
{% set commissioned = states(operation_entity) in commissioned_battery_modes %}
{% if states(mode_entity) != 'Simulation' and not commissioned %}
  {% set ns.valid=false %}
  {% set ns.reason='bateria: tryb ' ~ states(operation_entity) ~ ' niezatwierdzony; przywróć ' ~ commissioned_battery_modes|join(', ') ~ ' lub wykonaj odbiór fizycznych progów przed dopuszczeniem tego trybu' %}
  {% set state='BASE' %}{% set target_soc=10 %}{% set target_voltage=49.6 %}
  {% set direction='Disabled' %}{% set charge=relinquish_current %}{% set discharge=relinquish_current %}{% set grid=0 %}{% set power=max_power_w %}
{% endif %}
{% set ns.desired=dict([(charge_entity,charge),(discharge_entity,discharge),(grid_entity,grid)]) %}
{% for i in range(1,7) %}
  {% set active=i == ns.active and state != 'BASE' %}
  {% set prefix='number.' ~ program_prefix ~ i ~ '_' %}
  {% set ns.desired=dict(ns.desired, **dict([(prefix~'soc', target_soc if active else 10),(prefix~'voltage',target_voltage if active else 49.6),(prefix~'power',power if active else max_power_w),('select.' ~ program_prefix ~ i ~ '_charging',direction if active else 'Disabled')])) %}
{% endfor %}
{% set norm=namespace(values={},ok=true) %}
{% for e,value in ns.desired.items() %}
  {% if e.startswith('number.') %}
    {% set step=state_attr(e,'step')|float(0) %}
    {% set minimum=state_attr(e,'min') %}{% set maximum=state_attr(e,'max') %}
    {% if not is_number(minimum) or not is_number(maximum) or step <= 0 or minimum|float > value or maximum|float < value %}{% set norm.ok=false %}
    {% else %}
      {% set step=[step,100]|max if e.endswith('_power') else step %}
      {% set rounding='ceil' if (e.endswith('_soc') or e.endswith('_voltage')) and state not in ['CHARGE_GRID','HOLD','CURTAIL'] else 'floor' %}
      {# Decimal targets must not lose a step to binary division noise. Power
         uses 100 W buckets; only sub-microwatt noise may round up a boundary. #}
      {% set units=(value/step)|round(9) if e.endswith('_soc') or e.endswith('_voltage') or e.endswith('_power') else value/step %}
      {% set value=(units|round(0,rounding)*step)|round(6) %}
    {% endif %}
  {% elif value not in (state_attr(e,'options') or []) %}{% set norm.ok=false %}{% endif %}
  {# Voltage rebounds after disabling battery current. Keep this transaction's
     current ceilings monotone; cleanup retains its original BASE profile. #}
  {% if state != 'BASE' and e in [charge_entity,discharge_entity,grid_entity]
    and e in (current_ceiling|default({})) %}
    {% set value=[value,current_ceiling[e]]|min %}
  {% endif %}
  {% set norm.values=dict(norm.values, **dict([(e,value)])) %}
{% endfor %}
{% if not norm.ok %}{% set ns.valid=false %}{% set ns.reason='nastawy: brak zakresu/kroku lub cel poza zakresem encji' %}{% endif %}
{{ dict(valid=ns.valid, reason=ns.reason, state=state, desired=norm.values, telemetry=ns.telemetry,
  handoff_pending=handoff_pending and norm.ok and ns.writers and commissioned,
  writers_safe=ns.writers, settings_ok=norm.ok, active_tou=ns.active, times=ns.times,
  retained=retained, generation=cache.get('generated_at'), deadline=cache.get('valid_until'),
  row_start=ns.row.get('start'), row_end=ns.row.get('end'), operation=states(operation_entity),
  requested_mode=states(mode_entity), commissioned=commissioned, target_reached=ns.reached, reached_key=reached_key, voltage=v, soc=soc, target_voltage=target_voltage, target_soc=target_soc,
  energy_mode=energy_mode, discharged_kwh=discharged|round(3),
  slot_energy=dict(key=reached_key, discharge=slot_start) if energy_mode else rt.get('slot_energy'),
  warning='CURTAIL nieobsługiwany: zastosowano HOLD' if state=='CURTAIL' else '') }}
"""

BASE_DECISION = DECISION.replace(
    "{% if states(mode_entity) == 'Off' %}", "{% if true %}"
)

DIFF = r"""
{% set rt=state_attr(runtime_entity,'runtime') or {} %}
{% set dirty=rt.get('uncertain',[]) %}
{% set ns=namespace(changed=[], stop=[], limits=[], enable=[], protective=false, stopped=[]) %}
{% set active='select.' ~ program_prefix ~ target.active_tou ~ '_charging' %}
{% for e,value in target.desired.items() %}
  {% set equal = states(e)==value if e.startswith('select.') else is_number(states(e)) and (states(e)|float-value|float)|abs < 0.00001 %}
  {% if not equal or e in dirty %}{% set ns.changed=ns.changed+[e] %}{% endif %}
{% endfor %}
{% for e in ns.changed %}
  {% if e == active or e in dirty or (e in [charge_entity,discharge_entity,grid_entity] and target.desired[e] == 0 and states(e)|float(-1) != 0)
    or ('_program_'~target.active_tou~'_' in e and states(active) != 'Disabled') %}{% set ns.protective=true %}{% endif %}
{% endfor %}
{% if ns.protective %}
  {% for e in [charge_entity,grid_entity,discharge_entity] %}
    {% if states(e)|float(-1) != 0 or e in dirty %}{% set ns.stop=ns.stop+[dict(entity=e,value=0,phase='disable')] %}{% set ns.stopped=ns.stopped+[e] %}{% endif %}
  {% endfor %}
  {% for i in range(1,7) %}
    {% set e='select.' ~ program_prefix ~ i ~ '_charging' %}
    {% if states(e) != 'Disabled' or e in dirty %}{% set ns.stop=ns.stop+[dict(entity=e,value='Disabled',phase='disable')] %}{% set ns.stopped=ns.stopped+[e] %}{% endif %}
  {% endfor %}
{% endif %}
{% for e,value in target.desired.items() %}
  {% if e in ns.changed or e in ns.stopped %}
    {% if e.startswith('select.') %}
      {% if value != 'Disabled' %}{% set ns.enable=ns.enable+[dict(entity=e,value=value,phase='enable')] %}
      {% elif e not in ns.stopped %}{% set ns.stop=ns.stop+[dict(entity=e,value=value,phase='disable')] %}{% endif %}
    {% elif e in [grid_entity,charge_entity,discharge_entity] %}
      {% if value > 0 %}{% set ns.enable=ns.enable+[dict(entity=e,value=value,phase='enable')] %}
      {% elif e not in ns.stopped %}{% set ns.stop=ns.stop+[dict(entity=e,value=0,phase='disable')] %}{% endif %}
    {% else %}{% set ns.limits=ns.limits+[dict(entity=e,value=value,phase='limits')] %}{% endif %}
  {% endif %}
{% endfor %}
{{ ns.stop + ns.limits + ns.enable }}
"""
GUARD = r"""
{% set rt=state_attr(runtime_entity,'runtime') or {} %}
{% set current=baseline if cleanup else fresh %}
{% set ns=namespace(prerequisites=true) %}
{% if command.phase == 'enable' %}
  {% for e,value in target.desired.items() %}
    {% if (e.startswith('number.' ~ program_prefix) or value == 0 or value == 'Disabled') and rt.get('confirmed',{}).get(e) != value %}{% set ns.prerequisites=false %}{% endif %}
  {% endfor %}
{% endif %}
{{ ns.prerequisites and current.writers_safe and states(mode_entity) != 'Simulation'
 and (command.phase == 'disable' or
 (current.settings_ok and not failed and ((cleanup and baseline.desired == target.desired
 and baseline.operation == target.operation and baseline.times == target.times
 and (command.phase != 'enable' or baseline.telemetry)) or (fresh.valid and fresh.requested_mode == 'Auto'
 and fresh.generation == target.generation and fresh.deadline == target.deadline
 and fresh.row_start == target.row_start and fresh.active_tou == target.active_tou
 and fresh.times == target.times and fresh.operation == target.operation
 and fresh.desired == target.desired)))) }}
"""

CAP_ONLY_REPLAN = r"""
{% set rt=state_attr(runtime_entity,'runtime') or {} %}
{% set ns=namespace(same=true,changed=false) %}
{% for e,value in target.desired.items() %}
  {% if e in [charge_entity,discharge_entity,grid_entity] %}
    {% if value != fresh.desired.get(e) %}{% set ns.changed=true %}{% endif %}
  {% elif value != fresh.desired.get(e) %}{% set ns.same=false %}{% endif %}
{% endfor %}
{{ not cleanup and transaction_attempt == 1 and not rt.get('uncertain',[])
  and ns.same and ns.changed and fresh.valid and fresh.writers_safe and fresh.settings_ok
  and fresh.requested_mode == target.requested_mode == 'Auto'
  and fresh.state == target.state and fresh.target_reached == target.target_reached
  and fresh.generation == target.generation and fresh.deadline == target.deadline
  and fresh.row_start == target.row_start and fresh.row_end == target.row_end
  and fresh.active_tou == target.active_tou and fresh.times == target.times
  and fresh.operation == target.operation }}
"""


def variables(**kw):
    return {"variables": kw}


def condition(value):
    return {"condition": "template", "value_template": value}


def service(name, entity=None, data=None, **kw):
    out = {"action": name}
    if entity:
        out["target"] = {"entity_id": entity}
    if data:
        out["data"] = data
    return out | kw


def persist(runtime):
    return [
        variables(runtime_update=runtime),
        {
            "event": "energy_compass_deye_runtime",
            "event_data": {"runtime": "{{ runtime_update }}"},
        },
        {
            "wait_template": "{{ state_attr(runtime_entity,'runtime') == runtime_update }}",
            "timeout": {"seconds": 1},
            "continue_on_timeout": False,
        },
    ]


INITIAL = {
    "plan_entity": PLAN,
    "optimizer_entity": OPTIMIZER,
    "valid_entity": VALID,
    "alert_entity": ALERT,
    "cache_entity": CACHE,
    "runtime_entity": RUNTIME,
    "mode_entity": MODE,
    "session_entity": SESSION,
    "session_start_entity": START,
    "restore_entity": PENDING,
    "charge_entity": CHARGE,
    "discharge_entity": DISCHARGE,
    "grid_entity": GRID,
    "voltage_entity": Input("voltage_entity"),
    "soc_entity": Input("soc_entity"),
    "operation_entity": Input("operation_entity"),
    "commissioned_battery_modes": Input("commissioned_battery_modes"),
    "discharge_energy_entity": Input("discharge_energy_entity"),
    "hold_grid_current": Input("hold_grid_current"),
    "balance_grid_current": Input("balance_grid_current"),
    "telemetry_entities": Input("telemetry_entities"),
    "eta": Input("eta"),
    "relinquish_current": Input("relinquish_current"),
    "capacity_kwh": Input("capacity_kwh"),
    "max_power_w": Input("max_power_w"),
    "max_current": Input("max_current"),
    "max_grid_current": Input("max_grid_current"),
    "old_writers": Input("old_writers"),
    "program_prefix": f"{{{{ state_attr('{TOU_SETTINGS}', 'prefix') or '' }}}}",
    "registers": REGISTERS,
}

mark_dirty = "{{ dict(state_attr(runtime_entity,'runtime') or {}, uncertain=((state_attr(runtime_entity,'runtime') or {}).get('uncertain',[]) + [command.entity])|unique|list) }}"
mark_confirmed = "{{ dict(state_attr(runtime_entity,'runtime') or {}, uncertain=(state_attr(runtime_entity,'runtime') or {}).get('uncertain',[])|reject('equalto',command.entity)|list, confirmed=dict((state_attr(runtime_entity,'runtime') or {}).get('confirmed',{}), **dict([(command.entity,command.value)])), last_confirmation=now().isoformat()) }}"
WRITE = [
    variables(fresh=DECISION, baseline=BASE_DECISION, allowed=GUARD),
    {
        "if": [condition("{{ allowed and not confirmed_write }}")],
        "then": [
            *persist(mark_dirty),
            variables(fresh=DECISION, baseline=BASE_DECISION, allowed=GUARD),
            {
                "if": [condition("{{ allowed }}")],
                "then": [
                    {
                        "choose": [
                            {
                                "conditions": [
                                    condition(
                                        "{{ command.entity.startswith('select.') }}"
                                    )
                                ],
                                "sequence": [
                                    service(
                                        "select.select_option",
                                        "{{ command.entity }}",
                                        {"option": "{{ command.value }}"},
                                        continue_on_error=True,
                                    )
                                ],
                            }
                        ],
                        "default": [
                            service(
                                "number.set_value",
                                "{{ command.entity }}",
                                {"value": "{{ command.value }}"},
                                continue_on_error=True,
                            )
                        ],
                    },
                    variables(raw_response={}),
                    service(
                        "solarman.read_holding_registers",
                        data={
                            "device": Input("solarman_device"),
                            "address": 108,
                            "count": 70,
                        },
                        response_variable="raw_response",
                        continue_on_error=True,
                    ),
                    variables(
                        observed="{% set ns=namespace(values={}) %}{% for e,reg in registers.items() %}{% set raw=raw_response.get(reg.address,raw_response.get(reg.address|string)) if raw_response is mapping else none %}{% if is_number(raw) %}{% set value={0:'Disabled',1:'Grid',32:'Sell'}.get(raw|int,'unsupported') if e.startswith('select.') else (raw|float*reg.scale)|round(6) %}{% set ns.values=dict(ns.values, **dict([(e,value)])) %}{% endif %}{% endfor %}{{ ns.values }}"
                    ),
                    *persist(
                        "{% set rt=state_attr(runtime_entity,'runtime') or {} %}{% set ns=namespace(dirty=rt.get('uncertain',[])) %}{% for e,value in observed.items() %}{% set equal=states(e)==value if e.startswith('select.') else is_number(states(e)) and (states(e)|float-value|float)|abs < 0.00001 %}{% if not equal %}{% set ns.dirty=ns.dirty+[e] %}{% endif %}{% endfor %}{{ dict(rt, uncertain=ns.dirty|unique|list, confirmed=dict(rt.get('confirmed',{}), **observed)) }}"
                    ),
                    variables(
                        raw_value="{{ raw_response.get(registers[command.entity].address, raw_response.get(registers[command.entity].address|string)) if raw_response is mapping else none }}",
                        confirmed_write="{{ is_number(raw_value) and ((raw_value|int == {'Disabled':0,'Grid':1,'Sell':32}.get(command.value,-1)) if command.entity.startswith('select.') else (raw_value|float * registers[command.entity].scale - command.value|float)|abs < 0.00001) }}",
                    ),
                    {
                        "if": [condition("{{ confirmed_write }}")],
                        "then": persist(mark_confirmed),
                    },
                ],
            },
        ],
    },
]

ACTIONS = [
    variables(**INITIAL),
    variables(current_ceiling={}),
    {
        "if": [
            condition(
                "{{ trigger.id|default('') == 'start' or not is_state(session_entity,'on') }}"
            )
        ],
        "then": [
            service(
                "input_datetime.set_datetime",
                START,
                {"timestamp": "{{ as_timestamp(now())|round(0,'ceil') }}"},
            ),
            service("input_boolean.turn_on", SESSION),
        ],
    },
    {
        "if": [
            condition(
                "{{ states(optimizer_entity) not in ['ready','calculating'] or not is_state(alert_entity,'off') or (trigger.platform|default('') == 'state' and trigger.to_state is not none and ((trigger.entity_id == alert_entity and trigger.to_state.state != 'off') or (trigger.entity_id == optimizer_entity and trigger.to_state.state not in ['ready','calculating']))) }}"
            )
        ],
        # An error that persists revokes again on every run. Keep the first
        # revocation time of the same generation: a plan computed after it but
        # published while the alert was still on (entities update one by one)
        # must stay acceptable.
        "then": persist(
            "{% set rt = state_attr(runtime_entity,'runtime') or {} %}{% set g = (state_attr(cache_entity,'snapshot') or {}).get('generated_at') %}{{ dict(rt, revoked_generation=g, revoked_at=rt.get('revoked_at') if rt.get('revoked_at') and 'revoked_generation' in rt and rt.get('revoked_generation') == g else now().isoformat(), revoked_reason='Błąd obliczeń lub źródeł: wymagany nowy plan') }}"
        ),
    },
    # Re-read a split publication before persisting a failure or touching Deye.
    # Only transient handoff mismatches retry; all safety gates stay in DECISION.
    variables(candidate={}, decision={}),
    {
        "repeat": {
            "count": 3,
            "sequence": [
                {
                    "if": [
                        condition("{{ repeat.index == 1 or decision.handoff_pending }}")
                    ],
                    "then": [
                        variables(candidate=CANDIDATE),
                        {
                            "if": [condition("{{ candidate|length > 0 }}")],
                            "then": [
                                {
                                    "event": "energy_compass_deye_accept_plan",
                                    "event_data": {"snapshot": "{{ candidate }}"},
                                },
                                {
                                    "wait_template": "{{ state_attr(cache_entity,'snapshot') == candidate }}",
                                    "timeout": {"seconds": 1},
                                    "continue_on_timeout": False,
                                },
                            ],
                        },
                        variables(decision=DECISION),
                        {
                            "if": [
                                condition(
                                    "{{ decision.handoff_pending and repeat.index < 3 }}"
                                )
                            ],
                            "then": [{"delay": {"milliseconds": 200}}],
                        },
                    ],
                },
            ],
        }
    },
    *persist(
        "{{ dict(state_attr(runtime_entity,'runtime') or {}, reached_key=decision.reached_key if decision.target_reached else (state_attr(runtime_entity,'runtime') or {}).get('reached_key'), slot_energy=decision.slot_energy, desired=decision.desired, requested_mode=decision.requested_mode, battery_mode_commissioned=decision.commissioned, state=decision.state, accepted_generation=decision.generation, original_deadline=decision.deadline, active_tou=decision.active_tou, retained=decision.retained, code='ok' if decision.valid else 'blocked', reason=decision.reason, warning=decision.warning, takeover_blocked=not decision.writers_safe, since=(state_attr(runtime_entity,'runtime') or {}).get('since',now().isoformat()) if (state_attr(runtime_entity,'runtime') or {}).get('reason') == decision.reason else now().isoformat()) }}"
    ),
    {
        "if": [
            condition(
                "{{ is_state(mode_entity,'Simulation') and is_state(restore_entity,'on') }}"
            )
        ],
        "then": [
            service("input_select.select_option", MODE, {"option": "Off"}),
            *persist(
                "{{ dict(state_attr(runtime_entity,'runtime') or {},code='simulation_blocked',reason='Najpierw przywróć bazę w Off; po potwierdzeniu wybierz Simulation ponownie') }}"
            ),
            {
                "stop": "Simulation odrzucone: pozostał obowiązek przywrócenia, wybrano Off"
            },
        ],
    },
    {
        "if": [condition("{{ is_state(mode_entity,'Simulation') }}")],
        "then": [{"stop": "Symulacja: profil obliczony, bez zapisów falownika"}],
    },
    {
        "if": [
            condition(
                "{{ is_state(mode_entity,'Off') and not is_state(restore_entity,'on') }}"
            )
        ],
        "then": [{"stop": "Sterowanie zwolnione: pozostaw nastawy ręczne"}],
    },
    {
        "if": [condition("{{ not decision.writers_safe }}")],
        "then": [
            {
                "stop": "Przejęcie zablokowane: wyłącz dawne automatyzacje i zatrzymaj wykonania"
            }
        ],
    },
    variables(
        aborted=False,
        cleanup="{{ not decision.valid or is_state(mode_entity,'Off') or is_state(restore_entity,'on') and not (state_attr(runtime_entity,'runtime') or {}).get('owned_session') == state_attr(session_start_entity,'timestamp') }}",
    ),
    {
        "repeat": {
            "count": 3,
            "sequence": [
                variables(
                    target=DECISION,
                    transaction_attempt="{{ repeat.index }}",
                    replan=False,
                ),
                # A publication can also land after preflight. Before any transaction,
                # leave the still-valid profile for the already queued state trigger.
                {
                    "if": [
                        condition(
                            "{{ transaction_attempt == 1 and not cleanup and not aborted and target.handoff_pending }}"
                        )
                    ],
                    "then": [
                        {
                            "stop": "Publikacja planu po kontroli: ponowny odczyt w kolejnym przebiegu"
                        }
                    ],
                },
                {
                    "if": [condition("{{ cleanup or aborted }}")],
                    "then": [variables(cleanup=True), variables(target=BASE_DECISION)],
                },
                {
                    "if": [condition("{{ not cleanup }}")],
                    "then": [
                        variables(
                            current_ceiling="{{ dict([(charge_entity,target.desired[charge_entity]),(discharge_entity,target.desired[discharge_entity]),(grid_entity,target.desired[grid_entity])]) }}"
                        ),
                    ],
                },
                variables(commands=DIFF, failed=False),
                {
                    "if": [condition("{{ commands|length > 0 }}")],
                    "then": [
                        service("input_boolean.turn_on", PENDING),
                        *persist(
                            "{{ dict(state_attr(runtime_entity,'runtime') or {}, owned_session=state_attr(session_start_entity,'timestamp')) }}"
                        ),
                    ],
                },
                {
                    "repeat": {
                        "for_each": "{{ commands }}",
                        "sequence": [
                            {
                                "if": [condition("{{ not replan }}")],
                                "then": [
                                    variables(
                                        command="{{ repeat.item }}",
                                        confirmed_write=False,
                                    ),
                                    {"repeat": {"count": 3, "sequence": WRITE}},
                                    {
                                        "if": [condition("{{ not confirmed_write }}")],
                                        "then": [
                                            {
                                                "if": [
                                                    condition(
                                                        "{{ not failed and not aborted and not allowed }}"
                                                    )
                                                ],
                                                "then": [
                                                    variables(replan=CAP_ONLY_REPLAN)
                                                ],
                                            },
                                            variables(failed=True, aborted=True),
                                        ],
                                    },
                                ],
                            }
                        ],
                    }
                },
                variables(
                    fresh=DECISION,
                    baseline=BASE_DECISION,
                    profile_confirmed="{% set rt=state_attr(runtime_entity,'runtime') or {} %}{% set ns=namespace(ok=not rt.get('uncertain',[])) %}{% for e,value in target.desired.items() %}{% if rt.get('confirmed',{}).get(e) != value %}{% set ns.ok=false %}{% endif %}{% endfor %}{{ ns.ok }}",
                ),
                {
                    "if": [condition("{{ replan }}")],
                    "then": [variables(replan=CAP_ONLY_REPLAN)],
                },
                {
                    "if": [
                        condition(
                            "{{ transaction_attempt == 1 and not cleanup and not aborted and commands|length == 0 and fresh.handoff_pending }}"
                        )
                    ],
                    "then": [
                        {
                            "stop": "Publikacja planu podczas kontroli bez zapisów: ponowny odczyt w kolejnym przebiegu"
                        }
                    ],
                },
                {
                    "if": [
                        condition(
                            "{{ not cleanup and (not fresh.valid or fresh.desired != target.desired or fresh.generation != target.generation or fresh.row_start != target.row_start or fresh.requested_mode != 'Auto') }}"
                        )
                    ],
                    "then": [
                        {
                            "if": [condition("{{ not failed and not aborted }}")],
                            "then": [variables(replan=CAP_ONLY_REPLAN)],
                        },
                        variables(aborted=True),
                    ],
                },
                {
                    "if": [
                        condition(
                            "{{ cleanup and (baseline.desired != target.desired or baseline.operation != target.operation or baseline.times != target.times or not baseline.settings_ok or not baseline.writers_safe or is_state(mode_entity,'Simulation')) }}"
                        )
                    ],
                    "then": [variables(aborted=True)],
                },
                {
                    "if": [condition("{{ not failed and not aborted }}")],
                    "then": [
                        {
                            "if": [condition("{{ cleanup and profile_confirmed }}")],
                            "then": [service("input_boolean.turn_off", PENDING)],
                        },
                        *persist(
                            "{{ dict(state_attr(runtime_entity,'runtime') or {}, confirmed_mode=('BASE' if cleanup else target.state) if profile_confirmed else 'unconfirmed', code=('restored' if cleanup else 'ok') if profile_confirmed else 'verification_required', reason=target.reason if profile_confirmed else 'Wymagane potwierdzenie bazowych nastaw podczas przejęcia sterowania') }}"
                        ),
                        {
                            "stop": "Przebieg zakończony: sprawdź stan potwierdzenia w diagnostyce"
                        },
                    ],
                },
                variables(cleanup="{{ not replan }}", aborted=False),
            ],
        }
    },
    *persist(
        "{{ dict(state_attr(runtime_entity,'runtime') or {}, confirmed_mode='unconfirmed',code='write_failed',reason='Brak potwierdzenia nastaw; zachowano obowiązek przywrócenia') }}"
    ),
]

TRIGGERS = [
    {"trigger": "homeassistant", "event": "start", "id": "start"},
    {"trigger": "time_pattern", "minutes": "/1", "id": "minute"},
    {
        "trigger": "state",
        "entity_id": [
            PLAN,
            OPTIMIZER,
            VALID,
            ALERT,
            Input("compass_entity"),
            MODE,
            SESSION,
            PENDING,
            Input("operation_entity"),
        ],
        "id": "change",
    },
    {"trigger": "state", "entity_id": Input("old_writers"), "id": "change"},
    # Attribute changes carry the TOU program values and times, so no `to:` here.
    {"trigger": "state", "entity_id": TOU_SETTINGS, "id": "settings"},
    {
        "trigger": "state",
        "entity_id": [Input("soc_entity"), CHARGE, DISCHARGE, GRID],
        "to": None,
        "id": "settings",
    },
    {"trigger": "time", "at": "sensor.energy_compass_deye_deadline", "id": "expiry"},
    {
        "trigger": "time",
        "at": "sensor.energy_compass_deye_interval_end",
        "id": "interval",
    },
    {"trigger": "time", "at": "sensor.energy_compass_deye_next_tou", "id": "tou"},
]
for field in ["from", "to"]:
    for state in ["unknown", "unavailable"]:
        TRIGGERS.append(
            {
                "trigger": "state",
                "entity_id": Input("telemetry_entities"),
                field: state,
                "id": "availability",
            }
        )

DESCRIPTION = """Executes an Energy Compass plan on a Deye hybrid inverter through the Solarman integration.

Requires the companion package `packages/energy_compass_deye.yaml` (mode select, session helpers, plan cache, runtime and timing sensors). The package's `inverter_deye_program_` prefix must match your Solarman TOU program entities.

Mode `Off` releases control after a confirmed restore, `Simulation` computes targets without writing, `Auto` writes and verifies every register. See the Deye controller section of the Energy Compass guide."""

BLUEPRINT = {
    "blueprint": {
        "name": "Energy Compass Deye (Solarman) controller",
        "description": DESCRIPTION,
        "domain": "automation",
        "source_url": "https://github.com/datamindzio/ha-energy-compass/blob/main/blueprints/automation/energy_compass/deye_solarman_controller.yaml",
        "homeassistant": {"min_version": "2026.9.1"},
        "input": INPUTS,
    },
    "mode": "queued",
    "max": 2,
    "max_exceeded": "silent",
    "triggers": TRIGGERS,
    "actions": ACTIONS,
}


def tou_settings(prefix):
    entities = [
        f"{'select' if field == 'charging' else 'number'}.{prefix}{{i}}_{field}"
        for field, _, _ in PROGRAM_FIELDS
    ] + [f"time.{prefix}{{i}}_time"]
    values = "".join(
        f"{{% set ns.v = dict(ns.v, **dict([('{e}', states('{e}'))])) %}}".replace(
            "{i}", "' ~ i ~ '"
        )
        for e in entities
    )
    return {
        "name": "Energy Compass Deye TOU settings",
        "unique_id": "energy_compass_deye_tou_settings",
        "state": "{% set ns = namespace(v={}) %}{% for i in range(1,7) %}"
        + values
        + "{% endfor %}{{ ns.v.values()|reject('in', ['unknown','unavailable'])|list|length }}",
        "attributes": {
            "prefix": prefix,
            "values": "{% set ns = namespace(v={}) %}{% for i in range(1,7) %}"
            + values
            + "{% endfor %}{{ ns.v }}",
        },
    }


def package(prefix=DEFAULT_PROGRAM_PREFIX):
    next_tou = (
        "{% set ns=namespace(times=[]) %}{% for i in range(1,7) %}"
        f"{{% set raw=states('time.{prefix}'~i~'_time') %}}"
        "{% if raw not in ['unknown','unavailable'] %}{% set at=today_at(raw) %}{% set at=at if at > now() else at + timedelta(days=1) %}"
        "{% set ns.times=ns.times+[at] %}{% endif %}{% endfor %}{{ (ns.times|min).isoformat() if ns.times else none }}"
    )
    return {
        "input_select": {
            "energy_compass_deye_mode": {
                "name": "Energy Compass Deye mode",
                "options": ["Off", "Simulation", "Auto"],
                "icon": "mdi:compass",
            }
        },
        "input_boolean": {
            "energy_compass_deye_session": {
                "name": "Energy Compass Deye session ready",
                "initial": False,
            },
            "energy_compass_deye_restore_pending": {
                "name": "Energy Compass Deye restore pending"
            },
        },
        "input_datetime": {
            "energy_compass_deye_session_start": {
                "name": "Energy Compass Deye session start",
                "has_date": True,
                "has_time": True,
            }
        },
        "template": [
            {
                "triggers": [
                    {
                        "trigger": "event",
                        "event_type": "energy_compass_deye_accept_plan",
                    }
                ],
                "sensor": [
                    {
                        "name": "Energy Compass Deye plan",
                        "unique_id": "energy_compass_deye_plan",
                        "state": "{{ trigger.event.data.snapshot.generated_at }}",
                        "attributes": {"snapshot": "{{ trigger.event.data.snapshot }}"},
                    }
                ],
            },
            {
                "triggers": [
                    {"trigger": "event", "event_type": "energy_compass_deye_runtime"}
                ],
                "sensor": [
                    {
                        "name": "Energy Compass Deye runtime",
                        "unique_id": "energy_compass_deye_runtime",
                        "state": '{{ trigger.event.data.runtime.get("code", "waiting") }}',
                        "attributes": {"runtime": "{{ trigger.event.data.runtime }}"},
                    }
                ],
            },
            {
                "sensor": [
                    tou_settings(prefix),
                    {
                        "name": "Energy Compass Deye next TOU",
                        "unique_id": "energy_compass_deye_next_tou",
                        "device_class": "timestamp",
                        "state": next_tou,
                    },
                    {
                        "name": "Energy Compass Deye deadline",
                        "unique_id": "energy_compass_deye_deadline",
                        "device_class": "timestamp",
                        "state": "{{ (state_attr('sensor.energy_compass_deye_plan','snapshot') or {}).get('valid_until') }}",
                    },
                    {
                        "name": "Energy Compass Deye interval end",
                        "unique_id": "energy_compass_deye_interval_end",
                        "device_class": "timestamp",
                        "state": "{% set c=state_attr('sensor.energy_compass_deye_plan','snapshot') or {} %}{% set ns=namespace(end=none) %}{% for r in c.get('intervals',[]) %}{% if as_timestamp(r.start) <= as_timestamp(now()) < as_timestamp(r.end) %}{% set ns.end=r.end %}{% endif %}{% endfor %}{{ ns.end }}",
                    },
                ]
            },
        ],
    }


HEADER = "# Generated by tools/deye_controller/build.py - do not edit; edit the generator and rebuild.\n"


class Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def string(dumper, value):
    return dumper.represent_scalar(
        "tag:yaml.org,2002:str",
        value.strip() if "\n" in value else value,
        style="|" if "\n" in value else None,
    )


Dumper.add_representer(str, string)
Dumper.add_representer(
    Input, lambda dumper, value: dumper.represent_scalar("!input", value.name)
)


def dump(value):
    return HEADER + yaml.dump(
        value, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=120
    )


def outputs(prefix=DEFAULT_PROGRAM_PREFIX):
    return {BLUEPRINT_PATH: dump(BLUEPRINT), PACKAGE_PATH: dump(package(prefix))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="fail if the committed files are stale"
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PROGRAM_PREFIX,
        help="TOU program entity prefix; with a non-default prefix the package is printed, not written",
    )
    args = parser.parse_args(argv)
    if args.prefix != DEFAULT_PROGRAM_PREFIX:
        sys.stdout.write(dump(package(args.prefix)))
        return 0
    stale = [
        path
        for path, text in outputs().items()
        if not path.exists() or path.read_text() != text
    ]
    if args.check:
        for path in stale:
            print(
                f"stale: {path.relative_to(REPO)} - run python tools/deye_controller/build.py",
                file=sys.stderr,
            )
        return 1 if stale else 0
    for path, text in outputs().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
