"""
TRELLIS.2 3D model generation service on DGX Spark.
Takes a photo of a furniture item and generates a 3D model (GLB format).

Model: microsoft/TRELLIS.2-4B (MIT license)
VRAM: ~16-24 GB
Time: 3-17s depending on resolution
"""

import asyncio
import os
import io
import sys
import base64
import struct
import tempfile
import torch
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from PIL import Image

app = FastAPI(title="TRELLIS.2 3D Service")

TRELLIS_REPO = os.environ.get("TRELLIS_REPO", "/app/trellis_repo")
HF_MODEL_ID = os.environ.get("TRELLIS_MODEL", "microsoft/TRELLIS.2-4B")

_pipeline = None


class _NoOpRembg:
    """Passthrough background removal — adds opaque alpha, no actual removal."""
    def __call__(self, img):
        if isinstance(img, Image.Image):
            return img.convert("RGBA")
        return img

    def __getattr__(self, name):
        """Handle .to(), .cpu(), .cuda(), .eval(), etc. — return self for chaining."""
        return lambda *a, **kw: self


def get_pipeline():
    """Load TRELLIS.2 pipeline."""
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    # Try real TRELLIS.2 pipeline — manual loading to work around config issues
    try:
        if TRELLIS_REPO not in sys.path:
            sys.path.insert(0, TRELLIS_REPO)

        # Patch torch.linspace for RMBG meta tensor issue
        _orig_linspace = torch.linspace
        def _patched_linspace(*args, **kwargs):
            kwargs.pop("device", None)
            return _orig_linspace(*args, **kwargs, device="cpu")
        torch.linspace = _patched_linspace

        # Patch transformers for BiRefNet compatibility (missing all_tied_weights_keys)
        _patched_ptm = False
        try:
            from transformers import PreTrainedModel
            if not hasattr(PreTrainedModel, 'all_tied_weights_keys'):
                def _get_tied(self):
                    return getattr(self, '_all_tied_weights_keys_cache',
                        self.get_expanded_tied_weights_keys()
                        if hasattr(self, 'get_expanded_tied_weights_keys') else {})
                def _set_tied(self, value):
                    self._all_tied_weights_keys_cache = value
                PreTrainedModel.all_tied_weights_keys = property(_get_tied, _set_tied)
                _patched_ptm = True
                print("[TRELLIS] Patched PreTrainedModel.all_tied_weights_keys", flush=True)
        except Exception:
            pass

        try:
            from huggingface_hub import hf_hub_download
            import json

            # Use local config file (HF cache gets corrupted by v1 repo download)
            local_config = Path("/app/pipeline_v2.json")
            if local_config.exists():
                with open(local_config) as f:
                    args = json.load(f)['args']
            else:
                cf = hf_hub_download(HF_MODEL_ID, "pipeline.json", force_download=True)
                with open(cf) as f:
                    args = json.load(f)['args']
            print(f"[TRELLIS] Config keys: {list(args.keys())}", flush=True)

            from trellis2.pipelines import Trellis2ImageTo3DPipeline, samplers, rembg
            from trellis2 import models as tmodels
            from trellis2.modules import image_feature_extractor

            print(f"[TRELLIS] Loading {len(args['models'])} models...", flush=True)

            # Load all sub-models
            model_names_to_load = Trellis2ImageTo3DPipeline.model_names_to_load
            _models = {}
            for k, v in args['models'].items():
                if k not in model_names_to_load:
                    continue
                last_err = None
                for attempt in [f"{HF_MODEL_ID}/{v}", v]:
                    try:
                        _models[k] = tmodels.from_pretrained(attempt)
                        print(f"  {k}: OK (via {attempt})", flush=True)
                        break
                    except Exception as me:
                        last_err = me
                        continue
                else:
                    print(f"  {k}: FAILED — {last_err}", flush=True)

            loaded = set(_models.keys())
            missing = set(model_names_to_load) - loaded
            print(f"[TRELLIS] Loaded {len(loaded)}/{len(model_names_to_load)} models", flush=True)
            if missing:
                print(f"[TRELLIS] Missing: {missing}", flush=True)

            # Construct pipeline manually
            pipe = Trellis2ImageTo3DPipeline(_models)

            # Set up samplers
            pipe.sparse_structure_sampler = getattr(samplers, args['sparse_structure_sampler']['name'])(**args['sparse_structure_sampler']['args'])
            pipe.sparse_structure_sampler_params = args['sparse_structure_sampler']['params']
            pipe.shape_slat_sampler = getattr(samplers, args['shape_slat_sampler']['name'])(**args['shape_slat_sampler']['args'])
            pipe.shape_slat_sampler_params = args['shape_slat_sampler']['params']
            pipe.tex_slat_sampler = getattr(samplers, args['tex_slat_sampler']['name'])(**args['tex_slat_sampler']['args'])
            pipe.tex_slat_sampler_params = args['tex_slat_sampler']['params']
            pipe.shape_slat_normalization = args['shape_slat_normalization']
            pipe.tex_slat_normalization = args['tex_slat_normalization']

            # Image conditioning model
            pipe.image_cond_model = getattr(image_feature_extractor, args['image_cond_model']['name'])(**args['image_cond_model']['args'])

            # Background removal model (may fail with transformers version mismatch)
            try:
                pipe.rembg_model = getattr(rembg, args['rembg_model']['name'])(**args['rembg_model']['args'])
            except Exception as e:
                print(f"[TRELLIS] RMBG init failed ({e}), using passthrough", flush=True)
                pipe.rembg_model = _NoOpRembg()

            pipe.default_pipeline_type = args.get('default_pipeline_type', '1024_cascade')
            pipe.cuda()
        finally:
            torch.linspace = _orig_linspace
        _pipeline = _RealTrellisPipeline(pipe)
        print(f"[TRELLIS] Real model loaded: {HF_MODEL_ID}")
        return _pipeline

    except Exception as e:
        print(f"[TRELLIS] Real pipeline import failed: {e}", flush=True)
        import traceback; traceback.print_exc()
        # Reset pipeline to None so fallback is used
        _pipeline = None

    # Try older TRELLIS v1
    try:
        if TRELLIS_REPO not in sys.path:
            sys.path.insert(0, TRELLIS_REPO)

        from trellis.pipelines import TrellisImageTo3DPipeline

        pipe = TrellisImageTo3DPipeline.from_pretrained("microsoft/TRELLIS-image-large")
        pipe.cuda()
        _pipeline = _RealTrellisPipeline(pipe)
        print("[TRELLIS] v1 model loaded")
        return _pipeline

    except Exception as e:
        print(f"[TRELLIS] v1 pipeline also failed: {e}")

    # Fallback
    print("[TRELLIS] Using fallback pipeline (minimal GLB)")
    _pipeline = _FallbackTrellisPipeline()
    return _pipeline


