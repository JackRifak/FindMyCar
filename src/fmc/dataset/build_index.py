"""Build the VPR vector index from a site's dataset.jsonl.

Reads every registered reference image, computes an embedding for each
(current embedder is a placeholder — see fmc/vpr/embedder.py), and writes
embeddings.npz (id-aligned embedding matrix) for fast loading by VPR search.

Run again whenever the dataset changes OR the embedding model is swapped
(Deliverable 4 benchmark outcome).
"""
from __future__ import annotations

import hashlib

import cv2
import numpy as np

from fmc.config import SiteConfig
from fmc.dataset.schema import load_records
from fmc.vpr.embedder import get_embedder


def _dataset_version(dataset_jsonl_path) -> str:
    with open(dataset_jsonl_path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()[:12]


def build_index(site: SiteConfig) -> None:
    records = load_records(site.dataset_jsonl_path)
    if not records:
        raise RuntimeError(f"No dataset records found at {site.dataset_jsonl_path}")

    embedder = get_embedder(site)
    ids: list[str] = []
    vectors: list[np.ndarray] = []

    for record in records:
        img_path = site.processed_dir / record.processed_path
        img = cv2.imread(str(img_path))
        if img is None:
            print(f"WARNING: skipping unreadable image {img_path}")
            continue
        vectors.append(embedder.embed(img))
        ids.append(record.image_id)

    embedding_matrix = np.stack(vectors).astype(np.float32)
    site.index_dir.mkdir(parents=True, exist_ok=True)
    np.savez(
        site.embeddings_path,
        ids=np.array(ids),
        embeddings=embedding_matrix,
        dataset_version=_dataset_version(site.dataset_jsonl_path),
    )
    print(f"Built index: {len(ids)} images -> {site.embeddings_path}")


if __name__ == "__main__":
    import argparse

    from fmc.config import load_site_config

    parser = argparse.ArgumentParser()
    parser.add_argument("--site", default="mock_site")
    args = parser.parse_args()

    build_index(load_site_config(args.site))
