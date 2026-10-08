"""Load the Deye controller blueprint in Home Assistant itself and pin its shape."""

import importlib.util
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from deye_harness import Harness
from deye_oracle import BlueprintLoader, InputRef
from homeassistant.components import automation
from homeassistant.components.blueprint import models
from homeassistant.core import callback
from homeassistant.setup import async_setup_component
from homeassistant.util import yaml as yaml_util

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "deye_controller_build", ROOT / "tools/deye_controller/build.py"
)
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
BLUEPRINT = ROOT / "blueprints/automation/energy_compass/deye_solarman_controller.yaml"
PATH = "energy_compass/deye_solarman_controller.yaml"
CONTROLLER = "sensor.test_deye_controller"
INPUTS = {
    "controller_entity": CONTROLLER,
    "charge_entity": "number.inverter_deye_battery_max_charging_current",
    "discharge_entity": "number.inverter_deye_battery_max_discharging_current",
    "grid_entity": "number.inverter_deye_battery_grid_charging_current",
    "operation_entity": "select.inverter_deye_battery_operation_mode",
    "soc_entity": "sensor.inverter_deye_battery",
    "voltage_entity": "sensor.inverter_deye_battery_voltage",
    "telemetry_entities": ["sensor.inverter_deye_battery"],
}
EXPECTED_INPUTS = {
    "controller_entity",
    "charge_entity",
    "discharge_entity",
    "grid_entity",
    "operation_entity",
    "soc_entity",
    "voltage_entity",
    "telemetry_entities",
    "commissioned_battery_modes",
    "discharge_energy_entity",
    "voltage_grid_charge_ceiling",
    "hold_grid_current",
    "reached_discharge_current",
    "balance_grid_current",
    "max_power_w",
    "max_current",
    "max_grid_current",
    "relinquish_current",
    "old_writers",
}


@pytest.fixture
def expected_lingering_timers() -> bool:
    """The minute trigger keeps a timer."""
    return True


@contextmanager
def loaded_blueprint():
    original = models.DomainBlueprints._load_blueprint

    @callback
    def load(self, path):
        if path != PATH:
            return original(self, path)
        return models.Blueprint(
            yaml_util.load_yaml(BLUEPRINT),
            expected_domain=self.domain,
            path=path,
            schema=automation.config.AUTOMATION_BLUEPRINT_SCHEMA,
        )

    with patch.object(models.DomainBlueprints, "_load_blueprint", load):
        yield


async def setup_controller(hass, **inputs):
    with loaded_blueprint():
        assert await async_setup_component(
            hass,
            automation.DOMAIN,
            {
                automation.DOMAIN: {
                    "id": "test_deye",
                    "alias": "test_deye",
                    "use_blueprint": {"path": PATH, "input": INPUTS | inputs},
                }
            },
        )
    await hass.async_block_till_done()


def controller_state(hass, **attributes):
    hass.states.async_set(
        CONTROLLER,
        "2099-01-01T00:00:00+00:00",
        {
            "device_class": "timestamp",
            "controller_schema": 1,
            "mode_entity": "select.test_deye_mode",
            "program_prefix": "inverter_deye_program_",
            "device_id": "test_solarman_device",
            "plan_reason": "ok",
            **attributes,
        },
    )


def raw():
    return yaml.load(BLUEPRINT.read_text(), Loader=BlueprintLoader)


def plain(value):
    if isinstance(value, InputRef):
        return f"!input {value.name}"
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def test_committed_outputs_match_generator():
    stale = [
        path.relative_to(ROOT)
        for path, text in build.outputs().items()
        if path.read_text() != text
    ]
    assert not stale, "run python tools/deye_controller/build.py"


def test_package_file_is_gone():
    assert not (ROOT / "packages/energy_compass_deye.yaml").exists()
    assert set(build.outputs()) == {BLUEPRINT}


def test_the_blueprint_has_nineteen_inputs():
    sections = raw()["blueprint"]["input"]
    names = [name for section in sections.values() for name in section["input"]]
    assert len(names) == 19 and set(names) == EXPECTED_INPUTS
    selector = sections["energy_compass"]["input"]["controller_entity"]["selector"]
    assert selector == {
        "entity": {
            "filter": {
                "domain": "sensor",
                "integration": "energy_compass",
                "device_class": "timestamp",
            }
        }
    }


def test_trigger_table_is_exact():
    triggers = plain(raw()["triggers"])
    availability = [
        {
            "trigger": "state",
            "entity_id": "!input telemetry_entities",
            field: state,
            "id": "availability",
        }
        for field in ("from", "to")
        for state in ("unknown", "unavailable")
    ]
    assert triggers == [
        {"trigger": "homeassistant", "event": "start", "id": "start"},
        {"trigger": "time_pattern", "minutes": "/1", "id": "minute"},
        {
            "trigger": "state",
            "entity_id": ["!input controller_entity", "!input operation_entity"],
            "id": "change",
        },
        {"trigger": "state", "entity_id": "!input old_writers", "id": "change"},
        {
            "trigger": "state",
            "entity_id": [
                "!input soc_entity",
                "!input charge_entity",
                "!input discharge_entity",
                "!input grid_entity",
            ],
            "to": None,
            "id": "settings",
        },
        {"trigger": "time", "at": "!input controller_entity", "id": "timing"},
        *availability,
    ]


