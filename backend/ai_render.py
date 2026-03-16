"""
AI photorealistic render via Flux on DGX Spark.
Converts room_data JSON → descriptive prompt → calls Flux service.

Supports:
- txt2img (generate from prompt only)
- img2img (Blender render → photorealistic)
- controlled render (Blender render + depth map → structure-preserving photorealism)
- inpainting (selective region regeneration)
- quick preview (fast low-quality photorealism for iterative feedback)
"""

import base64
import json
import os
import subprocess
import tempfile
import httpx
from pathlib import Path

import asyncio

DGX_URL = os.environ.get("DGX_FLUX_URL", "http://192.168.0.200:8001")
TIMEOUT = 300.0
PREVIEW_TIMEOUT = 15.0


async def _post_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """POST with retry on 503 (GPU busy). Waits up to 60s for semaphore to free."""
    for attempt in range(12):
        resp = await client.post(url, **kwargs)
        if resp.status_code != 503:
            return resp
        print(f"[flux] 503 GPU busy, retry {attempt+1}/12 in 5s...")
        await asyncio.sleep(5)
    return resp  # return last 503 if all retries fail

# ── House style profile ───────────────────────────────────────────────────────

_house_style = None
_house_style_mtime = 0.0
HOUSE_STYLE_PATH = Path(__file__).parent.parent / "sessions" / "house_style.json"


def get_house_style() -> dict:
    """Load house style profile. Returns empty dict if not available.
    Re-reads from disk if file has been modified since last load."""
    global _house_style, _house_style_mtime
    if HOUSE_STYLE_PATH.exists():
        mtime = HOUSE_STYLE_PATH.stat().st_mtime
        if _house_style is None or mtime != _house_style_mtime:
            _house_style = json.loads(HOUSE_STYLE_PATH.read_text())
            _house_style_mtime = mtime
    elif _house_style is None:
        _house_style = {}
    return _house_style


def get_room_style_prompt(room_type: str) -> str:
    """Get the DeloDesign-specific prompt for a room type (salon, kuchnia, sypialnia, etc.)."""
    hs = get_house_style()
    room_styles = hs.get("room_styles", {})
    room = room_styles.get(room_type, {})
    return room.get("prompt", "")


# ── Prompt builder ─────────────────────────────────────────────────────────────

STYLE_HINTS = {
    "tile":     "large format porcelain tiles, subtle grout lines, polished surface",
    "wood":     "engineered oak hardwood floor, visible natural grain, warm matte finish",
    "concrete": "microcement / architectural concrete finish, subtle texture variation",
    "vinyl":    "luxury vinyl plank flooring",
    "paint":    "smooth painted walls, fine matte finish",
    "brick":    "exposed brick wall, sealed finish",
    "metal":    "brushed stainless steel, satin metallic surface",
    "ceramic":  "white porcelain ceramic, high gloss finish",
}

FURNITURE_HINTS = {
    "washing_machine": "front-loading washing machine",
    "dryer":           "tumble dryer",
    "shelf":           "floating open shelf",
    "cabinet":         "built-in handleless storage cabinet",
    "sink":            "undermount basin with modern faucet",
    "table":           "designer wooden table",
    "basket":          "woven storage basket",
    "pipe":            "exposed copper pipe",
    "sofa":            "upholstered sofa with cushions",
    "armchair":        "designer armchair",
    "chair":           "modern dining chair",
    "plant":           "lush green potted plant",
    "lamp":            "designer pendant lamp",
    "floor_lamp":      "modern floor lamp",
    "table_lamp":      "designer table lamp",
    "mirror":          "large wall mirror with thin frame",
    "rug":             "textured area rug",
    "bookshelf":       "open bookshelf with books and decor",
    "bed":             "platform bed with linen bedding",
    "tv":              "wall-mounted flat screen TV",
    "curtain":         "floor-length linen curtains",
    "vase":            "decorative ceramic vase",
    "wall_art":        "framed wall art",
    "painting":        "abstract painting",
    "desk":            "modern workspace desk",
}

# Phase 2: Default negative prompt to eliminate common AI artifacts
DEFAULT_NEGATIVE_PROMPT = (
    "cartoon, CGI look, flat lighting, plastic look, low quality, blurry, "
    "unrealistic, oversaturated, deformed, distorted geometry, text, watermark, "
    "logo, signature, out of frame, primitive shapes, blocky furniture, "
    "wireframe, untextured surfaces, amateur render"
)


