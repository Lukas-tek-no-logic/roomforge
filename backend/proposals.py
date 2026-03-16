import asyncio
import json
import os
from pathlib import Path

from . import session as sess
from .claude_api import ask_text, parse_json_array
from .render import BLENDER_BIN, BLENDER_SCRIPT

# Lock to protect concurrent writes to proposals status.json
_proposals_lock = asyncio.Lock()


def _on_background_task_done(session_id: str, pdir: Path, task: asyncio.Task) -> None:
    """Catch unhandled exceptions in proposals background tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"[proposals] Background task for {session_id} failed: {exc}")
        try:
            (pdir / "status.json").write_text(json.dumps({"status": "error", "error": str(exc)}))
        except Exception:
            pass

def _validate_idx(idx: int) -> None:
    """Raise ValueError if idx is not a non-negative integer."""
    if not isinstance(idx, int) or idx < 0:
        raise ValueError(f"Invalid proposal index: {idx!r}")


STYLES = [
    ("Modern Minimalist",  "Biały, szary, czyste linie. Brak zbędnych ozdób, ukryte instalacje."),
    ("Scandinavian Warm",  "Jasne drewno, biel, miękkie kolory. Przytulnie i funkcjonalnie."),
    ("Industrial Loft",    "Ciemny metal, cegła, odsłonięte rury. Surowy ale stylowy charakter."),
    ("Classic Elegant",    "Kremowe kolory, drewniane szafki, dekoracyjne kafelki. Ponadczasowy styl."),
]

PROPOSAL_PROMPT = """You are an expert interior designer specializing in laundry rooms.

Room dimensions: {W}m wide × {D}m deep × {H}m tall.
{base_info}

Generate {n} distinct laundry room design proposals in these styles:
{styles_list}

Return a JSON array with exactly {n} objects. Each object must be a complete room_data with these extra fields added:
- "style_name": the style name (string)
- "style_description": 1-2 sentence Polish description of this design

Each room_data must have: room, walls, floor, ceiling, furniture, openings, lighting.

Rules:
- Furniture must fit inside the room (positions within ±W/2, ±D/2)
- Include: at least a washing machine, dryer or second washer, storage shelves/cabinets
- Coherent color palette matching the style
- Realistic sizes (washing machine ~0.6×0.6×0.85m)
- Vary colors, materials, furniture arrangement between styles

