"""
Depth-based 3D position computation.

Takes a depth map (from Depth Anything V2) + furniture bboxes (from Claude)
and computes accurate 3D positions using pinhole camera projection.
"""

import math
from pathlib import Path

import numpy as np


def estimate_fov(image_path: str) -> float:
    """Estimate horizontal FOV from EXIF or use smartphone default."""
    try:
        from PIL import Image
        from PIL.ExifTags import Base as ExifBase
        img = Image.open(image_path)
        exif = img.getexif()
        # Try 35mm equivalent focal length first
        focal_35 = exif.get(41989)  # FocalLengthIn35mmFilm
        if focal_35 and focal_35 > 0:
            fov = 2 * math.atan(36 / (2 * focal_35))
            return math.degrees(fov)
        # Try raw focal length + sensor size
        focal = exif.get(37386)  # FocalLength
        if focal and focal > 0:
            # Assume APS-C crop factor ~1.5 if no sensor info
            focal_35_equiv = float(focal) * 1.5
            fov = 2 * math.atan(36 / (2 * focal_35_equiv))
            return math.degrees(fov)
    except Exception:
        pass
    # Default: typical smartphone ~28mm equiv → ~67° horizontal FOV
    return 67.0


def build_intrinsics(img_w: int, img_h: int, fov_h_deg: float) -> tuple[float, float, float, float]:
    """Build pinhole camera intrinsics from image dimensions and FOV.
    Returns (fx, fy, cx, cy)."""
    fov_h = math.radians(fov_h_deg)
    fx = img_w / (2 * math.tan(fov_h / 2))
    fy = fx  # square pixels
    cx = img_w / 2.0
    cy = img_h / 2.0
    return fx, fy, cx, cy


def scale_depth_to_metric(depth_map: np.ndarray, room_depth: float) -> np.ndarray:
    """Convert relative depth map to metric depth using room dimensions as anchor.

    Strategy: sample depth at floor-near-camera (bottom center) and back wall (upper center).
    The difference maps to the known room_depth.
    """
    h, w = depth_map.shape

    # Sample near floor (bottom 10%, center 20%)
    near_region = depth_map[int(h * 0.85):int(h * 0.95), int(w * 0.4):int(w * 0.6)]
    near_val = float(np.median(near_region)) if near_region.size > 0 else float(depth_map.max())

    # Sample back wall (top 15-30%, center 20%)
    far_region = depth_map[int(h * 0.15):int(h * 0.30), int(w * 0.4):int(w * 0.6)]
    far_val = float(np.median(far_region)) if far_region.size > 0 else float(depth_map.min())

    # Depth Anything V2 outputs INVERSE depth: larger = closer
    # Inverse depth d_inv = k / z_real, so z_real = k / d_inv
    # We need to find k (scale factor) using the known room depth.

    # Avoid division by zero
    depth_safe = np.clip(depth_map, 1e-3, None)

    # Convert inverse depth to proportional real distance
    # z_proportional = 1 / d_inv (larger inverse depth = smaller real distance)
    z_prop = 1.0 / depth_safe

    h, w = depth_map.shape

    # Find the min and max proportional distances (across whole image)
    z_min = float(np.percentile(z_prop, 2))   # nearest surface (ignore outliers)
    z_max = float(np.percentile(z_prop, 98))  # farthest surface

    z_range = z_max - z_min
    if abs(z_range) < 1e-8:
        return np.full_like(depth_map, room_depth / 2)

    # Normalize to 0..1 (0 = nearest, 1 = farthest)
    z_norm = (z_prop - z_min) / z_range
    z_norm = np.clip(z_norm, 0, 1)

    # Scale to metric: nearest ≈ 0.3m, farthest ≈ room_depth
    camera_offset = 0.3
    metric = camera_offset + z_norm * (room_depth - camera_offset)

    return metric


def compute_3d_positions(
    furniture: list[dict],
    depth_map: np.ndarray,
    intrinsics: tuple[float, float, float, float],
    room_dims: dict,
) -> list[dict]:
    """Compute 3D positions for furniture items using depth map + bbox.

    For each item:
    1. Get bbox center in pixel coordinates
    2. Sample depth at that pixel
    3. Pinhole projection → camera-space 3D point
    4. Transform to room coordinates
    5. If near a wall, snap to wall-relative coords

    Returns modified furniture list with accurate positions.
    """
    fx, fy, cx, cy = intrinsics
    img_h, img_w = depth_map.shape
    W = float(room_dims.get("width", 4.0))
    D = float(room_dims.get("depth", 3.5))
    H = float(room_dims.get("height", 2.5))
    half_w, half_d = W / 2, D / 2

    for item in furniture:
        wall = item.get("wall", "")
        bbox = item.get("bbox")

        # For wall-attached items: refine position_along_wall from bbox
        # Claude's wall assignment is correct, but position_along can be imprecise
        if wall in ("north", "south", "east", "west") and bbox and len(bbox) >= 4:
            bcx = (bbox[0] + bbox[2]) / 2  # 0..1 in image
            # Camera faces north: image left=west (0.0), image right=east (1.0)
            if wall in ("north", "south"):
                item["position_along_wall"] = bcx  # bbox x → position along wall
            elif wall == "east":
                bcy = (bbox[1] + bbox[3]) / 2
                item["position_along_wall"] = 1.0 - bcy  # higher in image = further north
            elif wall == "west":
                bcy = (bbox[1] + bbox[3]) / 2
                item["position_along_wall"] = 1.0 - bcy
            continue

        bbox = item.get("bbox")
        if not bbox or len(bbox) < 4:
            continue

        # Bbox center in pixel coords
        bcx = (bbox[0] + bbox[2]) / 2
        bcy = (bbox[1] + bbox[3]) / 2
        u = int(bcx * img_w)
        v = int(bcy * img_h)

        # Clamp to image bounds
        u = max(2, min(img_w - 3, u))
        v = max(2, min(img_h - 3, v))

        # Sample depth: median over 5x5 patch for robustness
        patch = depth_map[v - 2:v + 3, u - 2:u + 3]
        d = float(np.median(patch))

        if d <= 0:
            continue  # invalid depth

        # Pinhole projection: pixel → camera space
        X_cam = (u - cx) * d / fx
        Z_cam = d  # forward distance from camera

        # Camera-to-room transform:
        # Camera at south wall looking north (+Z = into room = +Y in room coords)
        # Camera +X = room +X (east)
        # Camera -Y = room +Z (up)
        room_x = X_cam
        room_y = Z_cam - half_d  # center-origin: camera at -D/2, back wall at +D/2

        # Z (height): for floor items, force 0
        item_size = item.get("size", {})
        fh = float(item_size.get("height", 0.5))
        elevation = float(item.get("elevation", 0))

        # Items in bottom half of image are on the floor
        if bcy > 0.45 and elevation == 0:
            room_z = 0.0
        else:
            Y_cam = (v - cy) * d / fy
            room_z = max(0, -Y_cam)

        # Clamp to room bounds
        fw = float(item_size.get("width", 0.3))
        fd = float(item_size.get("depth", 0.3))
        room_x = max(-half_w + fw / 2, min(half_w - fw / 2, room_x))
        room_y = max(-half_d + fd / 2, min(half_d - fd / 2, room_y))

        # Inject depth-computed position for free-standing items
        item["position"] = {
            "x": round(room_x, 3),
            "y": round(room_y, 3),
            "z": round(room_z, 3),
        }
        item["elevation"] = round(room_z, 3)

    return furniture
