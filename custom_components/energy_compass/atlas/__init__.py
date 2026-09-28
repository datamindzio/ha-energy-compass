"""Opt-in Energy Atlas delivery glue (ADR-0019). Imported only when enabled."""

from .attrs_builder import build_attrs
from .bridge import AtlasBridge
from .storage import forget_entry, is_registered

__all__ = ["AtlasBridge", "build_attrs", "forget_entry", "is_registered"]
