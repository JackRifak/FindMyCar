"""NetVLAD-style embedder: VLAD aggregation over pretrained CNN conv features.

Honest caveat up front: true NetVLAD (Arandjelovic et al., CVPR 2016) trains
the CNN backbone AND the VLAD cluster centers jointly, end-to-end, on a
place-recognition dataset (Pittsburgh 250k / Mapillary SLS). Those trained
weights aren't pip-installable. This class supports two modes:

  - checkpoint_path=None (default): ImageNet-pretrained backbone conv
    features + cluster centers fit via k-means on a sample of THIS site's
    own reference images (call fit_clusters() once before embedding). This
    is a legitimate NetVLAD *architecture* (hard-assignment VLAD, a common
    simplification of the paper's soft-assignment) run in a weaker
    "self-clustered" regime. Good enough for an apples-to-apples aggregation
    comparison against CLIP/DINOv2 global pooling on this site's data, but
    don't report it as matching published NetVLAD accuracy numbers.
  - checkpoint_path=<path>: load real pretrained NetVLAD cluster centers
    (e.g. converted from the Mapillary-SLS/Pitts30k open-source releases)
    for a genuine comparison instead.

Requires: pip install torch torchvision scikit-learn
"""
from __future__ import annotations

import cv2
import numpy as np

from fmc.vpr.embedders.base import BenchmarkEmbedder

_BACKBONE_CACHE: dict[str, object] = {}


class NetVLADEmbedder(BenchmarkEmbedder):
    def __init__(
        self,
        num_clusters: int = 64,
        backbone: str = "resnet18",
        device: str = "cpu",
        checkpoint_path: str | None = None,
    ):
        try:
            import torch
            import torch.nn as nn
            import torchvision.models as models
        except ImportError as e:
            raise ImportError(
                "NetVLADEmbedder requires torch and torchvision: "
                "pip install torch torchvision"
            ) from e

        self._torch = torch
        self.device = device
        self.num_clusters = num_clusters
        self.name = f"netvlad_{backbone}_k{num_clusters}"

        cache_key = f"{backbone}:{device}"
        if cache_key not in _BACKBONE_CACHE:
            base = getattr(models, backbone)(weights="DEFAULT")
            # Strip the classification head -- keep the conv feature map (N, C, H, W)
            feature_extractor = nn.Sequential(*list(base.children())[:-2]).to(device).eval()
            _BACKBONE_CACHE[cache_key] = feature_extractor
        self._backbone = _BACKBONE_CACHE[cache_key]

        with torch.no_grad():
            dummy = torch.zeros(1, 3, 224, 224, device=device)
            feat_dim = self._backbone(dummy).shape[1]
        self._feat_dim = feat_dim
        self._dim = num_clusters * feat_dim

        if checkpoint_path is not None:
            state = torch.load(checkpoint_path, map_location=device)
            self._centroids = state["centroids"].to(device)
        else:
            self._centroids = None  # must call fit_clusters() before embed()

    @property
    def dim(self) -> int:
        return self._dim

    def fit_clusters(self, sample_images: list[np.ndarray]) -> None:
        """K-means over conv descriptors pooled from sample_images (e.g. a
        subset of the site's own reference photos) to initialize VLAD
        cluster centers when no pretrained checkpoint is supplied.
        Call once, before embed()."""
        from sklearn.cluster import KMeans

        descriptors = []
        with self._torch.no_grad():
            for img in sample_images:
                feat_map = self._extract_feature_map(img)  # (C, H, W)
                c, h, w = feat_map.shape
                descriptors.append(feat_map.reshape(c, h * w).T.cpu().numpy())
        descriptors = np.concatenate(descriptors, axis=0)
        km = KMeans(n_clusters=self.num_clusters, n_init=4, random_state=0).fit(descriptors)
        self._centroids = self._torch.tensor(km.cluster_centers_, dtype=self._torch.float32, device=self.device)

    def _extract_feature_map(self, image: np.ndarray):
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (224, 224))
        tensor = self._torch.from_numpy(resized).float().permute(2, 0, 1).unsqueeze(0) / 255.0
        tensor = tensor.to(self.device)
        return self._backbone(tensor).squeeze(0)  # (C, H, W)

    def embed(self, image: np.ndarray) -> np.ndarray:
        if self._centroids is None:
            raise RuntimeError(
                "NetVLADEmbedder has no cluster centers yet -- call "
                "fit_clusters(sample_images) once, or construct with "
                "checkpoint_path=<pretrained weights>, before embed()."
            )
        with self._torch.no_grad():
            feat_map = self._extract_feature_map(image)  # (C, H, W)
            c, h, w = feat_map.shape
            descriptors = feat_map.reshape(c, h * w).T  # (H*W, C)
            descriptors = self._torch.nn.functional.normalize(descriptors, dim=1)

            dists = self._torch.cdist(descriptors, self._centroids)  # (H*W, K)
            assignment = dists.argmin(dim=1)  # hard assignment to nearest centroid

            vlad = self._torch.zeros(self.num_clusters, c, device=self.device)
            for k in range(self.num_clusters):
                mask = assignment == k
                if mask.any():
                    residuals = descriptors[mask] - self._centroids[k]
                    vlad[k] = residuals.sum(dim=0)

            vlad = self._torch.nn.functional.normalize(vlad, dim=1)  # intra-normalization
            vlad = vlad.flatten()
            vlad = self._torch.nn.functional.normalize(vlad, dim=0)  # global L2 norm
        return vlad.cpu().numpy().astype(np.float32)
