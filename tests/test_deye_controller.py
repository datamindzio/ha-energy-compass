"""Behaviour of the generated Deye (Solarman) controller blueprint.

The blueprint is loaded with the inputs below substituted, then its Jinja
templates and action tree run against a sanitized state sample.
"""

import copy
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import yaml
from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "blueprints/automation/energy_compass/deye_solarman_controller.yaml"
PACKAGE = ROOT / "packages/energy_compass_deye.yaml"
STATES = ROOT / "tests/fixtures/deye_controller/states.json"
P = "sensor.energy_compass_home_pilot_plan"
O = "sensor.energy_compass_home_pilot_stan_optymalizatora"
F = "binary_sensor.energy_compass_home_pilot_poprawna_prognoza"
A = "binary_sensor.energy_compass_home_pilot_alert"
CACHE = "sensor.energy_compass_deye_plan"
RT = "sensor.energy_compass_deye_runtime"
MODE = "input_select.energy_compass_deye_mode"
TOU = "sensor.energy_compass_deye_tou_settings"
PREFIX = "inverter_deye_program_"
INPUTS = {
    "plan_entity": P,
    "optimizer_entity": O,
    "valid_entity": F,
    "alert_entity": A,
    "compass_entity": "sensor.energy_compass_home_pilot_kompas_energii",
    "solarman_device": "test_solarman_device",
    "charge_entity": "number.inverter_deye_battery_max_charging_current",
    "discharge_entity": "number.inverter_deye_battery_max_discharging_current",
    "grid_entity": "number.inverter_deye_battery_grid_charging_current",
    "operation_entity": "select.inverter_deye_battery_operation_mode",
    "soc_entity": "sensor.inverter_deye_battery",
    "voltage_entity": "sensor.inverter_deye_battery_voltage",
    "telemetry_entities": [
        "sensor.inverter_deye_battery",
        "sensor.inverter_deye_battery_voltage",
        "sensor.inverter_deye_update_interval",
    ],
    "old_writers": [
        "automation.energy_storage_charging",
        "automation.auto_slow_battery_balancing",
        "automation.energy_storage_discharge",
        "automation.energy_storage_morning_discharge",
    ],
}


class InputRef:
    def __init__(self, name):
        self.name = name


class BlueprintLoader(yaml.SafeLoader):
    pass


BlueprintLoader.add_constructor(
    "!input", lambda loader, node: InputRef(loader.construct_scalar(node))
)


def load_blueprint(inputs=INPUTS):
    """Return the automation config a blueprint instance with `inputs` produces."""
    doc = yaml.load(BLUEPRINT.read_text(), Loader=BlueprintLoader)
    values = {
        key: spec["default"]
        for section in doc["blueprint"]["input"].values()
        for key, spec in section["input"].items()
        if "default" in spec
    }
    values |= inputs

    def substitute(x):
        if isinstance(x, InputRef):
            return copy.deepcopy(values[x.name])
        if isinstance(x, dict):
            return {k: substitute(v) for k, v in x.items()}
        if isinstance(x, list):
            return [substitute(v) for v in x]
        return x

    return substitute({k: v for k, v in doc.items() if k != "blueprint"})


def timestamp(x, default=None):
    try:
        if isinstance(x, (float, int)):
            return float(x)
        return (
            x if isinstance(x, dt.datetime) else dt.datetime.fromisoformat(x)
        ).timestamp()
    except ValueError, TypeError:
        return default


class States:
    def __init__(self, data):
        self.data = data

    def __call__(self, entity):
        return self.data.get(entity, {}).get("state", "unknown")

    def __getitem__(self, entity):
        if entity not in self.data:
            return None
        x = self.data[entity]
        return SimpleNamespace(
            state=x.get("state", "unknown"),
            attributes=x.get("attributes", {}),
            last_reported=timestamp(x.get("last_reported"), 0),
        )


class Harness:
    def __init__(self, inputs=INPUTS):
        self.doc = load_blueprint(inputs)
        self.data = {
            x["entity_id"]: copy.deepcopy(x) for x in json.loads(STATES.read_text())
        }
        self.now = dt.datetime(2026, 9, 19, 11, 42, tzinfo=ZoneInfo("Europe/Warsaw"))
        for x in self.data.values():
            x["last_reported"] = self.now.isoformat()
        self.set(MODE, "Auto")
        self.set("input_boolean.energy_compass_deye_session", "on")
        self.set("input_boolean.energy_compass_deye_restore_pending", "off")
        self.set(
            "input_datetime.energy_compass_deye_session_start",
            str(self.now.timestamp() - 180),
            timestamp=self.now.timestamp() - 180,
        )
        self.set(RT, "ok", runtime={})
        self.set(TOU, "30", prefix=PREFIX)
        for entity in self.doc["actions"][0]["variables"]["old_writers"]:
            self.set(entity, "off", current=0)
        self.env = NativeEnvironment(undefined=StrictUndefined)
        self.env.globals.update(
            states=States(self.data),
            state_attr=lambda e, a: self.data.get(e, {}).get("attributes", {}).get(a),
            is_state=lambda e, s: self.data.get(e, {}).get("state") == s,
            now=lambda: self.now,
            as_timestamp=timestamp,
            is_number=lambda x: isinstance(x, (int, float, str)) and self.finite(x),
            dict=dict,
        )
        self.compiled = {}
        # Script variables render in order; later ones see the earlier ones.
        self.ctx = {}
        for key, value in self.doc["actions"][0]["variables"].items():
            self.ctx[key] = self.render(value)

    @staticmethod
    def finite(x):
        import math

        try:
            return math.isfinite(float(x))
        except ValueError, TypeError:
            return False

    def set(self, entity, state, **attrs):
        self.data[entity] = {
            "state": state,
            "attributes": attrs,
            "last_reported": self.now.isoformat(),
        }

    def render(self, template, **kwargs):
        if not isinstance(template, str):
            return template
        if template not in self.compiled:
            self.compiled[template] = self.env.from_string(template)
        return self.compiled[template].render(**(self.ctx | kwargs))

    def expression(self, name):
        def walk(x):
            if isinstance(x, dict):
                if isinstance(x.get("variables", {}).get(name), str):
                    return x["variables"][name]
                for v in x.values():
                    r = walk(v)
                    if r is not None:
                        return r
            if isinstance(x, list):
                for v in x:
                    r = walk(v)
                    if r is not None:
                        return r

        return walk(self.doc)

    def accept(self):
        value = self.render(self.expression("candidate"))
        self.set(CACHE, value.get("generated_at", "none"), snapshot=value)
        return value

    def decision(self):
        return self.render(self.expression("decision"))


@pytest.fixture
def h():
    return Harness()


def test_full_profile(h):
    assert h.accept()["generated_at"]
    d = h.decision()
    assert d["valid"] and len(d["desired"]) == 27
    assert d["state"] == "CHARGE_PV"


@pytest.mark.parametrize(
    "state,charging,charge,discharge",
    [
        ("CHARGE_GRID", "Grid", True, False),
        ("CHARGE_PV", "Disabled", True, True),
        ("DISCHARGE_GRID", "Sell", False, True),
        ("SELF_CONSUME", "Disabled", True, True),
        ("HOLD", "Grid", True, False),
        ("CURTAIL", "Grid", True, False),
    ],
)
def test_state_table(h, state, charging, charge, discharge):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(
        state=state,
        charge_kwh=0.5,
        discharge_kwh=0.5,
        pv_kwh=0,
        load_kwh=0.05,
        end_soc_kwh=20 if charge else 5,
    )
    h.accept()
    d = h.decision()
    values = d["desired"]
    assert values["select.inverter_deye_program_3_charging"] == charging
    assert (values[h.ctx["charge_entity"]] > 0) == charge
    assert (values[h.ctx["discharge_entity"]] > 0) == discharge


def test_frozen_expiry_and_revoke(h):
    old = h.accept()
    h.data[P]["attributes"]["valid_until"] = "2026-09-20T00:00:00+00:00"
    assert h.render(h.expression("candidate")) == {}
    h.data[O]["state"] = "calculating"
    h.data[F]["state"] = "off"
    assert h.decision()["valid"]
    h.data[A]["state"] = "on"
    assert not h.decision()["valid"]
    h.set(RT, "error", runtime={"revoked_generation": old["generated_at"]})
    h.data[A]["state"] = "off"
    assert not h.decision()["valid"]


def test_startup_gate(h):
    h.accept()
    h.data["input_boolean.energy_compass_deye_session"]["state"] = "off"
    assert not h.decision()["valid"]


@pytest.mark.parametrize(
    "entity,value",
    [
        ("sensor.inverter_deye_battery", "unknown"),
        ("sensor.inverter_deye_battery_voltage", "unavailable"),
        ("sensor.inverter_deye_update_interval", "unknown"),
    ],
)
def test_unknown(h, entity, value):
    h.accept()
    h.data[entity]["state"] = value
    assert not h.decision()["valid"]


def test_no_catchup_and_current_floor(h):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", charge_kwh=0.0001, pv_kwh=0, end_soc_kwh=25)
    h.accept()
    values = h.decision()["desired"]
    assert values[h.ctx["grid_entity"]] == 0
    assert values["select.inverter_deye_program_3_charging"] == "Disabled"


def test_expiry(h):
    snap = h.accept()
    h.now = dt.datetime.fromisoformat(snap["valid_until"])
    assert not h.decision()["valid"]


def test_oldwriter(h):
    h.accept()
    h.data[h.ctx["old_writers"][0]]["state"] = "on"
    assert not h.decision()["writers_safe"]


class Stopped(Exception):
    pass


