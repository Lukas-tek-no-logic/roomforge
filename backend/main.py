import asyncio
import base64
import io
from pathlib import Path

import aiofiles
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import session as sess
from .analyze import analyze_photo
from .modify import modify_from_annotation, modify_from_text
from .render import (
    blender_available, trigger_render, render_camera,
    trigger_material_extraction, trigger_furniture_generation,
)
from . import proposals as prop
from .ai_render import (
    generate_ai_render, generate_finalized_render, generate_controlled_render,
    generate_inpaint_render, generate_draft_render,
    check_flux_health, room_data_to_prompt, DGX_URL,
    analyze_inspiration,
)
from . import dgx_manager as dgx
from .render import _on_task_done

app = FastAPI(title="Interior Room Designer")


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# Serve frontend static files
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def root():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


# ── Room templates ────────────────────────────────────────────────────────────

TEMPLATES_DIR = Path(__file__).parent.parent / "sessions" / "room_templates"


@app.get("/room-templates")
async def list_room_templates():
    """List available pre-built room templates from floor plans."""
    templates = []
    if TEMPLATES_DIR.exists():
        for f in sorted(TEMPLATES_DIR.glob("*.json")):
            import json as _json
            data = _json.loads(f.read_text())
            templates.append({
                "id": f.stem,
                "label": data.get("room_label", f.stem),
                "room_type": data.get("room_type", f.stem),
                "dimensions": data.get("room", {}),
            })
    return {"templates": templates}


@app.post("/sessions/{session_id}/load-template/{template_id}")
async def load_room_template(session_id: str, template_id: str):
    """Load a pre-built room template into a session. Triggers Blender render."""
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    template_path = TEMPLATES_DIR / f"{template_id}.json"
    if not template_path.exists():
        raise HTTPException(404, f"Template '{template_id}' not found")

    import json as _json
    room_data = _json.loads(template_path.read_text())
    await sess.save_room_data(session_id, room_data)

    if blender_available():
        await trigger_render(session_id)
    else:
        await sess.update_state(session_id, {"status": "no_blender"})

    return {"status": "ok", "room_type": room_data.get("room_type"),
            "room_label": room_data.get("room_label")}


# ── Generate room from text description ───────────────────────────────────────

ROOM_FROM_DESC_PROMPT = """You are an interior design expert. Create a complete room_data JSON from this description:

"{description}"

Return ONLY valid JSON with this structure:
{{
  "room_type": "<type like salon, sypialnia, kuchnia, lazienka, pokoj_dziecka, gabinet>",
  "room_label": "<human readable room name>",
  "room": {{"width": <float m>, "depth": <float m>, "height": <float m>}},
  "walls": {{"color": "<hex>", "material": "<paint|tile|brick|concrete>", "texture_roughness": <0-1>}},
  "floor": {{"color": "<hex>", "material": "<wood|tile|concrete|vinyl>", "texture_roughness": <0-1>}},
  "ceiling": {{"color": "<hex>", "height": <float>}},
  "furniture": [
    {{"id": "<unique>", "type": "<sofa|bed|table|chair|armchair|shelf|bookshelf|cabinet|desk|tv|lamp|plant|mirror|rug|wall_art|painting|curtain|vase|wardrobe|dresser|nightstand|decor|ottoman>",
      "label": "<detailed description for AI rendering>",
      "position": {{"x": <float>, "y": <float>, "z": <float>}},
      "size": {{"width": <float>, "depth": <float>, "height": <float>}},
      "color": "<hex>", "material": "<fabric|wood|metal|ceramic|leather|glass|plastic|generic>"}}
  ],
  "openings": [{{"type": "<window|door>", "wall": "<north|south|east|west>", "position_along_wall": <0-1>, "width": <float>, "height": <float>, "bottom_height": <float>}}],
  "lighting": {{"type": "<natural|ceiling_lamp|fluorescent>", "intensity": <0-5>, "color": "<hex>"}},
  "architectural": {{}}
}}

Rules:
- Use realistic dimensions in meters
- Place furniture naturally, spread throughout the room (x=left-right, y=front-back, z=up, center is 0,0)
- Include detailed labels that describe the exact appearance for AI image generation
- Add architectural features if mentioned (mezzanine, fireplace, stairs)
- For architectural: {{"mezzanine": {{"height": <m>, "depth": <m>, "wall": "north", "railing": "glass_metal"}}, "stairs": {{"position": "northwest", "treads": "walnut"}}, "fireplace": {{"wall": "north", "type": "built_in", "surround": "concrete"}}}}"""


