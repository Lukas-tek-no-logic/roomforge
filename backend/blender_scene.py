#!/usr/bin/env python3
"""Blender scene builder with procedural textures + Polyhaven HDRI/textures.
Run via: blender --background --python blender_scene.py -- --input room.json --output render.png"""

import sys
import json
import argparse
import math
from pathlib import Path


# ── Polyhaven assets ───────────────────────────────────────────────────────

POLYHAVEN_CDN_HDRI = "https://dl.polyhaven.org/file/ph-assets/HDRIs/exr/2k/{asset_id}_2k.exr"
POLYHAVEN_CDN_TEX  = "https://dl.polyhaven.org/file/ph-assets/Textures/png/{res}/{asset_id}/{asset_id}_{type}_{res}.png"
POLYHAVEN_TEX_RES  = "2k"  # 2K textures (was 1K)

HDRI_ASSETS = {
    "default":         "studio_small_09",
    "scandinavian":    "noon_grass",
    "industrial":      "industrial_sunset_puresky",
    "classic":         "photo_studio_loft_hall",
    "modern":          "studio_small_09",
    "contemporary":    "studio_small_09",
}

# Multiple variants per material — randomly selected for variety
import random as _random

MATERIAL_TEXTURES = {
    "wood": [
        "herringbone_parquet",
        "diagonal_parquet",
        "oak_veneer_01",
        "laminate_floor_02",
        "plank_flooring_02",
        "wood_floor_deck",
        "dark_wood",
    ],
    "tile": [
        "marble_01",
        "marble_tiles",
        "interior_tiles",
        "floor_tiles_06",
        "large_floor_tiles_02",
        "grey_tiles",
        "long_white_tiles",
    ],
    "concrete": [
        "brushed_concrete",
        "concrete_floor_02",
        "concrete_wall_008",
        "grey_plaster_02",
    ],
    "brick": [
        "brick_wall_001",
        "brick_wall_003",
        "red_brick_03",
        "herringbone_brick",
    ],
    "fabric": [
        "velour_velvet",
        "rough_linen",
        "wool_boucle",
        "cotton_jersey",
        "scuba_suede",
    ],
    "leather": [
        "fabric_leather_01",
        "brown_leather",
        "leather_white",
    ],
    # metal: procedural only (Polyhaven metal_plate looks like wood)
    "ceramic": [
        "marble_mosaic_tiles",
        "white_ceramic_tiles",
    ],
    "vinyl": [
        "linoleum_brown",
    ],
}


def _pick_texture(mat_type: str, seed: int = 0) -> str | None:
    """Pick a texture asset ID for a material type. Uses seed for determinism per-scene."""
    variants = MATERIAL_TEXTURES.get(mat_type)
    if not variants:
        return None
    rng = _random.Random(seed + hash(mat_type))
    return rng.choice(variants)


def _download(url: str, dest: Path) -> bool:
    """Download file to dest. Returns True on success, False on failure."""
    try:
        import urllib.request
        urllib.request.urlretrieve(url, str(dest))
        return True
    except Exception as e:
        print(f"[polyhaven] Download failed {url}: {e}")
        return False


def setup_hdri_lighting(world, cache_dir: str, style: str = "default"):
    """Replace flat sky with Polyhaven HDRI. Falls back to dark sky on error."""
    import bpy

    asset_id = HDRI_ASSETS.get(style.lower(), HDRI_ASSETS["default"])
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    exr_path = cache / f"{asset_id}_2k.exr"

    if not exr_path.exists():
        url = POLYHAVEN_CDN_HDRI.format(asset_id=asset_id)
        print(f"[polyhaven] Downloading HDRI {asset_id}…")
        if not _download(url, exr_path):
            return  # fallback: leave existing sky

    try:
        world.use_nodes = True
        nt = world.node_tree
        nt.nodes.clear()
        coord   = nt.nodes.new("ShaderNodeTexCoord")
        env_tex = nt.nodes.new("ShaderNodeTexEnvironment")
        env_tex.image = bpy.data.images.load(str(exr_path))
        bg  = nt.nodes.new("ShaderNodeBackground")
        bg.inputs["Strength"].default_value = 1.2
        out = nt.nodes.new("ShaderNodeOutputWorld")
        nt.links.new(coord.outputs["Generated"], env_tex.inputs["Vector"])
        nt.links.new(env_tex.outputs["Color"],   bg.inputs["Color"])
        nt.links.new(bg.outputs["Background"],   out.inputs["Surface"])
        print(f"[polyhaven] HDRI set: {asset_id}")
    except Exception as e:
        print(f"[polyhaven] HDRI setup failed: {e}")


def download_polyhaven_textures(asset_id: str, cache_dir: str, res: str = POLYHAVEN_TEX_RES) -> dict:
    """Download albedo, roughness, normal maps. Returns {tex_type: path} or {}."""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    types = {"Color": "diff", "Roughness": "rough", "Normal": "nor_gl"}
    paths = {}
    for tex_type, suffix in types.items():
        path = cache / f"{asset_id}_{suffix}_{res}.png"
        if not path.exists():
            url = POLYHAVEN_CDN_TEX.format(res=res, asset_id=asset_id, type=suffix)
            print(f"[polyhaven] Downloading texture {asset_id}/{suffix}…")
            if not _download(url, path):
                return {}  # partial download — skip all
        paths[tex_type] = str(path)
    return paths


# ── Color helpers ──────────────────────────────────────────────────────────────

def hex_to_linear(hex_color: str):
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    r, g, b = (int(hex_color[i:i+2], 16) / 255.0 for i in (0, 2, 4))
    def g2l(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return (g2l(r), g2l(g), g2l(b), 1.0)


def darken(color, factor=0.7):
    return tuple(c * factor for c in color[:3]) + (1.0,)


def _set(node, name, value):
    """Set node input by name (silently ignore if missing)."""
    if name in node.inputs:
        node.inputs[name].default_value = value


# ── Procedural material factory ────────────────────────────────────────────────

def make_material(name: str, color_hex: str, roughness: float = 0.5, mat_type: str = "generic",
                  texture_cache_dir: str = "sessions/texture_cache",
                  chord_dir: str = "", surface_name: str = ""):
    """Create material with hierarchy: CHORD maps > Polyhaven textures > procedural."""
    import bpy
    mat = bpy.data.materials.new(name=name)
    mat.use_backface_culling = False  # visible from both sides
    if hasattr(mat, "use_nodes"):
        mat.use_nodes = True

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (700, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (400, 0)
    links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    color = hex_to_linear(color_hex)

    # Phase 3: Try CHORD PBR maps first (extracted from real photo)
    if chord_dir and surface_name:
        chord_paths = _find_chord_maps(chord_dir, surface_name)
        if chord_paths:
            _mat_chord(nodes, links, bsdf, chord_paths)
            return mat

    # Try Polyhaven texture maps for supported material types (random variant per name)
    asset_id = _pick_texture(mat_type, seed=hash(name))
    if asset_id:
        tex_paths = download_polyhaven_textures(asset_id, texture_cache_dir)
        if tex_paths:
            _mat_polyhaven(nodes, links, bsdf, tex_paths)
            return mat

    dispatch = {
        "tile":     _mat_tile,
        "wood":     _mat_wood,
        "metal":    _mat_metal,
        "paint":    _mat_paint,
        "concrete": _mat_concrete,
        "ceramic":  _mat_ceramic,
        "plastic":  _mat_plastic,
        "vinyl":    _mat_vinyl,
        "brick":    _mat_brick,
        "fabric":   _mat_fabric,
        "leather":  _mat_leather,
    }
    fn = dispatch.get(mat_type)
    if fn:
        fn(nodes, links, bsdf, color, roughness)
    else:
        _set(bsdf, "Base Color", color)
        _set(bsdf, "Roughness", roughness)

    return mat


def _find_chord_maps(chord_dir: str, surface_name: str) -> dict:
    """Look for CHORD-generated PBR maps in the materials directory."""
    d = Path(chord_dir) / surface_name
    if not d.is_dir():
        return {}
    maps = {}
    for map_type, file_suffix in [("Color", "color"), ("Normal", "normal"),
                                   ("Roughness", "roughness"), ("Metallic", "metallic")]:
        p = d / f"{surface_name}_{file_suffix}.png"
        if p.exists():
            maps[map_type] = str(p)
    return maps if "Color" in maps else {}


def _mat_chord(nodes, links, bsdf, chord_paths: dict):
    """Phase 3: Apply CHORD PBR maps (from real photo) to BSDF."""
    import bpy

    tc = nodes.new("ShaderNodeTexCoord")
    tc.location = (-800, 0)
    mp = nodes.new("ShaderNodeMapping")
    mp.location = (-600, 0)
    mp.inputs["Scale"].default_value = (4, 4, 4)
    links.new(tc.outputs["Generated"], mp.inputs["Vector"])
    vec = mp.outputs["Vector"]

    x = -300
    if "Color" in chord_paths and Path(chord_paths["Color"]).exists():
        img_node = nodes.new("ShaderNodeTexImage")
        img_node.location = (x, 300)
        img_node.image = bpy.data.images.load(chord_paths["Color"])
        links.new(vec, img_node.inputs["Vector"])
        links.new(img_node.outputs["Color"], bsdf.inputs["Base Color"])

    if "Roughness" in chord_paths and Path(chord_paths["Roughness"]).exists():
        img_node = nodes.new("ShaderNodeTexImage")
        img_node.location = (x, 100)
        img_node.image = bpy.data.images.load(chord_paths["Roughness"])
        img_node.image.colorspace_settings.name = "Non-Color"
        links.new(vec, img_node.inputs["Vector"])
        links.new(img_node.outputs["Color"], bsdf.inputs["Roughness"])

    if "Normal" in chord_paths and Path(chord_paths["Normal"]).exists():
        img_node = nodes.new("ShaderNodeTexImage")
        img_node.location = (x, -100)
        img_node.image = bpy.data.images.load(chord_paths["Normal"])
        img_node.image.colorspace_settings.name = "Non-Color"
        nmap = nodes.new("ShaderNodeNormalMap")
        nmap.location = (x + 200, -100)
        links.new(vec, img_node.inputs["Vector"])
        links.new(img_node.outputs["Color"], nmap.inputs["Color"])
        links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    if "Metallic" in chord_paths and Path(chord_paths["Metallic"]).exists():
        img_node = nodes.new("ShaderNodeTexImage")
        img_node.location = (x, -300)
        img_node.image = bpy.data.images.load(chord_paths["Metallic"])
        img_node.image.colorspace_settings.name = "Non-Color"
        links.new(vec, img_node.inputs["Vector"])
        links.new(img_node.outputs["Color"], bsdf.inputs["Metallic"])


def _mat_polyhaven(nodes, links, bsdf, tex_paths: dict):
    """Apply Polyhaven albedo + roughness + normal maps to BSDF."""
    import bpy

    tc = nodes.new("ShaderNodeTexCoord")
    tc.location = (-800, 0)
    mp = nodes.new("ShaderNodeMapping")
    mp.location = (-600, 0)
    mp.inputs["Scale"].default_value = (4, 4, 4)
    links.new(tc.outputs["Generated"], mp.inputs["Vector"])
    vec = mp.outputs["Vector"]

    x = -300
    if "Color" in tex_paths and Path(tex_paths["Color"]).exists():
        img_node = nodes.new("ShaderNodeTexImage")
        img_node.location = (x, 200)
        img_node.image = bpy.data.images.load(tex_paths["Color"])
        links.new(vec, img_node.inputs["Vector"])
        links.new(img_node.outputs["Color"], bsdf.inputs["Base Color"])

    if "Roughness" in tex_paths and Path(tex_paths["Roughness"]).exists():
        img_node = nodes.new("ShaderNodeTexImage")
        img_node.location = (x, 0)
        img_node.image = bpy.data.images.load(tex_paths["Roughness"])
        img_node.image.colorspace_settings.name = "Non-Color"
        links.new(vec, img_node.inputs["Vector"])
        links.new(img_node.outputs["Color"], bsdf.inputs["Roughness"])

    if "Normal" in tex_paths and Path(tex_paths["Normal"]).exists():
        img_node = nodes.new("ShaderNodeTexImage")
        img_node.location = (x, -200)
        img_node.image = bpy.data.images.load(tex_paths["Normal"])
        img_node.image.colorspace_settings.name = "Non-Color"
        nmap = nodes.new("ShaderNodeNormalMap")
        nmap.location = (x + 200, -200)
        links.new(vec, img_node.inputs["Vector"])
        links.new(img_node.outputs["Color"], nmap.inputs["Color"])
        links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])


