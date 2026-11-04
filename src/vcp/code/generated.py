"""The generated-code registry (spec 2026-10-31 §5.2): where frameworks write modules they build at
run time from the program's code and their own version.

``torch.compile`` and a JIT-built C++ extension leave Python files in a cache directory and import
them from there. Those files are not the run's code and not a third party's: they are derived, so a
snapshot neither reads them (``generated_exclusions``) nor counts importing one as a gap in the
trace. An entry is a name plus a resolver; the resolver reads its environment variable first, then
asks the framework if it is already loaded, then falls back to the framework's documented default.
Resolving never imports anything.

A training script that uses another framework adds an entry with ``register_generated``.
"""

from __future__ import annotations

import getpass
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["GeneratedEntry", "generated_locations", "register_generated"]

Resolver = Callable[[], Path | str | None]


@dataclass(frozen=True)
class GeneratedEntry:
    """``resolver`` gives the directory, or None when it cannot say. ``env`` names the variable
    that points the framework at it: only an entry with one can be an exclusion before the run,
    because only then is the location known to the wrapper."""

    name: str
    resolver: Resolver
    env: str | None = None


def _from_env(var: str) -> str | None:
    return os.environ.get(var) or None


def _loaded_attribute(module: str, attribute: str) -> Resolver | None:
    """``attribute`` of ``module`` when something has imported it already; never imports."""
    found = getattr(sys.modules.get(module), attribute, None)
    return found if callable(found) else None


def _torch_inductor() -> Path | str | None:
    explicit = _from_env("TORCHINDUCTOR_CACHE_DIR")
    if explicit:
        return explicit
    asked = _loaded_attribute("torch._inductor.runtime.cache_dir_utils", "cache_dir")
    if asked is not None:
        return asked()
    return Path(tempfile.gettempdir()) / f"torchinductor_{getpass.getuser()}"


def _torch_extensions() -> Path | str | None:
    explicit = _from_env("TORCH_EXTENSIONS_DIR")
    if explicit:
        return explicit
    asked = _loaded_attribute("torch.utils.cpp_extension", "get_default_build_root")
    return asked() if asked is not None else None


def _triton() -> Path | str | None:
    return _from_env("TRITON_CACHE_DIR") or Path.home() / ".triton" / "cache"


_REGISTRY: dict[str, GeneratedEntry] = {
    entry.name: entry
    for entry in (
        GeneratedEntry("torch_inductor", _torch_inductor, "TORCHINDUCTOR_CACHE_DIR"),
        GeneratedEntry("torch_extensions", _torch_extensions, "TORCH_EXTENSIONS_DIR"),
        GeneratedEntry("triton", _triton, "TRITON_CACHE_DIR"),
    )
}


def register_generated(name: str, resolver: Resolver, *, env: str | None = None) -> None:
    """Add a generated-code location, or replace the entry of the same name. The registry belongs
    to the process: a program registers its framework once, early."""
    _REGISTRY[name] = GeneratedEntry(name, resolver, env)


def _resolve(entry: GeneratedEntry) -> Path | None:
    try:
        found = entry.resolver()
    except Exception:
        # A framework's resolver is third-party code: one that cannot say where its cache is
        # (no user name, no home directory) must not hide the other entries.
        return None
    return Path(found) if found else None


def generated_locations(*, env_only: bool = False) -> dict[str, Path]:
    """Name -> directory of every registered location that can be resolved. With ``env_only``:
    only the entries whose variable is set, at the variable's value -- the locations the wrapper
    can know before the child runs, which is what a snapshot excludes."""
    found: dict[str, Path] = {}
    for name, entry in _REGISTRY.items():
        if env_only:
            value = _from_env(entry.env) if entry.env else None
            location = Path(value) if value else None
        else:
            location = _resolve(entry)
        if location is not None:
            found[name] = location
    return found
