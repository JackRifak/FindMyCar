"""DINOv2 global-image embedder (HuggingFace transformers), patch-token GeM pooled.

This is the fairer DINOv2 baseline for place recognition: instead of relying
on the single CLS token, it pools the patch-token features in an AnyLoc-style
fashion, which better reflects the dense local visual evidence learned by
DINOv2 and makes the comparison against other retrieval backbones much more
honest.

Requires: pip install transformers torch pillow
"""
from __future__ import annotations

import cv2
import numpy as np

from fmc.vpr.embedders.base import BenchmarkEmbedder

_MODEL_CACHE: dict[str, tuple] = {}


class DINOv2Embedder(BenchmarkEmbedder):
    def __init__(self, model_name: str = "facebook/dinov2-small", device: str = "cpu", pool_p: float = 3.0):
        try:
            import torch
            from transformers import AutoImageProcessor, AutoModel
        except ImportError as e:
            raise ImportError(
                "DINOv2Embedder requires transformers and torch: "
                "pip install transformers torch"
            ) from e

        self._torch = torch
        self.device = device
        self.pool_p = float(pool_p)
        self.name = f"dinov2_{model_name.split('/')[-1]}"

        if model_name not in _MODEL_CACHE:
            processor = AutoImageProcessor.from_pretrained(model_name)
            model = AutoModel.from_pretrained(model_name).to(device).eval()
            _MODEL_CACHE[model_name] = (processor, model)
        self._processor, self._model = _MODEL_CACHE[model_name]
        self._dim = self._model.config.hidden_size

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, image: np.ndarray) -> np.ndarray:
        from PIL import Image

        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        inputs = self._processor(images=pil_img, return_tensors="pt").to(self.device)
        with self._torch.no_grad():
            outputs = self._model(**inputs)
            tokens = outputs.last_hidden_state
            if tokens.shape[1] > 1:
                # DINOv2 includes the CLS token at index 0; for dense patch-token
                # pooling, ignore the CLS and aggregate the remaining patch tokens.
                tokens = tokens[:, 1:, :]
            gem = tokens.pow(self.pool_p).mean(dim=1).pow(1.0 / self.pool_p)
            gem = gem / gem.norm(dim=-1, keepdim=True)
        return gem.squeeze(0).cpu().numpy().astype(np.float32)
