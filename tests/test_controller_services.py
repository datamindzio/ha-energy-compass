import json
from pathlib import Path

import pytest
from controller_support import controller_of, iso, publication, publish, rows
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import entity_registry as er

from custom_components.energy_compass import controller as core

pytestmark = pytest.mark.usefixtures("recorder_mock")

COMPONENT = Path(__file__).parents[1] / "custom_components/energy_compass"
CONTROLLER = "sensor.synthetic_deye_controller"
MODE = "select.synthetic_deye_mode"
PACKAGE_SESSION = 1791471600.0
PACKAGE_DOMAINS = {
    "mode": "input_select",
    "session": "input_boolean",
    "session_start": "input_datetime",
    "restore_pending": "input_boolean",
    "snapshot": "sensor",
    "runtime": "sensor",
}


def add_package(hass, *, session="on", mode="Auto", pending="on", snapshot=True):
    registry = er.async_get(hass)
    ids = {}
    for role, (platform, unique_id) in core.PACKAGE_IDENTITIES.items():
        ids[role] = registry.async_get_or_create(
            PACKAGE_DOMAINS[role], platform, unique_id
        ).entity_id
    hass.states.async_set(ids["mode"], mode)
    hass.states.async_set(ids["session"], session)
    hass.states.async_set(
        ids["session_start"], "2026-10-08 17:00:00", {"timestamp": PACKAGE_SESSION}
    )
    hass.states.async_set(ids["restore_pending"], pending)
    generated = iso(17, 0, 45)
    hass.states.async_set(
        ids["snapshot"],
        generated,
        {
            "snapshot": {
                "schema": 1,
                "session": PACKAGE_SESSION,
                "accepted_at": iso(17, 0, 46),
                "generated_at": generated,
                "valid_until": iso(19),
                "coverage_end": 1.0,
                "intervals": rows(),
                "dispatch_policy": {"a": 1},
            }
            if snapshot
            else {}
        },
    )
    hass.states.async_set(
        ids["runtime"],
        "ok",
        {
            "runtime": {
                "code": "ok",
                "state": "CHARGE_PV",
                "owned_session": PACKAGE_SESSION,
                "confirmed": {"number.x": 1.0},
                "revoked_generation": iso(16, 30),
                "revoked_at": iso(16, 31),
                "revoked_reason": "r",
                "conservative_grid_charge": True,
            }
        },
    )
    return ids


async def call(hass, service, data, **kwargs):
    return await hass.services.async_call(
        "energy_compass",
        service,
        data,
        blocking=True,
        return_response=True,
        **kwargs,
    )


async def test_runtime_read_returns_runtime_pending_and_session(hass, controller_site):
    answer = await call(hass, "controller_runtime", {"controller": CONTROLLER})
    assert answer == {
        "runtime": {},
        "restore_pending": False,
        "session": controller_of(controller_site).session,
    }
    assert controller_of(controller_site).state.runtime_written_at is None


async def test_runtime_replace_stores_and_returns_the_validated_runtime(
    hass, controller_site
):
    runtime = {"code": "ok", "state": "HOLD", "uncertain": ["number.x"]}
    answer = await call(
        hass,
        "controller_runtime",
        {"controller": CONTROLLER, "runtime": runtime, "restore_pending": True},
    )
    assert answer["runtime"] == runtime
    assert answer["restore_pending"] is True
    controller = controller_of(controller_site)
    assert controller.state.runtime == runtime
    assert controller.state.runtime_written_at is not None
    assert hass.states.get(CONTROLLER).attributes["restore_pending"] is True
    again = await call(hass, "controller_runtime", {"controller": CONTROLLER})
    assert again["runtime"] == runtime


async def test_runtime_service_works_without_a_response_variable(hass, controller_site):
    await hass.services.async_call(
        "energy_compass",
        "controller_runtime",
        {"controller": CONTROLLER, "runtime": {"code": "ok"}},
        blocking=True,
    )
    assert controller_of(controller_site).state.runtime == {"code": "ok"}


async def test_unknown_runtime_key_is_a_validation_error(hass, controller_site):
    with pytest.raises(ServiceValidationError) as raised:
        await call(
            hass,
            "controller_runtime",
            {"controller": CONTROLLER, "runtime": {"revoked_at": "x"}},
        )
    assert raised.value.translation_key == "runtime_invalid_key"
    assert raised.value.translation_placeholders == {"key": "revoked_at"}
    assert controller_of(controller_site).state.runtime == {}


async def test_oversized_runtime_is_a_validation_error(hass, controller_site):
    with pytest.raises(ServiceValidationError) as raised:
        await call(
            hass,
            "controller_runtime",
            {"controller": CONTROLLER, "runtime": {"reason": "x" * 40000}},
        )
    assert raised.value.translation_key == "runtime_too_large"


@pytest.mark.parametrize(
    "entity_id",
    ["sensor.synthetic_deye_controller_runtime", MODE, "sensor.nonexistent"],
)
async def test_other_entities_are_not_controllers(hass, controller_site, entity_id):
    for service, extra in (
        ("controller_runtime", {}),
        ("controller_import_package", {}),
    ):
        with pytest.raises(ServiceValidationError) as raised:
            await call(hass, service, {"controller": entity_id, **extra})
        assert raised.value.translation_key == "controller_not_found"
        assert raised.value.translation_placeholders == {"entity": entity_id}