@app.post("/sessions/{session_id}/generate-room")
async def generate_room_from_description(session_id: str, description: str = Form(...)):
    """Generate a complete room from a text description. No photo needed.
    Example: 'Modern 5x4m living room with double height ceiling, walnut floor,
    gray sofa, fireplace, mezzanine with glass railing'"""
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    await sess.update_state(session_id, {"status": "generating_room"})

    try:
        from .claude_api import ask_text, parse_json
        prompt = ROOM_FROM_DESC_PROMPT.format(description=description)
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, ask_text, prompt)
        room_data = parse_json(raw)
    except Exception as e:
        await sess.update_state(session_id, {"status": "error", "error": f"Room generation failed: {e}"})
        raise HTTPException(500, f"Room generation failed: {e}")

    await sess.save_room_data(session_id, room_data)

    if blender_available():
        await trigger_render(session_id)
    else:
        await sess.update_state(session_id, {"status": "no_blender"})

    return {"status": "ok", "room_data": room_data}


# ── Create room from floor plan + optional photos ─────────────────────────────

FLOORPLAN_PROMPT = """Analyze this floor plan. List each room with dimensions in meters (convert cm to m).
{extra_context}
Return ONLY a JSON array: [{{"name":"Room Name","type":"salon","width":5.0,"depth":4.0,"doors":1,"windows":2}}]
Types: salon, kuchnia, sypialnia, lazienka, pokoj_dziecka, gabinet, korytarz, garderoba, garaz.
No markdown, no explanation — just the JSON array."""

FLOORPLAN_WITH_PHOTOS_PROMPT = """You are an architect and interior designer. I'm showing you:
1. A floor plan with room dimensions
2. Photos of the actual room(s)

From the floor plan, extract exact room dimensions, door/window positions.
From the photos, extract materials, colors, furniture style, and lighting.

{extra_context}

Focus on the room described as: "{room_focus}"

Return a single room_data JSON object (not an array) with the same structure as before but enriched with materials and furniture from the photos. Return ONLY valid JSON."""


@app.post("/sessions/{session_id}/upload-floorplan")
async def upload_floorplan(
    session_id: str,
    floorplan: UploadFile = File(...),
    photos: list[UploadFile] = File(default=[]),
    room_focus: str = Form(default=""),
    description: str = Form(default=""),
):
    """Upload a floor plan (PDF/image) and optionally room photos.
    Claude extracts dimensions from the plan and materials from the photos.
    If room_focus is specified, generates room_data for that specific room.
    Otherwise, returns a list of all rooms found in the plan."""
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    session_dir = sess.get_session_dir(session_id)

    # Save floor plan
    ext = (floorplan.filename or "plan.pdf").rsplit(".", 1)[-1].lower()
    plan_path = session_dir / f"floorplan.{ext}"
    async with aiofiles.open(str(plan_path), "wb") as f:
        await f.write(await floorplan.read())

    # If PDF, we need to convert first page to image for Claude
    if ext == "pdf":
        plan_image_path = await _pdf_to_image(str(plan_path), str(session_dir / "floorplan_page1.png"))
    else:
        plan_image_path = str(plan_path)

    # Save room photos if provided
    photo_paths = []
    for i, photo in enumerate(photos):
        p_ext = (photo.filename or "photo.jpg").rsplit(".", 1)[-1].lower()
        p_path = session_dir / f"room_photo_{i}.{p_ext}"
        async with aiofiles.open(str(p_path), "wb") as f:
            await f.write(await photo.read())
        photo_paths.append(str(p_path))

    await sess.update_state(session_id, {"status": "analyzing_floorplan"})

    extra_context = f'Additional context from user: "{description}"' if description else ""

    try:
        from .claude_api import ask_with_image, parse_json, parse_json_array

        loop = asyncio.get_event_loop()

        if photo_paths and room_focus:
            # Floor plan + photos + specific room → single enriched room_data
            # Send floor plan image to Claude with the photo
            prompt = FLOORPLAN_WITH_PHOTOS_PROMPT.format(
                extra_context=extra_context, room_focus=room_focus)
            # For now, analyze floor plan first, then combine
            raw = await loop.run_in_executor(None, ask_with_image, prompt, plan_image_path)
            room_data = parse_json(raw)

            await sess.save_room_data(session_id, room_data)
            if blender_available():
                await trigger_render(session_id)
            return {"status": "ok", "mode": "single_room", "room_data": room_data}

        else:
            # Floor plan only → extract basic room dimensions, then expand
            prompt = FLOORPLAN_PROMPT.format(extra_context=extra_context)
            raw = await loop.run_in_executor(None, ask_with_image, prompt, plan_image_path)

            try:
                basic_rooms = parse_json_array(raw)
            except Exception:
                basic_rooms = [parse_json(raw)]

            # Expand basic {name, type, width, depth} into full room_data
            rooms = []
            for br in basic_rooms:
                room_data = _expand_basic_room(br)
                rooms.append(room_data)

            # Save all rooms as templates in session
            rooms_dir = session_dir / "extracted_rooms"
            rooms_dir.mkdir(exist_ok=True)
            import json as _json
            room_list = []
            for room in rooms:
                rid = room.get("room_type", "room") + "_" + room.get("room_label", "").replace(" ", "_")[:20].lower()
                (rooms_dir / f"{rid}.json").write_text(_json.dumps(room, indent=2))
                room_list.append({
                    "id": rid,
                    "label": room.get("room_label", rid),
                    "room_type": room.get("room_type", ""),
                    "dimensions": room.get("room", {}),
                })

            # Load first room by default
            if rooms:
                await sess.save_room_data(session_id, rooms[0])
                if blender_available():
                    await trigger_render(session_id)

            return {"status": "ok", "mode": "multi_room", "rooms": room_list,
                    "loaded": room_list[0]["label"] if room_list else None}

    except Exception as e:
        await sess.update_state(session_id, {"status": "error", "error": str(e)})
        raise HTTPException(500, f"Floor plan analysis failed: {e}")


