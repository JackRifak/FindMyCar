"""Vector similarity search over the reference image embedding index.

Loads embeddings.npz (built by fmc.dataset.build_index) and returns the
Top-K nearest reference images to a query embedding by cosine similarity.

This is a plain numpy brute-force implementation — fine for small/mock
datasets. Swap for FAISS/Qdrant/pgvector (per docs) once the reference
dataset is large enough that brute force is a bottleneck.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from fmc.config import TOP_K_CANDIDATES, SiteConfig
from fmc.dataset.schema import ReferenceImage, load_records


@dataclass
class Candidate:
    image_id: str
    similarity: float
    record: ReferenceImage


class VectorIndex:
    def __init__(self, ids: np.ndarray, embeddings: np.ndarray, records_by_id: dict[str, ReferenceImage]):
        self.ids = ids
        # Pre-normalize for cosine similarity via dot product.
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1e-8
        self.embeddings = embeddings / norms
        self.records_by_id = records_by_id

    @classmethod
    def load(cls, site: SiteConfig) -> "VectorIndex":
        data = np.load(site.embeddings_path, allow_pickle=True)
        records = {r.image_id: r for r in load_records(site.dataset_jsonl_path)}
        return cls(ids=data["ids"], embeddings=data["embeddings"], records_by_id=records)

    def query(self, embedding: np.ndarray, k: int = TOP_K_CANDIDATES) -> list[Candidate]:
        q = embedding / max(np.linalg.norm(embedding), 1e-8)
        similarities = self.embeddings @ q
        top_k_idx = np.argsort(-similarities)[:k]
        return [
            Candidate(
                image_id=str(self.ids[i]),
                similarity=float(similarities[i]),
                record=self.records_by_id[str(self.ids[i])],
            )
            for i in top_k_idx
        ]