class Runner:
    def __init__(self, h):
        self.h = h
        self.ctx = {"trigger": {"id": "change"}}
        self.writes = []
        self.reads = []
        self.raw = {}
        self.drop = set()
        self.hook = None
        self.before_action = None
        self.delays = []
        for entity, reg in h.ctx["registers"].items():
            value = h.data[entity]["state"]
            self.raw[reg["address"]] = (
                {"Disabled": 0, "Grid": 1, "Sell": 32}.get(value, 0)
                if entity.startswith("select.")
                else round(float(value) / reg["scale"])
            )

    def value(self, v):
        if isinstance(v, str):
            return self.h.render(v, **self.ctx)
        if isinstance(v, dict):
            return {k: self.value(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(x) for x in v]
        return v

    def conditions(self, conds):
        return all(self.value(x["value_template"]) for x in conds)

    def run(self):
        try:
            self.actions(self.h.doc["actions"])
        except Stopped:
            pass

    def actions(self, actions):
        for action in actions:
            if self.before_action:
                self.before_action(self, action)
            if "variables" in action:
                for k, v in action["variables"].items():
                    self.ctx[k] = self.value(v)
            elif "if" in action:
                self.actions(
                    action.get("then", [])
                    if self.conditions(action["if"])
                    else action.get("else", [])
                )
            elif "choose" in action:
                selected = next(
                    (c for c in action["choose"] if self.conditions(c["conditions"])),
                    None,
                )
                self.actions(
                    selected["sequence"] if selected else action.get("default", [])
                )
            elif "repeat" in action:
                spec = action["repeat"]
                old = self.ctx.get("repeat")
                values = (
                    self.value(spec["for_each"])
                    if "for_each" in spec
                    else range(int(self.value(spec["count"])))
                )
                for i, item in enumerate(values):
                    self.ctx["repeat"] = {"item": item, "index": i + 1}
                    self.actions(spec["sequence"])
                self.ctx["repeat"] = old
            elif "event" in action:
                payload = self.value(action["event_data"])
                if action["event"] == "energy_compass_deye_accept_plan":
                    self.h.set(
                        CACHE,
                        payload["snapshot"]["generated_at"],
                        snapshot=payload["snapshot"],
                    )
                else:
                    self.h.set(
                        RT,
                        payload["runtime"].get("code", "waiting"),
                        runtime=payload["runtime"],
                    )
            elif "wait_template" in action:
                if not self.value(action["wait_template"]):
                    raise Stopped()
            elif "delay" in action:
                seconds = self.value(action["delay"]).get("milliseconds", 0) / 1000
                self.delays.append(seconds)
                self.h.now += dt.timedelta(seconds=seconds)
            elif "stop" in action:
                raise Stopped()
            elif "action" in action:
                self.service(action)
            else:
                raise AssertionError(action)

    def service(self, action):
        service = action["action"]
        data = self.value(action.get("data", {}))
        entity = self.value(action.get("target", {})).get("entity_id")
        if service in ["number.set_value", "select.select_option"]:
            value = data.get("value", data.get("option"))
            self.writes.append((entity, value))
            self.h.data[entity]["state"] = str(value)
            register = self.h.ctx["registers"][entity]
            if entity not in self.drop:
                self.raw[register["address"]] = (
                    {"Disabled": 0, "Grid": 1, "Sell": 32}[value]
                    if service.startswith("select")
                    else int(float(value) / register["scale"])
                )
            if self.hook:
                self.hook(self, entity, value)
        elif service == "solarman.read_holding_registers":
            self.reads.append(data["address"])
            self.ctx[action["response_variable"]] = {
                address: raw
                for address, raw in self.raw.items()
                if data["address"] <= address < data["address"] + data["count"]
            }
        elif service.startswith("input_boolean."):
            self.h.data[entity]["state"] = (
                "on" if service.endswith("turn_on") else "off"
            )
        elif service == "input_select.select_option":
            self.h.data[entity]["state"] = data["option"]
        elif service == "input_datetime.set_datetime":
            self.h.set(entity, str(data["timestamp"]), timestamp=int(data["timestamp"]))
        else:
            raise AssertionError(service)


def test_action_order_and_ten_noops(h):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, end_soc_kwh=20)
    runner = Runner(h)
    runner.run()
    assert (
        runner.writes
        and h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "CHARGE_GRID"
    )
    enable = next(i for i, (e, v) in enumerate(runner.writes) if v == "Grid")
    limits = [
        i
        for i, (e, v) in enumerate(runner.writes)
        if e.endswith(("_voltage", "_soc", "_power"))
    ]
    assert max(limits) < enable
    assert runner.writes[0] == (h.ctx["charge_entity"], 0)
    count = len(runner.writes)
    reads = len(runner.reads)
    for _ in range(10):
        runner.run()
    assert len(runner.writes) == count and len(runner.reads) == reads


def test_lost_optimistic_write_retried(h):
    runner = Runner(h)
    bad = "number.inverter_deye_program_3_voltage"
    runner.drop.add(bad)
    runner.run()
    assert bad in h.data[RT]["attributes"]["runtime"]["uncertain"]
    assert h.data["input_boolean.energy_compass_deye_restore_pending"]["state"] == "on"
    assert not any(v == "Grid" or v == "Sell" for e, v in runner.writes)
    n = sum(e == bad for e, v in runner.writes)
    assert n >= 3
    runner.run()
    assert sum(e == bad for e, v in runner.writes) > n
    runner.drop.clear()
    runner.run()
    assert bad not in h.data[RT]["attributes"]["runtime"]["uncertain"]


def test_off_restores_then_releases(h):
    runner = Runner(h)
    runner.run()
    h.data[MODE]["state"] = "Off"
    runner.run()
    assert h.data["input_boolean.energy_compass_deye_restore_pending"]["state"] == "off"
    n = len(runner.writes)
    h.data[h.ctx["grid_entity"]]["state"] = "7"
    runner.run()
    assert len(runner.writes) == n


@pytest.mark.parametrize(
    "mutation", ["generation", "expiry", "off", "simulation", "writer"]
)
def test_mid_sequence_never_enables_obsolete(h, mutation):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, end_soc_kwh=20)
    runner = Runner(h)

    def hook(r, e, v):
        if e.endswith("_soc"):
            if mutation == "generation":
                h.data[P]["attributes"]["generated_at"] = "2026-09-19T09:41:00+00:00"
            elif mutation == "expiry":
                h.now = dt.datetime.fromisoformat(
                    h.data[CACHE]["attributes"]["snapshot"]["valid_until"]
                )
            elif mutation == "writer":
                h.data[h.ctx["old_writers"][0]]["state"] = "on"
            else:
                h.data[MODE]["state"] = "Off" if mutation == "off" else "Simulation"
            r.hook = None

    runner.hook = hook
    runner.run()
    assert not any(v == "Grid" for e, v in runner.writes)


def test_simulation_no_mutation_with_old_writer(h):
    h.data[MODE]["state"] = "Simulation"
    h.data[h.ctx["old_writers"][0]]["state"] = "on"
    r = Runner(h)
    r.run()
    rt = h.data[RT]["attributes"]["runtime"]
    assert rt["state"] == "CHARGE_PV" and rt["takeover_blocked"]
    assert not r.writes and not r.reads


@pytest.mark.parametrize(
    "soc,charge_v,discharge_v",
    [
        (10, 49.6, 49.6),
        (20, 51.2, 51.2),
        (30, 51.5, 51.5),
        (40, 52, 52),
        (50, 52.2, 52.2),
        (60, 52.3, 52.3),
        (70, 52.8, 52.8),
        (75, 52.9, 53),
        (80, 53.1, 53.1),
        (90, 53.6, 53.6),
        (95, 56, 54),
        (100, 58.4, 54.4),
    ],
)
def test_voltage_curves(h, soc, charge_v, discharge_v):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", end_soc_kwh=soc / 4)
    h.accept()
    assert h.decision()["target_voltage"] == charge_v
    h.data[CACHE]["attributes"]["snapshot"]["intervals"][0]["state"] = "DISCHARGE_GRID"
    assert h.decision()["target_voltage"] == discharge_v


def test_voltage_safety_and_pv_uncapped(h):
    h.data[MODE]["state"] = "Simulation"
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", end_soc_kwh=25)
    h.data[h.ctx["operation_entity"]]["state"] = "Voltage"
    h.accept()
    assert not h.decision()["valid"]
    h.data[CACHE]["attributes"]["snapshot"]["intervals"][0].update(
        state="CHARGE_PV", charge_kwh=0, end_soc_kwh=2.5
    )
    d = h.decision()
    assert d["valid"] and d["desired"][h.ctx["charge_entity"]] == 14


COUNTER = "sensor.inverter_deye_total_battery_discharge"


def voltage_sell(counter=True):
    """DISCHARGE_GRID of 2 kWh in Voltage mode, pack sagging below the curve target."""
    inputs = INPUTS | {"commissioned_battery_modes": ["Capacity", "Voltage"]}
    if counter:
        inputs |= {"discharge_energy_entity": COUNTER}
    h = Harness(inputs)
    h.set(COUNTER, "5600.0")
    h.data[h.ctx["operation_entity"]]["state"] = "Voltage"
    h.data[P]["attributes"]["intervals"][0].update(
        state="DISCHARGE_GRID", charge_kwh=0, discharge_kwh=2.0, end_soc_kwh=19.2
    )
    # 53.1 V curve target; 7 kW of discharge pulls the pack to 52.46 V.
    h.data[h.ctx["voltage_entity"]]["state"] = "524.6"
    return h


def test_voltage_sell_ends_on_discharged_energy_not_sag():
    h = voltage_sell()
    r = Runner(h)
    r.run()
    d = h.decision()
    sell = f"select.{PREFIX}{d['active_tou']}_charging"
    assert d["energy_mode"] and not d["target_reached"]
    assert d["desired"][h.ctx["discharge_entity"]] > 0
    assert d["desired"][sell] == "Sell" and d["target_voltage"] == 49.6
    assert h.data[RT]["attributes"]["runtime"]["slot_energy"]["discharge"] == 5600.0
    h.set(COUNTER, "5601.9")
    d = h.decision()
    assert not d["target_reached"] and d["discharged_kwh"] == 1.9
    h.set(COUNTER, "5601.96")
    r.run()
    d = h.decision()
    assert d["target_reached"] and d["desired"][h.ctx["discharge_entity"]] == 0
    assert d["desired"][sell] == "Disabled"
    assert h.data[RT]["attributes"]["runtime"]["reached_key"] == d["reached_key"]


