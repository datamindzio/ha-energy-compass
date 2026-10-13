"""Pick the controller engine a scenario test runs against.

`test_deye_controller.py` runs every scenario against the generated blueprint and
the native controller; `test_controller_native_parity.py` runs it against the frozen
v0.1.37 blueprint ("oracle") and the native controller. All three expose the same
harness API; `use` selects the active one and `Harness`/`Runner` build from it.
"""

import copy

import deye_harness as blueprint
import deye_native_harness as native
import deye_oracle_v0137 as oracle

MODULES = {"blueprint": blueprint, "native": native, "oracle": oracle}
ControllerGone = (
    blueprint.ControllerGone,
    native.ControllerGone,
    oracle.ControllerGone,
)
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
