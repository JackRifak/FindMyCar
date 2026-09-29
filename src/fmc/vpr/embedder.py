"""Image embedding model interface.

Production embedder selection: this project now uses a NetVLAD-style
place-recognition backbone rather than the placeholder color histogram.
The site-specific VLAD cluster centers are fit on the site's own reference
images before use so the model matches this environment instead of relying
on a generic pretrained setting.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import cv2
import numpy as np

from fmc.config import EMBEDDING_DIM


class Embedder(ABC):
    @abstractmethod
    def embed(self, image: np.ndarray) -> np.ndarray:
        """Return a fixed-length embedding vector (float32) for a BGR image."""
        raise NotImplementedError


class ColorHistogramEmbedder(Embedder):
    """Baseline placeholder: normalized HSV color histogram.

    Deliberately simple and fast so the pipeline (index build, VPR search,
    geometric verification, fusion) can be exercised end-to-end on the mock
    site without heavy ML dependencies (torch/CLIP etc.) installed yet.
    """

    def __init__(self, bins: tuple[int, int, int] = (8, 4, 2)):
        self.bins = bins
        assert bins[0] * bins[1] * bins[2] == EMBEDDING_DIM, (
            "Histogram bin product must match EMBEDDING_DIM in fmc.config"
        )

    def embed(self, image: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist(
            [hsv], [0, 1, 2], None, list(self.bins),
            [0, 180, 0, 256, 0, 256],
        )
        hist = cv2.normalize(hist, hist).flatten()
        return hist.astype(np.float32)


def _fit_netvlad_on_site(netvlad_embedder, site) -> None:
    from fmc.dataset.schema import load_records

    records = load_records(site.dataset_jsonl_path)
    sample_images = []
    max_samples = min(len(records), 40)
    for record in records[:max_samples]:
        img_path = site.processed_dir / record.processed_path
        image = cv2.imread(str(img_path))
        if image is not None:
            sample_images.append(image)

    if not sample_images:
        raise RuntimeError(f"No valid processed images found to fit NetVLAD on site '{site.site_id}'")

    netvlad_embedder.fit_clusters(sample_images)


def get_embedder(site=None) -> Embedder:
    """Return the production NetVLAD embedder, optionally fitting on the site."""
    from fmc.vpr.embedders.netvlad_embedder import NetVLADEmbedder

    embedder = NetVLADEmbedder(backbone="resnet18")
    if site is not None:
        _fit_netvlad_on_site(embedder, site)
    return embedder