def room_data_to_prompt(room_data: dict, style_override: str = "",
                        inspiration_desc: str = "", room_type: str = "") -> str:
    hs = get_house_style()

    # If house style has a room-specific prompt, use it as the core
    room_style_prompt = ""
    if room_type:
        room_style_prompt = get_room_style_prompt(room_type)
    if not room_style_prompt and hs:
        # Try to auto-detect room type from furniture
        room_style_prompt = _auto_detect_room_prompt(room_data, hs)

    if room_style_prompt:
        # House style mode: use the detailed room-specific prompt
        prefix = hs.get("global_prompt_prefix", "Professional interior design visualization")
        suffix = hs.get("global_prompt_suffix", "photorealistic, 8K resolution")
        neg = hs.get("negative_prompt", DEFAULT_NEGATIVE_PROMPT)

        parts = [prefix, room_style_prompt]
        if style_override:
            parts.append(style_override)
        if inspiration_desc:
            parts.append(inspiration_desc)
        parts.append(suffix)
        return ", ".join(parts)

    # Fallback: generic prompt from room_data
    room     = room_data.get("room", {})
    walls    = room_data.get("walls", {})
    floor    = room_data.get("floor", {})
    lighting = room_data.get("lighting", {})
    furniture = room_data.get("furniture", [])

    wall_mat  = STYLE_HINTS.get(walls.get("material",  "tile"),  "walls")
    floor_mat = STYLE_HINTS.get(floor.get("material",  "tile"),  "floor")
    wall_color_name = _hex_to_color_name(walls.get("color", "#E8E8E8"))
    floor_color_name = _hex_to_color_name(floor.get("color", "#C8A882"))

    seen_types = set()
    furniture_items = []
    for item in furniture:
        t = item.get("type", "")
        label = item.get("label", "")
        hint = FURNITURE_HINTS.get(t, label or t)
        if hint and t not in seen_types:
            furniture_items.append(hint)
            seen_types.add(t)
    furniture_desc = ", ".join(furniture_items[:8])

    light_type = lighting.get("type", "ceiling_lamp")
    light_desc = {
        "ceiling_lamp":  "warm overhead ceiling lights with soft diffusion",
        "fluorescent":   "bright track lighting with spotlights",
        "natural":       "abundant natural daylight through large windows",
        "none":          "soft indirect ambient lighting",
    }.get(light_type, "ambient lighting")

    style = style_override or _guess_style(walls, floor)

    parts = [
        f"Professional interior design visualization, {style}",
        f"{wall_color_name} {wall_mat}",
        f"{floor_color_name} {floor_mat}",
        f"{light_desc}",
    ]
    if furniture_desc:
        parts.append(f"featuring {furniture_desc}")
    if inspiration_desc:
        parts.append(inspiration_desc)
    parts.extend([
        "architectural photography, V-Ray quality render",
        "photorealistic materials and textures, volumetric lighting",
        "sharp focus, 8K resolution, professional composition",
        "Dezeen magazine style, no people, no text",
    ])
    return ", ".join(parts)


def _auto_detect_room_prompt(room_data: dict, hs: dict) -> str:
    """Try to detect which room type this is from furniture and return the house style prompt."""
    furniture = room_data.get("furniture", [])
    types = {item.get("type", "") for item in furniture}
    labels = " ".join(item.get("label", "").lower() for item in furniture)
    room_styles = hs.get("room_styles", {})

    # Detection heuristics
    if "washing_machine" in types or "dryer" in types:
        return ""  # laundry — no house style
    if "bed" in types and ("dziec" in labels or "child" in labels or "teo" in labels):
        return room_styles.get("pokoj_dziecka", {}).get("prompt", "")
    if "bed" in types:
        return room_styles.get("sypialnia", {}).get("prompt", "")
    if any(t in types for t in ("sofa", "couch")) and any("piano" in labels for _ in [1]):
        return room_styles.get("salon", {}).get("prompt", "")
    if any(t in types for t in ("sofa", "couch", "armchair")):
        return room_styles.get("salon", {}).get("prompt", "")
    if "bookshelf" in types and "desk" in types:
        return room_styles.get("gabinet", {}).get("prompt", "")
    if "sink" in types and ("bathtub" in labels or "shower" in labels or "toilet" in labels):
        return room_styles.get("lazienka", {}).get("prompt", "")

    return ""


def room_data_to_negative_prompt(room_data: dict) -> str:
    """Generate negative prompt — uses house style if available."""
    hs = get_house_style()
    return hs.get("negative_prompt", DEFAULT_NEGATIVE_PROMPT)


