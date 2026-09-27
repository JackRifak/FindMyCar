"""DINOv2 global-image embedder (HuggingFace transformers), CLS-token pooled.

Self-supervised on fine-grained visual detail rather than semantic/caption
alignment -- generally the strongest of the general-purpose options for
instance-level place matching (telling two similar-looking bays apart),
which is exactly what CLIP tends to be weak at.

Requires: pip install transformers torch pillow
"""
from __future__ import annotations

import cv2
import numpy as np

from fmc.vpr.embedders.base import BenchmarkEmbedder

_MODEL_CACHE: dict[str, tuple] = {}


class DINOv2Embedder(BenchmarkEmbedder):
    def __init__(self, model_name: str = "facebook/dinov2-small", device: str = "cpu"):
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
            cls_embedding = outputs.last_hidden_state[:, 0, :]
            cls_embedding = cls_embedding / cls_embedding.norm(dim=-1, keepdim=True)
        return cls_embedding.squeeze(0).cpu().numpy().astype(np.float32)