def _texcoord_mapping(nodes, links, scale=(8, 8, 8)):
    tc = nodes.new("ShaderNodeTexCoord")
    tc.location = (-800, 0)
    mp = nodes.new("ShaderNodeMapping")
    mp.location = (-600, 0)
    mp.inputs["Scale"].default_value = scale
    links.new(tc.outputs["Generated"], mp.inputs["Vector"])
    return mp.outputs["Vector"]


def _noise_roughness(nodes, links, bsdf, base, noise_scale=40, spread=0.06):
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = noise_scale
    noise.inputs["Detail"].default_value = 3.0
    mr = nodes.new("ShaderNodeMapRange")
    mr.inputs["From Min"].default_value = 0.0
    mr.inputs["From Max"].default_value = 1.0
    mr.inputs["To Min"].default_value = max(0.0, base - spread)
    mr.inputs["To Max"].default_value = min(1.0, base + spread)
    links.new(noise.outputs["Fac"], mr.inputs["Value"])
    links.new(mr.outputs["Result"], bsdf.inputs["Roughness"])
    return noise


def _mat_tile(nodes, links, bsdf, color, roughness):
    """Ceramic tiles with grout lines."""
    vec = _texcoord_mapping(nodes, links, scale=(6, 6, 6))

    brick = nodes.new("ShaderNodeTexBrick")
    brick.location = (-200, 100)
    brick.inputs["Scale"].default_value = 1.0
    brick.inputs["Mortar Size"].default_value = 0.025
    brick.inputs["Mortar Smooth"].default_value = 0.15
    brick.inputs["Color1"].default_value = color
    brick.inputs["Color2"].default_value = color
    grout = darken(color, 0.75)
    brick.inputs["Mortar"].default_value = grout
    links.new(vec, brick.inputs["Vector"])
    links.new(brick.outputs["Color"], bsdf.inputs["Base Color"])

    # Roughness: tile smooth, grout rougher
    rough_ramp = nodes.new("ShaderNodeValToRGB")
    rough_ramp.location = (-200, -150)
    rough_ramp.color_ramp.elements[0].position = 0.0
    rough_ramp.color_ramp.elements[0].color = (roughness + 0.15,) * 3 + (1,)  # grout
    rough_ramp.color_ramp.elements[1].position = 1.0
    rough_ramp.color_ramp.elements[1].color = (roughness,) * 3 + (1,)          # tile
    links.new(brick.outputs["Fac"], rough_ramp.inputs["Fac"])
    links.new(rough_ramp.outputs["Color"], bsdf.inputs["Roughness"])

    _set(bsdf, "Specular IOR Level", 0.7)
    _set(bsdf, "Specular", 0.7)


def _mat_wood(nodes, links, bsdf, color, roughness):
    """Wood grain using wave + noise distortion."""
    vec = _texcoord_mapping(nodes, links, scale=(3, 3, 3))

    # Distortion noise
    dist = nodes.new("ShaderNodeTexNoise")
    dist.location = (-400, -150)
    dist.inputs["Scale"].default_value = 8.0
    dist.inputs["Detail"].default_value = 6.0
    dist.inputs["Roughness"].default_value = 0.6

    # Wave for grain lines
    wave = nodes.new("ShaderNodeTexWave")
    wave.location = (-200, 100)
    wave.wave_type = "BANDS"
    wave.bands_direction = "X"
    wave.inputs["Scale"].default_value = 6.0
    wave.inputs["Distortion"].default_value = 2.5
    wave.inputs["Detail"].default_value = 4.0
    wave.inputs["Detail Scale"].default_value = 1.0

    # Color gradient: dark grain lines → base wood color
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.location = (0, 100)
    ramp.color_ramp.interpolation = "LINEAR"
    ramp.color_ramp.elements[0].position = 0.0
    ramp.color_ramp.elements[0].color = darken(color, 0.6)
    ramp.color_ramp.elements[1].position = 1.0
    ramp.color_ramp.elements[1].color = color

    links.new(vec, dist.inputs["Vector"])
    links.new(vec, wave.inputs["Vector"])
    links.new(dist.outputs["Fac"], wave.inputs["Detail Roughness"])
    links.new(wave.outputs["Fac"], ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])

    n = _noise_roughness(nodes, links, bsdf, roughness, noise_scale=20, spread=0.04)
    links.new(vec, n.inputs["Vector"])

    _set(bsdf, "Specular IOR Level", 0.2)
    _set(bsdf, "Specular", 0.2)


def _mat_metal(nodes, links, bsdf, color, roughness):
    """Brushed metal (washing machine, appliances)."""
    vec = _texcoord_mapping(nodes, links, scale=(20, 20, 20))

    # Anisotropic scratches via noise
    noise = nodes.new("ShaderNodeTexNoise")
    noise.location = (-300, 100)
    noise.inputs["Scale"].default_value = 60.0
    noise.inputs["Detail"].default_value = 4.0
    noise.inputs["Roughness"].default_value = 0.5

    # Mix base color with slight noise
    mix = nodes.new("ShaderNodeMixRGB")
    mix.location = (0, 100)
    mix.blend_type = "MULTIPLY"
    mix.inputs["Fac"].default_value = 0.08
    mix.inputs["Color1"].default_value = color
    mix.inputs["Color2"].default_value = (1, 1, 1, 1)

    links.new(vec, noise.inputs["Vector"])
    links.new(noise.outputs["Color"], mix.inputs["Color2"])
    links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])

    n = _noise_roughness(nodes, links, bsdf, roughness, noise_scale=80, spread=0.03)
    links.new(vec, n.inputs["Vector"])

    _set(bsdf, "Metallic", 0.9)
    _set(bsdf, "Specular IOR Level", 1.0)
    _set(bsdf, "Specular", 1.0)
    _set(bsdf, "Anisotropic", 0.4)


def _mat_paint(nodes, links, bsdf, color, roughness):
    """Painted wall — subtle surface variation."""
    vec = _texcoord_mapping(nodes, links, scale=(15, 15, 15))

    noise = nodes.new("ShaderNodeTexNoise")
    noise.location = (-300, 0)
    noise.inputs["Scale"].default_value = 25.0
    noise.inputs["Detail"].default_value = 4.0
    noise.inputs["Roughness"].default_value = 0.7

    mix = nodes.new("ShaderNodeMixRGB")
    mix.location = (0, 0)
    mix.blend_type = "SOFT_LIGHT"
    mix.inputs["Fac"].default_value = 0.06
    mix.inputs["Color1"].default_value = color

    links.new(vec, noise.inputs["Vector"])
    links.new(noise.outputs["Color"], mix.inputs["Color2"])
    links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])

    n = _noise_roughness(nodes, links, bsdf, roughness, noise_scale=30, spread=0.05)
    links.new(vec, n.inputs["Vector"])


def _mat_concrete(nodes, links, bsdf, color, roughness):
    """Rough concrete — heavy noise."""
    vec = _texcoord_mapping(nodes, links, scale=(5, 5, 5))

    noise = nodes.new("ShaderNodeTexNoise")
    noise.location = (-300, 100)
    noise.inputs["Scale"].default_value = 12.0
    noise.inputs["Detail"].default_value = 8.0
    noise.inputs["Roughness"].default_value = 0.75

    mix = nodes.new("ShaderNodeMixRGB")
    mix.location = (0, 100)
    mix.blend_type = "MULTIPLY"
    mix.inputs["Fac"].default_value = 0.25
    mix.inputs["Color1"].default_value = color
    mix.inputs["Color2"].default_value = (0.9, 0.9, 0.85, 1.0)

    links.new(vec, noise.inputs["Vector"])
    links.new(noise.outputs["Color"], mix.inputs["Color2"])
    links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])

    n = _noise_roughness(nodes, links, bsdf, roughness, noise_scale=15, spread=0.1)
    links.new(vec, n.inputs["Vector"])


def _mat_ceramic(nodes, links, bsdf, color, roughness):
    """Smooth ceramic (sink, basin)."""
    _set(bsdf, "Base Color", color)
    _set(bsdf, "Roughness", 0.05)
    _set(bsdf, "Specular IOR Level", 1.0)
    _set(bsdf, "Specular", 1.0)
    _set(bsdf, "Clearcoat", 0.5)
    _set(bsdf, "Coat Weight", 0.5)


