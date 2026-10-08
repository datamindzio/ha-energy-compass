from datetime import UTC, datetime, timedelta
from math import sqrt

import pytest
from controller_support import controller_site  # noqa: F401

from custom_components.energy_compass.engine.models import (
    Battery,
    Problem,
    SiteLimits,
    Slot,
)


@pytest.fixture
def negative_price_problem():
    start = datetime(2026, 9, 17, tzinfo=UTC)
    slots = tuple(
        Slot(
            start + timedelta(hours=index),
            start + timedelta(hours=index + 1),
            buy,
            sell,
            0.0,
            0.5,
        )
        for index, (buy, sell) in enumerate(((-0.50, -0.20), (0.40, 0.30)))
    )
    battery = Battery(
        25.0, 0.2, 1.0, 15.0, 8.0, 8.0, sqrt(0.88), sqrt(0.88), 0.14, True, True
    )
    return Problem(
        slots,
        SiteLimits(8.0, 8.0, 8.0, False),
        battery,
        "preserve_initial",
        0.0,
        (),
        "Europe/Warsaw",
    )


@pytest.fixture
def grid_only_problem():
    start = datetime(2026, 9, 17, tzinfo=UTC)
    slots = tuple(
        Slot(
            start + timedelta(hours=index),
            start + timedelta(hours=index + 1),
            0.60,
            -0.10,
            0.0,
            1.0,
        )
        for index in range(2)
    )
    battery = Battery(10.0, 0.2, 0.8, 5.0, 0.0, 0.0, 1.0, 1.0, 0.0, False, False)
    return Problem(
        slots,
        SiteLimits(0.0, 3.0, 0.0, False),
        battery,
        "preserve_initial",
        0.0,
        (),
        "Europe/Warsaw",
    )


@pytest.fixture(autouse=True)
def immediate_replan(request, monkeypatch):
    """Pin the immediate-rerun contract most lifecycle tests assert.

    The shipped minimum_replan_seconds pause has its own tests, marked
    replan_cooldown.
    """
    if request.node.get_closest_marker("replan_cooldown"):
        return
    from custom_components.energy_compass import settings

    spec = settings.NUMBERS["minimum_replan_seconds"]
    monkeypatch.setitem(
        settings.NUMBERS, "minimum_replan_seconds", (spec[0], 0, *spec[2:])
    )
