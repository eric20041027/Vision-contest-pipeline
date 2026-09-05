"""``--plugin``: import user modules (``projects/<contest>/...``) so they can register metrics
and converters.

This is the one extension point that lets a contest's official scorer or submission format live
outside ``src/vcp`` (spec 2.1): the module registers itself at import time, and every command
that takes ``--plugin`` imports it before touching a registry.
"""

from __future__ import annotations

import importlib

from vcp.core.errors import VcpError


def load_plugins(modules: list[str] | None) -> list[str]:
    """Import each module, returning the names actually imported."""
    loaded: list[str] = []
    for name in modules or []:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - surface the plugin's own error text
            raise VcpError(f"cannot import plugin {name!r}: {type(e).__name__}: {e}") from e
        loaded.append(name)
    return loaded
