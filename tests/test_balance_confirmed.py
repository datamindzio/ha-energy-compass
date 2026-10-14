from datetime import UTC, datetime, timedelta

from custom_components.energy_compass.balance_tracker import (
    balance_settings,
    confirmed_percent,
    empty_state,
    observe,
)

START = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def _hold(confirmed):
    settings = balance_settings({"balance_hold_minutes": 60})
    state = empty_state()
    for minute in range(0, 70, 5):
        state = observe(
            state,
            confirmed_percent(99.9, confirmed),
            START + timedelta(minutes=minute),
            settings,
        )
    return state


def test_an_unconfirmed_top_never_starts_a_hold():
    state = _hold(False)
    assert state["hold_started_at"] is None
    assert state["last_completed_at"] is None


def test_a_confirmed_top_completes_the_hold():
    state = _hold(True)
    assert state["last_completed_at"] == (START + timedelta(minutes=60)).isoformat()