def _mat_plastic(nodes, links, bsdf, color, roughness):
    _set(bsdf, "Base Color", color)
    _set(bsdf, "Roughness", roughness)
    _set(bsdf, "Specular IOR Level", 0.5)
    _set(bsdf, "Specular", 0.5)


def _mat_vinyl(nodes, links, bsdf, color, roughness):
    """Vinyl floor — subtle texture."""
    vec = _texcoord_mapping(nodes, links, scale=(4, 4, 4))
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 80.0
    noise.inputs["Detail"].default_value = 2.0
    mix = nodes.new("ShaderNodeMixRGB")
    mix.blend_type = "SOFT_LIGHT"
    mix.inputs["Fac"].default_value = 0.05
    mix.inputs["Color1"].default_value = color
    links.new(vec, noise.inputs["Vector"])
    links.new(noise.outputs["Color"], mix.inputs["Color2"])
    links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
    _set(bsdf, "Roughness", roughness)


def _mat_brick(nodes, links, bsdf, color, roughness):
    """Exposed brick wall."""
    vec = _texcoord_mapping(nodes, links, scale=(4, 4, 4))
    brick = nodes.new("ShaderNodeTexBrick")
    brick.inputs["Scale"].default_value = 1.0
    brick.inputs["Mortar Size"].default_value = 0.04
    brick.inputs["Color1"].default_value = color
    brick.inputs["Color2"].default_value = darken(color, 0.85)
    brick.inputs["Mortar"].default_value = (0.55, 0.52, 0.5, 1.0)
    links.new(vec, brick.inputs["Vector"])
    links.new(brick.outputs["Color"], bsdf.inputs["Base Color"])
    _set(bsdf, "Roughness", roughness)


def _mat_fabric(nodes, links, bsdf, color, roughness):
    """Fabric — soft woven texture with subsurface fuzz."""
    vec = _texcoord_mapping(nodes, links, scale=(8, 8, 8))
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 120.0
    noise.inputs["Detail"].default_value = 6.0
    noise.inputs["Roughness"].default_value = 0.8
    mix = nodes.new("ShaderNodeMixRGB")
    mix.blend_type = "SOFT_LIGHT"
    mix.inputs["Fac"].default_value = 0.15
    mix.inputs["Color1"].default_value = color
    links.new(vec, noise.inputs["Vector"])
    links.new(noise.outputs["Color"], mix.inputs["Color2"])
    links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
    _set(bsdf, "Roughness", max(roughness, 0.7))
    _set(bsdf, "Sheen Weight", 0.3)
    _set(bsdf, "Sheen Tint", color)


def _mat_leather(nodes, links, bsdf, color, roughness):
    """Leather — fine grain with subtle reflections."""
    vec = _texcoord_mapping(nodes, links, scale=(6, 6, 6))
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 200.0
    noise.inputs["Detail"].default_value = 8.0
    noise.inputs["Roughness"].default_value = 0.6
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.1
    links.new(vec, noise.inputs["Vector"])
    links.new(noise.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    _set(bsdf, "Base Color", color)
    _set(bsdf, "Roughness", max(roughness, 0.35))
    _set(bsdf, "Specular IOR Level", 0.6)
    _set(bsdf, "Clearcoat Weight", 0.1)


# ── Scene geometry helpers ─────────────────────────────────────────────────────

def add_plane(name, location, scale, rotation=(0, 0, 0), color_hex="#CCCCCC",
              roughness=0.5, mat_type="generic", texture_cache_dir="sessions/texture_cache",
              chord_dir="", surface_name=""):
    import bpy
    bpy.ops.mesh.primitive_plane_add(size=1, location=location)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = scale
    obj.rotation_euler = rotation
    mat = make_material(f"mat_{name}", color_hex, roughness, mat_type, texture_cache_dir,
                        chord_dir=chord_dir, surface_name=surface_name)
    obj.data.materials.append(mat)
    # Solidify: makes planes visible from both sides in Cycles
    if "wall" in name or "ceiling" in name:
        mod = obj.modifiers.new("Solidify", 'SOLIDIFY')
        mod.thickness = 0.01
        mod.offset = 0
    return obj


def add_box(name, location, scale, color_hex="#888888", roughness=0.5, mat_type="generic",
            texture_cache_dir="sessions/texture_cache",
            chord_dir="", surface_name=""):
    import bpy
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = scale
    mat = make_material(f"mat_{name}", color_hex, roughness, mat_type, texture_cache_dir,
                        chord_dir=chord_dir, surface_name=surface_name)
    obj.data.materials.append(mat)
    return obj


# ── Better furniture shapes ────────────────────────────────────────────────

def _make_washer(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Washing machine: box body + circular porthole."""
    import bpy
    # Force procedural metal (not Polyhaven metal_plate texture)
    mat = make_material(f"mat_{name}", color_hex, 0.25, "paint", tex_cache)

    # Body
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, z0 + fh / 2))
    body = bpy.context.active_object
    body.name = name
    body.scale = (fw, fd, fh)
    body.data.materials.append(mat)

    # Porthole (cylinder on front face)
    r = min(fw, fh) * 0.28
    cy_y = y - fd / 2 - 0.01
    bpy.ops.mesh.primitive_cylinder_add(
        radius=r, depth=0.04,
        location=(x, cy_y, z0 + fh * 0.55)
    )
    port = bpy.context.active_object
    port.name = f"{name}_porthole"
    port.rotation_euler = (math.pi / 2, 0, 0)
    glass_mat = make_material(f"mat_{name}_glass", "#1A2A3A", 0.05, "ceramic", tex_cache)
    port.data.materials.append(glass_mat)


def _make_shelf(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Shelf: 3-5 horizontal planks stacked with gap."""
    import bpy
    mat = make_material(f"mat_{name}", color_hex, roughness, mat_type, tex_cache)
    plank_h = 0.025
    n_planks = max(2, min(5, int(fh / 0.22)))
    gap = (fh - n_planks * plank_h) / max(1, n_planks - 1)
    for i in range(n_planks):
        z = z0 + i * (plank_h + gap) + plank_h / 2
        bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, z))
        plank = bpy.context.active_object
        plank.name = f"{name}_plank{i}"
        plank.scale = (fw, fd, plank_h)
        plank.data.materials.append(mat)

    # Back panel
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y + fd / 2 - 0.01, z0 + fh / 2))
    back = bpy.context.active_object
    back.name = f"{name}_back"
    back.scale = (fw, 0.02, fh)
    back.data.materials.append(mat)


def _make_pipe(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Pipe as a cylinder — auto-detects orientation from dimensions."""
    import bpy
    # Pipes: always procedural (textures look wrong on thin cylinders)
    pipe_mat = "plastic" if mat_type == "plastic" else "metal"
    mat = make_material(f"mat_{name}", color_hex, max(roughness, 0.2), pipe_mat, tex_cache)

    if fw >= max(fh, fd):  # horizontal along X
        r = max(min(fd, fh) / 2, 0.02)
        bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=fw, location=(x, y, z0 + fh / 2))
        obj = bpy.context.active_object
        obj.rotation_euler = (0, math.pi / 2, 0)
    elif fd >= max(fh, fw):  # horizontal along Y
        r = max(min(fw, fh) / 2, 0.02)
        bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=fd, location=(x, y, z0 + fh / 2))
        obj = bpy.context.active_object
        obj.rotation_euler = (math.pi / 2, 0, 0)
    else:  # vertical (default)
        r = max(min(fw, fd) / 2, 0.02)
        bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=fh, location=(x, y, z0 + fh / 2))
        obj = bpy.context.active_object

    obj.name = name
    obj.data.materials.append(mat)


def _make_sofa(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Sofa: seat base + back cushion + two armrests."""
    import bpy
    mat = make_material(f"mat_{name}", color_hex, roughness, mat_type, tex_cache)

    seat_h = fh * 0.4
    back_h = fh * 0.6
    arm_w = fw * 0.08

    # Seat
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, z0 + seat_h / 2))
    seat = bpy.context.active_object
    seat.name = name
    seat.scale = (fw, fd, seat_h)
    seat.data.materials.append(mat)

    # Back
    bpy.ops.mesh.primitive_cube_add(size=1,
        location=(x, y + fd / 2 - fd * 0.12, z0 + seat_h + back_h / 2))
    back = bpy.context.active_object
    back.name = f"{name}_back"
    back.scale = (fw - arm_w * 2, fd * 0.24, back_h)
    back.data.materials.append(mat)

    # Armrests
    for side, sx in [("L", x - fw / 2 + arm_w / 2), ("R", x + fw / 2 - arm_w / 2)]:
        bpy.ops.mesh.primitive_cube_add(size=1,
            location=(sx, y, z0 + (seat_h + back_h * 0.5) / 2))
        arm = bpy.context.active_object
        arm.name = f"{name}_arm{side}"
        arm.scale = (arm_w, fd, seat_h + back_h * 0.5)
        arm.data.materials.append(mat)