def test_voltage_sell_without_fresh_counter_keeps_voltage_target():
    for h in [voltage_sell(counter=False), voltage_sell()]:
        if h.ctx["discharge_energy_entity"]:
            h.data[COUNTER]["last_reported"] = (
                h.now - dt.timedelta(seconds=121)
            ).isoformat()
        h.accept()
        d = h.decision()
        assert not d["energy_mode"] and d["target_reached"]
        assert d["target_voltage"] == 53.1
        assert d["desired"][h.ctx["discharge_entity"]] == 0


def test_voltage_sell_counts_from_start_of_each_row():
    h = voltage_sell()
    h.accept()
    h.set(RT, "ok", runtime={"slot_energy": {"key": ["old"], "discharge": 5590.0}})
    d = h.decision()
    assert d["discharged_kwh"] == 0 and not d["target_reached"]
    assert d["slot_energy"] == {"key": d["reached_key"], "discharge": 5600.0}


def test_generation_consistency_and_session(h):
    h.data[F]["attributes"]["generated_at"] = "other"
    assert h.accept() == {}
    h.data[F]["attributes"]["generated_at"] = h.data[P]["attributes"]["generated_at"]
    h.data["input_datetime.energy_compass_deye_session_start"]["attributes"][
        "timestamp"
    ] = h.now.timestamp()
    assert h.accept() == {}


def test_stale_and_future_reports(h):
    h.accept()
    e = h.ctx["voltage_entity"]
    h.data[e]["last_reported"] = (h.now - dt.timedelta(seconds=31)).isoformat()
    assert not h.decision()["valid"]
    h.data[e]["last_reported"] = (h.now + dt.timedelta(seconds=1)).isoformat()
    assert not h.decision()["valid"]


def test_retained_row_transition(h):
    h.accept()
    h.data[O]["state"] = "calculating"
    h.data[F]["state"] = "off"
    first = h.decision()["row_start"]
    h.now = h.now.replace(minute=45)
    for x in h.data.values():
        x["last_reported"] = h.now.isoformat()
    assert h.decision()["valid"] and h.decision()["row_start"] != first


@pytest.mark.parametrize(
    "clock,active", [(0, 1), (6, 2), (9, 3), (13, 4), (15, 5), (22, 6), (23, 6)]
)
def test_tou_boundaries(h, clock, active):
    h.now = h.now.replace(hour=clock, minute=0, second=0)
    assert h.decision()["active_tou"] == active


@pytest.mark.parametrize("fold", [0, 1])
def test_dst_uses_local_tou(h, fold):
    h.now = dt.datetime(
        2026, 10, 25, 2, 30, tzinfo=ZoneInfo("Europe/Warsaw"), fold=fold
    )
    assert h.decision()["active_tou"] == 1


def test_single_inactive_parameter_correction(h):
    r = Runner(h)
    r.run()
    r.writes.clear()
    r.reads.clear()
    entity = "number.inverter_deye_program_1_soc"
    h.data[entity]["state"] = "12"
    r.run()
    assert r.writes == [(entity, 10)] and len(r.reads) == 1


def test_identical_initial_profile_does_not_write(h):
    h.accept()
    d = h.decision()
    for e, v in d["desired"].items():
        h.data[e]["state"] = str(v)
    r = Runner(h)
    r.run()
    assert not r.writes and not r.reads


@pytest.mark.parametrize("rows", [None, [], [None], ["bad"], {}, [{"state": "HOLD"}]])
def test_malformed_rows_rejected(h, rows):
    h.data[P]["attributes"]["intervals"] = rows
    assert h.accept() == {}


def test_target_reached_latches_same_row(h):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, end_soc_kwh=12.5)
    r = Runner(h)
    r.run()
    assert h.data[RT]["attributes"]["runtime"]["reached_key"]
    h.data[h.ctx["soc_entity"]]["state"] = "49"
    d = h.decision()
    values = d["desired"]
    assert d["target_reached"]
    assert values[h.ctx["charge_entity"]] == 14
    assert values[h.ctx["grid_entity"]] == values[h.ctx["discharge_entity"]] == 0
    assert values["select.inverter_deye_program_3_charging"] == "Disabled"


def test_current_caps_and_ac_power(h):
    for state in ["CHARGE_GRID", "DISCHARGE_GRID", "CHARGE_PV"]:
        row = h.data[P]["attributes"]["intervals"][0]
        row.update(
            state=state,
            charge_kwh=100,
            discharge_kwh=100,
            pv_kwh=0,
            end_soc_kwh=20 if state == "CHARGE_GRID" else 5,
        )
        h.data.pop(CACHE, None)
        h.accept()
        d = h.decision()
        assert d["desired"][h.ctx["charge_entity"]] <= 18
        assert d["desired"][h.ctx["grid_entity"]] <= 16
        assert d["desired"][h.ctx["discharge_entity"]] <= 18
        assert d["desired"][h.ctx["charge_entity"]] * d["voltage"] <= 8000
        assert (
            d["desired"][h.ctx["discharge_entity"]] * d["voltage"] / h.ctx["eta"]
            <= 8000
        )


def test_restart_restores_before_fresh_forcing(h):
    r = Runner(h)
    r.run()
    h.data["input_boolean.energy_compass_deye_session"]["state"] = "off"
    r.ctx["trigger"] = {"id": "start"}
    r.writes.clear()
    r.run()
    assert not any(value in ["Grid", "Sell"] for e, value in r.writes)
    assert not h.decision()["valid"]


def test_cache_replaced_atomically(h):
    old = h.accept()
    h.data[P]["attributes"]["generated_at"] = "2026-09-19T09:41:59.123456+00:00"
    for entity in [O, F]:
        h.data[entity]["attributes"]["generated_at"] = h.data[P]["attributes"][
            "generated_at"
        ]
    new = h.accept()
    assert new["generated_at"] != old["generated_at"]
    assert (
        new["valid_until"] == old["valid_until"]
        and new["intervals"] == old["intervals"]
    )


def test_off_oldwriter_blocks_even_cleanup(h):
    h.data[MODE]["state"] = "Off"
    h.data["input_boolean.energy_compass_deye_restore_pending"]["state"] = "on"
    h.data[h.ctx["old_writers"][0]]["attributes"]["current"] = 1
    r = Runner(h)
    r.run()
    assert (
        not r.writes
        and h.data["input_boolean.energy_compass_deye_restore_pending"]["state"] == "on"
    )


def test_unconfirmed_equal_profile_not_claimed_confirmed(h):
    h.accept()
    d = h.decision()
    for e, v in d["desired"].items():
        h.data[e]["state"] = str(v)
    r = Runner(h)
    r.run()
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "unconfirmed"
    assert not r.writes and not r.reads


def test_real_register_mismatch_blocks_new_direction(h):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, end_soc_kwh=20)
    r = Runner(h)
    bad = "number.inverter_deye_program_1_soc"
    h.data[bad]["state"] = "10"
    r.raw[h.ctx["registers"][bad]["address"]] = 45
    r.drop.add(bad)
    r.run()
    assert not any(v == "Grid" for e, v in r.writes)
    assert bad in h.data[RT]["attributes"]["runtime"]["uncertain"]


def test_all_templates_compile_and_one_automation(h):
    def walk(value):
        if isinstance(value, str) and ("{{" in value or "{%" in value):
            h.env.parse(value)
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    package = yaml.safe_load(PACKAGE.read_text())
    walk(h.doc)
    walk(package)
    assert "automation" not in package
    assert h.doc["mode"] == "queued" and h.doc["max"] == 2
    assert not any(t.get("seconds") for t in h.doc["triggers"])
    assert len(h.ctx["registers"]) == 27


def test_register_map_matches_solarman_layout(h):
    expected = {
        INPUTS["charge_entity"]: {"address": 108, "scale": 1},
        INPUTS["discharge_entity"]: {"address": 109, "scale": 1},
        INPUTS["grid_entity"]: {"address": 128, "scale": 1},
    }
    for i in range(1, 7):
        for field, address, scale in [
            ("power", 153 + i, 10),
            ("voltage", 159 + i, 0.01),
            ("soc", 165 + i, 1),
            ("charging", 171 + i, 1),
        ]:
            expected[
                f"{'select' if field == 'charging' else 'number'}.{PREFIX}{i}_{field}"
            ] = {"address": address, "scale": scale}
    assert h.ctx["registers"] == expected


def test_installation_limits_come_from_inputs():
    h = Harness(INPUTS | {"max_current": 5, "max_power_w": 1000})
    h.accept()
    d = h.decision()
    assert d["state"] == "CHARGE_PV"
    assert 0 < d["desired"][INPUTS["charge_entity"]] <= 5
    assert all(d["desired"][f"number.{PREFIX}{i}_power"] <= 1000 for i in range(1, 7))


def test_missing_package_blocks_control(h):
    h.accept()
    h.data[TOU]["attributes"] = {}
    h.ctx["program_prefix"] = h.render(
        h.doc["actions"][0]["variables"]["program_prefix"]
    )
    assert not h.decision()["valid"]


