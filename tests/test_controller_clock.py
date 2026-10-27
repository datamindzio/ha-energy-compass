"""The inverter clock against Home Assistant time: skew, hold, issue, alert, texts."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from controller_support import controller_of, tick
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.energy_compass import controller_clock as clock
from custom_components.energy_compass.diagnostics import (
    async_get_config_entry_diagnostics,
)

WARSAW = ZoneInfo("Europe/Warsaw")
T0 = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
CLOCK = "datetime.inverter_deye_date_time"


def reading(offset, at=T0):
    """The entity as HA stores it: the inverter clock, reported at `at`."""
    return {"state": (at + timedelta(seconds=offset)).isoformat(), "reported": at}


def run(offsets, watch=None, start=T0, step=60):
    """Poll once per `step` seconds with the given skews; return every watch."""
    watch = watch or clock.ClockWatch()
    seen = []
    for index, offset in enumerate(offsets):
        at = start + timedelta(seconds=index * step)
        watch = clock.observe(watch, reading(offset, at), at, WARSAW)
        seen.append(watch)
    return seen


def test_under_the_threshold_is_never_an_alert():
    seen = run([299] * 30)
    assert not any(watch.active for watch in seen)
    assert seen[-1].over_since is None
    assert seen[-1].skew_seconds == 299


def test_exactly_the_threshold_is_not_over():
    assert run([clock.THRESHOLD_SECONDS] * 30)[-1].over_since is None


def test_over_the_threshold_alerts_only_after_the_hold():
    seen = run([2940] * 12)  # +49 min, polled every minute
    assert [watch.active for watch in seen] == [False] * 10 + [True] * 2
    assert seen[0].over_since == T0.isoformat()
    assert seen[10].skew_minutes == 49


def test_a_negative_skew_counts_the_same():
    assert run([-2940] * 12)[-1].active
    assert run([-2940] * 12)[-1].skew_minutes == -49


def test_one_bad_read_restarts_the_hold():
    seen = run([2940] * 9 + [10] + [2940] * 5)
    assert not any(watch.active for watch in seen)
    assert seen[9].over_since is None
    assert seen[10].over_since == (T0 + timedelta(minutes=10)).isoformat()


def test_a_read_under_the_threshold_ends_an_alert():
    seen = run([2940] * 12 + [20])
    assert seen[11].active and not seen[12].active
    assert seen[12].over_since is None
    assert seen[12].skew_seconds == 20


@pytest.mark.parametrize("state", ["unavailable", "unknown", "", "garbage", None])
def test_an_unreadable_state_changes_nothing(state):
    held = run([2940] * 12)[-1]
    later = T0 + timedelta(minutes=12)
    assert clock.observe(held, {"state": state, "reported": later}, later, WARSAW) == (
        held
    )


def test_a_stale_state_changes_nothing():
    held = run([2940] * 12)[-1]
    old = T0 + timedelta(minutes=11)
    now = old + timedelta(seconds=clock.STALE_SECONDS + 1)
    assert clock.observe(held, reading(0, old), now, WARSAW) == held


def test_a_missing_entity_starts_over():
    held = run([2940] * 12)[-1]
    assert clock.observe(held, None, T0, WARSAW) == clock.ClockWatch()


def test_inverter_on_summer_time_while_ha_is_on_winter_time_is_plus_an_hour():
    # 2026-10-27 is CET (UTC+1); the inverter stayed on CEST (UTC+2) and shows 15:00
    # on its panel when HA says 14:00. Solarman makes the panel time aware in the HA
    # zone, which is 15:00+01:00 = 14:00Z while it is 13:00Z.
    now = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
    panel = {"state": "2026-10-27T15:00:00", "reported": now}
    watch = clock.observe(clock.ClockWatch(), panel, now, WARSAW)
    assert watch.skew_seconds == 3600
    assert watch.skew_minutes == 60


def test_an_aware_state_is_taken_as_the_instant():
    now = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
    state = {"state": "2026-10-27T14:00:00+00:00", "reported": now}
    assert clock.observe(clock.ClockWatch(), state, now, WARSAW).skew_seconds == 3600


def test_the_attribute_names_the_diagnostics_fields():
    watch = run([2940] * 12)[-1]
    attribute = watch.as_attribute()
    assert attribute["clock_skew_seconds"] == 2940
    assert attribute["clock_read_at"] == (T0 + timedelta(minutes=11)).isoformat()
    assert attribute["inverter_clock"].startswith("2026-10-27T14:")
    assert attribute["alert"] is True
    assert clock.problem(watch) == "inverter_clock_skew"
    assert clock.problem(clock.ClockWatch()) is None


# --- in Home Assistant -------------------------------------------------------------


def add_clock(hass, entry, translation_key="date_time"):
    controller = controller_of(entry)
    entity = er.async_get(hass).async_get_or_create(
        "datetime",
        "solarman",
        "inverter_date_time",
        device_id=controller.device_id,
        translation_key=translation_key,
        suggested_object_id="inverter_deye_date_time",
    )
    return entity.entity_id


def report(hass, offset_seconds):
    """The Solarman poll: the inverter clock as UTC, offset from HA now."""
    now = datetime.now(UTC)
    hass.states.async_set(CLOCK, (now + timedelta(seconds=offset_seconds)).isoformat())


async def poll(hass, freezer, offset_seconds, minutes=1):
    for _ in range(minutes):
        await tick(hass, freezer, 60)
        report(hass, offset_seconds)
        await hass.async_block_till_done()


def issues(hass, entry):
    return ir.async_get(hass).async_get_issue(
        "energy_compass", f"deye_clock_skew_{entry.entry_id}"
    )


def alert_state(hass, entry):
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", "energy_compass", f"{entry.entry_id}_alert"
    )
    return hass.states.get(entity_id)


async def test_without_a_clock_entity_the_feature_is_silently_off(
    controller_site, hass, freezer
):
    attrs = controller_of(controller_site).view()[1]
    assert attrs["clock"] is None
    assert attrs["tou_problem"] is None
    await tick(hass, freezer, 3600)
    assert issues(hass, controller_site) is None
    assert alert_state(hass, controller_site).state == "off"
    diagnostics = await async_get_config_entry_diagnostics(hass, controller_site)
    assert diagnostics["deye_controller"]["clock"] is None


async def test_a_matching_clock_changes_nothing(controller_site, hass, freezer):
    assert add_clock(hass, controller_site) == CLOCK
    await poll(hass, freezer, 30, minutes=20)
    attrs = controller_of(controller_site).view()[1]
    assert attrs["clock"]["clock_skew_seconds"] == 30
    assert attrs["clock"]["alert"] is False
    assert attrs["tou_problem"] is None
    assert issues(hass, controller_site) is None
    assert alert_state(hass, controller_site).state == "off"


async def test_the_clock_entity_is_found_by_object_id_without_a_translation_key(
    controller_site, hass, freezer
):
    add_clock(hass, controller_site, translation_key=None)
    await poll(hass, freezer, 30)
    assert controller_of(controller_site).view()[1]["clock"] is not None


async def test_a_skew_alerts_after_the_hold_and_names_it_everywhere(
    controller_site, hass, freezer
):
    add_clock(hass, controller_site)
    await poll(hass, freezer, 2940, minutes=9)
    assert issues(hass, controller_site) is None
    assert alert_state(hass, controller_site).state == "off"
    assert controller_of(controller_site).view()[1]["tou_problem"] is None

    await poll(hass, freezer, 2940, minutes=2)
    attrs = controller_of(controller_site).view()[1]
    assert attrs["tou_problem"] == "inverter_clock_skew"
    assert attrs["clock"]["clock_skew_minutes"] == 49
    assert attrs["clock"]["alert"] is True

    issue = issues(hass, controller_site)
    assert issue.translation_key == "deye_clock_skew"
    assert issue.translation_placeholders["skew"] == "+49"
    assert issue.is_fixable is False

    alert = alert_state(hass, controller_site)
    assert alert.state == "on"
    assert alert.attributes["code"] == "inverter_clock_skew"
    assert alert.attributes["clock_skew_minutes"] == 49

    diagnostics = await async_get_config_entry_diagnostics(hass, controller_site)
    block = diagnostics["deye_controller"]
    assert block["tou_problem"] == "inverter_clock_skew"
    assert block["clock"]["clock_skew_seconds"] == 2940
    assert block["clock"]["clock_read_at"]
    assert block["clock"]["inverter_clock"]


async def test_recovery_clears_the_issue_and_the_alert(controller_site, hass, freezer):
    add_clock(hass, controller_site)
    await poll(hass, freezer, 2940, minutes=11)
    assert issues(hass, controller_site) is not None
    await poll(hass, freezer, 4)
    assert issues(hass, controller_site) is None
    assert alert_state(hass, controller_site).state == "off"
    attrs = controller_of(controller_site).view()[1]
    assert attrs["tou_problem"] is None
    assert attrs["clock"]["alert"] is False


async def test_dst_boundary_inverter_on_summer_time_is_plus_sixty(
    controller_site, hass, freezer
):
    freezer.move_to("2026-10-27T13:00:00+00:00")
    add_clock(hass, controller_site)
    for _ in range(12):
        await tick(hass, freezer, 60)
        # the panel shows 15:xx CEST, Solarman reads it in the HA zone (CET)
        panel = datetime.now(UTC).astimezone(WARSAW) + timedelta(hours=1)
        hass.states.async_set(CLOCK, panel.replace(tzinfo=None).isoformat())
        await hass.async_block_till_done()
    attrs = controller_of(controller_site).view()[1]
    assert attrs["clock"]["clock_skew_minutes"] == 60
    assert attrs["tou_problem"] == "inverter_clock_skew"
    assert issues(hass, controller_site).translation_placeholders["skew"] == "+60"


async def test_the_skew_changes_neither_the_plan_nor_the_writes(
    controller_site, hass, freezer
):
    from controller_support import publication, publish

    controller = controller_of(controller_site)
    publish(controller_site, publication())
    before = (controller.state.accepted, controller.state.mode)
    add_clock(hass, controller_site)
    await poll(hass, freezer, 2940, minutes=11)
    assert (controller.state.accepted, controller.state.mode) == before
