"""CLIP global-image embedder (open_clip), as a general-purpose baseline
against place-recognition-specific models (DINOv2, NetVLAD) per Deliverable 4.

CLIP was trained for image/caption alignment, not fine-grained instance
matching -- expect it to separate "this looks like a parking garage" well
but do worse than DINOv2/NetVLAD at telling two visually similar bays apart.

Requires: pip install open_clip_torch torch pillow
"""
from __future__ import annotations

import cv2
import numpy as np

from fmc.vpr.embedders.base import BenchmarkEmbedder

# Cache loaded models across multiple CLIPEmbedder instantiations within one
# process (the benchmark script only builds one per name, but this also
# protects ad-hoc reuse elsewhere).
_MODEL_CACHE: dict[str, tuple] = {}


class CLIPEmbedder(BenchmarkEmbedder):
    def __init__(self, model_name: str = "ViT-B-32", pretrained: str = "openai", device: str = "cpu"):
        try:
            import open_clip
            import torch
        except ImportError as e:
            raise ImportError(
                "CLIPEmbedder requires open_clip_torch and torch: "
                "pip install open_clip_torch torch"
            ) from e

        self._torch = torch
        self.device = device
        self.name = f"clip_{model_name}_{pretrained}"

        cache_key = f"{model_name}:{pretrained}:{device}"
        if cache_key not in _MODEL_CACHE:
            model, _, preprocess = open_clip.create_model_and_transforms(model_name, pretrained=pretrained)
            model = model.to(device).eval()
            _MODEL_CACHE[cache_key] = (model, preprocess)
        self._model, self._preprocess = _MODEL_CACHE[cache_key]
        self._dim = self._model.visual.output_dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, image: np.ndarray) -> np.ndarray:
        from PIL import Image

        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        tensor = self._preprocess(pil_img).unsqueeze(0).to(self.device)
        with self._torch.no_grad():
            features = self._model.encode_image(tensor)
            features = features / features.norm(dim=-1, keepdim=True)
        return features.squeeze(0).cpu().numpy().astype(np.float32)