async def test_import_maps_the_package_and_adopts_its_session(hass, controller_site):
    add_package(hass)
    answer = await call(hass, "controller_import_package", {"controller": CONTROLLER})
    assert answer == {
        "mode": "Auto",
        "session": PACKAGE_SESSION,
        "restore_pending": True,
        "accepted_generation": iso(17, 0, 45),
        "revoked_generation": iso(16, 30),
        "runtime_keys": ["code", "confirmed", "owned_session", "state"],
        "dropped_keys": ["conservative_grid_charge"],
        "snapshot_kept": True,
    }
    controller = controller_of(controller_site)
    attrs = hass.states.get(CONTROLLER).attributes
    assert attrs["session"] == PACKAGE_SESSION == controller.session
    assert attrs["plan_reason"] == "ok"
    assert attrs["mode"] == "Auto"
    assert attrs["generation"] == iso(17, 0, 45)
    assert attrs["revoked_generation"] == iso(16, 30)
    assert controller.state.runtime["owned_session"] == controller.session
    assert controller.state.runtime_written_at is None
    assert controller.state.imported_at is not None
    assert hass.states.get(MODE).state == "Auto"
    assert hass.states.get("sensor.synthetic_deye_controller_runtime").state == "ok"
    assert [event.kind for event in controller.state.history][-1] == "imported"


async def test_a_later_publication_uses_the_adopted_session(hass, controller_site):
    add_package(hass)
    await call(hass, "controller_import_package", {"controller": CONTROLLER})
    controller = controller_of(controller_site)
    publish(controller_site, publication(iso(17, 0, 55)))
    assert controller.state.accepted["generated_at"] == iso(17, 0, 55)
    assert controller.state.accepted["session"] == PACKAGE_SESSION


async def test_import_without_a_usable_snapshot_reports_it(hass, controller_site):
    add_package(hass, snapshot=False)
    answer = await call(hass, "controller_import_package", {"controller": CONTROLLER})
    assert answer["snapshot_kept"] is False
    assert answer["accepted_generation"] is None
    assert hass.states.get(CONTROLLER).attributes["plan_reason"] == "session"


@pytest.mark.parametrize(
    "overrides, key",
    [
        ({"session": "off"}, "package_session_inactive"),
        ({"mode": "Dance"}, "package_mode_invalid"),
    ],
)
async def test_import_refusals_change_nothing(hass, controller_site, overrides, key):
    add_package(hass, **overrides)
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "controller_import_package", {"controller": CONTROLLER})
    assert raised.value.translation_key == key
    controller = controller_of(controller_site)
    assert controller.state.imported_at is None and controller.state.runtime == {}


async def test_import_reports_a_missing_helper(hass, controller_site):
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "controller_import_package", {"controller": CONTROLLER})
    assert raised.value.translation_key == "package_missing"
    assert raised.value.translation_placeholders == {"role": "mode"}


async def test_a_second_import_is_refused_even_before_any_runtime_write(
    hass, controller_site
):
    add_package(hass)
    await call(hass, "controller_import_package", {"controller": CONTROLLER})
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "controller_import_package", {"controller": CONTROLLER})
    assert raised.value.translation_key == "package_import_refused"
    assert controller_of(controller_site).state.runtime_written_at is None


async def test_import_is_refused_once_the_new_blueprint_wrote(hass, controller_site):
    add_package(hass)
    await call(hass, "controller_import_package", {"controller": CONTROLLER})
    await call(
        hass,
        "controller_runtime",
        {"controller": CONTROLLER, "runtime": {"code": "ok"}},
    )
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "controller_import_package", {"controller": CONTROLLER})
    assert raised.value.translation_key == "package_import_refused"
    forced = await call(
        hass, "controller_import_package", {"controller": CONTROLLER, "force": True}
    )
    assert forced["runtime_keys"] == ["code", "confirmed", "owned_session", "state"]
    assert controller_of(controller_site).state.runtime_written_at is None
    with pytest.raises(ServiceValidationError):
        await call(hass, "controller_import_package", {"controller": CONTROLLER})


async def test_import_needs_an_administrator(
    hass, controller_site, hass_read_only_user, hass_admin_user
):
    add_package(hass)
    with pytest.raises(Unauthorized):
        await call(
            hass,
            "controller_import_package",
            {"controller": CONTROLLER},
            context=Context(user_id=hass_read_only_user.id),
        )
    answer = await call(
        hass,
        "controller_import_package",
        {"controller": CONTROLLER},
        context=Context(user_id=hass_admin_user.id),
    )
    assert answer["mode"] == "Auto"


@pytest.mark.parametrize("path", ["strings.json", "translations/en.json"])
def test_english_exception_texts(path):
    data = json.loads((COMPONENT / path).read_text())
    exceptions = data["exceptions"]
    assert exceptions["controller_not_found"]["message"] == (
        "{entity} is not an enabled Energy Compass Deye controller."
    )
    assert exceptions["runtime_too_large"]["message"] == (
        "Controller runtime exceeds 32768 bytes."
    )
    assert exceptions["package_import_refused"]["message"] == (
        "The package state was already imported; set force to import again."
    )
    assert set(data["services"]) >= {"controller_runtime", "controller_import_package"}


def test_polish_exception_texts_exist_for_every_key():
    english = json.loads((COMPONENT / "translations/en.json").read_text())["exceptions"]
    polish = json.loads((COMPONENT / "translations/pl.json").read_text())
    assert set(polish["exceptions"]) == set(english)
    for key, value in polish["exceptions"].items():
        assert value["message"] and value["message"] != english[key]["message"]
        placeholders = {"{entity}", "{key}", "{role}"}
        assert {p for p in placeholders if p in english[key]["message"]} == {
            p for p in placeholders if p in value["message"]
        }
    assert polish["services"]["controller_runtime"]["name"] == (
        "Stan pracy sterownika Deye"
    )
    assert polish["services"]["controller_import_package"]["name"] == (
        "Importuj stan pakietu Deye"
    )


def test_services_yaml_declares_both_services():
    text = (COMPONENT / "services.yaml").read_text()
    assert "controller_runtime:" in text and "controller_import_package:" in text
