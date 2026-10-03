"""Download H2GIS standalone jars into lib/h2gis/ (requires network once)."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
URL = "https://github.com/orbisgis/h2gis/releases/download/v2.2.5/h2gis-standalone-bin.zip"
MARKER = LIB / "h2gis" / "h2gis-standalone" / "bin" / "h2.jar"


def main() -> None:
    if MARKER.exists() or (LIB / "h2gis" / "h2gis-standalone" / "bin" / "h2-2.4.240.jar").exists():
        print(f"H2GIS already present under {LIB / 'h2gis'}")
        return
    LIB.mkdir(parents=True, exist_ok=True)
    zip_path = LIB / "h2gis-standalone-bin.zip"
    print(f"Downloading {URL} ...")
    urlretrieve(URL, zip_path)
    print(f"Extracting to {LIB / 'h2gis'} ...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(LIB / "h2gis")
    print("Done.")


if __name__ == "__main__":
    main()
