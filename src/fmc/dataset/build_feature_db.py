"""Build the offline 2D/3D feature database from the site's dataset.

This script processes every reference image in the dataset, extracts local features
(keypoints and descriptors, e.g., ORB, SuperPoint), and stores them in an HDF5
or structured NPZ database. 

Pre-computing these features means the real-time VPR pipeline (geometric_verification) 
no longer needs to load heavy images from disk or extract features on-the-fly, 
making localization vastly faster. It also lays the exact groundwork needed for 
3D triangulation (SfM) if you want to upgrade to a full 3D point cloud later.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import cv2
import h5py
import numpy as np

from fmc.config import SiteConfig, load_site_config
from fmc.dataset.schema import load_records


def build_feature_database(site: SiteConfig) -> None:
    records = load_records(site.dataset_jsonl_path)
    if not records:
        raise RuntimeError(f"No dataset records found at {site.dataset_jsonl_path}")

    # Use the same feature extractor configured for verification
    # For baseline, we use ORB. In the future, this can swap to SuperPoint.
    detector = cv2.ORB_create(nfeatures=5000)
    
    feature_db_path = site.index_dir / "features.h5"
    site.index_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Building feature database for {len(records)} images at {feature_db_path}...")
    t0 = time.perf_counter()
    
    with h5py.File(feature_db_path, "w") as db:
        # Create groups for keypoints and descriptors
        grp_kpts = db.create_group("keypoints")
        grp_desc = db.create_group("descriptors")
        grp_scores = db.create_group("scores")
        
        valid_count = 0
        
        for i, record in enumerate(records):
            img_path = site.processed_dir / record.processed_path
            img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
            
            if img is None:
                print(f"WARNING: skipping unreadable image {img_path}")
                continue
                
            # Extract features
            keypoints, descriptors = detector.detectAndCompute(img, None)
            
            if keypoints is None or len(keypoints) == 0:
                print(f"WARNING: No features found in {record.image_id}")
                continue
                
            # Convert KeyPoint objects to numpy arrays for storage
            # format: [x, y, size, angle, response, octave, class_id]
            kpts_array = np.array([[kp.pt[0], kp.pt[1], kp.size, kp.angle, kp.response, kp.octave, kp.class_id] 
                                  for kp in keypoints], dtype=np.float32)
            
            # Save to HDF5 (using image_id as the dataset key)
            grp_kpts.create_dataset(record.image_id, data=kpts_array, compression="gzip")
            grp_desc.create_dataset(record.image_id, data=descriptors, compression="gzip")
            
            # Just saving the response scores separately for easy top-K filtering later
            scores_array = np.array([kp.response for kp in keypoints], dtype=np.float32)
            grp_scores.create_dataset(record.image_id, data=scores_array, compression="gzip")
            
            valid_count += 1
            if valid_count % 100 == 0:
                print(f"Processed {valid_count}/{len(records)} images...")

    t_total = time.perf_counter() - t0
    print(f"Feature database built! Processed {valid_count} images in {t_total:.2f}s.")
    print(f"Database saved to: {feature_db_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", default="mock_site")
    args = parser.parse_args()

    build_feature_database(load_site_config(args.site))