def test_utc_duration_across_dst(h):
    h.now = dt.datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo("Europe/Warsaw"), fold=1)
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(
        start="2026-10-25T02:00:00+02:00",
        end="2026-10-25T03:00:00+01:00",
        state="CHARGE_GRID",
        charge_kwh=2,
        discharge_kwh=0,
        pv_kwh=0,
        end_soc_kwh=20,
    )
    h.data[P]["attributes"].update(
        intervals=[row],
        generated_at="2026-10-25T00:00:00+00:00",
        valid_until="2026-10-25T02:00:00+00:00",
    )
    for e in [O, F]:
        h.data[e]["attributes"]["generated_at"] = "2026-10-25T00:00:00+00:00"
    h.data["input_datetime.energy_compass_deye_session_start"]["attributes"][
        "timestamp"
    ] = timestamp("2026-10-24T00:00:00+00:00")
    for x in h.data.values():
        x["last_reported"] = h.now.isoformat()
    h.accept()
    d = h.decision()
    assert d["valid"] and d["desired"][h.ctx["grid_entity"]] == 1


def test_missing_plan_and_telemetry_entities(h):
    h.data.pop(P)
    assert h.accept() == {}
    assert not h.decision()["valid"]
    h.data.pop(h.ctx["voltage_entity"])
    assert not h.decision()["valid"]


def test_owned_to_simulation_requires_off_cleanup(h):
    r = Runner(h)
    r.run()
    h.data[MODE]["state"] = "Simulation"
    count = len(r.writes)
    r.run()
    assert h.data[MODE]["state"] == "Off" and len(r.writes) == count
    assert h.data[RT]["attributes"]["runtime"]["code"] == "simulation_blocked"
    r.run()
    assert h.data["input_boolean.energy_compass_deye_restore_pending"]["state"] == "off"
    h.data[MODE]["state"] = "Simulation"
    count = len(r.writes)
    r.run()
    assert h.data[MODE]["state"] == "Simulation" and len(r.writes) == count


@pytest.mark.parametrize("voltage", ["unknown", "610"])
@pytest.mark.parametrize("phase", ["first_cleanup_write", "last_enable"])
def test_cleanup_rechecks_fresh_baseline_before_enable_and_release(h, voltage, phase):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, end_soc_kwh=20)
    r = Runner(h)
    r.run()
    h.data[MODE]["state"] = "Off"
    changed = []

    def hook(runner, entity, value):
        if phase == "first_cleanup_write" or (
            entity == h.ctx["discharge_entity"] and value > 0
        ):
            h.data[h.ctx["voltage_entity"]]["state"] = voltage
            changed.append(len(runner.writes))
            runner.hook = None

    r.hook = hook
    r.run()
    assert changed
    fresh = h.decision()
    for entity, value in r.writes[changed[0] :]:
        if entity in [
            h.ctx["charge_entity"],
            h.ctx["discharge_entity"],
            h.ctx["grid_entity"],
        ]:
            assert value <= fresh["desired"][entity]
    if h.data["input_boolean.energy_compass_deye_restore_pending"]["state"] == "off":
        assert h.data[RT]["attributes"]["runtime"]["confirmed"] == fresh["desired"]
    else:
        assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "unconfirmed"


@pytest.mark.parametrize(
    "state,expected",
    [
        (
            "CHARGE_GRID",
            [49.6, 51.2, 51.5, 52, 52.2, 52.3, 52.8, 52.9, 53.1, 53.6, 56, 58.4],
        ),
        (
            "DISCHARGE_GRID",
            [49.6, 51.2, 51.5, 52, 52.2, 52.3, 52.8, 53, 53.1, 53.6, 54, 54.4],
        ),
    ],
)
@pytest.mark.parametrize(
    "index,soc", list(enumerate([10, 20, 30, 40, 50, 60, 70, 75, 80, 90, 95, 100]))
)
def test_voltage_normalized_desired_and_device_encoding(h, state, expected, index, soc):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state=state, end_soc_kwh=soc / 4)
    h.accept()
    d = h.decision()
    value = d["desired"]["number.inverter_deye_program_3_voltage"]
    assert value == expected[index]
    assert int(value / 0.01) == round(expected[index] * 100)


def test_truncated_native_deadline_stops_without_extending_exact_cache(h):
    exact = (h.now + dt.timedelta(seconds=40, microseconds=139992)).isoformat()
    h.data[P]["attributes"]["valid_until"] = exact
    snap = h.accept()
    cutoff = timestamp(exact) // 1
    h.now = dt.datetime.fromtimestamp(cutoff - 0.001, ZoneInfo("Europe/Warsaw"))
    for entity in h.ctx["telemetry_entities"]:
        h.data[entity]["last_reported"] = h.now.isoformat()
    assert h.decision()["valid"]
    h.now = dt.datetime.fromtimestamp(cutoff, ZoneInfo("Europe/Warsaw"))
    d = h.decision()
    assert not d["valid"] and d["state"] == "BASE"
    assert (
        h.data[CACHE]["attributes"]["snapshot"]["valid_until"]
        == exact
        == snap["valid_until"]
    )


@pytest.mark.parametrize("boundary", ["row", "coverage"])
def test_fractional_end_boundaries_stop_at_native_timer_second(h, boundary):
    snap = h.accept()
    exact = h.now.timestamp() + 40.4
    if boundary == "coverage":
        snap["coverage_end"] = exact
    else:
        snap["intervals"][0]["end"] = dt.datetime.fromtimestamp(
            exact, ZoneInfo("Europe/Warsaw")
        ).isoformat()
    h.now = dt.datetime.fromtimestamp(exact // 1, ZoneInfo("Europe/Warsaw"))
    for entity in h.ctx["telemetry_entities"]:
        h.data[entity]["last_reported"] = h.now.isoformat()
    assert not h.decision()["valid"]


def test_native_floor_deadline_cannot_enable_mid_transaction(h):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, end_soc_kwh=20)
    deadline = (h.now + dt.timedelta(seconds=20, microseconds=139992)).isoformat()
    h.data[P]["attributes"]["valid_until"] = deadline
    r = Runner(h)

    def hook(runner, entity, value):
        h.now = dt.datetime.fromtimestamp(
            timestamp(deadline) // 1, ZoneInfo("Europe/Warsaw")
        )
        for sensor in h.ctx["telemetry_entities"]:
            h.data[sensor]["last_reported"] = h.now.isoformat()
        runner.hook = None

    r.hook = hook
    r.run()
    assert not any(value == "Grid" for entity, value in r.writes)
    assert h.data[CACHE]["attributes"]["snapshot"]["valid_until"] == deadline


def test_voltage_calculations_remain_simulation_only(h):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, end_soc_kwh=20)
    h.data[h.ctx["operation_entity"]]["state"] = "Voltage"
    h.data[h.ctx["voltage_entity"]]["state"] = "520"
    h.data[MODE]["state"] = "Simulation"
    h.accept()
    simulation = h.decision()
    assert simulation["valid"] and simulation["state"] == "CHARGE_GRID"
    assert simulation["desired"][h.ctx["charge_entity"]] > 0
    assert simulation["target_voltage"] == 53.1
    h.data[MODE]["state"] = "Auto"
    automatic = h.decision()
    assert not automatic["valid"] and automatic["state"] == "BASE"
    assert "niezatwierdzony" in automatic["reason"]
    assert h.ctx["commissioned_battery_modes"] == ["Capacity"]
    relinquish = h.ctx["relinquish_current"]
    assert automatic["desired"][h.ctx["charge_entity"]] == relinquish
    assert automatic["desired"][h.ctx["discharge_entity"]] == relinquish
    assert automatic["desired"][h.ctx["grid_entity"]] == 0
    assert all(
        value == "Disabled"
        for entity, value in automatic["desired"].items()
        if entity.startswith("select.")
    )


@pytest.mark.parametrize("mode", ["Auto", "Off"])
def test_switch_to_uncommissioned_mode_stops_owned_profile(h, mode):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, end_soc_kwh=20)
    h.data[h.ctx["voltage_entity"]]["state"] = "520"
    r = Runner(h)
    r.run()
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "CHARGE_GRID"
    h.data[h.ctx["operation_entity"]]["state"] = "Voltage"
    h.data[MODE]["state"] = mode
    r.writes.clear()
    r.run()
    relinquish = h.ctx["relinquish_current"]
    assert not any(value in ["Grid", "Sell"] for entity, value in r.writes)
    assert all(
        value == 0 for entity, value in r.writes if entity == h.ctx["grid_entity"]
    )
    assert float(h.data[h.ctx["grid_entity"]]["state"]) == 0
    for entity in [h.ctx["charge_entity"], h.ctx["discharge_entity"]]:
        assert all(value in (0, relinquish) for e, value in r.writes if e == entity)
        assert float(h.data[entity]["state"]) == relinquish


@pytest.mark.parametrize("phase", ["forcing", "cleanup"])
def test_uncommissioned_mode_mid_sequence_never_enables_forcing(h, phase):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, end_soc_kwh=20)
    h.data[h.ctx["voltage_entity"]]["state"] = "520"
    h.data[h.ctx["voltage_entity"]]["state"] = "520"
    r = Runner(h)
    if phase == "cleanup":
        r.run()
        h.data[MODE]["state"] = "Off"
        r.writes.clear()

    def hook(runner, entity, value):
        h.data[h.ctx["operation_entity"]]["state"] = "Voltage"
        runner.hook = None

    r.hook = hook
    r.run()
    relinquish = h.ctx["relinquish_current"]
    assert not any(value in ["Grid", "Sell"] for entity, value in r.writes)
    assert all(
        value == 0 for entity, value in r.writes if entity == h.ctx["grid_entity"]
    )
    assert float(h.data[h.ctx["grid_entity"]]["state"]) == 0
    for entity in [h.ctx["charge_entity"], h.ctx["discharge_entity"]]:
        assert all(value in (0, relinquish) for e, value in r.writes if e == entity)
        assert float(h.data[entity]["state"]) == relinquish