def _make_armchair(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Armchair: seat + back + two wide armrests."""
    import bpy
    mat = make_material(f"mat_{name}", color_hex, roughness, mat_type, tex_cache)

    seat_h = fh * 0.45
    arm_w = fw * 0.15

    # Seat
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, z0 + seat_h / 2))
    seat = bpy.context.active_object
    seat.name = name
    seat.scale = (fw, fd, seat_h)
    seat.data.materials.append(mat)

    # Back
    bpy.ops.mesh.primitive_cube_add(size=1,
        location=(x, y + fd / 2 - fd * 0.1, z0 + seat_h + fh * 0.35))
    back = bpy.context.active_object
    back.name = f"{name}_back"
    back.scale = (fw - arm_w * 2, fd * 0.2, fh * 0.55)
    back.data.materials.append(mat)

    # Armrests
    for side, sx in [("L", x - fw / 2 + arm_w / 2), ("R", x + fw / 2 - arm_w / 2)]:
        bpy.ops.mesh.primitive_cube_add(size=1,
            location=(sx, y, z0 + seat_h * 0.8))
        arm = bpy.context.active_object
        arm.name = f"{name}_arm{side}"
        arm.scale = (arm_w, fd, seat_h * 0.6)
        arm.data.materials.append(mat)


def _make_plant(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Plant: pot + foliage sphere."""
    import bpy
    pot_h = fh * 0.25
    pot_r = min(fw, fd) * 0.35

    # Pot (brown cylinder)
    pot_mat = make_material(f"mat_{name}_pot", "#6B4226", 0.7, "ceramic", tex_cache)
    bpy.ops.mesh.primitive_cylinder_add(radius=pot_r, depth=pot_h,
        location=(x, y, z0 + pot_h / 2))
    pot = bpy.context.active_object
    pot.name = f"{name}_pot"
    pot.data.materials.append(pot_mat)

    # Foliage (ico sphere)
    foliage_r = max(fw, fd) * 0.45
    foliage_mat = make_material(f"mat_{name}", color_hex, roughness, mat_type, tex_cache)
    bpy.ops.mesh.primitive_ico_sphere_add(
        radius=foliage_r, subdivisions=2,
        location=(x, y, z0 + pot_h + foliage_r * 0.8))
    foliage = bpy.context.active_object
    foliage.name = name
    foliage.data.materials.append(foliage_mat)


def _make_lamp(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Lamp: thin pole + cone/cylinder shade."""
    import bpy
    pole_r = min(fw, fd) * 0.06
    shade_r = min(fw, fd) * 0.4
    shade_h = fh * 0.3

    # Pole
    mat = make_material(f"mat_{name}_pole", "#888888", 0.3, "metal", tex_cache)
    bpy.ops.mesh.primitive_cylinder_add(radius=pole_r, depth=fh * 0.7,
        location=(x, y, z0 + fh * 0.35))
    pole = bpy.context.active_object
    pole.name = f"{name}_pole"
    pole.data.materials.append(mat)

    # Shade
    shade_mat = make_material(f"mat_{name}", color_hex, roughness, mat_type, tex_cache)
    bpy.ops.mesh.primitive_cone_add(
        radius1=shade_r, radius2=shade_r * 0.6, depth=shade_h,
        location=(x, y, z0 + fh - shade_h / 2))
    shade = bpy.context.active_object
    shade.name = name
    shade.data.materials.append(shade_mat)


def _make_mirror(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Mirror: thin reflective rectangle."""
    import bpy
    mat = make_material(f"mat_{name}", "#E8E8E8", 0.02, "metal", tex_cache)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, z0 + fh / 2))
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = (fw, max(fd, 0.03), fh)
    obj.data.materials.append(mat)


def _make_rug(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Rug: thin flat plane just above the floor."""
    import bpy
    mat = make_material(f"mat_{name}", color_hex, roughness, "fabric" if mat_type == "generic" else mat_type, tex_cache)
    bpy.ops.mesh.primitive_plane_add(size=1, location=(x, y, 0.005))
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = (fw, fd, 1)
    obj.data.materials.append(mat)


def _make_wall_art(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Wall art / picture frame: frame border + inner canvas."""
    import bpy
    frame_w = 0.03

    # Frame (darker border)
    frame_mat = make_material(f"mat_{name}_frame", "#2A2A2A", 0.3, "wood", tex_cache)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, z0 + fh / 2))
    frame = bpy.context.active_object
    frame.name = name
    frame.scale = (fw, max(fd, 0.025), fh)
    frame.data.materials.append(frame_mat)

    # Inner canvas (lighter, slightly recessed)
    canvas_mat = make_material(f"mat_{name}_canvas", color_hex, roughness, mat_type, tex_cache)
    bpy.ops.mesh.primitive_cube_add(size=1,
        location=(x, y - 0.005, z0 + fh / 2))
    canvas = bpy.context.active_object
    canvas.name = f"{name}_canvas"
    canvas.scale = (fw - frame_w * 2, 0.005, fh - frame_w * 2)
    canvas.data.materials.append(canvas_mat)


def _make_bed(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Bed: base frame + mattress + headboard + pillows."""
    import bpy
    mat = make_material(f"mat_{name}", color_hex, roughness, mat_type, tex_cache)

    frame_h = fh * 0.25
    mattress_h = fh * 0.2

    # Frame
    frame_mat = make_material(f"mat_{name}_frame", "#5C4033", 0.5, "wood", tex_cache)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, z0 + frame_h / 2))
    frame = bpy.context.active_object
    frame.name = name
    frame.scale = (fw, fd, frame_h)
    frame.data.materials.append(frame_mat)

    # Mattress
    bpy.ops.mesh.primitive_cube_add(size=1,
        location=(x, y - fd * 0.02, z0 + frame_h + mattress_h / 2))
    mattress = bpy.context.active_object
    mattress.name = f"{name}_mattress"
    mattress.scale = (fw * 0.95, fd * 0.95, mattress_h)
    mattress.data.materials.append(mat)

    # Headboard
    bpy.ops.mesh.primitive_cube_add(size=1,
        location=(x, y + fd / 2 - 0.04, z0 + fh * 0.6))
    head = bpy.context.active_object
    head.name = f"{name}_headboard"
    head.scale = (fw, 0.08, fh * 0.7)
    head.data.materials.append(frame_mat)

    # Pillows
    pillow_mat = make_material(f"mat_{name}_pillow", "#F5F0E8", 0.6, "fabric", tex_cache)
    for offset in (-fw * 0.22, fw * 0.22):
        bpy.ops.mesh.primitive_cube_add(size=1,
            location=(x + offset, y + fd * 0.3, z0 + frame_h + mattress_h + 0.06))
        pillow = bpy.context.active_object
        pillow.name = f"{name}_pillow"
        pillow.scale = (fw * 0.3, fd * 0.2, 0.08)
        pillow.data.materials.append(pillow_mat)


def _make_bookshelf(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Bookshelf: vertical sides + horizontal shelves + random colored books."""
    import bpy
    mat = make_material(f"mat_{name}", color_hex, roughness, mat_type, tex_cache)
    shelf_h = 0.02

    # Two sides
    for sx in (x - fw / 2 + 0.015, x + fw / 2 - 0.015):
        bpy.ops.mesh.primitive_cube_add(size=1, location=(sx, y, z0 + fh / 2))
        side = bpy.context.active_object
        side.name = f"{name}_side"
        side.scale = (0.03, fd, fh)
        side.data.materials.append(mat)

    # Horizontal shelves
    n_shelves = max(2, min(6, int(fh / 0.3)))
    for i in range(n_shelves):
        sz = z0 + i * (fh / max(1, n_shelves - 1))
        bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, sz))
        shelf = bpy.context.active_object
        shelf.name = f"{name}_shelf{i}"
        shelf.scale = (fw, fd, shelf_h)
        shelf.data.materials.append(mat)

    # Back panel
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y + fd / 2 - 0.01, z0 + fh / 2))
    back = bpy.context.active_object
    back.name = f"{name}_back"
    back.scale = (fw, 0.015, fh)
    back.data.materials.append(mat)


def _make_tv(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """TV: thin screen + small stand base."""
    import bpy
    # Screen
    screen_mat = make_material(f"mat_{name}", "#0A0A0A", 0.05, "plastic", tex_cache)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, z0 + fh / 2))
    screen = bpy.context.active_object
    screen.name = name
    screen.scale = (fw, max(fd, 0.03), fh)
    screen.data.materials.append(screen_mat)

    # Stand
    stand_mat = make_material(f"mat_{name}_stand", "#333333", 0.2, "metal", tex_cache)
    bpy.ops.mesh.primitive_cylinder_add(radius=fw * 0.05, depth=fh * 0.15,
        location=(x, y, z0 + fh * 0.075))
    stand = bpy.context.active_object
    stand.name = f"{name}_stand"
    stand.data.materials.append(stand_mat)

    # Base
    bpy.ops.mesh.primitive_cube_add(size=1,
        location=(x, y - fd * 0.2, z0 + 0.01))
    base = bpy.context.active_object
    base.name = f"{name}_base"
    base.scale = (fw * 0.4, fd * 0.8, 0.015)
    base.data.materials.append(stand_mat)