def _hex_to_color_name(hex_color: str) -> str:
    hex_color = hex_color.lstrip("#")
    try:
        r, g, b = (int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    except Exception:
        return "neutral"
    brightness = (r + g + b) / 3
    if brightness > 220:
        return "bright white"
    elif brightness > 180:
        return "light gray"
    elif brightness > 140:
        return "warm beige"
    elif r > g + 20 and r > b + 20:
        return "warm terracotta"
    elif b > r + 20 and b > g + 20:
        return "cool blue-gray"
    elif g > r + 10:
        return "soft green"
    else:
        return "neutral gray"


def _guess_style(walls: dict, floor: dict) -> str:
    wall_mat  = walls.get("material", "paint")
    floor_mat = floor.get("material", "tile")
    wall_color = walls.get("color", "#E8E8E8").lstrip("#")
    try:
        r, g, b = (int(wall_color[i:i+2], 16) for i in (0, 2, 4))
        brightness = (r + g + b) / 3
    except Exception:
        brightness = 200

    if wall_mat == "brick" or floor_mat == "concrete":
        return "industrial loft style"
    elif brightness > 210 and floor_mat == "wood":
        return "Scandinavian minimalist style"
    elif brightness > 200:
        return "modern minimalist style"
    else:
        return "contemporary style"


def _encode_image(path: str) -> str:
    """Read file and return base64 string."""
    return base64.b64encode(Path(path).read_bytes()).decode()


# PNG magic bytes: \x89PNG\r\n\x1a\n
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# JPEG magic bytes: \xff\xd8\xff
_JPEG_MAGIC = b"\xff\xd8\xff"


def _validate_image_response(resp: httpx.Response) -> bytes:
    """Validate that an HTTP response contains image data. Returns image bytes."""
    data = resp.content
    if len(data) < 100:
        raise ValueError(f"DGX returned too-small response ({len(data)} bytes), likely an error")
    if not (data[:8] == _PNG_MAGIC or data[:3] == _JPEG_MAGIC):
        # Try to decode as text for a better error message
        try:
            text = data[:500].decode("utf-8", errors="replace")
        except Exception:
            text = "(binary)"
        raise ValueError(f"DGX returned non-image response: {text[:200]}")
    return data


def _atomic_write_image(output_path: str, data: bytes) -> None:
    """Write image data atomically using temp-then-rename."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, suffix=".tmp")
    closed = False
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        closed = True
        os.rename(tmp, out)
    except BaseException:
        if not closed:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


INSPIRATION_PROMPT = """Analyze this interior design visualization image. Extract a concise style description
that can be used as a prompt to recreate a similar aesthetic in another room.

Focus on:
- Overall style (modern minimalist, Scandinavian, industrial, etc.)
- Color palette (specific tones, warm/cool, contrast levels)
- Material finishes (concrete, wood species, metal finishes, tile types)
- Lighting style (track lights, pendants, indirect LED, natural light quality)
- Furniture design language (clean lines, curved, mid-century, etc.)
- Decorative elements and mood

Return ONLY a single paragraph of comma-separated descriptive terms suitable for an AI image generation prompt.
No explanations, no numbered lists — just the style description. Max 100 words."""


def analyze_inspiration(image_path: str) -> str:
    """Use Claude to extract a style description from an inspiration image."""
    from .claude_api import ask_with_image
    raw = ask_with_image(INSPIRATION_PROMPT, image_path)
    # Clean up — remove any markdown or quotes
    return raw.strip().strip('"').strip("'")


# ── HTTP client ────────────────────────────────────────────────────────────────

async def generate_ai_render(
    room_data: dict,
    output_path: str,
    style_override: str = "",
    width: int = 1280,
    height: int = 720,
    steps: int = 4,
    seed: int = -1,
) -> bool:
    prompt = room_data_to_prompt(room_data, style_override)
    negative_prompt = room_data_to_negative_prompt(room_data)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _post_with_retry(client,
            f"{DGX_URL}/generate",
            json={
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "width": width,
                "height": height,
                "steps": steps,
                "guidance": 0.0,
                "seed": seed,
            },
        )
        resp.raise_for_status()
        _atomic_write_image(output_path, _validate_image_response(resp))
        return True


async def generate_finalized_render(
    blender_png_path: str,
    room_data: dict,
    output_path: str,
    style_override: str = "",
    strength: float = 0.65,
    seed: int = -1,
    inspiration_desc: str = "",
) -> bool:
    """Send Blender render as structural guide → Flux img2img → photorealistic result."""
    prompt = room_data_to_prompt(room_data, style_override, inspiration_desc=inspiration_desc)
    negative_prompt = room_data_to_negative_prompt(room_data)
    image_b64 = _encode_image(blender_png_path)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _post_with_retry(client,
            f"{DGX_URL}/img2img",
            json={
                "image_b64": image_b64,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "strength": strength,
                "steps": 20,
                "seed": seed,
            },
        )
        resp.raise_for_status()
        _atomic_write_image(output_path, _validate_image_response(resp))
        return True


async def generate_controlled_render(
    blender_png_path: str,
    depth_png_path: str,
    room_data: dict,
    output_path: str,
    style_override: str = "",
    strength: float = 0.90,
    controlnet_conditioning_scale: float = 0.6,
    seed: int = -1,
    inspiration_desc: str = "",
) -> bool:
    """Phase 1: Depth-conditioned render — high photorealism + preserved room layout."""
    prompt = room_data_to_prompt(room_data, style_override, inspiration_desc=inspiration_desc)
    negative_prompt = room_data_to_negative_prompt(room_data)
    image_b64 = _encode_image(blender_png_path)
    depth_b64 = _encode_image(depth_png_path)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _post_with_retry(client,
            f"{DGX_URL}/controlled-render",
            json={
                "image_b64": image_b64,
                "depth_b64": depth_b64,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "strength": strength,
                "controlnet_conditioning_scale": controlnet_conditioning_scale,
                "steps": 20,
                "seed": seed,
            },
        )
        resp.raise_for_status()
        _atomic_write_image(output_path, _validate_image_response(resp))
        return True


async def generate_inpaint_render(
    image_path: str,
    mask_path: str,
    prompt: str,
    output_path: str,
    negative_prompt: str = "",
    strength: float = 0.85,
    seed: int = -1,
) -> bool:
    """Phase 2: Inpainting — regenerate only the masked region."""
    image_b64 = _encode_image(image_path)
    mask_b64 = _encode_image(mask_path)
    if not negative_prompt:
        negative_prompt = DEFAULT_NEGATIVE_PROMPT

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _post_with_retry(client,
            f"{DGX_URL}/inpaint",
            json={
                "image_b64": image_b64,
                "mask_b64": mask_b64,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "strength": strength,
                "steps": 20,
                "seed": seed,
            },
        )
        resp.raise_for_status()
        _atomic_write_image(output_path, _validate_image_response(resp))
        return True


async def generate_draft_render(
    blender_png_path: str,
    depth_png_path: str,
    room_data: dict,
    output_path: str,
    prompt: str = "",
    style_override: str = "",
    inspiration_desc: str = "",
    seed: int = -1,
    steps: int = 4,
) -> bool:
    """Fast draft render (512px, 4 steps) for quick browsing. ~5-8s each."""
    if not prompt:
        prompt = room_data_to_prompt(room_data, style_override, inspiration_desc=inspiration_desc)
    negative_prompt = room_data_to_negative_prompt(room_data)
    image_b64 = _encode_image(blender_png_path)

    if depth_png_path and Path(depth_png_path).exists():
        depth_b64 = _encode_image(depth_png_path)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{DGX_URL}/controlled-render",
                json={
                    "image_b64": image_b64,
                    "depth_b64": depth_b64,
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "strength": 0.92,
                    "controlnet_conditioning_scale": 0.35,
                    "steps": steps,
                    "guidance": 3.5,
                    "seed": seed,
                },
            )
    else:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{DGX_URL}/img2img",
                json={
                    "image_b64": image_b64,
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "strength": 0.82,
                    "steps": steps,
                    "seed": seed,
                },
            )

    resp.raise_for_status()
    _atomic_write_image(output_path, _validate_image_response(resp))
    return True


async def generate_quick_preview(
    blender_png_path: str,
    depth_png_path: str,
    room_data: dict,
    output_path: str,
    style_override: str = "",
    seed: int = -1,
) -> bool:
    """Phase 6: Fast photorealistic preview (~1-2s) using lightweight model."""
    prompt = room_data_to_prompt(room_data, style_override)
    negative_prompt = room_data_to_negative_prompt(room_data)
    image_b64 = _encode_image(blender_png_path)
    depth_b64 = _encode_image(depth_png_path) if depth_png_path and Path(depth_png_path).exists() else ""

    async with httpx.AsyncClient(timeout=PREVIEW_TIMEOUT) as client:
        resp = await _post_with_retry(client,
            f"{DGX_URL}/quick-preview",
            json={
                "image_b64": image_b64,
                "depth_b64": depth_b64,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "steps": 4,
                "seed": seed,
            },
        )
        resp.raise_for_status()
        _atomic_write_image(output_path, _validate_image_response(resp))
        return True


async def check_flux_health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{DGX_URL}/health")
            return resp.status_code == 200
    except Exception:
        return False