@pytest.mark.parametrize("transition", ["direction", "cleanup"])
def test_total_charge_stops_before_grid_current_is_cleared(h, transition):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, end_soc_kwh=20)
    r = Runner(h)
    r.run()
    r.writes.clear()
    if transition == "cleanup":
        h.data[MODE]["state"] = "Off"
    else:
        h.data[CACHE]["attributes"]["snapshot"]["intervals"][0]["state"] = (
            "SELF_CONSUME"
        )
    r.run()
    charge_stop = r.writes.index((h.ctx["charge_entity"], 0))
    grid_clear = r.writes.index((h.ctx["grid_entity"], 0))
    assert charge_stop < grid_clear


def test_grid_charge_leaves_full_charge_ceiling_for_pv(h):
    # PV above forecast must reach the battery, not the export meter: only the
    # grid share follows the plan, total charging keeps the CHARGE_PV ceiling.
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(
        state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0.3, load_kwh=0.05, end_soc_kwh=20
    )
    h.accept()
    d = h.decision()
    values = d["desired"]
    assert values[h.ctx["charge_entity"]] == 14
    assert 0 < values[h.ctx["grid_entity"]] < values[h.ctx["charge_entity"]]
    assert values[h.ctx["discharge_entity"]] == 0
    assert values["select.inverter_deye_program_3_charging"] == "Grid"
    assert d["target_soc"] == 80 and d["target_voltage"] == 53.1
    assert "grid_current_limit_commissioned" not in h.ctx
    assert not d["warning"]


def test_grid_charge_below_forecast_surplus_still_charges_from_pv(h):
    # The first CHARGE_GRID interval of 23.09 15:00: planned charge fully
    # covered by forecast PV, so no grid share; the ceiling must stay full.
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(
        state="CHARGE_GRID",
        charge_kwh=0.527,
        pv_kwh=0.616,
        load_kwh=0.114,
        end_soc_kwh=11.014,
    )
    h.data[h.ctx["soc_entity"]]["state"] = "43"
    h.accept()
    values = h.decision()["desired"]
    assert values[h.ctx["charge_entity"]] == 14
    assert values[h.ctx["grid_entity"]] == 0
    assert values["select.inverter_deye_program_3_charging"] == "Disabled"


def test_grid_charge_at_full_soc_stops_charging(h):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(
        state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0.3, load_kwh=0.05, end_soc_kwh=25
    )
    h.data[h.ctx["soc_entity"]]["state"] = "100"
    h.accept()
    values = h.decision()["desired"]
    assert values[h.ctx["charge_entity"]] == values[h.ctx["grid_entity"]] == 0
    assert values["select.inverter_deye_program_3_charging"] == "Disabled"


@pytest.mark.parametrize("mode", ["Auto", "Simulation"])
def test_grid_charge_profile_identical_for_simulation(h, mode):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(
        state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0.3, load_kwh=0.05, end_soc_kwh=20
    )
    h.data[MODE]["state"] = mode
    h.accept()
    d = h.decision()
    assert d["desired"][h.ctx["charge_entity"]] == 14
    assert 0 < d["desired"][h.ctx["grid_entity"]] < 14


def test_grid_cap_zero_pv_substep_and_target_completion(h):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(
        state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, load_kwh=0.05, end_soc_kwh=20
    )
    h.accept()
    d = h.decision()
    assert d["desired"][h.ctx["charge_entity"]] == 14
    assert 0 < d["desired"][h.ctx["grid_entity"]] < 14
    row = h.data[CACHE]["attributes"]["snapshot"]["intervals"][0]
    row.update(pv_kwh=0.54)
    d = h.decision()
    assert d["desired"][h.ctx["charge_entity"]] == 14
    assert d["desired"][h.ctx["grid_entity"]] == 0
    assert d["desired"]["select.inverter_deye_program_3_charging"] == "Disabled"
    row.update(pv_kwh=0.3)
    h.data[h.ctx["soc_entity"]]["state"] = "80"
    d = h.decision()
    assert d["target_reached"]
    assert d["desired"][h.ctx["charge_entity"]] == 14
    assert d["desired"][h.ctx["grid_entity"]] == 0
    assert d["desired"]["select.inverter_deye_program_3_charging"] == "Disabled"


def test_grid_charge_transition_preserves_full_pv_charge(h):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(
        state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0.3, load_kwh=0.05, end_soc_kwh=20
    )
    r = Runner(h)
    r.run()
    assert float(h.data[h.ctx["charge_entity"]]["state"]) == 14
    assert 0 < float(h.data[h.ctx["grid_entity"]]["state"]) < 14
    row = h.data[CACHE]["attributes"]["snapshot"]["intervals"][0]
    row.update(state="CHARGE_PV", charge_kwh=0, end_soc_kwh=2.5)
    r.writes.clear()
    r.run()
    assert float(h.data[h.ctx["charge_entity"]]["state"]) == 14
    assert float(h.data[h.ctx["grid_entity"]]["state"]) == 0
    assert h.data["select.inverter_deye_program_3_charging"]["state"] == "Disabled"
    assert r.writes.index((h.ctx["charge_entity"], 0)) < r.writes.index(
        (h.ctx["grid_entity"], 0)
    )


@pytest.mark.parametrize(
    "voltage,charge,discharge", [(400, 18, 18), (540, 14, 14), (610, 13, 12)]
)
@pytest.mark.parametrize(
    "charge_kwh,discharge_kwh,end_soc_kwh,soc",
    [(0, 0, 2.5, 10), (0.0001, 0.0001, 25, 70), (100, 100, 20, 70)],
)
def test_pv_buffer_uses_safe_caps_independent_of_forecast(
    h, voltage, charge, discharge, charge_kwh, discharge_kwh, end_soc_kwh, soc
):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(
        state="CHARGE_PV",
        charge_kwh=charge_kwh,
        discharge_kwh=discharge_kwh,
        end_soc_kwh=end_soc_kwh,
    )
    h.data[h.ctx["voltage_entity"]]["state"] = str(voltage)
    h.data[h.ctx["soc_entity"]]["state"] = str(soc)
    h.accept()
    d = h.decision()
    values = d["desired"]
    assert d["valid"] and d["state"] == "CHARGE_PV"
    assert values[h.ctx["charge_entity"]] == charge
    assert values[h.ctx["discharge_entity"]] == discharge
    assert values[h.ctx["grid_entity"]] == 0
    for i in range(1, 7):
        assert values[f"select.inverter_deye_program_{i}_charging"] == "Disabled"
        assert values[f"number.inverter_deye_program_{i}_soc"] == 10
        assert values[f"number.inverter_deye_program_{i}_voltage"] == 49.6
        assert values[f"number.inverter_deye_program_{i}_power"] == 8000
    h.data[MODE]["state"] = "Off"
    off = h.decision()["desired"]
    relinquish = h.ctx["relinquish_current"]
    assert off[h.ctx["charge_entity"]] == off[h.ctx["discharge_entity"]] == relinquish
    assert off[h.ctx["grid_entity"]] == 0
    keep = lambda d: {
        k: v
        for k, v in d.items()
        if k.startswith(("number.inverter_deye_program_", "select."))
    }
    assert keep(off) == keep(values)


def test_pv_buffer_at_full_soc_only_stops_charging(h):
    h.data[h.ctx["soc_entity"]]["state"] = "100"
    h.accept()
    d = h.decision()
    assert d["valid"] and d["state"] == "CHARGE_PV"
    assert d["desired"][h.ctx["charge_entity"]] == 0
    assert d["desired"][h.ctx["discharge_entity"]] > 0
    assert d["desired"][h.ctx["grid_entity"]] == 0
    assert all(
        value == "Disabled"
        for entity, value in d["desired"].items()
        if entity.startswith("select.")
    )


@pytest.mark.parametrize("end_soc_kwh,soc", [(2.5, 84), (20, 84), (25, 10)])
def test_self_consume_sets_no_soc_floor(h, end_soc_kwh, soc):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(
        state="SELF_CONSUME", charge_kwh=0, discharge_kwh=0, end_soc_kwh=end_soc_kwh
    )
    h.data[h.ctx["soc_entity"]]["state"] = str(soc)
    h.accept()
    d = h.decision()
    values = d["desired"]
    assert d["valid"] and d["state"] == "SELF_CONSUME" and not d["target_reached"]
    assert values[h.ctx["discharge_entity"]] > 0
    assert values[h.ctx["charge_entity"]] > 0 and values[h.ctx["grid_entity"]] == 0
    for i in range(1, 7):
        assert values[f"number.inverter_deye_program_{i}_soc"] == 10
        assert values[f"number.inverter_deye_program_{i}_voltage"] == 49.6
        assert values[f"select.inverter_deye_program_{i}_charging"] == "Disabled"


def test_self_consume_buffers_pv_only_after_grid_charge(h):
    """Z CHARGE_GRID do SELF_CONSUME: najpierw pełne zatrzymanie ładowania, potem
    ładowanie wraca do capu, ale bez kierunku Grid i z prądem sieciowym 0 A."""
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, end_soc_kwh=20)
    r = Runner(h)
    r.run()
    r.writes.clear()
    h.data[CACHE]["attributes"]["snapshot"]["intervals"][0]["state"] = "SELF_CONSUME"
    r.run()
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "SELF_CONSUME"
    assert float(h.data[h.ctx["charge_entity"]]["state"]) > 0
    assert float(h.data[h.ctx["grid_entity"]]["state"]) == 0
    assert all(
        h.data[f"select.inverter_deye_program_{i}_charging"]["state"] == "Disabled"
        for i in range(1, 7)
    )


def test_self_consume_stops_charging_at_full_battery(h):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="SELF_CONSUME", charge_kwh=0, discharge_kwh=0, end_soc_kwh=25)
    h.data[h.ctx["soc_entity"]]["state"] = "100"
    h.accept()
    values = h.decision()["desired"]
    assert values[h.ctx["charge_entity"]] == 0 and values[h.ctx["discharge_entity"]] > 0


