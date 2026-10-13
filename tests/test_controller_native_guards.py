"""Two writers never: the native engine and a blueprint automation exclude each other."""

import json
from pathlib import Path

import pytest
import test_deye_controller_blueprint as blueprint
from controller_support import controller_of
from homeassistant.exceptions import ServiceValidationError
from native_site import native_site

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
COMPONENT = Path(__file__).parents[1] / "custom_components/energy_compass"
AUTOMATION = "automation.test_deye"
RUNTIME_SENSOR = "sensor.synthetic_deye_controller"


@pytest.fixture
def expected_lingering_timers() -> bool:
    return True


@pytest.fixture
async def site(hass, freezer):
    entry, inverter = await native_site(hass, freezer)
    yield entry, inverter


async def select_mode(hass, entry, mode):
    entity_id = hass.states.get("select.synthetic_deye_mode").entity_id
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": mode},
        blocking=True,
    )


async def test_the_native_engine_will_not_leave_off_beside_an_enabled_blueprint(
    hass, site
):
    entry, _ = site
    await blueprint.setup_controller(hass)
    assert hass.states.get(AUTOMATION).state == "on"
    for mode in ("Auto", "Simulation"):
        with pytest.raises(ServiceValidationError, match=AUTOMATION):
            await select_mode(hass, entry, mode)
    assert controller_of(entry).state.mode == "Off"
    await select_mode(hass, entry, "Off")


async def test_a_switched_off_blueprint_automation_does_not_block(hass, site):
    entry, _ = site
    await blueprint.setup_controller(hass)
    await hass.services.async_call(
        "automation", "turn_off", {"entity_id": AUTOMATION}, blocking=True
    )
    # It is still listed as using the blueprint, but it cannot write.
    from custom_components.energy_compass.controller_ha import blueprint_automations

    assert [e.entity_id for e in blueprint_automations(hass)] == [AUTOMATION]
    await select_mode(hass, entry, "Auto")
    assert controller_of(entry).state.mode == "Auto"


async def test_the_block_names_every_enabled_automation(hass, site):
    entry, _ = site
    await blueprint.setup_controller(hass)
    with pytest.raises(ServiceValidationError) as raised:
        await select_mode(hass, entry, "Auto")
    assert raised.value.translation_key == "controller_blueprint_active"
    assert raised.value.translation_placeholders == {"automation": AUTOMATION}


async def test_the_blueprint_engine_is_not_restricted(hass, controller_site):
    await blueprint.setup_controller(hass)
    controller = controller_of(controller_site)
    controller.async_set_mode("Auto")
    assert controller.state.mode == "Auto"


async def test_the_runtime_service_is_closed_to_the_native_engine(hass, site):
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            "energy_compass",
            "controller_runtime",
            {"controller": RUNTIME_SENSOR},
            blocking=True,
            return_response=True,
        )
    assert raised.value.translation_key == "controller_native"


async def test_the_runtime_service_still_serves_the_blueprint_engine(
    hass, controller_site
):
    result = await hass.services.async_call(
        "energy_compass",
        "controller_runtime",
        {"controller": RUNTIME_SENSOR},
        blocking=True,
        return_response=True,
    )
    assert "restore_pending" in result


def test_the_manifest_lists_solarman_and_logbook_after_the_energy_dependency():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert manifest["after_dependencies"] == ["energy", "logbook", "solarman"]
    assert manifest["iot_class"] == "calculated"


@pytest.mark.parametrize(
    "path", ["strings.json", "translations/en.json", "translations/pl.json"]
)
def test_the_guard_messages_exist_in_every_translation_file(path):
    exceptions = json.loads((COMPONENT / path).read_text())["exceptions"]
    assert "{automation}" in exceptions["controller_blueprint_active"]["message"]
    assert exceptions["controller_native"]["message"]
