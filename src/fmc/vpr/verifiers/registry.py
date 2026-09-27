"""Factory + registry for benchmarkable geometric verifiers (Deliverable 5).

Each verifier module is imported lazily, inside its factory function, so a
missing optional dependency for one method never prevents benchmarking the
others -- scripts/benchmark_verifiers.py catches ImportError per-verifier
and just skips it with a message.
"""
from __future__ import annotations

from typing import Callable

_REGISTRY: dict[str, Callable[..., object]] = {}


def register(name: str):
    def _wrap(factory):
        _REGISTRY[name] = factory
        return factory

    return _wrap


def available_verifiers() -> list[str]:
    return sorted(_REGISTRY.keys())


def get_benchmark_verifier(name: str, **kwargs):
    if name not in _REGISTRY:
        raise KeyError(f"Unknown verifier '{name}'. Available: {available_verifiers()}")
    return _REGISTRY[name](**kwargs)


@register("orb_ransac")
def _orb(**kwargs):
    from fmc.vpr.verifiers.orb_verifier import ORBVerifier

    return ORBVerifier(**kwargs)


@register("superpoint_lightglue")
def _lightglue(**kwargs):
    from fmc.vpr.verifiers.lightglue_verifier import LightGlueVerifier

    return LightGlueVerifier(**kwargs)


@register("superpoint_superglue")
def _superglue(**kwargs):
    from fmc.vpr.verifiers.superglue_verifier import SuperGlueVerifier

    return SuperGlueVerifier(**kwargs)


@register("loftr")
def _loftr(**kwargs):
    from fmc.vpr.verifiers.loftr_verifier import LoFTRVerifier

    return LoFTRVerifier(**kwargs)