def _make_curtain(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Curtain: two panels flanking the center with a rod on top."""
    import bpy
    mat = make_material(f"mat_{name}", color_hex, roughness, "fabric", tex_cache)

    panel_w = fw * 0.35
    # Left panel
    bpy.ops.mesh.primitive_cube_add(size=1,
        location=(x - fw / 2 + panel_w / 2, y, z0 + fh / 2))
    lp = bpy.context.active_object
    lp.name = f"{name}_left"
    lp.scale = (panel_w, 0.04, fh)
    lp.data.materials.append(mat)

    # Right panel
    bpy.ops.mesh.primitive_cube_add(size=1,
        location=(x + fw / 2 - panel_w / 2, y, z0 + fh / 2))
    rp = bpy.context.active_object
    rp.name = f"{name}_right"
    rp.scale = (panel_w, 0.04, fh)
    rp.data.materials.append(mat)

    # Rod
    rod_mat = make_material(f"mat_{name}_rod", "#888888", 0.3, "metal", tex_cache)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.01, depth=fw * 1.1,
        location=(x, y, z0 + fh + 0.02))
    rod = bpy.context.active_object
    rod.name = f"{name}_rod"
    rod.rotation_euler = (0, math.pi / 2, 0)
    rod.data.materials.append(rod_mat)


def _make_vase(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Decorative vase: tapered cylinder."""
    import bpy
    mat = make_material(f"mat_{name}", color_hex, roughness, mat_type, tex_cache)
    r_bottom = min(fw, fd) * 0.35
    r_top = r_bottom * 0.55
    bpy.ops.mesh.primitive_cone_add(
        radius1=r_bottom, radius2=r_top, depth=fh,
        location=(x, y, z0 + fh / 2))
    obj = bpy.context.active_object
    obj.name = name
    obj.data.materials.append(mat)


def _make_radiator(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Radiator/heater: flat panel with horizontal fins."""
    import bpy
    mat = make_material(f"mat_{name}", color_hex, max(roughness, 0.3), "metal", tex_cache)

    # Main body (thin panel)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, z0 + fh / 2))
    body = bpy.context.active_object
    body.name = name
    body.scale = (fw, max(fd, 0.05), fh)
    body.data.materials.append(mat)

    # Horizontal fins (4-8 depending on height)
    n_fins = max(3, min(8, int(fh / 0.1)))
    fin_gap = fh / (n_fins + 1)
    fin_mat = make_material(f"mat_{name}_fin", color_hex, 0.35, "metal", tex_cache)
    for i in range(n_fins):
        fz = z0 + fin_gap * (i + 1)
        bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y - fd / 2 - 0.005, fz))
        fin = bpy.context.active_object
        fin.name = f"{name}_fin_{i}"
        fin.scale = (fw * 0.95, 0.003, 0.015)
        fin.data.materials.append(fin_mat)


def _make_decor(name, x, y, z0, fw, fd, fh, color_hex, roughness, mat_type, tex_cache):
    """Smart decor: picks shape based on proportions and material."""
    import bpy
    # Small items: use procedural material (Polyhaven textures have wrong scale)
    vol = fw * fd * fh
    if vol < 0.05:
        mat = make_material(f"mat_{name}", color_hex, roughness, "paint", tex_cache)
    else:
        mat = make_material(f"mat_{name}", color_hex, roughness, mat_type, tex_cache)

    # Very flat → rug/mat
    if fh <= 0.04:
        bpy.ops.mesh.primitive_plane_add(size=1, location=(x, y, z0 + fh / 2))
        obj = bpy.context.active_object
        obj.scale = (fw, fd, 1)
        obj.name = name
        obj.data.materials.append(mat)
        return

    # Thin panel (radiator, screen, leaning object)
    if fd <= 0.08 or fw <= 0.08:
        bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, z0 + fh / 2))
        obj = bpy.context.active_object
        obj.scale = (fw, max(fd, 0.03), fh)
        obj.name = name
        obj.data.materials.append(mat)
        return

    # Tall and thin → pole/stick
    if fh > 2.5 * max(fw, fd):
        r = min(fw, fd) / 2
        bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=fh, location=(x, y, z0 + fh / 2))
        obj = bpy.context.active_object
        obj.name = name
        obj.data.materials.append(mat)
        return

    # Fabric → soft rounded shape (cylinder)
    if mat_type == "fabric":
        r = max(fw, fd) / 2
        bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=fh, location=(x, y, z0 + fh / 2))
        obj = bpy.context.active_object
        obj.name = name
        obj.data.materials.append(mat)
        return

    # Default: beveled box (rounded edges)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, z0 + fh / 2))
    obj = bpy.context.active_object
    obj.scale = (fw, fd, fh)
    obj.name = name
    obj.data.materials.append(mat)
    bevel = obj.modifiers.new("Bevel", 'BEVEL')
    bevel.width = min(fw, fd, fh) * 0.08
    bevel.segments = 2


def place_furniture(item, tex_cache, furniture_glb_dir="", room_w=None, room_d=None):
    """Dispatch furniture creation: GLB import (Phase 4) > shape builder > box fallback."""
    item_type = item.get("type", "")
    defaults  = FURNITURE_DEFAULTS.get(item_type, ("#999999", "generic", 0.5))

    raw_color = item.get("color", defaults[0])
    raw_mat   = item.get("material", defaults[1])
    mat_type  = MAT_TYPE_MAP.get(raw_mat, defaults[1])
    roughness = defaults[2]

    pos  = item.get("position", {})
    size = item.get("size",     {})
    x  = float(pos.get("x", 0))
    y  = float(pos.get("y", 0))
    z0 = float(pos.get("z", 0))
    fw = float(size.get("width",  0.6))
    fd = float(size.get("depth",  0.6))
    fh = float(size.get("height", 0.85))
    name = item.get("id", item_type)

    # Floor-standing items: force z=0 unless explicitly elevated (shelves, pipes, wall items)
    floor_types = {"washing_machine", "washer", "dryer", "sofa", "couch", "armchair",
                   "chair", "table", "bed", "cabinet", "bookshelf", "tv_stand",
                   "sideboard", "console", "wardrobe", "dresser", "nightstand",
                   "stool", "ottoman", "plant", "basket", "decor", "vase", "rug", "carpet"}
    if item_type in floor_types and z0 > 0.1 and z0 < fh:
        z0 = 0.0

    # Clamp furniture position to stay within room bounds
    if room_w is not None and room_d is not None:
        half_w = room_w / 2 - 0.05
        half_d = room_d / 2 - 0.05
        x = max(-half_w + fw / 2, min(half_w - fw / 2, x))
        y = max(-half_d + fd / 2, min(half_d - fd / 2, y))

    # Phase 4: Try importing real 3D model (GLB) from TRELLIS.2
    if furniture_glb_dir:
        glb_path = Path(furniture_glb_dir) / f"{name}.glb"
        if glb_path.exists():
            if _import_glb(name, str(glb_path), x, y, z0, fw, fd, fh):
                return

    # Shape builders by type
    shape_builders = {
        "washing_machine": _make_washer,
        "dryer":           _make_radiator,
        "radiator":        _make_radiator,
        "heater":          _make_radiator,
        "shelf":           _make_shelf,
        "pipe":            _make_pipe,
        "sofa":            _make_sofa,
        "couch":           _make_sofa,
        "armchair":        _make_armchair,
        "chair":           _make_armchair,
        "plant":           _make_plant,
        "lamp":            _make_lamp,
        "floor_lamp":      _make_lamp,
        "table_lamp":      _make_lamp,
        "mirror":          _make_mirror,
        "rug":             _make_rug,
        "carpet":          _make_rug,
        "wall_art":        _make_wall_art,
        "painting":        _make_wall_art,
        "picture":         _make_wall_art,
        "poster":          _make_wall_art,
        "clock":           _make_wall_art,
        "bed":             _make_bed,
        "bookshelf":       _make_bookshelf,
        "tv":              _make_tv,
        "television":      _make_tv,
        "monitor":         _make_tv,
        "curtain":         _make_curtain,
        "drapes":          _make_curtain,
        "vase":            _make_vase,
        "decor":           _make_decor,
        "basket":          _make_decor,
    }
    builder = shape_builders.get(item_type)
    if builder:
        builder(name, x, y, z0, fw, fd, fh, raw_color, roughness, mat_type, tex_cache)
    else:
        _make_decor(name, x, y, z0, fw, fd, fh, raw_color, roughness, mat_type, tex_cache)


def _import_glb(name: str, glb_path: str, x: float, y: float, z0: float,
                fw: float, fd: float, fh: float) -> bool:
    """Phase 4: Import GLB file and scale/position to match room_data dimensions."""
    import bpy
    try:
        bpy.ops.import_scene.gltf(filepath=glb_path)
        imported = bpy.context.selected_objects
        if not imported:
            return False

        # Find bounding box of imported objects
        import mathutils
        all_verts = []
        for obj in imported:
            if obj.type == "MESH":
                for v in obj.data.vertices:
                    all_verts.append(obj.matrix_world @ v.co)

        if not all_verts:
            # No mesh data — remove and fallback
            for obj in imported:
                bpy.data.objects.remove(obj, do_unlink=True)
            return False

        # Calculate imported bounds
        min_co = mathutils.Vector((min(v.x for v in all_verts),
                                   min(v.y for v in all_verts),
                                   min(v.z for v in all_verts)))
        max_co = mathutils.Vector((max(v.x for v in all_verts),
                                   max(v.y for v in all_verts),
                                   max(v.z for v in all_verts)))
        imp_size = max_co - min_co
        imp_center = (min_co + max_co) / 2

        # Calculate scale to fit target dimensions
        sx = fw / max(imp_size.x, 0.001)
        sy = fd / max(imp_size.y, 0.001)
        sz = fh / max(imp_size.z, 0.001)
        scale = min(sx, sy, sz)  # Uniform scale to fit within bounds

        # Parent all imported objects to an empty
        bpy.ops.object.empty_add(location=(x, y, z0 + fh / 2))
        parent = bpy.context.active_object
        parent.name = name

        for obj in imported:
            obj.parent = parent

        parent.scale = (scale, scale, scale)

        # Offset children so the model is centered at the parent
        offset = -imp_center * scale
        for obj in imported:
            obj.location += offset

        return True
    except Exception as e:
        print(f"[furniture] GLB import failed for {name}: {e}")
        return False


# ── Main scene builder ─────────────────────────────────────────────────────────

# Map JSON material strings to shader types
MAT_TYPE_MAP = {
    "tile":      "tile",
    "wood":      "wood",
    "concrete":  "concrete",
    "vinyl":     "vinyl",
    "paint":     "paint",
    "brick":     "brick",
    "metal":     "metal",
    "plastic":   "plastic",
    "ceramic":   "ceramic",
    "fabric":    "fabric",
    "leather":   "leather",
    "glass":     "glass",
}

FURNITURE_DEFAULTS = {
    "washing_machine": ("#EFEFEF", "metal",   0.25),
    "dryer":           ("#E8E8E8", "metal",   0.25),
    "radiator":        ("#D0D0D0", "metal",   0.35),
    "heater":          ("#D0D0D0", "metal",   0.35),
    "shelf":           ("#C4A265", "wood",    0.55),
    "cabinet":         ("#D2B48C", "wood",    0.50),
    "sink":            ("#F0F4F8", "ceramic", 0.05),
    "table":           ("#8B7355", "wood",    0.55),
    "basket":          ("#D2B48C", "plastic", 0.70),
    "pipe":            ("#A9A9A9", "metal",   0.40),
    "sofa":            ("#8B7355", "fabric",  0.65),
    "couch":           ("#8B7355", "fabric",  0.65),
    "armchair":        ("#C4A265", "fabric",  0.65),
    "chair":           ("#8B7355", "wood",    0.55),
    "plant":           ("#3A6B35", "plastic", 0.70),
    "lamp":            ("#F5F0E8", "ceramic", 0.30),
    "floor_lamp":      ("#F5F0E8", "ceramic", 0.30),
    "table_lamp":      ("#F5F0E8", "ceramic", 0.30),
    "mirror":          ("#E8E8E8", "metal",   0.02),
    "rug":             ("#D4C9B0", "fabric",  0.80),
    "carpet":          ("#D4C9B0", "fabric",  0.80),
    "wall_art":        ("#1A1A1A", "generic", 0.50),
    "decor":           ("#999999", "generic", 0.50),
    "bed":             ("#E8E0D0", "fabric",  0.65),
    "desk":            ("#8B7355", "wood",    0.55),
    "bookshelf":       ("#C4A265", "wood",    0.55),
    "tv":              ("#1A1A1A", "plastic", 0.10),
    "tv_stand":        ("#3D3028", "wood",    0.50),
    "painting":        ("#E8E0D0", "generic", 0.50),
    "picture":         ("#E8E0D0", "generic", 0.50),
    "poster":          ("#F5F0E8", "generic", 0.50),
    "clock":           ("#1A1A1A", "generic", 0.30),
    "curtain":         ("#E8E0D0", "fabric",  0.70),
    "drapes":          ("#C4A265", "fabric",  0.70),
    "vase":            ("#8B5E3C", "ceramic", 0.30),
    "cushion":         ("#C8B89A", "fabric",  0.65),
    "ottoman":         ("#8B7355", "fabric",  0.65),
    "stool":           ("#8B7355", "wood",    0.55),
    "sideboard":       ("#3D3028", "wood",    0.50),
    "console":         ("#3D3028", "wood",    0.50),
    "nightstand":      ("#8B7355", "wood",    0.55),
    "wardrobe":        ("#D2B48C", "wood",    0.50),
    "dresser":         ("#D2B48C", "wood",    0.50),
    "monitor":         ("#1A1A1A", "plastic", 0.10),
    "television":      ("#1A1A1A", "plastic", 0.10),
}


CAMERA_CORNERS = [
    # Camera on south side — always sees north wall front face
    # (corner_x_sign, corner_y_sign)
    (-1, -1),  # SW corner → looks NE
    ( 1, -1),  # SE corner → looks NW
    ( 0, -1),  # S center  → looks N (front view)
    (-1,  0),  # W center  → looks E (side view)
]


def _setup_camera(bpy, W: float, D: float, H: float, camera_id: int = 0,
                   furniture_data: list | None = None):
    """Smart camera: picks best corner to see most furniture, or uses camera_id preset."""
    import mathutils

    buf = 0.25  # distance from wall
    eye_h = min(H * 0.45, 1.50)
    # Wider FOV for small rooms, narrower for large
    room_diag = math.sqrt(W**2 + D**2)
    lens = max(14, min(22, 18 - (room_diag - 5) * 1.2))

    # Target: blend between room center and furniture centroid
    cx, cy, cz = 0.0, 0.0, H * 0.35
    if furniture_data:
        positions = []
        for item in furniture_data:
            pos = item.get("position", {})
            sz = item.get("size", {})
            px = float(pos.get("x", 0))
            py = float(pos.get("y", 0))
            pz = float(pos.get("z", 0)) + float(sz.get("height", 0.5)) * 0.5
            positions.append((px, py, pz))
        if positions:
            fcx = sum(p[0] for p in positions) / len(positions)
            fcy = sum(p[1] for p in positions) / len(positions)
            # Blend 60% room center + 40% furniture centroid
            cx = fcx * 0.4
            cy = fcy * 0.4
            cz = H * 0.35

    if camera_id == 0 and furniture_data:
        # Auto-select: pick corner farthest from furniture centroid
        best_corner = 0
        best_dist = -1
        for i, (sx, sy) in enumerate(CAMERA_CORNERS):
            corner_x = sx * (W / 2 - buf)
            corner_y = sy * (D / 2 - buf)
            dist = math.sqrt((corner_x - cx) ** 2 + (corner_y - cy) ** 2)
            if dist > best_dist:
                best_dist = dist
                best_corner = i
        sx, sy = CAMERA_CORNERS[best_corner]
    else:
        sx, sy = CAMERA_CORNERS[camera_id % len(CAMERA_CORNERS)]

    cam_loc = mathutils.Vector((
        sx * (W / 2 - buf),
        sy * (D / 2 - buf),
        eye_h
    ))
    target = mathutils.Vector((cx, cy, cz))
    direction = target - cam_loc
    rot_quat = direction.to_track_quat("-Z", "Y")

    bpy.ops.object.camera_add(location=cam_loc)
    cam = bpy.context.active_object
    cam.rotation_euler = rot_quat.to_euler()
    cam.data.lens = lens
    cam.data.sensor_width = 36
    cam.data.clip_start = 0.1
    cam.data.clip_end = max(50, W + D + H)
    bpy.context.scene.camera = cam


def _build_architectural_features(arch: dict, W: float, D: float, H: float, tex_cache: str):
    """Add architectural features: mezzanine, stairs, fireplace, window bays."""
    import bpy

    # ── Mezzanine / gallery balcony ──
    mezz = arch.get("mezzanine")
    if mezz:
        mezz_h = float(mezz.get("height", 2.8))
        mezz_depth = float(mezz.get("depth", 1.5))
        wall = mezz.get("wall", "north")

        # Floor slab
        slab_mat = make_material("mat_mezz_slab", "#6B4A32", 0.4, "wood", tex_cache)
        if wall == "north":
            slab_y = D / 2 - mezz_depth / 2
        else:
            slab_y = -(D / 2 - mezz_depth / 2)
        bpy.ops.mesh.primitive_cube_add(size=1, location=(0, slab_y, mezz_h))
        slab = bpy.context.active_object
        slab.name = "mezzanine_slab"
        slab.scale = (W, mezz_depth, 0.12)
        slab.data.materials.append(slab_mat)

        # Railing (glass + metal frame)
        rail_mat = make_material("mat_mezz_rail", "#2A2A2A", 0.2, "metal", tex_cache)
        glass_mat = make_material("mat_mezz_glass", "#D8E8F0", 0.02, "ceramic", tex_cache)

        # Top rail bar
        rail_y = slab_y - mezz_depth / 2
        bpy.ops.mesh.primitive_cube_add(size=1, location=(0, rail_y, mezz_h + 0.55))
        rail_top = bpy.context.active_object
        rail_top.name = "mezzanine_rail_top"
        rail_top.scale = (W, 0.04, 0.04)
        rail_top.data.materials.append(rail_mat)

        # Glass panel
        bpy.ops.mesh.primitive_cube_add(size=1, location=(0, rail_y, mezz_h + 0.30))
        glass = bpy.context.active_object
        glass.name = "mezzanine_glass"
        glass.scale = (W * 0.95, 0.01, 0.50)
        glass.data.materials.append(glass_mat)

        # Vertical posts
        for px in (-W / 2 + 0.05, 0, W / 2 - 0.05):
            bpy.ops.mesh.primitive_cube_add(size=1,
                location=(px, rail_y, mezz_h + 0.30))
            post = bpy.context.active_object
            post.name = "mezzanine_post"
            post.scale = (0.03, 0.03, 0.60)
            post.data.materials.append(rail_mat)

    # ── Stairs ──
    stairs = arch.get("stairs")
    if stairs and mezz:
        mezz_h = float(mezz.get("height", 2.8))
        pos = stairs.get("position", "northwest")
        tread_mat = make_material("mat_stair_tread", "#6B4A32", 0.4, "wood", tex_cache)
        riser_mat = make_material("mat_stair_riser", "#F5F0E8", 0.5, "paint", tex_cache)

        n_steps = int(mezz_h / 0.18)
        step_h = mezz_h / n_steps
        step_d = 0.28
        step_w = 0.95

        # Position stairs along west or east wall
        if "west" in pos:
            sx = -W / 2 + step_w / 2 + 0.1
        else:
            sx = W / 2 - step_w / 2 - 0.1

        for i in range(n_steps):
            sz = i * step_h + step_h / 2
            sy = D / 2 - 0.5 - i * step_d * 0.6

            # Stop if stair extends past room bounds
            if sy - step_d / 2 < -D / 2:
                break

            # Tread
            bpy.ops.mesh.primitive_cube_add(size=1, location=(sx, sy, sz))
            tread = bpy.context.active_object
            tread.name = f"stair_tread_{i}"
            tread.scale = (step_w, step_d, 0.04)
            tread.data.materials.append(tread_mat)

            # Riser
            bpy.ops.mesh.primitive_cube_add(size=1,
                location=(sx, sy + step_d / 2, sz - step_h / 2))
            riser = bpy.context.active_object
            riser.name = f"stair_riser_{i}"
            riser.scale = (step_w, 0.02, step_h)
            riser.data.materials.append(riser_mat)

    # ── Fireplace ──
    fp = arch.get("fireplace")
    if fp:
        fp_wall = fp.get("wall", "north")
        surround_mat = make_material("mat_fireplace", "#A0988E", 0.35, "concrete", tex_cache)
        glass_mat = make_material("mat_fp_glass", "#1A1A1A", 0.05, "ceramic", tex_cache)

        if fp_wall == "north":
            fy = D / 2 - 0.25
        elif fp_wall == "south":
            fy = -(D / 2 - 0.25)
        else:
            fy = 0

        # Concrete surround column
        bpy.ops.mesh.primitive_cube_add(size=1, location=(0, fy, 1.2))
        surround = bpy.context.active_object
        surround.name = "fireplace_surround"
        surround.scale = (1.4, 0.50, 2.4)
        surround.data.materials.append(surround_mat)

        # Glass fire opening
        bpy.ops.mesh.primitive_cube_add(size=1, location=(0, fy - 0.22, 0.5))
        opening = bpy.context.active_object
        opening.name = "fireplace_glass"
        opening.scale = (1.0, 0.05, 0.50)
        opening.data.materials.append(glass_mat)

        # Black base
        base_mat = make_material("mat_fp_base", "#1A1A1A", 0.15, "metal", tex_cache)
        bpy.ops.mesh.primitive_cube_add(size=1, location=(0, fy - 0.15, 0.15))
        base = bpy.context.active_object
        base.name = "fireplace_base"
        base.scale = (1.2, 0.35, 0.30)
        base.data.materials.append(base_mat)


def _build_walls_with_openings(openings, W, D, H, wall_color, wall_rough, wall_mat,
                                texture_cache, chord_dir):
    """Build walls with cutouts for windows and doors."""
    import bpy

    # Wall definitions: name → (center, wall_length, rotation, normal_axis)
    wall_specs = {
        "north": {"pos_y":  D/2, "length": W, "rot": (math.pi/2, 0, 0),         "axis": "x"},
        "south": {"pos_y": -D/2, "length": W, "rot": (-math.pi/2, 0, 0),        "axis": "x"},
        "east":  {"pos_x":  W/2, "length": D, "rot": (math.pi/2, 0, math.pi/2), "axis": "y"},
        "west":  {"pos_x": -W/2, "length": D, "rot": (math.pi/2, 0, -math.pi/2),"axis": "y"},
    }

    # Group openings by wall
    wall_openings: dict[str, list] = {k: [] for k in wall_specs}
    for op in openings:
        wname = op.get("wall", "").lower()
        if wname in wall_openings:
            wall_openings[wname].append(op)

    glass_mat = None  # lazy create

    for wname, spec in wall_specs.items():
        ops = sorted(wall_openings[wname], key=lambda o: o.get("position_along_wall", 0.5))
        L = spec["length"]
        rot = spec["rot"]

        # Wall position in 3D (use default args to capture loop values)
        if "pos_y" in spec:
            wall_y = spec["pos_y"]
            def _loc(u_center, v_center, sx, sy, _L=L, _wy=wall_y):
                return ((-_L/2 + u_center), _wy, v_center), (sx, sy, 1)
        else:
            wall_x = spec["pos_x"]
            def _loc(u_center, v_center, sx, sy, _L=L, _wx=wall_x):
                return (_wx, (-_L/2 + u_center), v_center), (sx, sy, 1)

        if not ops:
            # No openings — full wall
            center_u = L / 2
            center_v = H / 2
            loc, sc = _loc(center_u, center_v, L, H)
            add_plane(f"wall_{wname}", loc, sc, rotation=rot,
                      color_hex=wall_color, roughness=wall_rough, mat_type=wall_mat,
                      texture_cache_dir=texture_cache,
                      chord_dir=chord_dir, surface_name="wall")
            continue

        # Build wall segments around openings
        seg_idx = 0
        prev_right = 0.0  # in wall-local U coords (0..L)

        for op in ops:
            pos = float(op.get("position_along_wall", 0.5))
            ow = float(op.get("width", 0.8))
            oh = float(op.get("height", 1.2))
            ob = float(op.get("bottom_height", 0.9))
            otype = op.get("type", "window")

            u_left = max(0, pos * L - ow / 2)
            u_right = min(L, pos * L + ow / 2)
            v_bottom = ob
            v_top = min(H, ob + oh)

            # Left full-height segment (from prev_right to u_left)
            if u_left - prev_right > 0.01:
                seg_w = u_left - prev_right
                cu = prev_right + seg_w / 2
                loc, sc = _loc(cu, H / 2, seg_w, H)
                add_plane(f"wall_{wname}_s{seg_idx}", loc, sc, rotation=rot,
                          color_hex=wall_color, roughness=wall_rough, mat_type=wall_mat,
                          texture_cache_dir=texture_cache, chord_dir=chord_dir, surface_name="wall")
                seg_idx += 1

            # Below opening
            if v_bottom > 0.01:
                seg_h = v_bottom
                cu = (u_left + u_right) / 2
                loc, sc = _loc(cu, seg_h / 2, ow, seg_h)
                add_plane(f"wall_{wname}_s{seg_idx}", loc, sc, rotation=rot,
                          color_hex=wall_color, roughness=wall_rough, mat_type=wall_mat,
                          texture_cache_dir=texture_cache, chord_dir=chord_dir, surface_name="wall")
                seg_idx += 1

            # Above opening
            if v_top < H - 0.01:
                seg_h = H - v_top
                cu = (u_left + u_right) / 2
                loc, sc = _loc(cu, v_top + seg_h / 2, ow, seg_h)
                add_plane(f"wall_{wname}_s{seg_idx}", loc, sc, rotation=rot,
                          color_hex=wall_color, roughness=wall_rough, mat_type=wall_mat,
                          texture_cache_dir=texture_cache, chord_dir=chord_dir, surface_name="wall")
                seg_idx += 1

            # Window glass pane or door panel
            cu = (u_left + u_right) / 2
            cv = v_bottom + oh / 2
            glass_loc, _ = _loc(cu, cv, 0, 0)

            if otype == "window":
                if glass_mat is None:
                    glass_mat = bpy.data.materials.new(name="mat_window_glass")
                    glass_mat.use_backface_culling = False
                    glass_mat.use_nodes = True
                    bsdf = glass_mat.node_tree.nodes.get("Principled BSDF")
                    if bsdf:
                        _set(bsdf, "Base Color", (0.85, 0.92, 1.0, 1.0))
                        _set(bsdf, "Roughness", 0.0)
                        _set(bsdf, "Transmission Weight", 0.95)
                        _set(bsdf, "IOR", 1.5)

                # Glass pane — place as flat cube aligned to wall
                # For N/S walls: flat in Y. For E/W walls: flat in X.
                is_ns = "pos_y" in spec
                bpy.ops.mesh.primitive_cube_add(size=1, location=glass_loc)
                glass = bpy.context.active_object
                glass.name = f"window_{wname}_{seg_idx}"
                if is_ns:
                    glass.scale = (ow - 0.06, 0.005, oh - 0.06)
                else:
                    glass.scale = (0.005, ow - 0.06, oh - 0.06)
                glass.data.materials.append(glass_mat)

                # Window frame (brown wood border)
                frame_mat = make_material(f"mat_frame_{wname}", "#8B7355", 0.4, "paint",
                                          texture_cache)
                frame_t = 0.04  # frame thickness
                frame_d = 0.04  # frame depth

                # 4 frame pieces: top, bottom/sill, left, right
                for fname, fu, fv, fsx, fsz in [
                    ("top",   cu,      v_top,    ow + frame_t, frame_t),
                    ("sill",  cu,      v_bottom, ow + frame_t*2, frame_t),
                    ("left",  u_left,  cv,       frame_t,  oh + frame_t),
                    ("right", u_right, cv,       frame_t,  oh + frame_t),
                ]:
                    floc, _ = _loc(fu, fv, 0, 0)
                    bpy.ops.mesh.primitive_cube_add(size=1, location=floc)
                    fr = bpy.context.active_object
                    fr.name = f"frame_{wname}_{fname}"
                    if is_ns:
                        fr.scale = (fsx, frame_d, fsz)
                    else:
                        fr.scale = (frame_d, fsx, fsz)
                    fr.data.materials.append(frame_mat)

            elif otype == "door":
                door_mat = make_material(f"mat_door_{wname}", "#D2B48C", 0.5, "wood",
                                         texture_cache)
                bpy.ops.mesh.primitive_cube_add(size=1, location=glass_loc)
                door = bpy.context.active_object
                door.name = f"door_{wname}"
                is_ns = "pos_y" in spec
                if is_ns:
                    door.scale = (ow - 0.04, 0.04, oh - 0.02)
                else:
                    door.scale = (0.04, ow - 0.04, oh - 0.02)
                door.data.materials.append(door_mat)

            prev_right = u_right

        # Right remaining full-height segment
        if L - prev_right > 0.01:
            seg_w = L - prev_right
            cu = prev_right + seg_w / 2
            loc, sc = _loc(cu, H / 2, seg_w, H)
            add_plane(f"wall_{wname}_s{seg_idx}", loc, sc, rotation=rot,
                      color_hex=wall_color, roughness=wall_rough, mat_type=wall_mat,
                      texture_cache_dir=texture_cache, chord_dir=chord_dir, surface_name="wall")


def build_scene(data: dict, session_dir: str = ""):
    import bpy

    bpy.ops.wm.read_homefile(use_empty=True)

    room = data.get("room", {})
    W = float(room.get("width",  4.0))
    D = float(room.get("depth",  3.0))
    H = float(room.get("height", 2.5))

    walls_d   = data.get("walls",   {})
    floor_d   = data.get("floor",   {})
    ceiling_d = data.get("ceiling", {})

    # Cache directories (relative to cwd — backend sets this to project root)
    hdri_cache    = "sessions/hdri_cache"
    texture_cache = "sessions/texture_cache"
    # CHORD materials and GLB furniture directories (per-session)
    chord_materials_dir = str(Path(session_dir) / "materials") if session_dir else ""
    furniture_glb_dir   = str(Path(session_dir) / "furniture") if session_dir else ""
    Path(hdri_cache).mkdir(parents=True, exist_ok=True)
    Path(texture_cache).mkdir(parents=True, exist_ok=True)

    # Detect room style for HDRI selection
    room_style = data.get("style", "default")

    wall_color = walls_d.get("color", "#E8E8E8")
    wall_rough = float(walls_d.get("texture_roughness", 0.6))
    wall_mat   = MAT_TYPE_MAP.get(walls_d.get("material", "paint"), "paint")

    floor_color = floor_d.get("color", "#C8A882")
    floor_rough = float(floor_d.get("texture_roughness", 0.7))
    floor_mat   = MAT_TYPE_MAP.get(floor_d.get("material", "tile"), "tile")

    ceil_color = ceiling_d.get("color", "#F8F8F8")

    # ── Room surfaces (Phase 3: CHORD materials have priority) ──
    add_plane("floor", (0, 0, 0), (W, D, 1),
              color_hex=floor_color, roughness=floor_rough, mat_type=floor_mat,
              texture_cache_dir=texture_cache,
              chord_dir=chord_materials_dir, surface_name="floor")

    add_plane("ceiling", (0, 0, H), (W, D, 1), rotation=(math.pi, 0, 0),
              color_hex=ceil_color, roughness=0.85, mat_type="paint",
              texture_cache_dir=texture_cache,
              chord_dir=chord_materials_dir, surface_name="ceiling")

    _build_walls_with_openings(
        data.get("openings", []), W, D, H,
        wall_color, wall_rough, wall_mat,
        texture_cache, chord_materials_dir
    )

    # ── Baseboards (skirting) ──
    import bpy as _bpy
    baseboard_h = 0.06
    baseboard_d = 0.012
    bb_mat = make_material("mat_baseboard", "#F5F0E8", 0.4, "paint", texture_cache)
    for bb_name, bb_loc, bb_scale in [
        ("bb_north", (0,  D/2 - baseboard_d/2, baseboard_h/2), (W, baseboard_d, baseboard_h)),
        ("bb_south", (0, -D/2 + baseboard_d/2, baseboard_h/2), (W, baseboard_d, baseboard_h)),
        ("bb_east",  ( W/2 - baseboard_d/2, 0, baseboard_h/2), (baseboard_d, D, baseboard_h)),
        ("bb_west",  (-W/2 + baseboard_d/2, 0, baseboard_h/2), (baseboard_d, D, baseboard_h)),
    ]:
        _bpy.ops.mesh.primitive_cube_add(size=1, location=bb_loc)
        bb = _bpy.context.active_object
        bb.name = bb_name
        bb.scale = bb_scale
        bb.data.materials.append(bb_mat)

    # ── Furniture (Phase 4: GLB import has priority over primitives) ──
    for item in data.get("furniture", []):
        place_furniture(item, texture_cache, furniture_glb_dir=furniture_glb_dir,
                        room_w=W, room_d=D)

    # ── Architectural features (from room template) ──
    arch = data.get("architectural", {})
    if arch:
        _build_architectural_features(arch, W, D, H, texture_cache)

    # ── Camera ──
    camera_id = data.get("_camera_id", 0)
    _setup_camera(bpy, W, D, H, camera_id, furniture_data=data.get("furniture", []))

    # ── Lighting ──
    lighting = data.get("lighting", {})
    intensity = float(lighting.get("intensity", 2.0))
    lc = hex_to_linear(lighting.get("color", "#FFF8F0"))[:3]  # warm white

    # Ceiling light fixture (visible geometry)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.15, depth=0.03, location=(0, 0, H - 0.02))
    fixture = bpy.context.active_object
    fixture.name = "ceiling_light_fixture"
    fixture_mat = bpy.data.materials.new("mat_light_fixture")
    fixture_mat.use_nodes = True
    fn = fixture_mat.node_tree.nodes
    fn.clear()
    emit = fn.new("ShaderNodeEmission")
    emit.inputs["Color"].default_value = (*lc, 1.0)
    emit.inputs["Strength"].default_value = intensity * 5
    out = fn.new("ShaderNodeOutputMaterial")
    fixture_mat.node_tree.links.new(emit.outputs[0], out.inputs[0])
    fixture.data.materials.append(fixture_mat)

    # Main ceiling area light — smaller = harder shadows
    light_type = lighting.get("type", "ceiling_lamp")
    if light_type == "fluorescent":
        # Long rectangular fluorescent tube
        bpy.ops.object.light_add(type="AREA", location=(0, 0, H - 0.08))
        key = bpy.context.active_object
        key.data.energy = intensity * 150
        key.data.size = 0.12
        key.data.size_y = min(W, D) * 0.4
        key.data.color = lc
    else:
        bpy.ops.object.light_add(type="AREA", location=(0, 0, H - 0.08))
        key = bpy.context.active_object
        key.data.energy = intensity * 150
        key.data.size = min(3.0, min(W, D) * 0.4)
        key.data.color = lc
    key.rotation_euler = (0, 0, 0)

    # Subtle ambient fill (very low — keeps shadows visible)
    bpy.ops.object.light_add(type="AREA", location=(0, -D * 0.45, H * 0.5))
    fill = bpy.context.active_object
    fill.data.energy = intensity * 15
    fill.data.size = D * 0.6
    fill.rotation_euler = (math.pi / 2, 0, 0)

    # ── World / HDRI ──
    scene = bpy.context.scene
    if bpy.data.worlds:
        world = bpy.data.worlds[0]
    else:
        world = bpy.data.worlds.new("World")
    scene.world = world
    # Set flat dark sky as fallback, then try HDRI
    if hasattr(world, "use_nodes"):
        world.use_nodes = True
    if world.node_tree:
        wn = world.node_tree.nodes
        wn.clear()
        bg = wn.new("ShaderNodeBackground")
        out = wn.new("ShaderNodeOutputWorld")
        world.node_tree.links.new(bg.outputs["Background"], out.inputs["Surface"])
        bg.inputs["Color"].default_value    = (0.08, 0.09, 0.12, 1.0)
        bg.inputs["Strength"].default_value = 0.15
    setup_hdri_lighting(world, hdri_cache, room_style)

    # ── Render settings ──
    scene.render.engine              = "CYCLES"
    scene.cycles.samples             = 512
    scene.cycles.use_adaptive_sampling   = True
    scene.cycles.adaptive_threshold      = 0.01
    scene.cycles.adaptive_min_samples    = 64
    scene.cycles.use_denoising       = True
    scene.cycles.denoiser            = "OPENIMAGEDENOISE"
    scene.render.resolution_x        = 1280
    scene.render.resolution_y        = 720
    scene.render.image_settings.file_format     = "PNG"
    scene.render.image_settings.color_mode      = "RGBA"
    scene.render.image_settings.compression     = 15

    # Color management
    scene.view_settings.view_transform = "Filmic"
    scene.view_settings.look           = "None"
    scene.view_settings.exposure       = -0.5

    # ── Enable depth pass (for ControlNet pipeline) ──
    scene.view_layers[0].use_pass_z = True

    # ── Compositor: Blender 5.0 uses compositing_node_group instead of scene.node_tree ──
    _setup_compositor(scene)


def _setup_compositor(scene):
    """Set up compositor node group for Blender 5.0+.

    Blender 5.0 removed scene.node_tree and CompositorNodeComposite.
    Instead, use scene.compositing_node_group with a node group that has
    a GroupOutput node.  Falls back to the legacy API for Blender <5.0.
    """
    import bpy

    # ── Try Blender 5.0+ API (compositing_node_group) ──
    if hasattr(scene, "compositing_node_group"):
        try:
            ng = bpy.data.node_groups.new("ImageCompositor", "CompositorNodeTree")
            rl = ng.nodes.new("CompositorNodeRLayers")
            rl.location = (0, 0)
            grp_out = ng.nodes.new("NodeGroupOutput")
            grp_out.location = (400, 0)
            ng.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
            ng.links.new(rl.outputs["Image"], grp_out.inputs["Image"])
            scene.compositing_node_group = ng
            print("[SCENE] Compositor set up (Blender 5.0+ node group)")
            return
        except Exception as e:
            print(f"[SCENE] Blender 5.0 compositor failed: {e}")

    # ── Fallback: legacy Blender 4.x API ──
    try:
        scene.use_nodes = True
        tree = scene.node_tree
        if tree is None:
            return
        rl_node = comp_node = None
        for n in tree.nodes:
            if n.type == "R_LAYERS":
                rl_node = n
            elif n.type == "COMPOSITE":
                comp_node = n
        if rl_node and comp_node:
            tree.links.new(rl_node.outputs["Image"], comp_node.inputs["Image"])
            print("[SCENE] Compositor set up (legacy)")
    except (AttributeError, RuntimeError) as e:
        print(f"[SCENE] Compositor setup skipped: {e}")


def _render_depth(scene, depth_output: str):
    """Render depth map using a separate compositor pass (Blender 5.0+).

    Creates a depth compositor that routes Normalized Depth → CombineColor → GroupOutput,
    renders with minimal samples (depth is geometry-based, not shading-based),
    then restores the image compositor.
    """
    import bpy

    # Save the original compositor
    original_ng = scene.compositing_node_group if hasattr(scene, "compositing_node_group") else None

    # ── Try Blender 5.0+ approach ──
    if hasattr(scene, "compositing_node_group"):
        try:
            ng = bpy.data.node_groups.new("DepthCompositor", "CompositorNodeTree")
            rl = ng.nodes.new("CompositorNodeRLayers")
            norm = ng.nodes.new("CompositorNodeNormalize")
            combine = ng.nodes.new("CompositorNodeCombineColor")
            grp_out = ng.nodes.new("NodeGroupOutput")

            ng.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
            ng.links.new(rl.outputs["Depth"], norm.inputs["Value"])
            ng.links.new(norm.outputs["Value"], combine.inputs["Red"])
            ng.links.new(norm.outputs["Value"], combine.inputs["Green"])
            ng.links.new(norm.outputs["Value"], combine.inputs["Blue"])
            combine.inputs["Alpha"].default_value = 1.0
            ng.links.new(combine.outputs["Image"], grp_out.inputs["Image"])

            scene.compositing_node_group = ng

            # Depth doesn't need many samples — geometry-based
            orig_samples = scene.cycles.samples
            orig_denoise = scene.cycles.use_denoising
            scene.cycles.samples = 16
            scene.cycles.use_denoising = False

            scene.render.filepath = depth_output
            scene.render.image_settings.color_mode = "BW"
            bpy.ops.render.render(write_still=True)
            print(f"[DEPTH] Depth map saved to {depth_output}")

            # Restore
            scene.cycles.samples = orig_samples
            scene.cycles.use_denoising = orig_denoise
            scene.render.image_settings.color_mode = "RGBA"
            scene.compositing_node_group = original_ng

            # Clean up depth node group
            bpy.data.node_groups.remove(ng)
            return True
        except Exception as e:
            print(f"[DEPTH] Blender 5.0 depth render failed: {e}")
            # Restore on failure
            if hasattr(scene, "compositing_node_group"):
                scene.compositing_node_group = original_ng
            return False

    # ── Fallback: legacy Blender 4.x ──
    try:
        tree = scene.node_tree
        if tree is None:
            return False
        rl_node = None
        for n in tree.nodes:
            if n.type == "R_LAYERS":
                rl_node = n
                break
        if not rl_node:
            return False

        norm = tree.nodes.new("CompositorNodeNormalize")
        fout = tree.nodes.new("CompositorNodeOutputFile")
        fout.base_path = str(Path(depth_output).parent)
        fout.format.file_format = "PNG"
        fout.format.color_mode = "BW"
        fout.file_slots[0].path = "depth_"
        tree.links.new(rl_node.outputs["Depth"], norm.inputs["Value"])
        tree.links.new(norm.outputs["Value"], fout.inputs[0])

        bpy.ops.render.render(write_still=False)  # re-composite only

        # Rename depth file
        depth_dir = Path(depth_output).parent
        candidates = sorted(depth_dir.glob("depth_*.png"))
        if candidates:
            candidates[-1].rename(depth_output)
            print(f"[DEPTH] Depth map saved to {depth_output}")
            return True
    except (AttributeError, RuntimeError) as e:
        print(f"[DEPTH] Legacy depth output failed: {e}")
    return False


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []

    parser = argparse.ArgumentParser()
    parser.add_argument("--input",  required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--depth-output", default="")
    parser.add_argument("--session-dir", default="")
    parser.add_argument("--camera", type=int, default=0,
                        help="Camera preset index (0=front, 1=corner_left, 2=corner_right, 3=side)")
    args = parser.parse_args(argv)

    with open(args.input) as f:
        data = json.load(f)

    # Inject camera selection into data so build_scene picks it up
    data["_camera_id"] = args.camera

    # Pass session dir so build_scene can find CHORD materials and GLB furniture
    session_dir = args.session_dir or str(Path(args.input).parent)
    build_scene(data, session_dir=session_dir)

    import bpy
    # ── Enable GPU after read_homefile (which resets prefs) ──
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for device_type in ("CUDA", "OPTIX", "HIP", "METAL"):
        try:
            prefs.compute_device_type = device_type
            prefs.refresh_devices()
            gpus = [d for d in prefs.devices if d.type != "CPU"]
            if gpus:
                for d in prefs.devices:
                    d.use = True
                bpy.context.scene.cycles.device = "GPU"
                print(f"GPU: {device_type} — {[d.name for d in gpus]}")
                break
        except Exception:
            continue

    # ── Render main image ──
    bpy.context.scene.render.filepath = args.output
    bpy.ops.render.render(write_still=True)
    print(f"Rendered to {args.output}")

    # ── Render depth map (separate compositor pass, fast) ──
    if args.depth_output:
        _render_depth(bpy.context.scene, args.depth_output)


if __name__ == "__main__":
    main()