def test_no_template_reads_the_retired_helpers():
    text = "\n".join(strings(plain(raw()["actions"])))
    for retired in (
        "runtime_entity",
        "handoff_pending",
        "energy_compass_deye_",
        "wait_template",
        "cache_entity",
        "session_entity",
        "restore_entity",
    ):
        assert retired not in text, retired
    assert "wait_template" not in BLUEPRINT.read_text()


def walk(actions):
    for index, action in enumerate(actions):
        yield actions, index, action
        for key in ("then", "else", "default", "sequence"):
            if isinstance(action.get(key), list):
                yield from walk(action[key])
        if isinstance(action.get("repeat"), dict):
            yield from walk(action["repeat"].get("sequence", []))
        for option in action.get("choose", []) if isinstance(action, dict) else []:
            yield from walk(option["sequence"])


def test_every_persist_is_a_service_call_then_a_runtime_update():
    sites = 0
    for siblings, index, action in walk(plain(raw()["actions"])):
        if action.get("action") == "energy_compass.controller_runtime":
            sites += 1
            assert action["response_variable"] == "runtime_response"
            assert action["data"]["controller"] == "{{ controller_entity }}"
            follow = siblings[index + 1]["variables"]
            assert follow == {
                "runtime": "{{ runtime_response.runtime }}",
                "restore_pending": "{{ runtime_response.restore_pending }}",
            }
    assert sites >= 8


def test_restore_is_pending_before_the_first_write_and_cleared_after_cleanup():
    order = []
    for _, _, action in walk(plain(raw()["actions"])):
        service = action.get("action", "")
        if service == "energy_compass.controller_runtime":
            pending = action["data"].get("restore_pending")
            if pending is True:
                order.append("pending")
            elif pending is False:
                order.append("released")
        elif service in ("number.set_value", "select.select_option"):
            if action["target"]["entity_id"] != "{{ mode_entity }}":
                order.append("write")
    assert order[:2] == ["pending", "write"] and order.count("released") == 1
    released = [
        (condition, then)
        for _, _, node in walk(plain(raw()["actions"]))
        for condition, then in [(node.get("if"), node.get("then"))]
        if then
        and any(item.get("data", {}).get("restore_pending") is False for item in then)
    ]
    assert len(released) == 1
    assert released[0][0][0]["value_template"] == "{{ cleanup and profile_confirmed }}"


def test_simulation_refusal_selects_off_on_the_mode_entity():
    refusal = [
        node
        for _, _, node in walk(plain(raw()["actions"]))
        if node.get("if")
        and node["if"][0]["value_template"]
        == "{{ is_state(mode_entity,'Simulation') and restore_pending }}"
    ]
    assert len(refusal) == 1
    select = refusal[0]["then"][0]
    assert select["action"] == "select.select_option"
    assert select["target"] == {"entity_id": "{{ mode_entity }}"}
    assert select["data"] == {"option": "Off"}


def test_tou_values_come_from_the_controller():
    doc = plain(raw())
    changes = [t for t in doc["triggers"] if t["id"] == "change"]
    assert "!input controller_entity" in changes[0]["entity_id"]
    assert not any("tou" in str(trigger) for trigger in doc["triggers"])
    h = Harness()
    assert (
        h.data["sensor.energy_compass_home_pilot_deye_controller"]["attributes"]["tou"][
            "time.inverter_deye_program_6_time"
        ]
        == "22:00:00"
    )


def test_program_prefix_comes_from_energy_compass():
    h = Harness()
    h.prefix = "inverter_2_program_"
    for key, value in h.doc["actions"][0]["variables"].items():
        h.ctx[key] = h.render(value)
    assert h.ctx["program_prefix"] == "inverter_2_program_"
    programs = [e for e in h.ctx["registers"] if "program" in e]
    assert len(programs) == 24
    assert all(e.partition(".")[2].startswith("inverter_2_program_") for e in programs)
    assert not any("inverter_deye_program_" in e for e in h.ctx["registers"])


@pytest.mark.parametrize("old_writers", [None, ["automation.legacy_charging"]])
async def test_blueprint_instance_is_valid(hass, old_writers):
    controller_state(hass)
    inputs = {} if old_writers is None else {"old_writers": old_writers}
    await setup_controller(hass, **inputs)
    state = hass.states.get("automation.test_deye")
    assert state is not None and state.state == "on"


async def test_controller_change_runs_controller(hass):
    controller_state(hass)
    await setup_controller(hass)
    before = hass.states.get("automation.test_deye").attributes["last_triggered"]

    controller_state(hass, tou={"number.inverter_deye_program_4_power": "5000"})
    await hass.async_block_till_done()

    after = hass.states.get("automation.test_deye").attributes["last_triggered"]
    assert after is not None and after != before