@pytest.mark.parametrize(
    "gate", ["stale_soc", "stale_voltage", "stale_heartbeat", "Voltage"]
)
def test_pv_buffer_safety_gate_releases_control(h, gate):
    r = Runner(h)
    r.run()
    assert float(h.data[h.ctx["discharge_entity"]]["state"]) > 0
    if gate == "Voltage":
        h.data[h.ctx["operation_entity"]]["state"] = "Voltage"
    else:
        entity = {
            "stale_soc": h.ctx["soc_entity"],
            "stale_voltage": h.ctx["voltage_entity"],
            "stale_heartbeat": h.ctx["telemetry_entities"][-1],
        }[gate]
        h.data[entity]["last_reported"] = (h.now - dt.timedelta(seconds=31)).isoformat()
    r.writes.clear()
    r.run()
    d = h.decision()
    assert not d["valid"] and d["state"] == "BASE"
    relinquish = h.ctx["relinquish_current"]
    assert (
        d["desired"][h.ctx["grid_entity"]] == 0
        and float(h.data[h.ctx["grid_entity"]]["state"]) == 0
    )
    assert not any(value in ["Grid", "Sell"] for entity, value in r.writes)
    for entity in [h.ctx["charge_entity"], h.ctx["discharge_entity"]]:
        assert d["desired"][entity] == relinquish
        # Brama nie zeruje juz pradow baterii. Przy nieswiezej telemetrii GUARD
        # blokuje faze 'enable', wiec podniesienie do relinquish_current nie
        # zostaje zapisane i obowiazuje poprzedni dodatni cap.
        assert float(h.data[entity]["state"]) > 0
        assert not any(e == entity and value == 0 for e, value in r.writes)


def test_pv_buffer_hold_round_trip_and_ten_noops(h):
    r = Runner(h)
    r.run()
    desired = h.decision()["desired"]
    assert (
        desired[h.ctx["charge_entity"]] > 0 and desired[h.ctx["discharge_entity"]] > 0
    )
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "CHARGE_PV"
    row = h.data[CACHE]["attributes"]["snapshot"]["intervals"][0]
    row["state"] = "HOLD"
    r.run()
    currents = [h.ctx["charge_entity"], h.ctx["discharge_entity"], h.ctx["grid_entity"]]
    hold = {
        h.ctx["charge_entity"]: h.ctx["hold_grid_current"],
        h.ctx["discharge_entity"]: 0,
        h.ctx["grid_entity"]: h.ctx["hold_grid_current"],
    }
    assert all(float(h.data[entity]["state"]) == hold[entity] for entity in currents)
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "HOLD"
    row["state"] = "CHARGE_PV"
    r.writes.clear()
    r.reads.clear()
    r.run()
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "CHARGE_PV"
    assert h.data[RT]["attributes"]["runtime"]["confirmed"] == desired
    assert not any(value in ["Grid", "Sell"] for entity, value in r.writes)
    assert set(r.writes) >= {
        (h.ctx["charge_entity"], desired[h.ctx["charge_entity"]]),
        (h.ctx["discharge_entity"], desired[h.ctx["discharge_entity"]]),
    }
    assert float(h.data[h.ctx["grid_entity"]]["state"]) == 0
    count = len(r.writes)
    reads = len(r.reads)
    for _ in range(10):
        r.run()
    assert len(r.writes) == count and len(r.reads) == reads


def exporting_runner(h):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="DISCHARGE_GRID", discharge_kwh=0.5, pv_kwh=0, end_soc_kwh=5)
    runner = Runner(h)
    runner.run()
    runner.writes.clear()
    runner.reads.clear()
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "DISCHARGE_GRID"
    return runner


def publish_generation(h, entities=(P, O, F)):
    for entity in entities:
        h.data[entity]["attributes"]["generated_at"] = "2026-09-19T09:41:59+00:00"
    h.data[O]["state"] = "ready"


def test_plan_published_between_candidate_and_decision_keeps_export(h):
    runner = exporting_runner(h)
    h.data[O]["state"] = "calculating"

    def publish(r, action):
        if isinstance(action.get("variables", {}).get("decision"), str):
            publish_generation(h)
            r.before_action = None

    runner.before_action = publish
    runner.run()
    assert not runner.writes
    assert (
        h.data[CACHE]["attributes"]["snapshot"]["generated_at"]
        == "2026-09-19T09:41:59+00:00"
    )
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "DISCHARGE_GRID"


def test_split_publication_rechecks_before_any_inverter_write(h):
    runner = exporting_runner(h)
    publish_generation(h, (P, O))

    def finish(r, action):
        if "delay" in action:
            assert not r.writes
            publish_generation(h)
            r.before_action = None

    runner.before_action = finish
    runner.run()
    assert not runner.writes
    assert (
        h.data[CACHE]["attributes"]["snapshot"]["generated_at"]
        == "2026-09-19T09:41:59+00:00"
    )
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "DISCHARGE_GRID"


@pytest.mark.parametrize(
    "fault", ["persistent_mismatch", "alert", "expired", "stale", "off", "revoked"]
)
def test_handoff_retry_never_masks_safety_failure(h, fault):
    runner = exporting_runner(h)
    old = h.data[CACHE]["attributes"]["snapshot"]["generated_at"]
    publish_generation(h, (P, O))

    def fail(r, action):
        if "delay" not in action:
            return
        if fault == "alert":
            h.data[A]["state"] = "on"
        elif fault == "expired":
            h.data[CACHE]["attributes"]["snapshot"]["valid_until"] = h.now.isoformat()
        elif fault == "stale":
            h.data[h.ctx["voltage_entity"]]["last_reported"] = (
                h.now - dt.timedelta(seconds=31)
            ).isoformat()
        elif fault == "off":
            h.data[MODE]["state"] = "Off"
        elif fault == "revoked":
            h.data[RT]["attributes"]["runtime"]["revoked_generation"] = old
        r.before_action = None

    runner.before_action = fail
    runner.run()
    assert runner.delays and sum(runner.delays) <= 1
    assert (h.ctx["discharge_entity"], 0) in runner.writes
    assert ("select.inverter_deye_program_3_charging", "Disabled") in runner.writes
    assert not any(value == "Sell" for entity, value in runner.writes)
    assert h.data[CACHE]["attributes"]["snapshot"]["generated_at"] == old


@pytest.mark.parametrize("phase", ["target", "fresh"])
def test_late_publication_without_writes_defers_to_queued_run(h, phase):
    runner = exporting_runner(h)
    h.data[O]["state"] = "calculating"

    def publish(r, action):
        if phase in action.get("variables", {}):
            publish_generation(h)
            r.before_action = None

    runner.before_action = publish
    runner.run()
    assert not runner.writes
    runner.run()
    assert not runner.writes
    assert (
        h.data[CACHE]["attributes"]["snapshot"]["generated_at"]
        == "2026-09-19T09:41:59+00:00"
    )
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "DISCHARGE_GRID"


@pytest.mark.parametrize("phase", ["before_current", "after_current"])
def test_voltage_rebound_replans_lower_current_without_base(h, phase):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="DISCHARGE_GRID", discharge_kwh=2, pv_kwh=0, end_soc_kwh=5)
    h.data[h.ctx["voltage_entity"]]["state"] = "516.6"
    r = Runner(h)
    changed = []

    def rebound(runner, entity, value):
        if (phase == "before_current" and entity.endswith("_soc")) or (
            phase == "after_current"
            and entity == h.ctx["discharge_entity"]
            and value == 15
        ):
            h.data[h.ctx["voltage_entity"]]["state"] = "521.1"
            changed.append(len(runner.writes))
            runner.hook = None

    r.hook = rebound
    r.run()
    assert changed
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "DISCHARGE_GRID"
    assert h.data["select.inverter_deye_program_3_charging"]["state"] == "Sell"
    assert float(h.data[h.ctx["discharge_entity"]]["state"]) == 14
    assert not any(e == h.ctx["charge_entity"] and v > 0 for e, v in r.writes)
    assert all(
        v <= 14 for e, v in r.writes[changed[0] :] if e == h.ctx["discharge_entity"]
    )


def test_current_retry_never_raises_ceiling_when_voltage_returns(h):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="DISCHARGE_GRID", discharge_kwh=2, pv_kwh=0, end_soc_kwh=5)
    h.data[h.ctx["voltage_entity"]]["state"] = "516.6"
    r = Runner(h)
    changes = []

    def rebound(runner, entity, value):
        if entity == h.ctx["discharge_entity"] and value == 15 and not changes:
            h.data[h.ctx["voltage_entity"]]["state"] = "521.1"
            changes.append(15)
        elif entity == h.ctx["discharge_entity"] and value == 14 and changes == [15]:
            h.data[h.ctx["voltage_entity"]]["state"] = "516.6"
            changes.append(14)

    r.hook = rebound
    r.run()
    assert changes == [15, 14]
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "DISCHARGE_GRID"
    assert float(h.data[h.ctx["discharge_entity"]]["state"]) == 14
    assert not any(e == h.ctx["charge_entity"] and v > 0 for e, v in r.writes)


