"""Run the frozen v0.1.35 Deye blueprint against a sanitized state sample.

`tests/golden/deye_controller_v0135.yaml` is the byte-identical blueprint that read
the plan from helper entities. It is the oracle the integration-owned acceptance
must agree with (see `test_controller_parity.py` and `test_controller_migration.py`).
Do not teach this module about the new blueprint: that has its own harness.
"""

import copy
import datetime as dt
import json
import math
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import yaml
from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/golden/deye_controller_v0135.yaml"
GOLDEN_SHA256 = "fa5416f0701799785ef25a1deec0b46718ef65ef0426bdecb3f625476aaa0071"
STATES = ROOT / "tests/fixtures/deye_controller/states.json"
P = "sensor.energy_compass_home_pilot_plan"
O = "sensor.energy_compass_home_pilot_stan_optymalizatora"
F = "binary_sensor.energy_compass_home_pilot_poprawna_prognoza"
A = "binary_sensor.energy_compass_home_pilot_alert"
CACHE = "sensor.energy_compass_deye_plan"
RT = "sensor.energy_compass_deye_runtime"
MODE = "input_select.energy_compass_deye_mode"
TOU = "sensor.energy_compass_deye_tou_settings"
SESSION = "input_boolean.energy_compass_deye_session"
SESSION_START = "input_datetime.energy_compass_deye_session_start"
PENDING = "input_boolean.energy_compass_deye_restore_pending"
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


def load_blueprint(inputs=INPUTS, path=GOLDEN):
    """Return the automation config a blueprint instance with `inputs` produces."""
    doc = yaml.load(path.read_text(), Loader=BlueprintLoader)
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
    def __init__(self, inputs=INPUTS, path=GOLDEN):
        self.doc = load_blueprint(inputs, path)
        self.data = {
            x["entity_id"]: copy.deepcopy(x) for x in json.loads(STATES.read_text())
        }
        self.now = dt.datetime(2026, 9, 19, 11, 42, tzinfo=ZoneInfo("Europe/Warsaw"))
        for x in self.data.values():
            x["last_reported"] = self.now.isoformat()
        self.set(MODE, "Auto")
        self.set(SESSION, "on")
        self.set(PENDING, "off")
        self.set(
            SESSION_START,
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

    def revoke_block(self):
        """The `if` action that stamps a revocation: (condition, runtime template)."""
        for action in self.doc["actions"]:
            if "if" in action and "revoked_at" in json.dumps(action.get("then")):
                condition = action["if"][0]["value_template"]
                update = action["then"][0]["variables"]["runtime_update"]
                return condition, update
        raise AssertionError("no revoke block")

    def accept(self):
        value = self.render(self.expression("candidate"))
        self.set(CACHE, value.get("generated_at", "none"), snapshot=value)
        return value

    def decision(self):
        return self.render(self.expression("decision"))


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
