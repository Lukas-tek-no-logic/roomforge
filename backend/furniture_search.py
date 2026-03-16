"""Search for real furniture items online and generate detailed descriptions for rendering."""
import json
from collections import OrderedDict
from .claude_api import ask_text

# LRU cache to avoid repeated searches (bounded to prevent memory leak)
_MAX_CACHE = 50
_cache: OrderedDict[str, dict] = OrderedDict()

SEARCH_PROMPT = """You are an interior design product specialist. For each furniture item below, suggest a specific real product or design piece that matches the description. Focus on well-known brands and iconic designs.

Room style: {style}
Items to find:
{items_list}

For each item, return a JSON object with:
- "id": the item ID from the list
- "product_name": specific product name (e.g., "IKEA SÖDERHAMN 3-seat sofa" or "HAY About A Chair AAC22")
- "render_description": a detailed visual description for AI image generation (50-80 words), describing exact appearance, materials, colors, proportions, and design details. This must be vivid enough that an AI image generator can create a photorealistic version.

Return ONLY a JSON array. No markdown, no explanation.

Example output:
[
  {{"id": "sofa_1", "product_name": "Muuto Connect Sofa", "render_description": "Large modular sofa in light warm gray bouclé fabric, low-profile design with thin metal legs, generous deep cushions with subtle quilted stitching, contemporary Scandinavian aesthetic, soft rounded edges"}},
  {{"id": "coffee_table_1", "product_name": "HAY Slit Table", "render_description": "Round coffee table with thin folded steel top in matte black finish, geometric angular base, minimalist industrial design, 45cm height"}}
]"""


def search_furniture(room_data: dict, style: str = "") -> dict[str, dict]:
    """Search for real furniture matching room_data items. Returns {item_id: {product_name, render_description}}."""
    furniture = room_data.get("furniture", [])
    if not furniture:
        return {}

    # Build cache key from furniture IDs
    cache_key = ",".join(sorted(item.get("id", "") for item in furniture))
    if cache_key in _cache:
        _cache.move_to_end(cache_key)
        return _cache[cache_key]

    if not style:
        style = _detect_style(room_data)

    # Group items and format for the prompt
    items_lines = []
    for item in furniture:
        item_id = item.get("id", "unknown")
        item_type = item.get("type", "furniture")
        label = item.get("label", item_type)
        color = item.get("color", "#888888")
        material = item.get("material", "generic")
        size = item.get("size", {})
        w = size.get("width", 0.5)
        h = size.get("height", 0.5)
        d = size.get("depth", 0.5)
        items_lines.append(
            f"- id={item_id}, type={item_type}, label=\"{label}\", "
            f"color={color}, material={material}, size={w:.1f}x{d:.1f}x{h:.1f}m"
        )

    # Limit to 12 most important items to keep prompt reasonable
    items_text = "\n".join(items_lines[:12])

    prompt = SEARCH_PROMPT.format(style=style, items_list=items_text)

    try:
        raw = ask_text(prompt)
        # Parse JSON array
        import re
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
        results = json.loads(raw.strip())

        enriched = {}
        for item in results:
            item_id = item.get("id", "")
            if item_id:
                enriched[item_id] = {
                    "product_name": item.get("product_name", ""),
                    "render_description": item.get("render_description", ""),
                }

        _cache[cache_key] = enriched
        while len(_cache) > _MAX_CACHE:
            _cache.popitem(last=False)
        return enriched
    except Exception as e:
        print(f"[furniture_search] Failed: {e}")
        return {}


def enrich_prompt_with_furniture(room_data: dict, base_prompt: str, style: str = "") -> str:
    """Enrich a render prompt with specific real furniture descriptions."""
    items = search_furniture(room_data, style)
    if not items:
        return base_prompt

    # Build a rich furniture description from the search results
    descriptions = []
    for item_id, info in items.items():
        desc = info.get("render_description", "")
        if desc:
            descriptions.append(desc)

    if not descriptions:
        return base_prompt

    # Replace generic furniture description in prompt with enriched one
    furniture_detail = "; ".join(descriptions[:8])  # limit to 8 items
    return f"{base_prompt}, featuring specifically: {furniture_detail}"


def _detect_style(room_data: dict) -> str:
    """Detect design style from room data."""
    walls = room_data.get("walls", {})
    floor = room_data.get("floor", {})
    wall_mat = walls.get("material", "paint")
    floor_mat = floor.get("material", "tile")

    if wall_mat == "brick" or floor_mat == "concrete":
        return "industrial loft"
    elif floor_mat == "wood":
        return "modern Scandinavian minimalist"
    else:
        return "contemporary modern"