@pytest.mark.parametrize(
    "fault",
    [
        "alert",
        "expiry",
        "generation",
        "reached",
        "uncertain",
        "write_failure",
        "second_drop",
    ],
)
def test_current_retry_preserves_fail_closed_cleanup(h, fault):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="DISCHARGE_GRID", discharge_kwh=2, pv_kwh=0, end_soc_kwh=5)
    h.data[h.ctx["voltage_entity"]]["state"] = "516.6"
    r = Runner(h)
    changed = []

    def rebound(runner, entity, value):
        if entity == h.ctx["discharge_entity"] and value == 15 and not changed:
            h.data[h.ctx["voltage_entity"]]["state"] = "521.1"
            changed.append(True)
            if fault == "alert":
                h.data[A]["state"] = "on"
            elif fault == "expiry":
                h.data[CACHE]["attributes"]["snapshot"]["valid_until"] = (
                    h.now.isoformat()
                )
            elif fault == "generation":
                publish_generation(h)
            elif fault == "reached":
                h.data[h.ctx["soc_entity"]]["state"] = "20"
            elif fault == "uncertain":
                runner.raw[
                    h.ctx["registers"]["number.inverter_deye_program_1_soc"]["address"]
                ] = 45
            elif fault == "write_failure":
                runner.drop.add(h.ctx["discharge_entity"])
                runner.raw[h.ctx["registers"][h.ctx["discharge_entity"]]["address"]] = 0
        elif (
            fault == "second_drop"
            and changed
            and entity == h.ctx["discharge_entity"]
            and value == 14
        ):
            h.data[h.ctx["voltage_entity"]]["state"] = "560"
            runner.hook = None

    r.hook = rebound
    r.run()
    assert changed
    assert not any(v == "Sell" for e, v in r.writes)
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] != "DISCHARGE_GRID"


def test_publication_during_cap_retry_does_not_defer_written_transaction(h):
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="DISCHARGE_GRID", discharge_kwh=2, pv_kwh=0, end_soc_kwh=5)
    h.data[h.ctx["voltage_entity"]]["state"] = "516.6"
    r = Runner(h)
    attempts = []

    def rebound(runner, entity, value):
        if entity == h.ctx["discharge_entity"] and value == 15:
            h.data[h.ctx["voltage_entity"]]["state"] = "521.1"
            runner.hook = None

    def publish(runner, action):
        if "transaction_attempt" in action.get("variables", {}):
            attempts.append(True)
            if len(attempts) == 2:
                publish_generation(h)

    r.hook = rebound
    r.before_action = publish
    r.run()
    assert len(attempts) >= 2
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "BASE"
    assert (
        float(h.data[h.ctx["discharge_entity"]]["state"]) == h.ctx["relinquish_current"]
    )
    assert all(
        value <= 15
        for entity, value in r.writes
        if entity == h.ctx["discharge_entity"] and value != h.ctx["relinquish_current"]
    )
    assert not any(v == "Sell" for e, v in r.writes)


@pytest.mark.parametrize(
    "watts,expected",
    [
        (7900, 7900),
        (7950, 7900),
        (7999.99, 7900),
        (7999.999999999, 8000),
        (8000, 8000),
        (8100, 8000),
    ],
)
def test_power_uses_hundreds_without_float_boundary_drop(h, watts, expected):
    row = h.data[P]["attributes"]["intervals"][0]
    hours = (timestamp(row["end"]) - timestamp(row["start"])) / 3600
    row.update(
        state="DISCHARGE_GRID",
        discharge_kwh=watts * hours / 1000,
        pv_kwh=0,
        end_soc_kwh=5,
    )
    h.accept()
    d = h.decision()
    assert d["valid"]
    assert d["desired"]["number.inverter_deye_program_3_power"] == expected


@pytest.mark.parametrize("before,after", [(8000, 7999.999999999), (7950, 7999)])
def test_sub_hundred_power_change_keeps_export_enabled(h, before, after):
    row = h.data[P]["attributes"]["intervals"][0]
    hours = (timestamp(row["end"]) - timestamp(row["start"])) / 3600
    row.update(
        state="DISCHARGE_GRID",
        discharge_kwh=before * hours / 1000,
        pv_kwh=0,
        end_soc_kwh=5,
    )
    runner = Runner(h)
    runner.run()
    runner.writes.clear()
    row["discharge_kwh"] = after * hours / 1000
    publish_generation(h)
    runner.run()
    assert not runner.writes
    assert h.data["select.inverter_deye_program_3_charging"]["state"] == "Sell"


@pytest.mark.parametrize(
    "minutes,valid", [(11, True), (61, True), (119, True), (120, False)]
)
def test_fixed_two_hour_plan_lifetime_with_fresh_telemetry(h, minutes, valid):
    deadline = (h.now + dt.timedelta(hours=2)).isoformat()
    h.data[P]["attributes"]["valid_until"] = deadline
    h.data[P]["attributes"]["intervals"][-1]["end"] = (
        h.now + dt.timedelta(hours=3)
    ).isoformat()
    accepted = h.accept()
    assert accepted["valid_until"] == deadline
    h.now += dt.timedelta(minutes=minutes)
    for state in h.data.values():
        state["last_reported"] = h.now.isoformat()
    assert h.decision()["valid"] is valid
    assert h.data[CACHE]["attributes"]["snapshot"]["valid_until"] == deadline


@pytest.mark.parametrize("voltage", [400, 540, 610])
def test_base_relinquish_current_is_independent_of_voltage(h, voltage):
    """BASE oddaje sterowanie stałym limitem; cap 8 kW obowiązuje tylko przy wymuszaniu."""
    h.data[h.ctx["voltage_entity"]]["state"] = str(voltage)
    h.accept()
    h.data[MODE]["state"] = "Off"
    d = h.decision()
    relinquish = h.ctx["relinquish_current"]
    assert d["state"] == "BASE"
    assert d["desired"][h.ctx["charge_entity"]] == relinquish
    assert d["desired"][h.ctx["discharge_entity"]] == relinquish
    assert d["desired"][h.ctx["grid_entity"]] == 0


@pytest.mark.parametrize("gate", ["stale_soc", "stale_voltage", "stale_heartbeat"])
def test_base_relinquishes_even_without_telemetry(h, gate):
    """Cap zależy od telemetrii i spada do 0; profil oddania sterowania nie może."""
    h.accept()
    entity = {
        "stale_soc": h.ctx["soc_entity"],
        "stale_voltage": h.ctx["voltage_entity"],
        "stale_heartbeat": h.ctx["telemetry_entities"][-1],
    }[gate]
    h.data[entity]["last_reported"] = (h.now - dt.timedelta(seconds=31)).isoformat()
    d = h.decision()
    relinquish = h.ctx["relinquish_current"]
    assert not d["valid"] and d["state"] == "BASE"
    assert not d["telemetry"]
    assert d["desired"][h.ctx["charge_entity"]] == relinquish
    assert d["desired"][h.ctx["discharge_entity"]] == relinquish
    assert d["desired"][h.ctx["grid_entity"]] == 0
    for i in range(1, 7):
        assert d["desired"][f"select.inverter_deye_program_{i}_charging"] == "Disabled"
        assert d["desired"][f"number.inverter_deye_program_{i}_soc"] == 10
        assert d["desired"][f"number.inverter_deye_program_{i}_voltage"] == 49.6
        assert d["desired"][f"number.inverter_deye_program_{i}_power"] == 8000


def test_stale_telemetry_cannot_raise_currents_out_of_zero(h):
    """Luka: GUARD blokuje fazę 'enable' bez świeżej telemetrii, więc prądy
    zatrzaśnięte na 0 A zostają na 0 A aż telemetria wróci."""
    r = Runner(h)
    r.run()
    currents = [h.ctx["charge_entity"], h.ctx["discharge_entity"]]
    for e in currents:
        h.data[e]["state"] = "0"
    h.data[h.ctx["voltage_entity"]]["last_reported"] = (
        h.now - dt.timedelta(seconds=31)
    ).isoformat()
    r.writes.clear()
    r.run()
    assert (
        h.decision()["desired"][h.ctx["charge_entity"]] == h.ctx["relinquish_current"]
    )
    assert all(float(h.data[e]["state"]) == 0 for e in currents)
    assert not any(e in currents and value > 0 for e, value in r.writes)


def test_hold_zero_currents_recover_once_telemetry_returns(h):
    """HOLD zostawia prądy na 0 A. Przy zwietrzałej telemetrii wyjście z HOLD
    czeka, ale po jej powrocie prądy muszą wrócić do limitów nowego stanu."""
    r = Runner(h)
    r.run()
    row = h.data[CACHE]["attributes"]["snapshot"]["intervals"][0]
    row["state"] = "HOLD"
    r.run()
    currents = [h.ctx["charge_entity"], h.ctx["discharge_entity"]]
    assert float(h.data[h.ctx["discharge_entity"]]["state"]) == 0
    for e in currents:
        h.data[e]["state"] = "0"
    fresh = h.data[h.ctx["voltage_entity"]]["last_reported"]
    h.data[h.ctx["voltage_entity"]]["last_reported"] = (
        h.now - dt.timedelta(seconds=31)
    ).isoformat()
    row["state"] = "CHARGE_PV"
    r.writes.clear()
    r.run()
    assert all(float(h.data[e]["state"]) == 0 for e in currents)
    h.data[h.ctx["voltage_entity"]]["last_reported"] = fresh
    r.run()
    assert h.data[RT]["attributes"]["runtime"]["confirmed_mode"] == "CHARGE_PV"
    assert all(float(h.data[e]["state"]) > 0 for e in currents)


@pytest.mark.parametrize("state", ["HOLD", "CURTAIL"])
@pytest.mark.parametrize(
    "end_soc_kwh,soc,voltage",
    [
        (0, 10, 49.6),
        (12, 48, 52.1),
        (20, 80, 53.1),
        (21.512, 86, 53.4),
        (25, 100, 54.4),
    ],
)
def test_hold_freezes_battery_at_plan_soc_on_discharge_curve(
    h, state, end_soc_kwh, soc, voltage
):
    """HOLD: rozładowanie 0 A, ładowanie i sieć hold_grid_current; próg TOU celuje
    w SOC z planu zaokrąglony w dół (21,512 kWh -> 86%, nie 87%), po krzywej
    rozładowania, z podłogą 10%."""
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(
        state=state, charge_kwh=0, discharge_kwh=0, pv_kwh=0, end_soc_kwh=end_soc_kwh
    )
    h.accept()
    d = h.decision()
    band = h.ctx["hold_grid_current"]
    assert d["valid"] and d["state"] == state
    assert d["target_soc"] == soc and d["target_voltage"] == voltage
    assert d["desired"][h.ctx["charge_entity"]] == band
    assert d["desired"][h.ctx["discharge_entity"]] == 0
    assert d["desired"][h.ctx["grid_entity"]] == band
    assert (
        d["desired"][f"select.inverter_deye_program_{d['active_tou']}_charging"]
        == "Grid"
    )
    assert d["desired"][f"number.inverter_deye_program_{d['active_tou']}_soc"] == soc
    assert (
        d["desired"][f"number.inverter_deye_program_{d['active_tou']}_voltage"]
        == voltage
    )