def _expand_basic_room(br: dict) -> dict:
    """Expand a basic {name, type, width, depth} into a full room_data JSON."""
    name = br.get("name", "Room")
    rtype = br.get("type", "salon")
    w = float(br.get("width", 4.0))
    d = float(br.get("depth", 3.0))
    h = 2.7
    n_doors = int(br.get("doors", 1))
    n_windows = int(br.get("windows", 1))

    # Default materials by room type
    floor_mats = {
        "salon": ("wood", "#C49A6C"), "kuchnia": ("tile", "#D4D0CC"),
        "sypialnia": ("wood", "#C49A6C"), "lazienka": ("tile", "#C8C0B8"),
        "pokoj_dziecka": ("wood", "#C49A6C"), "gabinet": ("wood", "#C49A6C"),
        "korytarz": ("tile", "#D4D0CC"), "garderoba": ("wood", "#C49A6C"),
        "garaz": ("concrete", "#A0A0A0"),
    }
    floor_mat, floor_color = floor_mats.get(rtype, ("wood", "#C49A6C"))

    openings = []
    walls = ["east", "west", "north", "south"]
    for i in range(min(n_windows, 3)):
        openings.append({"type": "window", "wall": walls[i % 4],
                         "position_along_wall": 0.5, "width": 1.0, "height": 1.5, "bottom_height": 0.8})
    for i in range(min(n_doors, 2)):
        openings.append({"type": "door", "wall": walls[(i + 2) % 4],
                         "position_along_wall": 0.3, "width": 0.9, "height": 2.1, "bottom_height": 0.0})

    return {
        "room_type": rtype,
        "room_label": name,
        "room": {"width": w, "depth": d, "height": h},
        "walls": {"color": "#F0EDE8", "material": "paint", "texture_roughness": 0.12},
        "floor": {"color": floor_color, "material": floor_mat, "texture_roughness": 0.35},
        "ceiling": {"color": "#FFFFFF", "height": h},
        "furniture": [],
        "openings": openings,
        "lighting": {"type": "natural", "intensity": 3.0, "color": "#FFF8E7"},
        "architectural": {},
    }


