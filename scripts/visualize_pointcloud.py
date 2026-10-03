import argparse
import json
import os
from pathlib import Path

import numpy as np

from fmc.config import load_site_config
from fmc.dataset.locations import load_locations_csv

try:
    import open3d as o3d
except ImportError:
    print("Error: Open3D is not installed.")
    print("Please install it to visualize 3D point clouds: pip install open3d")
    exit(1)


def create_3d_landmark_pin(x: float, y: float, z: float, label: str, is_user_tag: bool = False) -> list[o3d.geometry.TriangleMesh]:
    """Create a 3D pin marker (pole + sphere beacon + base disc) for Open3D visualization."""
    meshes = []
    # Color: Red for user tags, Gold for parking / facility landmarks
    beacon_color = [1.0, 0.2, 0.2] if is_user_tag else [1.0, 0.65, 0.0]
    pole_color = [0.9, 0.9, 0.9]

    pole_height = 1.5
    pole_radius = 0.06
    beacon_radius = 0.35

    # 1. Base ring / disc on the ground
    disc = o3d.geometry.TriangleMesh.create_cylinder(radius=0.45, height=0.04)
    disc.translate([x, y, z])
    disc.paint_uniform_color(beacon_color)
    disc.compute_vertex_normals()
    meshes.append(disc)

    # 2. Slender vertical pole
    pole = o3d.geometry.TriangleMesh.create_cylinder(radius=pole_radius, height=pole_height)
    pole.translate([x, y, z + pole_height / 2.0])
    pole.paint_uniform_color(pole_color)
    pole.compute_vertex_normals()
    meshes.append(pole)

    # 3. Beacon sphere head at the top
    beacon = o3d.geometry.TriangleMesh.create_sphere(radius=beacon_radius, resolution=16)
    beacon.translate([x, y, z + pole_height + beacon_radius * 0.7])
    beacon.paint_uniform_color(beacon_color)
    beacon.compute_vertex_normals()
    meshes.append(beacon)

    return meshes


def visualize_pointcloud(site_name: str) -> None:
    site = load_site_config(site_name)
    ply_path = site.index_dir / "pointcloud.ply"
    
    if not ply_path.exists():
        print(f"Error: Point cloud not found at {ply_path}")
        print("Please run the mapping pipeline first and finalize the map.")
        return
        
    print(f"Loading point cloud from: {ply_path}")
    pcd = o3d.io.read_point_cloud(str(ply_path))
    
    if pcd.is_empty():
        print("Point cloud is empty!")
        return
        
    print(f"Loaded {len(pcd.points)} points.")
    
    # Preserve point cloud colors if present, otherwise paint soft green
    if not pcd.has_colors():
        pcd.paint_uniform_color([0.2, 0.8, 0.4])

    pcd_points = np.asarray(pcd.points)
    ground_z = float(np.percentile(pcd_points[:, 2], 5)) if len(pcd_points) > 0 else 0.0

    # Draw coordinate frame at the origin (Facility X/Y/Z = 0)
    coord_frame = o3d.geometry.TriangleMesh.create_coordinate_frame(size=2.0, origin=[0, 0, 0])
    geometries: list[o3d.geometry.Geometry] = [pcd, coord_frame]

    # Load location tag landmarks
    tag_count = 0

    # 1. User-dropped ground control tags from tags.json
    tags_file = site.index_dir / "tags.json"
    if tags_file.exists():
        try:
            with open(tags_file, "r") as f:
                user_tags = json.load(f)
            for tag in user_tags:
                tx = float(tag.get("x", tag.get("facility_x", 0.0)))
                ty = float(tag.get("y", tag.get("facility_y", 0.0)))
                # pins share the floor plane with facility landmarks
                tz = ground_z
                tag_meshes = create_3d_landmark_pin(tx, ty, tz, tag.get("label", "Tag"), is_user_tag=True)
                geometries.extend(tag_meshes)
                print(f"  📍 Loaded User Tag [{tag.get('label', 'Tag')}]: (X={tx:.2f}, Y={ty:.2f}, Z={tz:.2f})")
                tag_count += 1
        except Exception as e:
            print(f"Warning: could not read {tags_file}: {e}")

    # 2. Facility locations from locations.csv (all on the same floor plane)
    loc_coords = []
    locations_file = site.index_dir / "locations.csv"
    if locations_file.exists():
        try:
            rows = load_locations_csv(locations_file)
            for r in rows:
                lx = float(r["x"])
                ly = float(r["y"])
                lz = ground_z
                loc_coords.append([lx, ly, lz])
                loc_meshes = create_3d_landmark_pin(lx, ly, lz, f"Spot {r['location_id']}", is_user_tag=False)
                geometries.extend(loc_meshes)
                print(f"  📍 Loaded Facility Landmark [Spot {r['location_id']}]: (X={lx:.2f}, Y={ly:.2f}, Z={lz:.2f}) [Floor 1 Plane]")
                tag_count += 1

            # Connect same-plane landmarks with an alignment line
            if len(loc_coords) >= 2:
                line_set = o3d.geometry.LineSet()
                line_set.points = o3d.utility.Vector3dVector(loc_coords)
                lines = [[i, i + 1] for i in range(len(loc_coords) - 1)]
                line_set.lines = o3d.utility.Vector2iVector(lines)
                line_set.paint_uniform_color([1.0, 0.65, 0.0])
                geometries.append(line_set)
        except Exception as e:
            print(f"Warning: could not read {locations_file}: {e}")

    print(f"\nOpening 3D Viewer with {len(pcd.points)} points and {tag_count} location landmarks on plane Z={ground_z:.2f}m...")
    print("  Controls: Left-Click + Drag to rotate | Right-Click + Drag to pan | Scroll to zoom")

    o3d.visualization.draw_geometries(
        geometries,
        window_name=f"FindMyCar 3D Map - {site_name} ({tag_count} Landmarks)",
        width=1100,
        height=800
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    default_site = os.environ.get("FMC_SITE_ID", "site_00")
    parser.add_argument("--site", default=default_site)
    args = parser.parse_args()
    visualize_pointcloud(args.site)

