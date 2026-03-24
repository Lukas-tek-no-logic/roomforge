"""
Phase 5: Video → 3D reconstruction via DN-Splatter.
Extracts frames from video, sends to DGX for COLMAP + DN-Splatter reconstruction,
imports resulting mesh into the scene pipeline.
"""

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

DGX_SPLATTER_URL = os.environ.get("DGX_SPLATTER_URL", "http://192.168.0.200:8004")
TIMEOUT = 600.0  # 10 min — reconstruction takes 2-5 min


async def reconstruct_from_video(video_path: str, session_dir: str) -> dict:
    """Full reconstruction pipeline: video → frames → DGX → mesh → room_data.

    Returns room_data dict with real measured dimensions.
    """
    session_path = Path(session_dir)
    frames_dir = session_path / "frames"
    recon_dir = session_path / "reconstruction"
    frames_dir.mkdir(exist_ok=True)
    recon_dir.mkdir(exist_ok=True)

    # Step 1: Extract frames from video using ffmpeg
    await _extract_frames(video_path, str(frames_dir), max_frames=150)

    # Step 2: Send frames to DGX for reconstruction
    room_data = await _reconstruct_on_dgx(str(frames_dir), str(recon_dir))

    # Step 3: If reconstruction returned a mesh, save its path
    mesh_path = recon_dir / "mesh.glb"
    if mesh_path.exists():
        room_data["reconstruction_mesh"] = str(mesh_path)

    # Step 4: Enrich room_data with Claude analysis for materials/furniture types
    # (The caller should run analyze_with_geometry after this)

    return room_data


async def _extract_frames(video_path: str, frames_dir: str, max_frames: int = 150) -> int:
    """Extract frames from video at ~10 fps."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", video_path,
        "-vf", f"fps=10",
        "-frames:v", str(max_frames),
        "-q:v", "2",
        f"{frames_dir}/frame_%04d.jpg",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode()[-300:]}")

    frame_count = len(list(Path(frames_dir).glob("frame_*.jpg")))
    return frame_count


async def _reconstruct_on_dgx(frames_dir: str, output_dir: str) -> dict:
    """Send reconstruction request to DGX DN-Splatter service."""
    import base64

    # Encode frames as base64
    frames_b64 = []
    frame_paths = sorted(Path(frames_dir).glob("frame_*.jpg"))
    for fp in frame_paths:
        frames_b64.append(base64.b64encode(fp.read_bytes()).decode())

    if not frames_b64:
        raise RuntimeError(f"No frames found in {frames_dir}")

    print(f"[reconstruct] Sending {len(frames_b64)} frames to DN-Splatter...")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{DGX_SPLATTER_URL}/reconstruct",
            json={
                "frames_b64": frames_b64,
                "output_name": "reconstruction",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        raise RuntimeError(data["error"])

    # Save mesh GLB if returned
    mesh_b64 = data.get("mesh_b64", "")
    if mesh_b64:
        mesh_path = Path(output_dir) / "mesh.glb"
        mesh_path.write_bytes(base64.b64decode(mesh_b64))
        print(f"[reconstruct] Mesh saved: {mesh_path} ({data.get('mesh_size', 0) // 1024} KB)")

    room_data = data.get("room_data", {})

    # Ensure required fields exist
    if "room" not in room_data:
        room_data["room"] = {"width": 3.0, "depth": 3.0, "height": 2.5}

    # Add default fields that Claude would normally generate
    room_data.setdefault("walls", {"color": "#E8E8E8", "material": "paint", "texture_roughness": 0.6})
    room_data.setdefault("floor", {"color": "#C8A882", "material": "tile", "texture_roughness": 0.7})
    room_data.setdefault("ceiling", {"color": "#F8F8F8", "height": room_data["room"].get("height", 2.5)})
    room_data.setdefault("furniture", [])
    room_data.setdefault("openings", [])
    room_data.setdefault("lighting", {"type": "ceiling_lamp", "intensity": 2.0, "color": "#FFF8F0"})

    return room_data


async def splatter_healthy() -> bool:
    """Check if DN-Splatter service is available."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{DGX_SPLATTER_URL}/health")
            return resp.status_code == 200
    except Exception:
        return False
