import json

from .claude_api import ask_text, ask_with_image_b64, parse_json

MODIFY_TEXT_PROMPT = """You are a 3D scene editor for an interior designer app.

Current room scene JSON:
{room_data}

The user says: "{command}"

Apply the requested changes to the scene. Return ONLY the complete modified JSON (same structure, no markdown, no explanation).
- Adjust positions, sizes, colors, materials as requested
- Add new furniture items with unique IDs if asked
- Remove items if asked to remove/delete them
- Do not change anything not mentioned in the command
- Keep all measurements in meters
- Available furniture types: sofa, armchair, chair, table, shelf, bookshelf, cabinet, bed, desk, tv, tv_stand, lamp, floor_lamp, table_lamp, plant, mirror, rug, carpet, wall_art, painting, picture, poster, clock, curtain, drapes, vase, washing_machine, dryer, sink, basket, pipe, decor, ottoman, stool, sideboard, console, nightstand, wardrobe, dresser
- Available materials: metal, plastic, wood, ceramic, fabric, leather, glass, wicker, organic, paint, tile, brick, concrete, vinyl
- Spread furniture naturally throughout the room. Avoid clustering everything in one area.
- Wall-mounted items (wall_art, mirror, clock, painting) should have y near the wall surface and z above ground"""

MODIFY_ANNOTATION_PROMPT = """You are a 3D scene editor for a laundry room designer.

Current room scene JSON:
{room_data}

The user drew a rectangle on the render image.
The rectangle is at coordinates (x1={x1}, y1={y1}, x2={x2}, y2={y2}) in a {render_w}x{render_h} render.
The user wrote: "{comment}"

Look at the image, understand which area/object was selected, then apply the requested change to the JSON.
Return ONLY the complete modified JSON (same structure, no markdown, no explanation)."""


def modify_from_text(room_data: dict, command: str) -> dict:
    prompt = MODIFY_TEXT_PROMPT.format(
        room_data=json.dumps(room_data, indent=2),
        command=command,
    )
    raw = ask_text(prompt)
    return parse_json(raw)


def modify_from_annotation(
    room_data: dict,
    render_image_b64: str,
    comment: str,
    x1: int, y1: int, x2: int, y2: int,
    render_w: int, render_h: int,
) -> dict:
    # Strip data URL prefix if present
    image_data = render_image_b64
    if image_data.startswith("data:"):
        image_data = image_data.split(",", 1)[1]

    prompt = MODIFY_ANNOTATION_PROMPT.format(
        room_data=json.dumps(room_data, indent=2),
        x1=x1, y1=y1, x2=x2, y2=y2,
        render_w=render_w, render_h=render_h,
        comment=comment,
    )
    raw = ask_with_image_b64(prompt, image_data, media_type="image/png")
    return parse_json(raw)
