"""In-process port registry for meinchat extension seams (S28.3a §2.5).

Mirrors the fe `checkoutSourceRegistry` / `paymentDataContributors` shape:
plugins register impls in `on_enable`, unregister in `on_disable`; meinchat
resolves them at call time.

- Single-impl ports use `resolve_first` — last-write-wins (the override
  registered after meinchat's default), falling back to the default.
- Multi-impl ports use `resolve_all` — chronological registration order.

One file, used by every port; no per-port duplication.
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional, Type, TypeVar

T = TypeVar("T")

_lock = threading.Lock()
_registry: Dict[type, List[object]] = {}


def register(port_cls: Type[T], impl: T) -> None:
    """Register an impl for a port. Idempotent on the same instance.

    Raises TypeError if `impl` does not structurally satisfy `port_cls`
    (the ports are `@runtime_checkable` Protocols).
    """
    if not isinstance(impl, port_cls):  # type: ignore[arg-type]
        raise TypeError(f"{type(impl).__name__} does not implement {port_cls.__name__}")
    with _lock:
        impls = _registry.setdefault(port_cls, [])
        if any(existing is impl for existing in impls):
            return
        impls.append(impl)


def unregister(port_cls: Type[T], impl: T) -> None:
    """Remove a previously-registered impl (plugin-on-disable counterpart)."""
    with _lock:
        impls = _registry.get(port_cls)
        if not impls:
            return
        _registry[port_cls] = [e for e in impls if e is not impl]


def resolve_first(port_cls: Type[T]) -> T:
    """Last-registered impl for a single-impl port.

    Raises LookupError when nothing (not even a default) is registered —
    meinchat registers every single-impl default in `on_enable`.
    """
    with _lock:
        impls = _registry.get(port_cls)
        if not impls:
            raise LookupError(f"no implementation registered for {port_cls.__name__}")
        return impls[-1]  # type: ignore[return-value]


def resolve_all(port_cls: Type[T]) -> List[T]:
    """All impls for a multi-impl port, in registration order."""
    with _lock:
        return list(_registry.get(port_cls, []))  # type: ignore[return-value]


def reset_for_tests(port_cls: Optional[type] = None) -> None:
    """Test teardown — wipe one port or the whole registry."""
    with _lock:
        if port_cls is None:
            _registry.clear()
        else:
            _registry.pop(port_cls, None)
