"""Check whether your survey photos already have a compass heading embedded
in EXIF (GPSImgDirection) -- if so, ingest_survey_locations.py can use it
automatically and you may not need to build a headings CSV by hand at all.

Run:
    python scripts/check_exif_compass.py /path/to/survey_photos
Scans every image under every location subfolder and reports how many have
a usable GPSImgDirection value.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from fmc.dataset.heading import exif_compass_heading

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("survey_root", type=Path)
    args = parser.parse_args()

    total = 0
    with_heading = 0
    examples = []

    for location_dir in sorted(p for p in args.survey_root.iterdir() if p.is_dir()):
        for photo_path in sorted(location_dir.iterdir()):
            if photo_path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            total += 1
            heading = exif_compass_heading(photo_path)
            if heading is not None:
                with_heading += 1
                if len(examples) < 5:
                    examples.append((photo_path, heading))

    print(f"{with_heading} / {total} photos have a GPSImgDirection EXIF heading.")
    if examples:
        print("Examples:")
        for path, heading in examples:
            print(f"  {path} -> {heading:.1f} deg")

    if total == 0:
        print("\nNo photos found under that path -- check survey_root.")
    elif with_heading == total:
        print("\nAll photos have EXIF heading -- ingest_survey_locations.py will use it "
              "automatically. No headings CSV needed.")
    elif with_heading > 0:
        print("\nSome photos are missing EXIF heading -- build a headings CSV to cover "
              "at least those (see docs/04_survey_ingestion_workflow.md, step 6).")
    else:
        print("\nNo EXIF heading found on any photo -- if you have compass readings from "
              "another source (field notes, a separate log), put them in a headings CSV "
              "(see docs/04_survey_ingestion_workflow.md, step 6). Otherwise the "
              "evenly-spaced approximation will be used for all photos.")


if __name__ == "__main__":
    main()
