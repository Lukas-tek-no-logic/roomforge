from .claude_api import ask_with_image, parse_json

ANALYZE_PROMPT = """You are a 3D scene reconstruction expert. Analyze this room photo and produce a JSON description for building a 3D Blender scene.

POSITIONING SYSTEM — two modes:

1) WALL-ATTACHED items (furniture against walls, pipes, shelves, appliances):
   Use "wall" + "position_along_wall" — the code computes exact 3D coordinates.
   - "wall": which wall the item is against ("north"=back, "south"=front/camera, "east"=right, "west"=left)
   - "position_along_wall": 0.0=left edge, 1.0=right edge (when FACING that wall from inside the room)
   - "distance_from_wall": 0.0=touching wall (default), meters if offset
   - "elevation": 0.0=on floor, >0 for wall-mounted items or pipes

2) FREE-STANDING items (objects on floor not against any wall):
   Use "wall": "none" — position is computed from bbox (image coordinates).

The camera in the photo is roughly at the SOUTH wall looking NORTH.
- Left side of photo = WEST wall
- Right side = EAST wall
- Back of photo = NORTH wall
- Camera position = SOUTH

Available furniture types:
- "washing_machine" → white box with porthole
- "radiator"/"heater" → flat metal panel with fins
- "pipe" → cylinder (vertical or horizontal based on size)
- "shelf", "cabinet", "bookshelf", "wardrobe", "dresser", "sideboard"
- "sofa"/"couch"/"armchair"/"chair"/"bed"/"table"/"desk"
- "tv"/"lamp"/"floor_lamp"/"plant"/"mirror"/"curtain"/"vase"
- "rug"/"carpet" → flat on floor
- "sink" → ceramic basin
- "basket" → cylindrical fabric container
- "decor" → smart fallback (panels, poles, boxes, soft shapes)
- "wall_art"/"painting" → frame on wall

Return ONLY valid JSON:
{{
  "room": {{"width": <float m>, "depth": <float m>, "height": <float m>}},
  "walls": {{"color": "<hex>", "material": "<paint|tile|brick|concrete>", "texture_roughness": <0-1>}},
  "floor": {{"color": "<hex>", "material": "<tile|wood|concrete|vinyl>", "texture_roughness": <0-1>}},
  "ceiling": {{"color": "<hex>", "height": <float>}},
  "furniture": [
    {{
      "id": "<unique_id>",
      "type": "<from list above>",
      "label": "<description>",
      "wall": "<north|south|east|west|none>",
      "position_along_wall": <0.0-1.0>,
      "distance_from_wall": <float, default 0>,
      "elevation": <float, 0=floor>,
      "size": {{"width": <float>, "depth": <float>, "height": <float>}},
      "color": "<hex>",
      "material": "<metal|plastic|wood|ceramic|fabric|leather|glass|generic>",
      "bbox": [<x1_norm>, <y1_norm>, <x2_norm>, <y2_norm>]
    }}
  ],
  "openings": [
    {{
      "type": "<door|window|opening>",
      "wall": "<north|south|east|west>",
      "position_along_wall": <0.0-1.0>,
      "width": <float>, "height": <float>,
      "bottom_height": <float, 0 for doors>
    }}
  ],
  "lighting": {{
    "type": "<ceiling_lamp|fluorescent|natural|none>",
    "intensity": <0.0-5.0>,
    "color": "<hex>"
  }},
  "surface_crops": [
    {{"type": "<floor|wall_north|wall_south|wall_east|wall_west|ceiling>",
      "bbox": [<x1_norm>, <y1_norm>, <x2_norm>, <y2_norm>]}}
  ]
}}

RULES:
- For wall-attached: position_along_wall is left-to-right when FACING that wall from inside the room
- Pipes running along a wall: wall=that wall, elevation=height of pipe center
- Items on top of other items: elevation = height of item below
- Small loose items on floor: wall="none", bbox is enough
- Estimate realistic metric dimensions"""


ANALYZE_WITH_GEOMETRY_PROMPT = """You are a 3D scene reconstruction expert. Room dimensions are known: {width}m x {depth}m x {height}m.

Use the same positioning system — "wall" + "position_along_wall" for wall-attached items, "wall": "none" for free-standing.
Camera is at SOUTH wall looking NORTH. Left=WEST, Right=EAST, Back=NORTH.

Return ONLY valid JSON with same structure as standard analysis."""


def analyze_photo(image_path: str) -> dict:
    raw = ask_with_image(ANALYZE_PROMPT, image_path)
    return parse_json(raw)


def analyze_with_geometry(image_path: str, room_dimensions: dict) -> dict:
    width = room_dimensions.get("width", 3.0)
    depth = room_dimensions.get("depth", 3.0)
    height = room_dimensions.get("height", 2.5)
    prompt = ANALYZE_WITH_GEOMETRY_PROMPT.format(width=width, depth=depth, height=height)
    raw = ask_with_image(prompt, image_path)
    return parse_json(raw)
