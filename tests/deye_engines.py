"""Pick the controller engine a scenario test runs against.

`test_deye_controller.py` runs every scenario against the generated blueprint and
the native controller. Both expose the same harness API; `use` selects the active one
and `Harness`/`Runner` build from it.
"""

import copy

import deye_harness as blueprint
import deye_native_harness as native

MODULES = {"blueprint": blueprint, "native": native}
ControllerGone = (blueprint.ControllerGone, native.ControllerGone)
_active = ["blueprint"]
# A list while a parity run records what each harness decided and did.
TRACE = None
_traced = {}


def _normalized_runtime(harness):
    runtime = copy.deepcopy(harness.data[native.RT]["attributes"]["runtime"])
    runtime.pop("reason_code", None)
    return runtime


def _harness_class(name):
    base = MODULES[name].Harness

    class Traced(base):
        def decision(self, *args, **kwargs):
            decision = super().decision(*args, **kwargs)
            if TRACE is not None:
                TRACE.append(("decision", copy.deepcopy(decision)))
            return decision

    return _traced.setdefault(("harness", name), Traced)


def _runner_class(name):
    base = MODULES[name].Runner

    class Traced(base):
        def run(self):
            result = super().run()
            if TRACE is not None:
                h = self.h
                TRACE.append(
                    (
                        "run",
                        list(self.writes),
                        list(self.reads),
                        len(self.runtime_calls),
                        _normalized_runtime(h),
                        h.restore_pending,
                        h.data[native.MODE]["state"],
                        {e: x["state"] for e, x in h.data.items()},
                    )
                )
            return result

    return _traced.setdefault(("runner", name), Traced)


def use(name):
    _active[0] = name


def active():
    return _active[0]


def Harness(*args, **kwargs):
    return _harness_class(_active[0])(*args, **kwargs)


def Runner(harness):
    return _runner_class(_active[0])(harness)


def is_native(harness):
    """True for the native harness: only its context carries the buffer limit."""
    return "min_buffer_current" in harness.ctx


# The full-pack current cap of the sample inverter (the harness entities' ceiling).
CURRENT_CAP = 14


def hold_grid(harness):
    """Grid charge current of a HOLD/CURTAIL row: the TOU floor holds natively (0 A)."""
    return 0 if is_native(harness) else harness.ctx["hold_grid_current"]


def hold_discharge(harness, blueprint=0):
    """Discharge limit of a HOLD/CURTAIL row (and of CHARGE_GRID): the cap natively."""
    return CURRENT_CAP if is_native(harness) else blueprint
