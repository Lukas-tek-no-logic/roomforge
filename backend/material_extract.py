"""
Phase 3: CHORD PBR material extraction from photos.
Extracts surface crops from the original photo and generates PBR maps
(color, normal, roughness, metallic) via CHORD model on DGX.
"""

import asyncio
import base64
import io
import os
from pathlib import Path

import httpx
from PIL import Image

DGX_CHORD_URL = os.environ.get("DGX_CHORD_URL", "http://192.168.0.200:8002")
TIMEOUT = 60.0


async def extract_and_generate_materials(
    photo_path: str, room_data: dict, session_dir: str
) -> dict[str, Path]:
    """Extract surface crops from photo and generate PBR maps via CHORD.

    Returns dict mapping surface name to directory containing PBR maps.
    """
    materials_dir = Path(session_dir) / "materials"
    materials_dir.mkdir(exist_ok=True)

    # Check if CHORD service is available
    if not await _chord_healthy():
        return {}

    # Extract surface crops based on room_data hints
    crops = _extract_surface_crops(photo_path, room_data)
    if not crops:
        return {}

    # Generate PBR maps for each crop in parallel
    tasks = []
    for surface_name, crop_image in crops.items():
        output_dir = materials_dir / surface_name
        output_dir.mkdir(exist_ok=True)
        tasks.append(_generate_pbr_maps(crop_image, output_dir, surface_name))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    generated = {}
    for surface_name, result in zip(crops.keys(), results):
        if not isinstance(result, Exception):
            generated[surface_name] = materials_dir / surface_name

    return generated


def _extract_surface_crops(photo_path: str, room_data: dict) -> dict[str, Image.Image]:
    """Extract surface regions from the photo based on room_data analysis.

    Uses surface_crops from room_data if available, otherwise estimates regions.
    """
    try:
        img = Image.open(photo_path).convert("RGB")
    except Exception:
        return {}

    w, h = img.size
    crops = {}

    # Use surface_crops from Claude analysis if available (Phase 3 enhanced analysis)
    surface_crops = room_data.get("surface_crops", [])
    if surface_crops:
        for crop_info in surface_crops:
            name = crop_info.get("type", "unknown")
            bbox = crop_info.get("bbox", [])
            if len(bbox) == 4:
                x1, y1, x2, y2 = bbox
                # Clamp to image bounds
                x1 = max(0, min(w, int(x1 * w) if x1 <= 1 else int(x1)))
                y1 = max(0, min(h, int(y1 * h) if y1 <= 1 else int(y1)))
                x2 = max(0, min(w, int(x2 * w) if x2 <= 1 else int(x2)))
                y2 = max(0, min(h, int(y2 * h) if y2 <= 1 else int(y2)))
                if x2 - x1 > 50 and y2 - y1 > 50:
                    crops[name] = img.crop((x1, y1, x2, y2))
        return crops

    # Fallback: estimate floor and wall regions from image geometry
    # Floor: bottom 30% of image, central 60%
    floor_crop = img.crop((
        int(w * 0.2), int(h * 0.65),
        int(w * 0.8), int(h * 0.95),
    ))
    crops["floor"] = floor_crop

    # Wall: upper-middle region (above furniture, below ceiling)
    wall_crop = img.crop((
        int(w * 0.1), int(h * 0.1),
        int(w * 0.4), int(h * 0.45),
    ))
    crops["wall"] = wall_crop

    return crops


async def _generate_pbr_maps(
    crop_image: Image.Image, output_dir: Path, surface_name: str
) -> bool:
    """Send surface crop to CHORD service and save PBR maps."""
    # Encode crop to base64
    buf = io.BytesIO()
    crop_image.save(buf, format="PNG")
    image_b64 = base64.b64encode(buf.getvalue()).decode()

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{DGX_CHORD_URL}/extract",
            json={"image_b64": image_b64, "tile_size": 512},
        )
        resp.raise_for_status()
        data = resp.json()

    # Save each map
    for map_name in ("color", "normal", "roughness", "metallic"):
        key = f"{map_name}_b64"
        if key in data:
            map_bytes = base64.b64decode(data[key])
            map_path = output_dir / f"{surface_name}_{map_name}.png"
            map_path.write_bytes(map_bytes)

    return True


async def _chord_healthy() -> bool:
    """Check if CHORD service is available."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{DGX_CHORD_URL}/health")
            return resp.status_code == 200
    except Exception:
        return False
