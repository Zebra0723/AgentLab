"""Arm discovery.

Every module in this package is imported on first lookup, so an arm registers
itself simply by existing. Nothing outside this package holds a list of arms.
"""

from __future__ import annotations

import importlib
import pkgutil

from .base import Arm, RunResult, Task, register, registry

_discovered = False


def _discover() -> None:
    global _discovered
    if _discovered:
        return
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_") or info.name == "base":
            continue
        importlib.import_module(f"{__name__}.{info.name}")
    _discovered = True


def available() -> list[str]:
    """Every arm name the harness can run, discovered from the filesystem."""
    _discover()
    return sorted(registry())


def describe() -> dict[str, str]:
    _discover()
    return {name: (cls.description or "") for name, cls in sorted(registry().items())}


def get_arm(name: str) -> type[Arm]:
    _discover()
    arms = registry()
    if name not in arms:
        raise KeyError(f"unknown arm {name!r}; available: {', '.join(sorted(arms)) or 'none'}")
    return arms[name]


__all__ = ["Arm", "RunResult", "Task", "register", "registry", "available", "describe", "get_arm"]
