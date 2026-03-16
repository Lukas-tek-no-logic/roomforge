"""
Phase 4: TRELLIS.2 3D furniture generation from photos.
Crops each furniture item from the original photo and generates
a 3D model (GLB) via TRELLIS.2 on DGX.
"""

import asyncio
import base64
import io
import os
from pathlib import Path

import httpx
from PIL import Image

DGX_TRELLIS_URL = os.environ.get("DGX_TRELLIS_URL", "http://192.168.0.200:8003")
TIMEOUT = 120.0
MAX_CONCURRENT = 4


async def generate_all_furniture(
    photo_path: str, room_data: dict, session_dir: str
) -> dict[str, Path]:
    """Generate 3D models for all furniture items with bounding boxes.

    Returns dict mapping furniture_id to GLB file path.
    """
    furniture_dir = Path(session_dir) / "furniture"
    furniture_dir.mkdir(exist_ok=True)

    # Check if TRELLIS.2 service is available
    if not await _trellis_healthy():
        return {}

    furniture = room_data.get("furniture", [])
    if not furniture:
        return {}

    try:
        img = Image.open(photo_path).convert("RGB")
    except Exception:
        return {}

    w, h = img.size

    # Collect items that have bounding boxes and don't already have GLB
    items_to_generate = []
    for item in furniture:
        item_id = item.get("id", "")
        if not item_id:
            continue

        glb_path = furniture_dir / f"{item_id}.glb"
        if glb_path.exists():
            continue  # Already generated

        bbox = item.get("bbox", [])
        if len(bbox) == 4:
            x1, y1, x2, y2 = bbox
            # Normalize: bbox can be in pixels or 0-1 range
            x1 = max(0, min(w, int(x1 * w) if x1 <= 1 else int(x1)))
            y1 = max(0, min(h, int(y1 * h) if y1 <= 1 else int(y1)))
            x2 = max(0, min(w, int(x2 * w) if x2 <= 1 else int(x2)))
            y2 = max(0, min(h, int(y2 * h) if y2 <= 1 else int(y2)))
            if x2 - x1 > 30 and y2 - y1 > 30:
                crop = img.crop((x1, y1, x2, y2))
                items_to_generate.append((item_id, crop, glb_path))

    if not items_to_generate:
        return {}

    # Generate in parallel (max MAX_CONCURRENT at once)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    results = {}

    async def gen_one(item_id: str, crop: Image.Image, glb_path: Path):
        async with semaphore:
            try:
                success = await _generate_3d_model(crop, glb_path)
                if success:
                    results[item_id] = glb_path
            except Exception as e:
                print(f"[furniture] Failed to generate {item_id}: {e}")

    tasks = [gen_one(item_id, crop, glb_path) for item_id, crop, glb_path in items_to_generate]
    await asyncio.gather(*tasks)

    return results


async def _generate_3d_model(crop_image: Image.Image, output_path: Path) -> bool:
    """Send furniture crop to TRELLIS.2 service and save GLB file."""
    buf = io.BytesIO()
    crop_image.save(buf, format="PNG")
    image_b64 = base64.b64encode(buf.getvalue()).decode()

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{DGX_TRELLIS_URL}/generate",
            json={"image_b64": image_b64, "resolution": 512},
        )
        resp.raise_for_status()
        output_path.write_bytes(resp.content)
        return True


async def _trellis_healthy() -> bool:
    """Check if TRELLIS.2 service is available."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{DGX_TRELLIS_URL}/health")
            return resp.status_code == 200
    except Exception:
        return False
