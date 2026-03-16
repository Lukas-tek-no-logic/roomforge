"""
Blender render service for DGX Spark.

Accepts room_data JSON (+ optional CHORD materials / furniture GLBs),
runs Blender with CUDA GPU, returns rendered PNG + depth map.
"""

import asyncio
import base64
import json
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Blender Render Service")

BLENDER_BIN = os.environ.get("BLENDER_BIN", "blender")
BLENDER_SCRIPT = Path(__file__).parent / "blender_scene.py"
TEXTURE_CACHE = Path("/data/texture_cache")
HDRI_CACHE = Path("/data/hdri_cache")
RENDER_TIMEOUT = 300  # 5 min max
SEMAPHORE = asyncio.Semaphore(1)  # one render at a time

# Ensure persistent caches exist
TEXTURE_CACHE.mkdir(parents=True, exist_ok=True)
HDRI_CACHE.mkdir(parents=True, exist_ok=True)


# ── Request / Response models ────────────────────────────────────────────

class RenderRequest(BaseModel):
    room_data: dict
    camera: int = 0
    width: int = 1280
    height: int = 720
    # Optional Phase 3 CHORD materials: {"floor_color": "base64...", "floor_normal": "base64...", ...}
    chord_materials: dict[str, str] | None = None
    # Optional Phase 4 furniture GLBs: {"washer_1": "base64...", ...}
    furniture_files: dict[str, str] | None = None


class RenderResponse(BaseModel):
    render_b64: str
    depth_b64: str = ""
    gpu_used: str = ""
    render_time_s: float = 0


# ── Helpers ──────────────────────────────────────────────────────────────

def _write_b64_files(data: dict[str, str], dest_dir: Path) -> None:
    """Decode base64 dict values and write to dest_dir as files."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for filename, b64_data in data.items():
        (dest_dir / filename).write_bytes(base64.b64decode(b64_data))


def _read_b64(path: Path) -> str:
    """Read file and return base64-encoded string."""
    if path.exists():
        return base64.b64encode(path.read_bytes()).decode()
    return ""


# ── Endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    blender_ok = shutil.which(BLENDER_BIN) is not None
    # Quick version check
    version = ""
    if blender_ok:
        try:
            proc = await asyncio.create_subprocess_exec(
                BLENDER_BIN, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            version = stdout.decode().split("\n")[0].strip()
        except Exception:
            pass
    return {
        "status": "ok" if blender_ok else "error",
        "blender": blender_ok,
        "version": version,
        "gpu": "nvidia",
    }


@app.post("/render", response_model=RenderResponse)
async def render(req: RenderRequest):
    """Render a room scene and return PNG + depth map."""
    async with SEMAPHORE:
        return await _do_render(req)


async def _do_render(req: RenderRequest) -> RenderResponse:
    import time
    t0 = time.monotonic()

    # Create temp workspace
    work_dir = Path(tempfile.mkdtemp(prefix="blender_"))
    try:
        # Symlink caches so Polyhaven assets persist across renders
        sessions_dir = work_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / "texture_cache").symlink_to(TEXTURE_CACHE)
        (sessions_dir / "hdri_cache").symlink_to(HDRI_CACHE)

        # Write room_data.json
        input_json = work_dir / "room_data.json"
        input_json.write_text(json.dumps(req.room_data, indent=2))

        # Write CHORD materials if provided
        if req.chord_materials:
            materials_dir = work_dir / "materials"
            # Group by surface: "floor_color" → materials/chord/floor/color.png
            surfaces: dict[str, dict[str, str]] = {}
            for key, b64 in req.chord_materials.items():
                # Expected format: "surfacename_maptype" e.g. "floor_color", "wall_normal"
                parts = key.rsplit("_", 1)
                if len(parts) == 2:
                    surface, maptype = parts
                    surfaces.setdefault(surface, {})[f"{maptype}.png"] = b64
                else:
                    # Just write as-is
                    (materials_dir / "chord").mkdir(parents=True, exist_ok=True)
                    (materials_dir / "chord" / key).write_bytes(base64.b64decode(b64))

            for surface, files in surfaces.items():
                surface_dir = materials_dir / "chord" / surface
                _write_b64_files(files, surface_dir)

        # Write furniture GLBs if provided
        if req.furniture_files:
            furniture_dir = work_dir / "furniture"
            furniture_dir.mkdir(parents=True, exist_ok=True)
            for name, b64 in req.furniture_files.items():
                fname = name if name.endswith(".glb") else f"{name}.glb"
                (furniture_dir / fname).write_bytes(base64.b64decode(b64))

        # Output paths
        render_png = work_dir / "render_current.png"
        depth_png = work_dir / "render_depth.png"

        # Build Blender command
        cmd = [
            BLENDER_BIN,
            "--background",
            "--python", str(BLENDER_SCRIPT),
            "--",
            "--input", str(input_json),
            "--output", str(render_png),
            "--depth-output", str(depth_png),
            "--session-dir", str(work_dir),
            "--camera", str(req.camera),
        ]

        # Run Blender with GPU (cwd=work_dir so relative cache paths resolve)
        gpu_used = "unknown"
        returncode, stderr_text, gpu_info = await _exec_blender(cmd, cwd=work_dir)

        if gpu_info:
            gpu_used = gpu_info

        # Log Blender output for debugging
        if stderr_text:
            for line in stderr_text.splitlines()[-20:]:
                print(f"[blender] {line}")

        # GPU crash → retry without GPU override (Blender still tries CUDA first)
        if returncode != 0 and stderr_text and "Memory access fault" in stderr_text:
            env_cpu = {"CYCLES_DEVICE": "CPU"}
            returncode, stderr_text, _ = await _exec_blender(cmd, env_extra=env_cpu, cwd=work_dir)
            gpu_used = "CPU (fallback)"

        if returncode != 0:
            error = stderr_text[-2000:] if stderr_text else "unknown error"
            raise HTTPException(status_code=500, detail=f"Blender failed (rc={returncode}): {error}")

        if not render_png.exists():
            raise HTTPException(status_code=500, detail=f"Blender produced no output (rc={returncode}, stderr last 500: {stderr_text[-500:] if stderr_text else 'empty'})")

        elapsed = time.monotonic() - t0
        return RenderResponse(
            render_b64=_read_b64(render_png),
            depth_b64=_read_b64(depth_png),
            gpu_used=gpu_used,
            render_time_s=round(elapsed, 2),
        )
    finally:
        # Clean up temp workspace
        shutil.rmtree(work_dir, ignore_errors=True)


async def _exec_blender(
    cmd: list[str],
    env_extra: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """Execute Blender subprocess. Returns (returncode, stderr, gpu_info)."""
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    # Do NOT set LIBGL_ALWAYS_SOFTWARE — we want GPU rendering on DGX
    env.pop("LIBGL_ALWAYS_SOFTWARE", None)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(cwd) if cwd else None,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=RENDER_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, f"Blender timed out after {RENDER_TIMEOUT}s", ""

    stdout_text = stdout.decode() if stdout else ""
    stderr_text = stderr.decode() if stderr else ""

    # Extract GPU info from Blender output
    gpu_info = ""
    for line in (stdout_text + stderr_text).splitlines():
        if line.startswith("GPU:"):
            gpu_info = line
            break

    return proc.returncode, stderr_text, gpu_info