async def _pdf_to_image(pdf_path: str, output_path: str) -> str:
    """Convert first page of PDF to PNG image for Claude analysis."""
    try:
        # Try pdftoppm (from poppler-utils)
        proc = await asyncio.create_subprocess_exec(
            "pdftoppm", "-png", "-f", "1", "-l", "1", "-r", "200",
            "-singlefile", pdf_path, output_path.replace(".png", ""),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        # pdftoppm appends .png to the output name
        candidate = output_path.replace(".png", "") + ".png"
        if Path(candidate).exists():
            return candidate
    except Exception:
        pass

    try:
        # Fallback: use Pillow if pdf2image is available
        from PIL import Image
        # Try to read PDF as image directly (works for some single-page PDFs)
        img = Image.open(pdf_path)
        img.save(output_path, "PNG")
        return output_path
    except Exception:
        pass

    # Last resort: return the PDF path itself (Claude can read PDFs)
    return pdf_path


# ── Create style profile from inspiration images ─────────────────────────────

@app.post("/sessions/{session_id}/create-style")
async def create_style_profile(session_id: str, files: list[UploadFile] = File(...),
                                style_name: str = Form("Custom Style")):
    """Upload 1-10 inspiration images. Claude analyzes them all and creates a unified
    style profile stored in the session. This overrides the default house style for this session."""
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    session_dir = sess.get_session_dir(session_id)
    style_dir = session_dir / "style_references"
    style_dir.mkdir(exist_ok=True)

    # Save uploaded images
    image_paths = []
    for i, file in enumerate(files[:10]):
        ext = (file.filename or "img.jpg").rsplit(".", 1)[-1].lower()
        path = style_dir / f"ref_{i:02d}.{ext}"
        async with aiofiles.open(str(path), "wb") as f:
            await f.write(await file.read())
        image_paths.append(str(path))

    # Analyze each image and combine descriptions
    from .ai_render import analyze_inspiration
    descriptions = []
    for path in image_paths:
        try:
            loop = asyncio.get_event_loop()
            desc = await loop.run_in_executor(None, analyze_inspiration, path)
            descriptions.append(desc)
        except Exception as e:
            print(f"[style] Failed to analyze {path}: {e}")

    if not descriptions:
        raise HTTPException(500, "Failed to analyze any inspiration images")

    # Combine into unified style
    combined = "; ".join(descriptions)

    # Save as session-level style
    style_data = {
        "name": style_name,
        "description": combined,
        "num_references": len(descriptions),
    }

    import json as _json
    (session_dir / "session_style.json").write_text(_json.dumps(style_data, indent=2))
    (session_dir / "inspiration_desc.txt").write_text(combined)

    return {"status": "ok", "style_name": style_name,
            "description": combined[:500] + "..." if len(combined) > 500 else combined,
            "num_references": len(descriptions)}


@app.post("/sessions/{session_id}/load-extracted-room/{room_id}")
async def load_extracted_room(session_id: str, room_id: str):
    """Load a room extracted from a floor plan analysis."""
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    room_path = sess.get_session_dir(session_id) / "extracted_rooms" / f"{room_id}.json"
    if not room_path.exists():
        raise HTTPException(404, f"Extracted room '{room_id}' not found")

    import json as _json
    room_data = _json.loads(room_path.read_text())
    await sess.save_room_data(session_id, room_data)

    if blender_available():
        await trigger_render(session_id)

    return {"status": "ok", "room_type": room_data.get("room_type"),
            "room_label": room_data.get("room_label")}


# ── Sessions ──────────────────────────────────────────────────────────────────

@app.post("/sessions")
async def create_session():
    session_id = sess.create_session()
    return {"session_id": session_id}


@app.get("/sessions/{session_id}/status")
async def get_status(session_id: str):
    try:
        state = sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")
    return state


# ── Upload & first render ──────────────────────────────────────────────────────

@app.post("/sessions/{session_id}/upload")
async def upload_photo(session_id: str, file: UploadFile = File(...)):
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    # Save original photo
    ext = (file.filename or "photo.jpg").rsplit(".", 1)[-1].lower()
    photo_path = sess.get_session_dir(session_id) / f"original_photo.{ext}"
    async with aiofiles.open(str(photo_path), "wb") as f:
        content = await file.read()
        await f.write(content)

    await sess.update_state(session_id, {"status": "analyzing"})

    try:
        room_data = analyze_photo(str(photo_path))
    except Exception as e:
        await sess.update_state(session_id, {"status": "error", "error": f"Analysis failed: {e}"})
        raise HTTPException(500, f"Analysis failed: {e}")

    await sess.save_room_data(session_id, room_data)

    # Phase 3+4: Kick off material extraction and furniture generation in background
    task = asyncio.create_task(_extract_assets(session_id))
    task.add_done_callback(lambda t: _on_task_done(session_id, t))

    if blender_available():
        await trigger_render(session_id)
    else:
        await sess.update_state(session_id, {"status": "no_blender"})

    return {"status": "ok", "room_data": room_data}


async def _extract_assets(session_id: str) -> None:
    """Background task: extract CHORD materials and generate 3D furniture."""
    await asyncio.gather(
        trigger_material_extraction(session_id),
        trigger_furniture_generation(session_id),
        return_exceptions=True,
    )


# ── Upload inspiration image ──────────────────────────────────────────────────

@app.post("/sessions/{session_id}/upload-inspiration")
async def upload_inspiration(session_id: str, file: UploadFile = File(...)):
    """Upload a design inspiration image. Claude analyzes it and stores a style description
    that will be used to guide the finalization render."""
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    ext = (file.filename or "inspo.jpg").rsplit(".", 1)[-1].lower()
    inspo_path = sess.get_session_dir(session_id) / f"inspiration.{ext}"
    async with aiofiles.open(str(inspo_path), "wb") as f:
        content = await file.read()
        await f.write(content)

    try:
        loop = asyncio.get_event_loop()
        desc = await loop.run_in_executor(None, analyze_inspiration, str(inspo_path))
    except Exception as e:
        raise HTTPException(500, f"Inspiration analysis failed: {e}")

    # Save the description alongside the image
    desc_path = sess.get_session_dir(session_id) / "inspiration_desc.txt"
    async with aiofiles.open(str(desc_path), "w") as f:
        await f.write(desc)

    return {"status": "ok", "description": desc}


# ── Upload video (Phase 5) ────────────────────────────────────────────────────

@app.post("/sessions/{session_id}/upload-video")
async def upload_video(session_id: str, file: UploadFile = File(...)):
    """Phase 5: Upload video for 3D reconstruction via DN-Splatter."""
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    ext = (file.filename or "video.mp4").rsplit(".", 1)[-1].lower()
    video_path = sess.get_session_dir(session_id) / f"original_video.{ext}"
    async with aiofiles.open(str(video_path), "wb") as f:
        content = await file.read()
        await f.write(content)

    await sess.update_state(session_id, {"status": "reconstructing"})

    task = asyncio.create_task(_reconstruct_and_render(session_id, str(video_path)))
    task.add_done_callback(lambda t: _on_task_done(session_id, t))
    return {"status": "reconstructing"}


async def _reconstruct_and_render(session_id: str, video_path: str) -> None:
    """Phase 5: Video → DN-Splatter → mesh → Blender scene → render."""
    try:
        from .reconstruct import reconstruct_from_video

        session_dir = str(sess.get_session_dir(session_id))
        room_data = await reconstruct_from_video(video_path, session_dir)

        await sess.save_room_data(session_id, room_data)
        await sess.update_state(session_id, {"status": "rendering"})

        if blender_available():
            await trigger_render(session_id)
        else:
            await sess.update_state(session_id, {"status": "no_blender"})
    except Exception as e:
        await sess.update_state(session_id, {"status": "error", "error": f"Reconstruction failed: {e}"})


# ── Render ────────────────────────────────────────────────────────────────────

@app.get("/sessions/{session_id}/render")
async def get_render(session_id: str):
    render_path = sess.get_render_path(session_id)
    if not render_path.exists():
        raise HTTPException(404, "Render not ready yet")
    return FileResponse(str(render_path), media_type="image/png")


# ── Preview (Phase 6) ────────────────────────────────────────────────────────

@app.get("/sessions/{session_id}/preview")
async def get_preview(session_id: str):
    """Phase 6: Return the quick photorealistic preview image."""
    preview_path = sess.get_preview_path(session_id)
    if not preview_path.exists():
        raise HTTPException(404, "Preview not ready yet")
    return FileResponse(str(preview_path), media_type="image/png")


# ── Chat (text command) ───────────────────────────────────────────────────────

@app.post("/sessions/{session_id}/chat")
async def chat(session_id: str, command: str = Form(...)):
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    try:
        new_room_data = await sess.modify_room_data(
            session_id, modify_from_text, command
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Modification failed: {e}")

    if blender_available():
        await trigger_render(session_id)
    else:
        await sess.update_state(session_id, {"status": "no_blender"})

    return {"status": "ok", "room_data": new_room_data}


# ── Annotation ────────────────────────────────────────────────────────────────

@app.post("/sessions/{session_id}/annotate")
async def annotate(
    session_id: str,
    image_b64: str = Form(...),
    comment: str = Form(...),
    x1: int = Form(...),
    y1: int = Form(...),
    x2: int = Form(...),
    y2: int = Form(...),
    render_w: int = Form(...),
    render_h: int = Form(...),
):
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    try:
        new_room_data = await sess.modify_room_data(
            session_id, modify_from_annotation,
            image_b64, comment, x1, y1, x2, y2, render_w, render_h
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Annotation modification failed: {e}")

    if blender_available():
        await trigger_render(session_id)
    else:
        await sess.update_state(session_id, {"status": "no_blender"})

    return {"status": "ok", "room_data": new_room_data}


# ── Inpainting (Phase 2) ─────────────────────────────────────────────────────

@app.post("/sessions/{session_id}/inpaint")
async def inpaint(
    session_id: str,
    mask_b64: str = Form(...),
    prompt: str = Form(...),
    strength: float = Form(0.85),
):
    """Phase 2: Inpainting — regenerate only the masked region of the current render."""
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    render_path = sess.get_render_path(session_id)
    finalized_path = sess.get_session_dir(session_id) / "finalized_render.png"
    # Use finalized render if available, otherwise Blender render
    source_path = finalized_path if finalized_path.exists() else render_path
    if not source_path.exists():
        raise HTTPException(400, "No render available for inpainting")

    if not await check_flux_health():
        raise HTTPException(503, "Flux service not available")

    # Save mask to temp file
    import tempfile
    mask_data = mask_b64
    if mask_data.startswith("data:"):
        mask_data = mask_data.split(",", 1)[1]
    mask_bytes = base64.b64decode(mask_data)

    session_dir = sess.get_session_dir(session_id)
    mask_path = session_dir / "inpaint_mask.png"
    mask_path.write_bytes(mask_bytes)

    output_path = session_dir / "inpaint_result.png"
    await sess.update_state(session_id, {"status": "inpainting"})

    task = asyncio.create_task(_run_inpaint(
        session_id, str(source_path), str(mask_path), prompt,
        str(output_path), strength,
    ))
    task.add_done_callback(lambda t: _on_task_done(session_id, t))
    return {"status": "inpainting"}


async def _run_inpaint(
    session_id: str, source_path: str, mask_path: str,
    prompt: str, output_path: str, strength: float,
):
    try:
        await generate_inpaint_render(
            source_path, mask_path, prompt, output_path, strength=strength,
        )
        await sess.update_state(session_id, {"status": "inpaint_done"})
    except Exception as e:
        await sess.update_state(session_id, {"status": "error", "error": str(e)})


@app.get("/sessions/{session_id}/inpaint")
async def get_inpaint_result(session_id: str):
    path = sess.get_session_dir(session_id) / "inpaint_result.png"
    if not path.exists():
        raise HTTPException(404, "Inpaint result not ready")
    return FileResponse(str(path), media_type="image/png")


# ── Proposals ─────────────────────────────────────────────────────────────────

@app.post("/sessions/{session_id}/proposals")
async def start_proposals(session_id: str, n: int = 4):
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")
    if not blender_available():
        raise HTTPException(400, "Blender not available")
    await prop.generate_proposals(session_id, n=n)
    return {"status": "generating"}


@app.get("/sessions/{session_id}/proposals")
async def get_proposals(session_id: str):
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")
    return prop.get_proposals_status(session_id)


@app.get("/sessions/{session_id}/proposals/{idx}/render")
async def get_proposal_render(session_id: str, idx: int):
    try:
        path = prop.get_proposal_render(session_id, idx)
    except FileNotFoundError:
        raise HTTPException(404, "Proposal render not ready")
    return FileResponse(str(path), media_type="image/png")


@app.post("/sessions/{session_id}/proposals/{idx}/select")
async def select_proposal(session_id: str, idx: int):
    try:
        room_data = await prop.select_proposal(session_id, idx)
    except FileNotFoundError:
        raise HTTPException(404, "Proposal not found")
    if blender_available():
        await trigger_render(session_id)
    return {"status": "ok", "room_data": room_data}


# ── AI Render (Flux on DGX Spark) ─────────────────────────────────────────────

@app.get("/ai-render/health")
async def ai_render_health():
    ok = await check_flux_health()
    return {"available": ok, "url": DGX_URL}


@app.post("/sessions/{session_id}/ai-render")
async def start_ai_render(session_id: str, style: str = "", seed: int = -1):
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    room_data = sess.get_room_data(session_id)
    if not room_data:
        raise HTTPException(400, "No scene data yet")

    if not await check_flux_health():
        raise HTTPException(503, "Flux service not available — start it on DGX Spark first")

    output_path = str(sess.get_session_dir(session_id) / "ai_render_current.png")
    await sess.update_state(session_id, {"status": "ai_rendering"})

    task = asyncio.create_task(_run_ai_render(session_id, room_data, output_path, style, seed))
    task.add_done_callback(lambda t: _on_task_done(session_id, t))
    prompt = room_data_to_prompt(room_data, style)
    return {"status": "generating", "prompt": prompt}


async def _run_ai_render(session_id: str, room_data: dict, output_path: str, style: str, seed: int):
    try:
        await generate_ai_render(room_data, output_path, style_override=style, seed=seed)
        await sess.update_state(session_id, {"status": "ai_done"})
    except Exception as e:
        await sess.update_state(session_id, {"status": "error", "error": str(e)})


@app.get("/sessions/{session_id}/ai-render")
async def get_ai_render(session_id: str):
    path = sess.get_session_dir(session_id) / "ai_render_current.png"
    if not path.exists():
        raise HTTPException(404, "AI render not ready")
    return FileResponse(str(path), media_type="image/png")


# ── Drafts: generate N quick variations for user to browse ────────────────────

@app.post("/sessions/{session_id}/drafts")
async def generate_drafts(session_id: str, count: int = 4, style: str = "",
                          room_type: str = ""):
    """Generate N quick draft renders (different seeds) for user to browse and pick from.
    room_type: salon, kuchnia, sypialnia, lazienka, pokoj_dziecka, gabinet, etc.
    Fast: ~8-15s each at lower quality. User picks a favorite, then refines."""
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    room_data = sess.get_room_data(session_id)
    if not room_data:
        raise HTTPException(400, "No scene data yet")

    blender_png = sess.get_render_path(session_id)
    if not blender_png.exists():
        raise HTTPException(400, "No Blender render yet — render first")

    if not await check_flux_health():
        raise HTTPException(503, "Flux service not available")

    count = min(count, 8)  # cap at 8

    # Load inspiration if available
    inspo_desc = ""
    inspo_file = sess.get_session_dir(session_id) / "inspiration_desc.txt"
    if inspo_file.exists():
        inspo_desc = inspo_file.read_text().strip()

    # Enrich prompt with real furniture descriptions
    base_prompt = room_data_to_prompt(room_data, style, inspiration_desc=inspo_desc,
                                      room_type=room_type)
    try:
        from .furniture_search import enrich_prompt_with_furniture
        prompt = enrich_prompt_with_furniture(room_data, base_prompt, style)
    except Exception:
        prompt = base_prompt

    depth_png = sess.get_depth_path(session_id)
    depth_str = str(depth_png) if depth_png.exists() else ""

    drafts_dir = sess.get_session_dir(session_id) / "drafts"
    drafts_dir.mkdir(exist_ok=True)

    await sess.update_state(session_id, {"status": "drafting", "drafts_total": count, "drafts_done": 0})

    task = asyncio.create_task(_run_drafts(
        session_id, str(blender_png), depth_str, room_data, prompt,
        str(drafts_dir), count, inspo_desc,
    ))
    task.add_done_callback(lambda t: _on_task_done(session_id, t))
    return {"status": "generating", "count": count, "prompt": prompt}


async def _run_drafts(
    session_id: str, blender_png: str, depth_png: str,
    room_data: dict, prompt: str, drafts_dir: str,
    count: int, inspiration_desc: str,
):
    """Generate drafts. Strategy:
    - If count <= 4: one design, different camera angles (same seed)
    - If count > 4: multiple designs × multiple cameras
    """
    import random
    num_cameras = 4
    drafts_path = Path(drafts_dir)

    if count <= num_cameras:
        # One design, multiple angles — same seed for consistent look
        seed = random.randint(0, 2**31)
        cameras = list(range(min(count, num_cameras)))
        plan = [(cam, seed) for cam in cameras]
    else:
        # Multiple designs × angles: first N seeds, cycle cameras
        num_designs = (count + num_cameras - 1) // num_cameras
        design_seeds = [random.randint(0, 2**31) for _ in range(num_designs)]
        plan = []
        for d_idx, seed in enumerate(design_seeds):
            for cam in range(num_cameras):
                if len(plan) >= count:
                    break
                plan.append((cam, seed))

    for i, (camera_id, seed) in enumerate(plan):
        output = str(drafts_path / f"draft_{i:02d}.png")

        # Render Blender scene from this camera angle (cache per angle)
        cam_render = drafts_path / f"_blender_cam{camera_id}.png"
        cam_depth = drafts_path / f"_depth_cam{camera_id}.png"

        if not cam_render.exists():
            ok = await render_camera(session_id, camera_id,
                                     str(cam_render), str(cam_depth))
            if not ok:
                cam_render = Path(blender_png)
                cam_depth = Path(depth_png) if depth_png else Path("")

        try:
            d_str = str(cam_depth) if cam_depth.exists() else ""
            await generate_draft_render(
                str(cam_render), d_str, room_data, output,
                prompt=prompt, seed=seed, steps=8,
                inspiration_desc=inspiration_desc,
            )
        except Exception as e:
            print(f"[drafts] Draft {i} (cam{camera_id}, seed={seed}) failed: {e}")
        await sess.update_state(session_id, {"drafts_done": i + 1})

    await sess.update_state(session_id, {"status": "drafts_ready", "drafts_total": count})


@app.get("/sessions/{session_id}/drafts")
async def get_drafts_status(session_id: str):
    """Get status and list of available drafts."""
    drafts_dir = sess.get_session_dir(session_id) / "drafts"
    if not drafts_dir.exists():
        return {"status": "none", "drafts": []}

    state = sess.get_state(session_id)
    draft_files = sorted(drafts_dir.glob("draft_*.png"))
    return {
        "status": state.get("status", "unknown"),
        "total": state.get("drafts_total", 0),
        "done": len(draft_files),
        "drafts": [{"index": i, "filename": f.name} for i, f in enumerate(draft_files)],
    }


@app.get("/sessions/{session_id}/drafts/{index}")
async def get_draft(session_id: str, index: int):
    """Get a specific draft render image."""
    path = sess.get_session_dir(session_id) / "drafts" / f"draft_{index:02d}.png"
    if not path.exists():
        raise HTTPException(404, f"Draft {index} not ready")
    return FileResponse(str(path), media_type="image/png")


# ── Finalize (Blender → Flux img2img, with depth ControlNet if available) ────

@app.post("/sessions/{session_id}/finalize")
async def finalize(session_id: str, style: str = "", strength: float = 0.65,
                   seed: int = -1, room_type: str = "", camera: int = 0):
    """Final high-quality render. Optionally specify camera angle (0-3) and room_type."""
    try:
        sess.get_state(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    room_data = sess.get_room_data(session_id)
    if not room_data:
        raise HTTPException(400, "No scene data yet")

    blender_png = sess.get_render_path(session_id)
    if not blender_png.exists():
        raise HTTPException(400, "No Blender render yet — render first")

    if not await check_flux_health():
        raise HTTPException(503, "Flux service not available — start it on DGX Spark first")

    session_dir = sess.get_session_dir(session_id)
    output_path = str(session_dir / "finalized_render.png")
    await sess.update_state(session_id, {"status": "finalizing"})

    # If specific camera requested, render Blender from that angle
    if camera > 0:
        cam_render = session_dir / f"_final_cam{camera}.png"
        cam_depth = session_dir / f"_final_depth_cam{camera}.png"
        ok = await render_camera(session_id, camera, str(cam_render), str(cam_depth))
        if ok:
            blender_png = cam_render
            depth_png_path = cam_depth
        else:
            depth_png_path = sess.get_depth_path(session_id)
    else:
        depth_png_path = sess.get_depth_path(session_id)

    use_depth = depth_png_path.exists()

    inspo_desc = ""
    inspo_file = session_dir / "inspiration_desc.txt"
    if inspo_file.exists():
        inspo_desc = inspo_file.read_text().strip()

    # Enrich prompt with furniture search + house style
    if not room_type:
        room_type = room_data.get("room_type", "")
    base_prompt = room_data_to_prompt(room_data, style, inspiration_desc=inspo_desc,
                                      room_type=room_type)
    try:
        from .furniture_search import enrich_prompt_with_furniture
        prompt = enrich_prompt_with_furniture(room_data, base_prompt, style)
    except Exception:
        prompt = base_prompt

    task = asyncio.create_task(_run_finalize(
        session_id, str(blender_png), room_data, output_path,
        style, strength, seed, str(depth_png_path) if use_depth else "",
        inspo_desc, prompt,
    ))
    task.add_done_callback(lambda t: _on_task_done(session_id, t))
    return {"status": "generating", "prompt": prompt[:200] + "...",
            "depth_guided": use_depth, "inspiration": bool(inspo_desc)}


async def _run_finalize(
    session_id: str,
    blender_png: str,
    room_data: dict,
    output_path: str,
    style: str,
    strength: float,
    seed: int,
    depth_png: str = "",
    inspiration_desc: str = "",
    enriched_prompt: str = "",
):
    try:
        if depth_png and Path(depth_png).exists():
            await generate_controlled_render(
                blender_png, depth_png, room_data, output_path,
                style_override=style, strength=0.92,
                controlnet_conditioning_scale=0.35,
                seed=seed, inspiration_desc=inspiration_desc,
            )
        else:
            await generate_finalized_render(
                blender_png, room_data, output_path,
                style_override=style, strength=0.85, seed=seed,
                inspiration_desc=inspiration_desc,
            )

        # Upscale to 2048px using Lanczos (high quality, no extra model needed)
        await _upscale_image(output_path, 2048)

        await sess.update_state(session_id, {"status": "finalized"})
    except Exception as e:
        await sess.update_state(session_id, {"status": "error", "error": str(e)})


async def _upscale_image(path: str, target_long_edge: int = 2048):
    """Upscale image using Pillow Lanczos resampling."""
    import asyncio
    from PIL import Image

    def _do_upscale():
        img = Image.open(path)
        w, h = img.size
        scale = target_long_edge / max(w, h)
        if scale <= 1.0:
            return  # already big enough
        new_w = int(w * scale)
        new_h = int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        img.save(path, "PNG", optimize=True)
        print(f"[upscale] {w}x{h} → {new_w}x{new_h}")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _do_upscale)


@app.get("/sessions/{session_id}/finalize")
async def get_finalized(session_id: str):
    path = sess.get_session_dir(session_id) / "finalized_render.png"
    if not path.exists():
        raise HTTPException(404, "Finalized render not ready")
    return FileResponse(str(path), media_type="image/png")


# ── DGX Service Management ────────────────────────────────────────────────────

def _validate_service(name: str):
    if name not in dgx.SERVICES:
        raise HTTPException(404, f"Unknown service '{name}'. Known: {', '.join(dgx.SERVICES)}")


@app.get("/dgx/services")
async def dgx_services():
    return await dgx.get_all_statuses()


@app.post("/dgx/services/{name}/start")
async def dgx_start(name: str):
    _validate_service(name)
    result = await dgx.start_service(name)
    if name == "blender":
        from .render import reset_dgx_blender_cache
        reset_dgx_blender_cache()
    return result


@app.post("/dgx/services/{name}/stop")
async def dgx_stop(name: str):
    _validate_service(name)
    result = await dgx.stop_service(name)
    if name == "blender":
        from .render import reset_dgx_blender_cache
        reset_dgx_blender_cache()
    return result