Return ONLY the JSON array, no markdown, no explanation."""


def _run_claude(prompt: str) -> str:
    return ask_text(prompt)


def proposals_dir(session_id: str) -> Path:
    d = sess.get_session_dir(session_id) / "proposals"
    d.mkdir(exist_ok=True)
    return d


async def generate_proposals(session_id: str, n: int = 4) -> None:
    """Generate n design proposals and render each. Runs fully async in background."""
    pdir = proposals_dir(session_id)

    # Mark as generating
    (pdir / "status.json").write_text(json.dumps({"status": "generating", "total": n, "done": 0}))

    room_data = sess.get_room_data(session_id)
    room = room_data.get("room", {})
    W = room.get("width",  3.0)
    D = room.get("depth",  3.0)
    H = room.get("height", 2.5)

    base_info = ""
    if room_data:
        base_info = f"Current room base (walls, floor, openings) for reference:\n{json.dumps({'walls': room_data.get('walls',{}), 'floor': room_data.get('floor',{}), 'openings': room_data.get('openings',[])}, indent=2)}"

    styles_list = "\n".join(f"{i+1}. {name}: {desc}" for i, (name, desc) in enumerate(STYLES[:n]))

    prompt = PROPOSAL_PROMPT.format(
        W=W, D=D, H=H,
        base_info=base_info,
        n=n,
        styles_list=styles_list,
    )

    task = asyncio.create_task(_run_and_render(session_id, prompt, n, pdir))
    task.add_done_callback(lambda t: _on_background_task_done(session_id, pdir, t))


async def _run_and_render(session_id: str, prompt: str, n: int, pdir: Path) -> None:
    try:
        # Run Claude in executor (blocking subprocess)
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, _run_claude, prompt)
        proposals = parse_json_array(raw)
    except Exception as e:
        (pdir / "status.json").write_text(json.dumps({"status": "error", "error": str(e)}))
        return

    # Save each proposal and start rendering
    render_tasks = []
    for i, proposal in enumerate(proposals[:n]):
        ppath = pdir / str(i)
        ppath.mkdir(exist_ok=True)
        style_name = proposal.pop("style_name", f"Proposal {i+1}")
        style_desc = proposal.pop("style_description", "")
        (ppath / "room_data.json").write_text(json.dumps(proposal, indent=2))
        (ppath / "meta.json").write_text(json.dumps({"style_name": style_name, "style_description": style_desc}))
        (ppath / "status.json").write_text(json.dumps({"status": "rendering"}))
        render_tasks.append(_render_proposal(i, ppath, pdir, n))

    # Update overall status
    (pdir / "status.json").write_text(json.dumps({"status": "rendering", "total": n, "done": 0}))

    await asyncio.gather(*render_tasks, return_exceptions=True)

    # Final status
    done = sum(1 for i in range(n) if (pdir / str(i) / "render.png").exists())
    (pdir / "status.json").write_text(json.dumps({"status": "done", "total": n, "done": done}))


async def _render_proposal(idx: int, ppath: Path, pdir: Path, total: int) -> None:
    input_json = str(ppath / "room_data.json")
    output_png = str(ppath / "render.png")

    cmd = [
        BLENDER_BIN, "--background",
        "--python", str(BLENDER_SCRIPT),
        "--", "--input", input_json, "--output", output_png,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "LIBGL_ALWAYS_SOFTWARE": "1"},
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        (ppath / "status.json").write_text(json.dumps({"status": "error", "error": "Blender render timed out after 120s"}))
        return

    if proc.returncode == 0:
        (ppath / "status.json").write_text(json.dumps({"status": "done"}))
    else:
        err = stderr.decode()[-300:] if stderr else "unknown"
        (ppath / "status.json").write_text(json.dumps({"status": "error", "error": err}))

    # Update overall done count (locked to avoid concurrent write corruption)
    try:
        async with _proposals_lock:
            overall = json.loads((pdir / "status.json").read_text())
            overall["done"] = sum(
                1 for i in range(total)
                if (pdir / str(i) / "status.json").exists()
                and json.loads((pdir / str(i) / "status.json").read_text()).get("status") == "done"
            )
            (pdir / "status.json").write_text(json.dumps(overall))
    except Exception:
        pass


def get_proposals_status(session_id: str) -> dict:
    pdir = proposals_dir(session_id)
    status_file = pdir / "status.json"
    if not status_file.exists():
        return {"status": "none"}

    overall = json.loads(status_file.read_text())
    n = overall.get("total", 4)

    items = []
    for i in range(n):
        ppath = pdir / str(i)
        if not ppath.exists():
            continue
        st = {}
        if (ppath / "status.json").exists():
            st = json.loads((ppath / "status.json").read_text())
        meta = {}
        if (ppath / "meta.json").exists():
            meta = json.loads((ppath / "meta.json").read_text())
        items.append({
            "index": i,
            "status": st.get("status", "pending"),
            "style_name": meta.get("style_name", f"Proposal {i+1}"),
            "style_description": meta.get("style_description", ""),
            "render_ready": (ppath / "render.png").exists(),
        })

    overall["proposals"] = items
    return overall


def get_proposal_render(session_id: str, idx: int) -> Path:
    _validate_idx(idx)
    p = proposals_dir(session_id) / str(idx) / "render.png"
    if not p.exists():
        raise FileNotFoundError(f"Proposal {idx} render not ready")
    return p


async def select_proposal(session_id: str, idx: int) -> dict:
    """Copy proposal room_data as the active scene and trigger re-render."""
    _validate_idx(idx)
    ppath = proposals_dir(session_id) / str(idx)
    room_data_path = ppath / "room_data.json"
    if not room_data_path.exists():
        raise FileNotFoundError(f"Proposal {idx} not found")
    room_data = json.loads(room_data_path.read_text())
    await sess.save_room_data(session_id, room_data)
    return room_data
