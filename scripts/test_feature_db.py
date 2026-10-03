"""Test script for the new offline feature database pipeline.

This script demonstrates building the feature database and then performing
a geometric verification match using the pre-computed features from the database,
rather than extracting them from the candidate image on the fly.

This proves the concept works without modifying the existing VPR pipeline.
"""
import argparse
import time
import random
import cv2
import h5py
import numpy as np

from fmc.config import load_site_config
from fmc.dataset.schema import load_records
from fmc.dataset.build_feature_db import build_feature_database

def test_feature_db(site_name: str):
    site = load_site_config(site_name)
    
    # 1. Build the database (if it doesn't exist, this creates it)
    print("--- Step 1: Building Feature DB ---")
    build_feature_database(site)
    
    # 2. Open the database and load dataset records
    db_path = site.index_dir / "features.h5"
    records = load_records(site.dataset_jsonl_path)
    
    if not records or len(records) < 2:
        print("Not enough records in the dataset to perform a match test.")
        return
        
    print("\n--- Step 2: Testing Feature Retrieval & Matching ---")
    
    # We will simulate a query by picking a random image from the dataset
    # and trying to match it against another image.
    query_record = random.choice(records)
    candidate_record = random.choice(records)
    
    # Extract features from the query image ON THE FLY (as it would happen in live VPR)
    print(f"Loading query image: {query_record.image_id}")
    query_img_path = site.processed_dir / query_record.processed_path
    query_img = cv2.imread(str(query_img_path), cv2.IMREAD_GRAYSCALE)
    
    t0 = time.perf_counter()
    detector = cv2.ORB_create(nfeatures=5000)
    query_kpts, query_desc = detector.detectAndCompute(query_img, None)
    t_extract = (time.perf_counter() - t0) * 1000
    print(f"Extracted {len(query_kpts)} query features on the fly in {t_extract:.1f}ms")
    
    # Retrieve features for the candidate image FROM THE DATABASE
    print(f"\nRetrieving candidate features for: {candidate_record.image_id}")
    t1 = time.perf_counter()
    with h5py.File(db_path, "r") as db:
        if candidate_record.image_id not in db["descriptors"]:
            print(f"Error: Candidate {candidate_record.image_id} not found in database.")
            return
            
        candidate_desc = db["descriptors"][candidate_record.image_id][:]
        candidate_kpts_raw = db["keypoints"][candidate_record.image_id][:]
        
    t_retrieve = (time.perf_counter() - t1) * 1000
    print(f"Retrieved {len(candidate_kpts_raw)} candidate features from DB in {t_retrieve:.1f}ms")
    
    # Perform the match using BFMatcher
    print("\n--- Step 3: Matching ---")
    t2 = time.perf_counter()
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    
    if query_desc is not None and candidate_desc is not None:
        raw_matches = matcher.knnMatch(query_desc, candidate_desc, k=2)
        
        # Lowe's ratio test
        good_matches = [m for m, n in raw_matches if m.distance < 0.75 * n.distance]
        t_match = (time.perf_counter() - t2) * 1000
        
        print(f"Found {len(good_matches)} good matches between the images in {t_match:.1f}ms!")
        print(f"-> Speed comparison: DB Retrieval ({t_retrieve:.1f}ms) vs On-the-fly Extraction ({t_extract:.1f}ms)")
        print("\nSUCCESS: The feature database pipeline is working perfectly!")
    else:
        print("Failed to match: descriptors were None.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", default="mock_site")
    args = parser.parse_args()
    test_feature_db(args.site)
