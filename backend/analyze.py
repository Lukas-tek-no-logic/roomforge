from .claude_api import ask_with_image, parse_json

ANALYZE_PROMPT = """You are a 3D scene reconstruction expert. Analyze this room photo and produce a JSON description that will be used to build a 3D Blender scene.

CRITICAL RULES:
- All floor-standing items MUST have z=0 (z is only > 0 for wall-mounted or elevated items like pipes, shelves on walls, wall art)
- Position: x=left-right, y=front-back (camera looks from -y toward +y). Room center is x=0, y=0. Floor is z=0.
- Estimate realistic metric dimensions.
- Include ALL visible objects, even small ones.

Available furniture types and what they render as:
- "washing_machine" → white box with round porthole
- "sink" → ceramic basin
- "shelf" → horizontal planks
- "cabinet" → box with wood texture
- "bookshelf" → grid of compartments
- "pipe" → cylinder (auto-detects vertical/horizontal from size — set width>height for horizontal pipes)
- "radiator" → flat metal panel with grid lines
- "sofa"/"couch" → L-shaped cushioned form
- "armchair"/"chair" → seat + backrest
- "bed" → frame + mattress + headboard
- "table"/"desk" → top surface on legs
- "tv" → flat black screen + stand
- "lamp"/"floor_lamp" → pole + shade
- "plant" → pot + foliage sphere
- "mirror" → reflective flat panel
- "rug"/"carpet" → flat plane on floor
- "curtain" → draped fabric panels
- "vase" → tapered ceramic cylinder
- "wall_art"/"painting" → frame + canvas on wall
- "basket" → cylindrical fabric container
- "decor" → smart fallback: thin items→panels, flat items→rugs, tall thin→poles, fabric→soft cylinders, default→beveled box

DO NOT use "dryer" for radiators/heaters — use "radiator" or "decor" with material="metal".
DO NOT use "cabinet" for cardboard boxes — use "decor" with material="generic".
For pipes: set size.width > size.height for HORIZONTAL pipes, size.height > size.width for VERTICAL.

Return ONLY valid JSON:
{{
  "room": {{
    "width": <float, meters>,
    "depth": <float, meters>,
    "height": <float, meters>
  }},
  "walls": {{
    "color": "<hex color — match actual wall color from photo>",
    "material": "<paint|tile|brick|concrete>",
    "texture_roughness": <0.0-1.0>
  }},
  "floor": {{
    "color": "<hex color — match actual floor>",
    "material": "<tile|wood|concrete|vinyl>",
    "texture_roughness": <0.0-1.0>
  }},
  "ceiling": {{
    "color": "<hex color>",
    "height": <float, meters>
  }},
  "furniture": [
    {{
      "id": "<unique_id>",
      "type": "<from list above>",
      "label": "<detailed description for rendering>",
      "position": {{"x": <float>, "y": <float>, "z": <float, 0 for floor items>}},
      "size": {{"width": <float>, "depth": <float>, "height": <float>}},
      "color": "<hex color — match actual color from photo>",
      "material": "<metal|plastic|wood|ceramic|fabric|leather|glass|generic>",
      "bbox": [<x1_norm>, <y1_norm>, <x2_norm>, <y2_norm>]
    }}
  ],
  "openings": [
    {{
      "type": "<door|window|opening>",
      "wall": "<north|south|east|west>",
      "position_along_wall": <float, 0.0-1.0>,
      "width": <float>,
      "height": <float>,
      "bottom_height": <float, 0 for doors>
    }}
  ],
  "lighting": {{
    "type": "<ceiling_lamp|fluorescent|natural|none>",
    "intensity": <0.0-5.0>,
    "color": "<hex color — #FFF5E0 warm, #F0F5FF cool/fluorescent>"
  }},
  "surface_crops": [
    {{
      "type": "<floor|wall_north|wall_south|wall_east|wall_west|ceiling>",
      "bbox": [<x1_norm>, <y1_norm>, <x2_norm>, <y2_norm>]
    }}
  ]
}}"""


ANALYZE_WITH_GEOMETRY_PROMPT = """You are a 3D scene reconstruction expert. The room dimensions are already known from 3D reconstruction.

Room: {{width}}m wide x {{depth}}m deep x {{height}}m tall.

Analyze the photo and extract materials, furniture, and lighting. Use the known dimensions for positioning.

CRITICAL: Floor-standing items MUST have z=0.

Available types: washing_machine, sink, shelf, cabinet, bookshelf, pipe, radiator, sofa, couch, armchair, chair, bed, table, desk, tv, tv_stand, lamp, floor_lamp, plant, mirror, rug, carpet, curtain, vase, wall_art, painting, basket, decor.

For pipes: width>height = horizontal, height>width = vertical.
Use "decor" for unrecognized objects (boxes, bags, tools, bottles).

Return ONLY valid JSON with same structure as standard analysis."""


def analyze_photo(image_path: str) -> dict:
    prompt = ANALYZE_PROMPT
    raw = ask_with_image(prompt, image_path)
    return parse_json(raw)


def analyze_with_geometry(image_path: str, room_dimensions: dict) -> dict:
    """Phase 5: Analyze photo with known room dimensions from 3D reconstruction."""
    width = room_dimensions.get("width", 3.0)
    depth = room_dimensions.get("depth", 3.0)
    height = room_dimensions.get("height", 2.5)

    prompt = ANALYZE_WITH_GEOMETRY_PROMPT.format(
        width=width, depth=depth, height=height,
    )
    raw = ask_with_image(prompt, image_path)
    return parse_json(raw)
