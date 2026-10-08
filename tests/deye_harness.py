"""Run the generated Deye blueprint against a sanitized state sample.

The blueprint reads Energy Compass through one controller sensor and stores its
runtime through a service. This harness plays Energy Compass: the plan, optimizer
status, validity and alert entities of the sample are the coordinator publication,
`controller.step` accepts or revokes it, and the controller sensor, mode select and
runtime entity are rebuilt from that state before every template renders.
"""

import copy
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from deye_oracle import (
    A,
    F,
    O,
    P,
    States,
    Stopped,
    load_blueprint,
    timestamp,
)
from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment

from custom_components.energy_compass import controller as core

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "blueprints/automation/energy_compass/deye_solarman_controller.yaml"
STATES = ROOT / "tests/fixtures/deye_controller/states.json"
CONTROLLER = "sensor.energy_compass_home_pilot_deye_controller"
MODE = "select.energy_compass_home_pilot_deye_mode"
RT = "sensor.energy_compass_home_pilot_deye_controller_runtime"
# The accepted snapshot lives in the controller state; tests edit it in place here.
CACHE = "controller.accepted_plan"
PREFIX = "inverter_deye_program_"
DEVICE = "test_solarman_device"
BATTERY = {
    "capacity_kwh": 25.0,
    "eta_charge": 0.9746794344808963,
    "eta_discharge": 0.9746794344808963,
    "charge_kw": 8.0,
    "discharge_kw": 8.0,
}
INPUTS = {
    "controller_entity": CONTROLLER,
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


class ControllerGone(Exception):
    """The controller service was called while Energy Compass is unavailable."""


class Harness:
    def __init__(self, inputs=INPUTS, path=BLUEPRINT):
        self.doc = load_blueprint(inputs, path)
        self.data = {
            x["entity_id"]: copy.deepcopy(x) for x in json.loads(STATES.read_text())
        }
        self.now = dt.datetime(2026, 9, 19, 11, 42, tzinfo=ZoneInfo("Europe/Warsaw"))
        for x in self.data.values():
            x["last_reported"] = self.now.isoformat()
        self.session = self.now.timestamp() - 180
        self.restore_pending = False
        self.revoked = None
        self.battery = dict(BATTERY)
        self.prefix = PREFIX
        # (state, attributes) of a controller sensor produced elsewhere.
        self.fixed_controller = None
        # None, "absent", "unavailable", "schema" or "no_mode_entity".
        self.controller_missing = None
        self.set(MODE, "Auto", options=["Off", "Simulation", "Auto"])
        self.set(RT, "waiting", runtime={})
        self.set(CACHE, "none", snapshot={})
        for entity in self.doc["actions"][0]["variables"]["old_writers"]:
            self.set(entity, "off", current=0)
        self.env = NativeEnvironment(undefined=StrictUndefined)
        self.env.globals.update(
            states=States(self.data),
            state_attr=lambda e, a: self.data.get(e, {}).get("attributes", {}).get(a),
            is_state=lambda e, s: self.data.get(e, {}).get("state") == s,
            now=lambda: self.now,
            as_timestamp=timestamp,
            is_number=lambda x: isinstance(x, (int, float, str)) and core.is_number(x),
            dict=dict,
        )
        self.compiled = {}
        self.ctx = {}
        self.sync()
        # Script variables render in order; later ones see the earlier ones.
        for key, value in self.doc["actions"][0]["variables"].items():
            self.ctx[key] = self.render(value)

    def set(self, entity, state, **attrs):
        self.data[entity] = {
            "state": state,
            "attributes": attrs,
            "last_reported": self.now.isoformat(),
        }

    def publication(self):
        """The coordinator data the plan, status, validity and alert entities show."""
        status = self.data[O]["state"]
        plan = self.data.get(P)
        data = {
            "status": status,
            "valid": self.data[F]["state"] == "on",
            "alert": {"code": "alert"} if self.data[A]["state"] == "on" else None,
            "controller_parameters": dict(self.battery),
        }
        if plan is not None:
            attrs = plan["attributes"]
            data |= {
                key: attrs.get(key)
                for key in (
                    "generated_at",
                    "valid_until",
                    "intervals",
                    "dispatch_policy",
                )
            }
            data["refreshing"] = attrs.get("refreshing", False)
            data["plan_retained"] = attrs.get("plan_retained", False)
        if status not in ("ready", "calculating") or self.data[A]["state"] == "on":
            data["reason"] = status
        return data

    def controller_state(self):
        snapshot = self.data[CACHE]["attributes"].get("snapshot") or None
        return core.ControllerState(
            mode=self.data[MODE]["state"],
            restore_pending=self.restore_pending,
            accepted=snapshot,
            revoked=self.revoked,
            runtime=self.data[RT]["attributes"].get("runtime", {}),
        )

    def sync(self):
        """Publish the controller sensor the blueprint reads (Energy Compass's job)."""
        state = self.controller_state()
        tou = {
            entity: x["state"]
            for entity, x in self.data.items()
            if "_program_" in entity
            and entity.startswith(("number.", "select.", "time."))
            and entity.split(".")[1].startswith(self.prefix)
            and not entity.endswith("_charging_raw")
        }
        if self.fixed_controller is not None:
            state, attrs = self.fixed_controller
            self.data[CONTROLLER] = {
                "state": state,
                "attributes": attrs,
                "last_reported": self.now.isoformat(),
            }
            return
        if self.controller_missing == "absent":
            self.data.pop(CONTROLLER, None)
            return
        if self.controller_missing == "unavailable":
            self.data[CONTROLLER] = {
                "state": "unavailable",
                "attributes": {},
                "last_reported": self.now.isoformat(),
            }
            return
        attrs = core.controller_attributes(
            state,
            self.publication(),
            session=self.session,
            now=self.now,
            mode_entity=None if self.controller_missing == "no_mode_entity" else MODE,
            fallback_battery=self.battery,
            tou=tou,
            device_id=DEVICE,
            program_prefix=self.prefix,
            tou_problem=None,
            next_tou_at=None,
        )
        if self.controller_missing == "schema":
            attrs["controller_schema"] = 2
        event = core.next_event(state.accepted, None, self.now)
        self.data[CONTROLLER] = {
            "state": event.isoformat() if event else "unknown",
            "attributes": attrs,
            "last_reported": self.now.isoformat(),
        }

    def render(self, template, **kwargs):
        if not isinstance(template, str):
            return template
        self.sync()
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
        """Let Energy Compass process the current publication; the new snapshot or {}."""
        if self.fixed_controller is not None:
            return {}
        before = self.controller_state()
        after = core.step(
            before,
            self.publication(),
            session=self.session,
            now=self.now,
            fallback_battery=self.battery,
        )
        self.revoked = after.revoked
        if after.accepted is before.accepted:
            return {}
        self.set(CACHE, after.accepted["generated_at"], snapshot=dict(after.accepted))
        return self.data[CACHE]["attributes"]["snapshot"]

    def restart(self):
        """A Core restart: a new session and no accepted plan."""
        self.session = self.now.timestamp()
        self.set(CACHE, "none", snapshot={})
        self.revoked = None

    def decision(self):
        self.ctx["runtime"] = self.data[RT]["attributes"].get("runtime", {})
        self.ctx["restore_pending"] = self.restore_pending
        return self.render(self.expression("decision"))


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
        self.runtime_calls = []
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
        # Energy Compass processes every publication before the blueprint runs.
        self.h.accept()
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
        h = self.h
        if service == "energy_compass.controller_runtime":
            if h.controller_missing:
                raise ControllerGone(service)
            self.runtime_calls.append(data)
            assert data["controller"] == CONTROLLER
            store = h.data[RT]["attributes"]
            if "runtime" in data:
                store["runtime"] = core.validate_runtime(data["runtime"])
                h.data[RT]["state"] = store["runtime"].get("code", "waiting")
            if "restore_pending" in data:
                assert isinstance(data["restore_pending"], bool)
                h.restore_pending = data["restore_pending"]
            self.ctx[action["response_variable"]] = {
                "runtime": copy.deepcopy(store.get("runtime", {})),
                "restore_pending": h.restore_pending,
                "session": h.session,
            }
        elif service == "select.select_option" and entity == MODE:
            h.data[MODE]["state"] = data["option"]
        elif service in ["number.set_value", "select.select_option"]:
            value = data.get("value", data.get("option"))
            self.writes.append((entity, value))
            h.data[entity]["state"] = str(value)
            register = h.ctx["registers"][entity]
            if entity not in self.drop:
                self.raw[register["address"]] = (
                    {"Disabled": 0, "Grid": 1, "Sell": 32}[value]
                    if service.startswith("select")
                    else int(float(value) / register["scale"])
                )
            if self.hook:
                self.hook(self, entity, value)
        elif service == "solarman.read_holding_registers":
            assert data["device"] == DEVICE
            self.reads.append(data["address"])
            self.ctx[action["response_variable"]] = {
                address: raw
                for address, raw in self.raw.items()
                if data["address"] <= address < data["address"] + data["count"]
            }
        else:
            raise AssertionError(service)
