from .claude_api import ask_with_image, parse_json

ANALYZE_PROMPT = """You are a 3D scene description expert. Analyze the laundry room image and extract a detailed JSON description.

Return ONLY valid JSON (no markdown, no explanation) with this exact structure:
{{
  "room": {{
    "width": <float, meters>,
    "depth": <float, meters>,
    "height": <float, meters>
  }},
  "walls": {{
    "color": "<hex color like #E8E8E8>",
    "material": "<paint|tile|brick|concrete>",
    "texture_roughness": <0.0-1.0>
  }},
  "floor": {{
    "color": "<hex color>",
    "material": "<tile|wood|concrete|vinyl>",
    "texture_roughness": <0.0-1.0>
  }},
  "ceiling": {{
    "color": "<hex color>",
    "height": <float, meters>
  }},
  "furniture": [
    {{
      "id": "<unique_id like washing_machine_1>",
      "type": "<sofa|armchair|chair|table|shelf|bookshelf|cabinet|bed|desk|tv|tv_stand|lamp|floor_lamp|table_lamp|plant|mirror|rug|carpet|wall_art|painting|picture|poster|clock|curtain|drapes|vase|washing_machine|dryer|sink|basket|pipe|decor|ottoman|stool|sideboard|console|nightstand|wardrobe|dresser>",
      "label": "<human readable name>",
      "position": {{"x": <float>, "y": <float>, "z": <float>}},
      "size": {{"width": <float>, "depth": <float>, "height": <float>}},
      "color": "<hex color>",
      "material": "<metal|plastic|wood|ceramic|fabric|leather|glass|wicker|organic|generic>",
      "bbox": [<x1_norm>, <y1_norm>, <x2_norm>, <y2_norm>]
    }}
  ],
  "openings": [
    {{
      "type": "<door|window>",
      "wall": "<north|south|east|west>",
      "position_along_wall": <float, 0.0-1.0>,
      "width": <float>,
      "height": <float>,
      "bottom_height": <float>
    }}
  ],
  "lighting": {{
    "type": "<ceiling_lamp|fluorescent|natural|none>",
    "intensity": <0.0-5.0>,
    "color": "<hex color>"
  }},
  "surface_crops": [
    {{
      "type": "<floor|wall|wall_north|wall_south|wall_east|wall_west|ceiling>",
      "bbox": [<x1_norm>, <y1_norm>, <x2_norm>, <y2_norm>]
    }}
  ]
}}

Important:
- Estimate reasonable metric dimensions. Place furniture with x=left-right, y=depth(front-back), z=up. Room center is (0,0) for x,y; floor at z=0.
- Include all visible objects.
- For each furniture item, include "bbox" with normalized bounding box coordinates [x1, y1, x2, y2] where values are 0.0-1.0 relative to the image dimensions.
- For "surface_crops", identify visible surface areas NOT blocked by furniture. These are used to extract material textures. Include bounding boxes as normalized 0.0-1.0 coordinates."""


ANALYZE_WITH_GEOMETRY_PROMPT = """You are a 3D scene description expert. The room has already been 3D-reconstructed from video.

Known room dimensions: {width}m wide x {depth}m deep x {height}m tall.

Analyze the representative frame and extract materials, furniture, and lighting.
You do NOT need to estimate room dimensions -- they are already known from 3D reconstruction.

Return ONLY valid JSON (no markdown, no explanation) with this exact structure:
{{
  "room": {{
    "width": {width},
    "depth": {depth},
    "height": {height}
  }},
  "walls": {{
    "color": "<hex color>",
    "material": "<paint|tile|brick|concrete>",
    "texture_roughness": <0.0-1.0>
  }},
  "floor": {{
    "color": "<hex color>",
    "material": "<tile|wood|concrete|vinyl>",
    "texture_roughness": <0.0-1.0>
  }},
  "ceiling": {{
    "color": "<hex color>",
    "height": {height}
  }},
  "furniture": [
    {{
      "id": "<unique_id>",
      "type": "<sofa|armchair|chair|table|shelf|bookshelf|cabinet|bed|desk|tv|tv_stand|lamp|floor_lamp|table_lamp|plant|mirror|rug|carpet|wall_art|painting|picture|poster|clock|curtain|drapes|vase|washing_machine|dryer|sink|basket|pipe|decor|ottoman|stool|sideboard|console|nightstand|wardrobe|dresser>",
      "label": "<human readable name>",
      "position": {{"x": <float>, "y": <float>, "z": <float>}},
      "size": {{"width": <float>, "depth": <float>, "height": <float>}},
      "color": "<hex color>",
      "material": "<metal|plastic|wood|ceramic|fabric|leather|glass|wicker|organic|generic>",
      "bbox": [<x1_norm>, <y1_norm>, <x2_norm>, <y2_norm>]
    }}
  ],
  "openings": [
    {{
      "type": "<door|window>",
      "wall": "<north|south|east|west>",
      "position_along_wall": <float, 0.0-1.0>,
      "width": <float>,
      "height": <float>,
      "bottom_height": <float>
    }}
  ],
  "lighting": {{
    "type": "<ceiling_lamp|fluorescent|natural|none>",
    "intensity": <0.0-5.0>,
    "color": "<hex color>"
  }},
  "surface_crops": [
    {{
      "type": "<floor|wall|wall_north|wall_south|wall_east|wall_west|ceiling>",
      "bbox": [<x1_norm>, <y1_norm>, <x2_norm>, <y2_norm>]
    }}
  ]
}}

Focus on material identification and furniture placement accuracy using the known dimensions."""


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
