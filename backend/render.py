import asyncio
import base64
import json
import os
import shutil
from pathlib import Path

import httpx

from . import session as sess


def _on_task_done(session_id: str, task: asyncio.Task) -> None:
    """Callback to catch unhandled exceptions in background tasks and set error state."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"[render] Background task for {session_id} failed: {exc}")
        asyncio.ensure_future(
            sess.update_state(session_id, {"status": "error", "error": f"Background task failed: {exc}"})
        )

BLENDER_SCRIPT = Path(__file__).parent / "blender_scene.py"
BLENDER_BIN = os.environ.get("BLENDER_BIN", "blender")

# DGX Blender service (port 8005)
DGX_HOST = os.environ.get("DGX_HOST", "192.168.0.200")
DGX_BLENDER_URL = f"http://{DGX_HOST}:8005"

# Cache DGX Blender availability (checked once, then reused)
_dgx_blender_available: bool | None = None


async def check_dgx_blender() -> bool:
    """Check if DGX Blender service is available. Result is cached."""
    global _dgx_blender_available
    if _dgx_blender_available is not None:
        return _dgx_blender_available
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{DGX_BLENDER_URL}/health")
            data = resp.json()
            _dgx_blender_available = data.get("status") == "ok"
    except Exception:
        _dgx_blender_available = False
    print(f"[render] DGX Blender available: {_dgx_blender_available}")
    return _dgx_blender_available


def reset_dgx_blender_cache() -> None:
    """Reset cached DGX Blender status (e.g. after service start/stop)."""
    global _dgx_blender_available
    _dgx_blender_available = None


async def trigger_render(session_id: str) -> None:
    """Start an async Blender render in the background. Updates state when done."""
    sess.archive_render(session_id)
    await sess.update_state(session_id, {"status": "rendering"})

    session_dir = sess.get_session_dir(session_id).resolve()
    input_json = session_dir / "room_data.json"
    output_png = session_dir / "render_current.png"
    depth_png  = session_dir / "render_depth.png"

    task = asyncio.create_task(_run_blender(session_id, str(input_json), str(output_png),
                                            str(depth_png), str(session_dir)))
    task.add_done_callback(lambda t: _on_task_done(session_id, t))


async def _run_blender(session_id: str, input_json: str, output_png: str,
                       depth_png: str = "", session_dir: str = "") -> None:
    try:
        # Try DGX Blender first (NVIDIA GPU, faster)
        if await check_dgx_blender():
            success = await _run_blender_dgx(session_id, input_json, output_png,
                                              depth_png, session_dir)
            if success:
                state = sess.get_state(session_id)
                iterations = state.get("iterations", 0) + 1
                await sess.update_state(session_id, {"status": "done", "iterations": iterations})
                await _maybe_generate_preview(session_id)
                return

        # Fallback: local Blender subprocess
        await _run_blender_local(session_id, input_json, output_png, depth_png, session_dir)
    except Exception as e:
        await sess.update_state(session_id, {"status": "error", "error": str(e)})


async def _run_blender_dgx(session_id: str, input_json: str, output_png: str,
                            depth_png: str, session_dir: str) -> bool:
    """Render via DGX Blender service. Returns True on success."""
    try:
        # Read room_data
        room_data = json.loads(Path(input_json).read_text())

        # Collect CHORD materials if present
        chord_materials: dict[str, str] = {}
        chord_dir = Path(session_dir) / "materials" / "chord"
        if chord_dir.exists():
            for surface_dir in chord_dir.iterdir():
                if surface_dir.is_dir():
                    for map_file in surface_dir.iterdir():
                        if map_file.suffix == ".png":
                            key = f"{surface_dir.name}_{map_file.stem}"
                            chord_materials[key] = base64.b64encode(
                                map_file.read_bytes()
                            ).decode()

        # Collect furniture GLBs if present
        furniture_files: dict[str, str] = {}
        furniture_dir = Path(session_dir) / "furniture"
        if furniture_dir.exists():
            for glb_file in furniture_dir.glob("*.glb"):
                furniture_files[glb_file.stem] = base64.b64encode(
                    glb_file.read_bytes()
                ).decode()

        payload = {
            "room_data": room_data,
            "camera": room_data.get("_camera_id", 0),
        }
        if chord_materials:
            payload["chord_materials"] = chord_materials
        if furniture_files:
            payload["furniture_files"] = furniture_files

        print(f"[render] Sending to DGX Blender ({DGX_BLENDER_URL})...")

        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(f"{DGX_BLENDER_URL}/render", json=payload)
            resp.raise_for_status()
            data = resp.json()

        # Write render output
        render_b64 = data.get("render_b64", "")
        if render_b64:
            Path(output_png).write_bytes(base64.b64decode(render_b64))
        else:
            return False

        # Write depth output
        depth_b64 = data.get("depth_b64", "")
        if depth_b64 and depth_png:
            Path(depth_png).write_bytes(base64.b64decode(depth_b64))

        gpu = data.get("gpu_used", "")
        render_time = data.get("render_time_s", 0)
        print(f"[render] DGX Blender done: {render_time}s, {gpu}")
        return True

    except Exception as e:
        print(f"[render] DGX Blender failed, falling back to local: {e}")
        reset_dgx_blender_cache()
        return False


async def _run_blender_local(session_id: str, input_json: str, output_png: str,
                              depth_png: str, session_dir: str) -> None:
    """Render via local Blender subprocess (original logic)."""
    cmd = [
        BLENDER_BIN,
        "--background",
        "--python", str(BLENDER_SCRIPT),
        "--",
        "--input", input_json,
        "--output", output_png,
        "--depth-output", depth_png,
        "--session-dir", session_dir,
    ]

    returncode, stderr_text = await _exec_blender(cmd)

    # GPU crash → retry with CPU
    if returncode != 0 and stderr_text and "Memory access fault" in stderr_text:
        print(f"[render] GPU crash detected, retrying with CPU for session {session_id}")
        env_cpu = {**os.environ, "LIBGL_ALWAYS_SOFTWARE": "1", "CYCLES_DEVICE": "CPU"}
        returncode, stderr_text = await _exec_blender(cmd, env_override=env_cpu)

    if returncode == 0:
        state = sess.get_state(session_id)
        iterations = state.get("iterations", 0) + 1
        await sess.update_state(session_id, {"status": "done", "iterations": iterations})
        await _maybe_generate_preview(session_id)
    else:
        error_msg = stderr_text[-2000:] if stderr_text else "unknown error"
        await sess.update_state(session_id, {"status": "error", "error": error_msg})


async def _exec_blender(cmd: list[str], env_override: dict | None = None) -> tuple[int, str]:
    """Execute Blender subprocess with timeout. Returns (returncode, stderr_text)."""
    env = env_override or {**os.environ, "LIBGL_ALWAYS_SOFTWARE": "1"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return (-1, "Blender render timed out after 180s")
    return (proc.returncode, stderr.decode() if stderr else "")


async def _maybe_generate_preview(session_id: str) -> None:
    """Phase 6: After a successful Blender render, generate a quick photorealistic preview."""
    try:
        from .ai_render import generate_quick_preview, check_flux_health

        if not await check_flux_health():
            return  # Flux not available — skip preview silently

        render_path = sess.get_render_path(session_id)
        depth_path = sess.get_depth_path(session_id)
        preview_path = sess.get_preview_path(session_id)
        room_data = sess.get_room_data(session_id)

        if not render_path.exists() or not room_data:
            return

        depth_str = str(depth_path) if depth_path.exists() else ""
        await generate_quick_preview(
            str(render_path), depth_str, room_data, str(preview_path)
        )
        await sess.update_state(session_id, {"preview_ready": True})
    except Exception as e:
        print(f"[preview] Quick preview failed (non-fatal): {e}")


async def trigger_material_extraction(session_id: str) -> None:
    """Phase 3: Extract CHORD PBR materials from the original photo."""
    try:
        from .material_extract import extract_and_generate_materials

        session_dir = sess.get_session_dir(session_id)
        room_data = sess.get_room_data(session_id)

        # Find original photo
        photo = None
        for ext in ("jpg", "jpeg", "png", "webp"):
            candidate = session_dir / f"original_photo.{ext}"
            if candidate.exists():
                photo = candidate
                break

        if not photo or not room_data:
            return

        await extract_and_generate_materials(str(photo), room_data, str(session_dir))
    except Exception as e:
        print(f"[materials] CHORD extraction failed (non-fatal): {e}")


async def trigger_furniture_generation(session_id: str) -> None:
    """Phase 4: Generate 3D furniture models from photo crops via TRELLIS.2."""
    try:
        from .furniture_3d import generate_all_furniture

        session_dir = sess.get_session_dir(session_id)
        room_data = sess.get_room_data(session_id)

        photo = None
        for ext in ("jpg", "jpeg", "png", "webp"):
            candidate = session_dir / f"original_photo.{ext}"
            if candidate.exists():
                photo = candidate
                break

        if not photo or not room_data:
            return

        await generate_all_furniture(str(photo), room_data, str(session_dir))
    except Exception as e:
        print(f"[furniture] TRELLIS.2 generation failed (non-fatal): {e}")


async def render_camera(session_id: str, camera_id: int,
                        output_png: str, depth_png: str = "") -> bool:
    """Render a specific camera angle. Does not change session state."""
    # Try DGX first
    if await check_dgx_blender():
        session_dir = sess.get_session_dir(session_id).resolve()
        input_json = session_dir / "room_data.json"
        if input_json.exists():
            room_data = json.loads(input_json.read_text())
            room_data["_camera_id"] = camera_id
            try:
                payload = {"room_data": room_data, "camera": camera_id}
                async with httpx.AsyncClient(timeout=300.0) as client:
                    resp = await client.post(f"{DGX_BLENDER_URL}/render", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                render_b64 = data.get("render_b64", "")
                if render_b64:
                    Path(output_png).write_bytes(base64.b64decode(render_b64))
                depth_b64 = data.get("depth_b64", "")
                if depth_b64 and depth_png:
                    Path(depth_png).write_bytes(base64.b64decode(depth_b64))
                return bool(render_b64)
            except Exception as e:
                print(f"[render] DGX camera render failed, trying local: {e}")

    # Fallback: local Blender
    session_dir = sess.get_session_dir(session_id).resolve()
    input_json = session_dir / "room_data.json"
    if not input_json.exists():
        return False

    cmd = [
        BLENDER_BIN,
        "--background",
        "--python", str(BLENDER_SCRIPT),
        "--",
        "--input", str(input_json),
        "--output", output_png,
        "--depth-output", depth_png,
        "--session-dir", str(session_dir),
        "--camera", str(camera_id),
    ]
    returncode, stderr_text = await _exec_blender(cmd)
    if returncode != 0 and stderr_text and "Memory access fault" in stderr_text:
        env_cpu = {**os.environ, "LIBGL_ALWAYS_SOFTWARE": "1", "CYCLES_DEVICE": "CPU"}
        returncode, stderr_text = await _exec_blender(cmd, env_override=env_cpu)
    return returncode == 0


def blender_available() -> bool:
    """True if Blender is available (local OR DGX remote)."""
    if shutil.which(BLENDER_BIN) is not None:
        return True
    # No local Blender — check if DGX Blender is cached as available
    return _dgx_blender_available is True
