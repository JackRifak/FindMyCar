"""DINOv2 patch-token VLAD embedder, following the AnyLoc aggregation approach.

Patch descriptors are L2-normalized, clustered on the site's own reference
images, and aggregated with hard-assignment VLAD. This avoids applying GeM's
fractional power to signed transformer features and uses the same VLAD
aggregation implementation as the NetVLAD-style embedder.

Requires: pip install transformers torch pillow
"""
from __future__ import annotations

import cv2
import numpy as np

from fmc.vpr.embedders.base import BenchmarkEmbedder
from fmc.vpr.embedders.netvlad_embedder import aggregate_vlad, fit_vlad_centroids

_MODEL_CACHE: dict[tuple[str, str], tuple] = {}


class DINOv2Embedder(BenchmarkEmbedder):
    def __init__(
        self,
        model_name: str = "facebook/dinov2-base",
        device: str = "cpu",
        num_clusters: int = 64,
    ):
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
        self.num_clusters = num_clusters
        self.name = f"dinov2_{model_name.split('/')[-1]}"

        cache_key = (model_name, device)
        if cache_key not in _MODEL_CACHE:
            processor = AutoImageProcessor.from_pretrained(model_name)
            model = AutoModel.from_pretrained(model_name).to(device).eval()
            _MODEL_CACHE[cache_key] = (processor, model)
        self._processor, self._model = _MODEL_CACHE[cache_key]
        self._feature_dim = self._model.config.hidden_size
        self._dim = self.num_clusters * self._feature_dim
        self._centroids = None

    @property
    def dim(self) -> int:
        return self._dim

    def _extract_patch_descriptors(self, image: np.ndarray):
        from PIL import Image

        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        inputs = self._processor(images=Image.fromarray(rgb), return_tensors="pt").to(self.device)
        with self._torch.no_grad():
            tokens = self._model(**inputs).last_hidden_state
            if tokens.shape[1] <= 1:
                raise RuntimeError("DINOv2 returned no patch tokens")
            patches = tokens[0, 1:, :]
            return self._torch.nn.functional.normalize(patches, dim=1)

    def fit_clusters(self, sample_images: list[np.ndarray]) -> None:
        """Fit VLAD centers over DINOv2 patch descriptors from site images."""
        descriptor_batches = []
        for image in sample_images:
            descriptors = self._extract_patch_descriptors(image)
            descriptor_batches.append(descriptors.cpu().numpy())
        if not descriptor_batches:
            raise ValueError("At least one image is required to fit DINOv2 VLAD centers")
        centers = fit_vlad_centroids(descriptor_batches, self.num_clusters)
        self._centroids = self._torch.tensor(
            centers,
            dtype=self._torch.float32,
            device=self.device,
        )

    def embed(self, image: np.ndarray) -> np.ndarray:
        if self._centroids is None:
            raise RuntimeError(
                "DINOv2 VLAD centers are not fitted; call fit_clusters(sample_images) before embedding."
            )
        descriptors = self._extract_patch_descriptors(image)
        with self._torch.no_grad():
            vlad = aggregate_vlad(descriptors, self._centroids)
        embedding = vlad.cpu().numpy().astype(np.float32)
        if not np.isfinite(embedding).all():
            raise RuntimeError("DINOv2 VLAD produced a non-finite embedding")
        return embedding
