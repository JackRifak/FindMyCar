"""Factory + registry for benchmarkable embedders (Deliverable 4).

Each embedder module is imported lazily, inside its factory function, so a
missing optional dependency for one model (e.g. torch not installed) never
prevents benchmarking the others -- scripts/benchmark_embedders.py catches
ImportError per-embedder and just skips it with a message.
"""
from __future__ import annotations

from typing import Callable

_REGISTRY: dict[str, Callable[..., object]] = {}


def register(name: str):
    def _wrap(factory):
        _REGISTRY[name] = factory
        return factory

    return _wrap


def available_embedders() -> list[str]:
    return sorted(_REGISTRY.keys())


def get_benchmark_embedder(name: str, **kwargs):
    if name not in _REGISTRY:
        raise KeyError(f"Unknown embedder '{name}'. Available: {available_embedders()}")
    return _REGISTRY[name](**kwargs)


@register("color_histogram")
def _color_histogram(**kwargs):
    from fmc.vpr.embedders.color_histogram import ColorHistogramBenchmarkEmbedder

    return ColorHistogramBenchmarkEmbedder(**kwargs)


@register("clip_vit_b32")
def _clip_vit_b32(**kwargs):
    from fmc.vpr.embedders.clip_embedder import CLIPEmbedder

    return CLIPEmbedder(model_name="ViT-B-32", pretrained="openai", **kwargs)


@register("clip_vit_l14")
def _clip_vit_l14(**kwargs):
    from fmc.vpr.embedders.clip_embedder import CLIPEmbedder

    return CLIPEmbedder(model_name="ViT-L-14", pretrained="openai", **kwargs)


@register("dinov2_small")
def _dinov2_small(**kwargs):
    from fmc.vpr.embedders.dinov2_embedder import DINOv2Embedder

    return DINOv2Embedder(model_name="facebook/dinov2-small", **kwargs)


@register("dinov2_base")
def _dinov2_base(**kwargs):
    from fmc.vpr.embedders.dinov2_embedder import DINOv2Embedder

    return DINOv2Embedder(model_name="facebook/dinov2-base", **kwargs)


@register("netvlad_resnet18")
def _netvlad_resnet18(**kwargs):
    from fmc.vpr.embedders.netvlad_embedder import NetVLADEmbedder

    return NetVLADEmbedder(backbone="resnet18", **kwargs)


@register("fusion_netvlad_dinov2_base")
def _fusion_netvlad_dinov2_base(**kwargs):
    from fmc.vpr.embedders.fusion_embedder import FusionEmbedder

    return FusionEmbedder(**kwargs)