class _RealTrellisPipeline:
    """Wraps real TRELLIS pipeline."""
    def __init__(self, pipe):
        self.pipe = pipe

    def __call__(self, image: Image.Image, resolution: int = 512, steps: int = 12) -> bytes:
        import numpy as np

        pipeline_type = '512' if resolution <= 512 else '1024_cascade'
        sampler_params = {"steps": steps} if steps != 12 else {}
        with torch.no_grad():
            results = self.pipe.run(
                image, seed=42, pipeline_type=pipeline_type,
                sparse_structure_sampler_params=sampler_params,
                shape_slat_sampler_params=sampler_params,
                tex_slat_sampler_params=sampler_params,
            )

        # results is List[MeshWithVoxel]
        mesh_obj = results[0]
        verts = mesh_obj.vertices.cpu().numpy()
        faces = mesh_obj.faces.cpu().numpy()

        # Query vertex colors from voxel attributes
        try:
            attrs = mesh_obj.query_vertex_attrs().cpu().numpy()
            # attrs layout: check pbr_attr_layout for channel mapping
            if attrs.shape[-1] >= 3:
                colors = np.clip(attrs[:, :3], 0, 1)
                colors_u8 = (colors * 255).astype(np.uint8)
            else:
                colors_u8 = None
        except Exception:
            colors_u8 = None

        import trimesh
        if colors_u8 is not None:
            alpha = np.full((colors_u8.shape[0], 1), 255, dtype=np.uint8)
            vc = np.hstack([colors_u8, alpha])
            mesh = trimesh.Trimesh(vertices=verts, faces=faces, vertex_colors=vc)
        else:
            mesh = trimesh.Trimesh(vertices=verts, faces=faces)

        # Decimate if too many faces (target ~100k for reasonable GLB size)
        MAX_FACES = 100_000
        n_faces = len(mesh.faces)
        if n_faces > MAX_FACES:
            try:
                ratio = 1.0 - (MAX_FACES / n_faces)  # reduction ratio for fast-simplification
                mesh = mesh.simplify_quadric_decimation(ratio)
                print(f"[TRELLIS] Decimated {n_faces} → {len(mesh.faces)} faces", flush=True)
            except Exception as e:
                print(f"[TRELLIS] Decimation failed ({e}), using full mesh ({n_faces} faces)", flush=True)

        return mesh.export(file_type='glb')


class _FallbackTrellisPipeline:
    """Fallback — returns minimal valid GLB."""
    def __call__(self, image: Image.Image, resolution: int = 512) -> bytes:
        json_str = b'{"asset":{"version":"2.0","generator":"trellis-fallback"},"scene":0,"scenes":[{"nodes":[]}]}'
        json_padded = json_str + b' ' * ((4 - len(json_str) % 4) % 4)
        header = struct.pack('<III', 0x46546C67, 2, 12 + 8 + len(json_padded))
        chunk = struct.pack('<II', len(json_padded), 0x4E4F534A) + json_padded
        return header + chunk


class Generate3DRequest(BaseModel):
    image_b64: str
    resolution: int = 512
    steps: int = 12  # diffusion steps per sampler (default 12, min 4 for speed)


async def _run_in_thread(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


_gpu_sem = asyncio.Semaphore(1)


@app.get("/health")
async def health():
    return {"status": "ok", "model": HF_MODEL_ID}


@app.post("/generate")
async def generate_3d(req: Generate3DRequest):
    if _gpu_sem.locked():
        return JSONResponse({"error": "GPU busy, retry later"}, status_code=503)
    async with _gpu_sem:
        pipe = get_pipeline()
        image_bytes = base64.b64decode(req.image_b64)
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        content = await _run_in_thread(pipe, image, req.resolution, req.steps)

        return Response(content=content, media_type="model/gltf-binary")
