"""Entity resolution and the derived limits of the native controller."""

import pytest

from custom_components.energy_compass import controller as core
from custom_components.energy_compass import controller_setup as setup


def facts(*, omit=(), disabled=(), duplicate=()):
    items = []
    for role, (domain, key) in {**setup.ROLES, "energy": setup.ENERGY_ROLE}.items():
        if role in omit:
            continue
        items.append(
            core.TouFact(f"{domain}.{role}", domain, "solarman", key, role in disabled)
        )
        if role in duplicate:
            items.append(
                core.TouFact(f"{domain}.{role}_2", domain, "solarman", key, False)
            )
    items.append(core.TouFact("sensor.other", "sensor", "other", "battery", False))
    return items


def test_every_role_is_found_by_its_translation_key():
    resolved = setup.resolve(facts())
    assert resolved.missing == ()
    assert resolved.entities["charge"] == "number.charge"
    assert resolved.entities["operation"] == "select.operation"
    assert resolved.energy == "sensor.energy"


@pytest.mark.parametrize(
    "kwargs",
    [{"omit": ("grid",)}, {"disabled": ("grid",)}, {"duplicate": ("grid",)}],
)
def test_a_missing_disabled_or_ambiguous_entity_is_reported(kwargs):
    assert setup.resolve(facts(**kwargs)).missing == ("grid",)


def test_the_energy_counter_is_optional():
    resolved = setup.resolve(facts(omit=("energy",)))
    assert resolved.missing == () and resolved.energy == ""


def test_an_override_stands_in_for_a_renamed_key():
    resolved = setup.resolve(facts(omit=("soc",)), {"soc": "sensor.mine"})
    assert resolved.missing == () and resolved.entities["soc"] == "sensor.mine"


def derive(options=None, **kwargs):
    values = {
        "charge_kw": 5.0,
        "discharge_kw": 4.0,
        "charge_max": 350,
        "grid_max": 350,
        "resolved": setup.resolve(facts()),
    }
    return setup.derive_limits(options, **{**values, **kwargs})


def test_the_defaults_follow_the_settings_and_the_entity_ranges():
    limits = derive()
    assert limits.max_power_w == 4000
    # The power cap in amperes at the lowest accepted pack voltage.
    assert (
        limits.max_current == limits.relinquish_current == limits.max_grid_current == 10
    )
    assert limits.hold_grid_current == 1
    assert limits.reached_discharge_current == 1
    assert limits.balance_grid_current == 2
    assert limits.voltage_grid_charge_ceiling == 55.2
    assert limits.commissioned_battery_modes == ("Capacity",)
    assert limits.old_writers == ()
    assert limits.telemetry_entities == ("sensor.soc", "sensor.voltage")
    assert limits.discharge_energy_entity == "sensor.energy"


def test_an_entity_range_below_the_cap_wins_and_a_missing_one_falls_back_to_the_cap():
    limits = derive(charge_max=6, grid_max=None)
    assert limits.max_current == limits.relinquish_current == 6
    assert limits.max_grid_current == 10


def test_stored_options_override_every_default():
    limits = derive(
        {
            "max_power_w": 2000,
            "max_current": 3,
            "hold_grid_current": 0,
            "commissioned_battery_modes": ["Capacity", "Voltage"],
            "old_writers": ["automation.a"],
            "telemetry_entities": ["sensor.t"],
            "discharge_energy_entity": "",
            "voltage_grid_charge_ceiling": 54.5,
        }
    )
    assert limits.max_power_w == 2000
    assert limits.max_current == 3
    assert limits.relinquish_current == 5  # 2000 W at 400 V
    assert limits.hold_grid_current == 0
    assert limits.commissioned_battery_modes == ("Capacity", "Voltage")
    assert limits.old_writers == ("automation.a",)
    assert limits.telemetry_entities == ("sensor.t",)
    assert limits.discharge_energy_entity == ""
    assert limits.voltage_grid_charge_ceiling == 54.5


def test_the_engine_is_blueprint_unless_the_options_say_native():
    assert core.controller_engine(None) == "blueprint"
    assert core.controller_engine({"enabled": True}) == "blueprint"
    assert core.controller_engine({"engine": "native"}) == "native"
    assert core.controller_engine({"engine": "nonsense"}) == "blueprint"


def test_changing_the_engine_reloads_but_limits_do_not():
    old = {"enabled": True, "device_id": "d"}
    assert core.controller_options_change(old, {**old, "engine": "native"}) == "reload"
    native = {**old, "engine": "native"}
    assert (
        core.controller_options_change(native, {**native, "limits": {"max_current": 3}})
        == "none"
    )
    assert (
        core.controller_options_change(native, {**native, "device_id": "e"}) == "live"
    )
    assert core.controller_options_change(old, {**old, "engine": "blueprint"}) == "none"