@pytest.mark.parametrize("end_soc_kwh", [0, 1, 5, 10, 12.5, 18, 22, 24, 25, 40])
def test_hold_band_voltage_stays_inside_approved_range(h, end_soc_kwh):
    """Krzywa ładowania dawałaby 584 V przy 100%; rozładowania trzyma <=544 V."""
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(
        state="HOLD", charge_kwh=0, discharge_kwh=0, pv_kwh=0, end_soc_kwh=end_soc_kwh
    )
    h.accept()
    d = h.decision()
    assert 49.5 <= d["target_voltage"] <= 56.0
    assert 10 <= d["target_soc"] <= 100


def test_hold_is_not_latched_by_reached_target(h):
    """Zatrzask reached_key nie zmienia HOLD: prądy zostają 0 A, cel śledzi plan."""
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="HOLD", charge_kwh=0, discharge_kwh=0, pv_kwh=0, end_soc_kwh=12)
    h.accept()
    d = h.decision()
    h.data[RT]["attributes"]["runtime"] = {"reached_key": d["reached_key"]}
    again = h.decision()
    band = h.ctx["hold_grid_current"]
    assert not again["target_reached"]
    assert again["desired"][h.ctx["charge_entity"]] == band
    assert again["desired"][h.ctx["discharge_entity"]] == 0
    assert again["desired"][h.ctx["grid_entity"]] == band


def test_hold_band_threshold_change_zeroes_total_charge_before_grid(h):
    """Zmiana progu TOU nadal przechodzi przez pełne zatrzymanie: falownik nie
    reaguje na sam prąd sieciowy, więc całkowity prąd ładowania gaśnie pierwszy."""
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, end_soc_kwh=20)
    r = Runner(h)
    r.run()
    r.writes.clear()
    snapshot = h.data[CACHE]["attributes"]["snapshot"]["intervals"][0]
    snapshot.update(state="HOLD", end_soc_kwh=12)
    r.run()
    assert r.writes.index((h.ctx["charge_entity"], 0)) < r.writes.index(
        (h.ctx["grid_entity"], 0)
    )
    band = h.ctx["hold_grid_current"]
    assert float(h.data[h.ctx["charge_entity"]]["state"]) == band
    assert float(h.data[h.ctx["grid_entity"]]["state"]) == band
    assert (
        h.data[f"select.inverter_deye_program_{h.decision()['active_tou']}_charging"][
            "state"
        ]
        == "Grid"
    )


def retained_publication(h):
    h.data[O]["state"] = "calculating"
    h.data[P]["attributes"].update(refreshing=True, plan_retained=True)


def test_retained_publication_accepted_while_recalculating(h):
    retained_publication(h)
    assert h.accept()["generated_at"] == h.data[P]["attributes"]["generated_at"]
    assert h.decision()["valid"]


@pytest.mark.parametrize(
    "refreshing,retained", [(False, True), (True, False), (False, False)]
)
def test_calculating_requires_retained_refreshing_flags(h, refreshing, retained):
    h.data[O]["state"] = "calculating"
    h.data[P]["attributes"].update(refreshing=refreshing, plan_retained=retained)
    assert h.accept() == {}


@pytest.mark.parametrize("state", ["invalid_input", "error", "unavailable"])
def test_retained_publication_rejected_outside_ready_or_calculating(h, state):
    retained_publication(h)
    h.data[O]["state"] = state
    assert h.accept() == {}


def test_retained_publication_rejected_with_alert_or_invalid_forecast(h):
    retained_publication(h)
    h.data[A]["state"] = "on"
    assert h.accept() == {}
    h.data[A]["state"] = "off"
    h.data[F]["state"] = "off"
    assert h.accept() == {}


def test_plan_generated_before_revocation_is_never_accepted(h):
    g = h.data[P]["attributes"]["generated_at"]
    h.set(
        RT,
        "restored",
        runtime={
            "revoked_generation": "older",
            "revoked_at": (
                dt.datetime.fromisoformat(g) + dt.timedelta(seconds=1)
            ).isoformat(),
        },
    )
    assert h.accept() == {}
    retained_publication(h)
    assert h.accept() == {}
    h.data[RT]["attributes"]["runtime"]["revoked_at"] = (
        dt.datetime.fromisoformat(g) - dt.timedelta(seconds=1)
    ).isoformat()
    assert h.accept()["generated_at"] == g


def test_revocation_records_time(h):
    text = BLUEPRINT.read_text()
    assert text.count("else now().isoformat(), revoked_reason=") == 1


def _balance_row(h, state, **changes):
    row = h.data[P]["attributes"]["intervals"][0]
    defaults = {
        "state": state,
        "balance_hold": True,
        "charge_kwh": 0,
        "discharge_kwh": 0,
        "pv_kwh": 0,
        "load_kwh": 0.5,
        "curtail_kwh": 0,
        "end_soc_kwh": 24.75,
    }
    defaults.update(changes)
    row.update(**defaults)
    return row


def test_balance_grid_charge_at_full_soc_keeps_absorbing(h):
    _balance_row(h, "CHARGE_GRID")
    h.data[h.ctx["soc_entity"]]["state"] = "100"
    h.accept()
    d = h.decision()
    values = d["desired"]
    assert values[h.ctx["charge_entity"]] > 0
    assert values[h.ctx["grid_entity"]] == h.ctx["balance_grid_current"] == 2
    assert values[h.ctx["discharge_entity"]] == 0
    assert values["select.inverter_deye_program_3_charging"] == "Grid"
    assert values["number.inverter_deye_program_3_soc"] == 100
    assert not d["target_reached"]


def test_balance_grid_charge_keeps_a_larger_planned_grid_share(h):
    _balance_row(h, "CHARGE_GRID", charge_kwh=2.5, end_soc_kwh=24.75)
    h.data[h.ctx["soc_entity"]]["state"] = "95"
    h.accept()
    values = h.decision()["desired"]
    assert 2 < values[h.ctx["grid_entity"]] <= 16
    assert values["select.inverter_deye_program_3_charging"] == "Grid"


def test_balance_grid_charge_ignores_a_stale_latch(h):
    _balance_row(h, "CHARGE_GRID")
    h.data[h.ctx["soc_entity"]]["state"] = "100"
    h.accept()
    d = h.decision()
    h.data[RT]["attributes"]["runtime"] = {"reached_key": d["reached_key"]}
    again = h.decision()
    assert again["desired"] == d["desired"]
    assert not again["target_reached"]


def test_balance_grid_charge_targets_100_below_threshold_rounding(h):
    _balance_row(h, "CHARGE_GRID", end_soc_kwh=24.75)
    h.data[h.ctx["soc_entity"]]["state"] = "98"
    h.accept()
    values = h.decision()["desired"]
    assert values["number.inverter_deye_program_3_soc"] == 100


def test_balance_pv_at_full_soc_keeps_charge_limit(h):
    _balance_row(h, "CHARGE_PV", pv_kwh=3, load_kwh=1)
    h.data[h.ctx["soc_entity"]]["state"] = "100"
    h.accept()
    values = h.decision()["desired"]
    assert values[h.ctx["charge_entity"]] > 0
    assert values[h.ctx["discharge_entity"]] > 0
    assert values["select.inverter_deye_program_3_charging"] == "Disabled"


def test_balance_rows_are_stable_across_full_soc(h):
    _balance_row(h, "CHARGE_GRID")
    h.data[h.ctx["soc_entity"]]["state"] = "99"
    h.accept()
    first = h.decision()["desired"]
    h.data[h.ctx["soc_entity"]]["state"] = "100"
    assert h.decision()["desired"] == first


@pytest.mark.parametrize("value", ["yes", 1, None, [True]])
def test_non_boolean_balance_hold_rejected(h, value):
    h.data[P]["attributes"]["intervals"][0]["balance_hold"] = value
    assert h.accept() == {}


def _revocation(h):
    def walk(x):
        if isinstance(x, dict):
            value = x.get("variables", {}).get("runtime_update")
            if isinstance(value, str) and "revoked_reason" in value:
                return value
            x = list(x.values())
        if isinstance(x, list):
            for v in x:
                r = walk(v)
                if r is not None:
                    return r

    return h.render(walk(h.doc["actions"]))


def test_repeated_revocation_keeps_the_first_revocation_time(h):
    # While an error persists every run revokes again. Moving revoked_at to
    # each run's now() rejected a plan computed after the first revocation but
    # published while the alert was still on (sensors update one by one).
    g = h.accept()["generated_at"]
    first = (h.now - dt.timedelta(minutes=45)).isoformat()
    h.set(RT, "restored", runtime={"revoked_generation": g, "revoked_at": first})
    runtime = _revocation(h)
    assert runtime["revoked_generation"] == g
    assert runtime["revoked_at"] == first


def test_revoking_a_new_generation_records_the_current_time(h):
    g = h.accept()["generated_at"]
    old = (h.now - dt.timedelta(hours=5)).isoformat()
    h.set(RT, "ok", runtime={"revoked_generation": "older", "revoked_at": old})
    runtime = _revocation(h)
    assert runtime["revoked_generation"] == g
    assert dt.datetime.fromisoformat(runtime["revoked_at"]) == h.now
