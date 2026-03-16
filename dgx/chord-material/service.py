"""
CHORD PBR material extraction service on DGX Spark.
Takes a photo of a surface and generates 4 PBR maps:
- Color (albedo without shadows)
- Normal (surface bumps/grooves)
- Roughness (smooth vs rough areas)
- Metallic (metallic vs dielectric)

Model: Ubisoft/ubisoft-laforge-chord (SIGGRAPH Asia 2025)
License: Ubisoft Machine Learning License (research-only)
VRAM: ~8-12 GB
"""

import asyncio
import os
import io
import sys
import base64
import torch
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image

app = FastAPI(title="CHORD Material Service")

CHORD_REPO = os.environ.get("CHORD_REPO", "/app/chord_repo")
HF_MODEL_ID = "Ubisoft/ubisoft-laforge-chord"

_pipeline = None


def get_pipeline():
    """Load CHORD pipeline from cloned repo."""
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    # Run CHORD via subprocess (test.py) — most reliable, uses exact author's code
    try:
        test_script = Path(CHORD_REPO) / "test.py"
        if test_script.exists():
            _pipeline = _SubprocessChordPipeline(CHORD_REPO)
            print("[CHORD] Using subprocess pipeline")
            return _pipeline
    except Exception as e:
        print(f"[CHORD] Subprocess pipeline failed: {e}")

    # Final fallback
    print("[CHORD] Using fallback pipeline (returns input as color map)")
    _pipeline = _FallbackChordPipeline()
    return _pipeline


class _RealChordPipeline:
    """Wraps real CHORD model."""
    def __init__(self, model, config=None):
        self.model = model
        self.config = config

    def __call__(self, image: Image.Image) -> dict:
        import torch
        from torchvision.transforms import v2
        from chord.io import read_image as _unused  # verify import works

        # Match CHORD's test.py: convert PIL → tensor, resize to 1024x1024
        transform = v2.Compose([
            v2.ToImage(),
            v2.ToDtype(torch.float32, scale=True),
            v2.Resize((1024, 1024), antialias=True),
        ])
        img_tensor = transform(image).unsqueeze(0).to("cuda")

        # Run with autocast like test.py does
        with torch.no_grad(), torch.autocast(device_type="cuda"):
            result = self.model(img_tensor)

        # Convert output tensors to PIL images
        maps = {}
        for key in result:
            t = result[key][0].float().clamp(0, 1).cpu()
            if t.shape[0] == 1:
                arr = (t[0].numpy() * 255).astype("uint8")
                pil = Image.fromarray(arr, mode="L")
            else:
                arr = (t.permute(1, 2, 0).numpy() * 255).astype("uint8")
                pil = Image.fromarray(arr, mode="RGB")
            # Map CHORD output keys to our standard names
            if "basecolor" in key or "albedo" in key or "diffuse" in key:
                maps["color"] = pil
            elif "normal" in key:
                maps["normal"] = pil
            elif "rough" in key:
                maps["roughness"] = pil
            elif "metal" in key:
                maps["metallic"] = pil

        maps.setdefault("color", image)
        maps.setdefault("normal", Image.new("RGB", image.size, (128, 128, 255)))
        maps.setdefault("roughness", Image.new("L", image.size, 128))
        maps.setdefault("metallic", Image.new("L", image.size, 0))
        return maps


class _SubprocessChordPipeline:
    """Runs CHORD via subprocess (test.py script)."""
    def __init__(self, repo_path):
        self.repo_path = Path(repo_path)

    def __call__(self, image: Image.Image) -> dict:
        import subprocess, tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "input"
            output_dir = Path(tmpdir) / "output"
            input_dir.mkdir()
            output_dir.mkdir()

            input_path = input_dir / "surface.png"
            image.save(str(input_path))

            env = {**os.environ}
            result = subprocess.run(
                [sys.executable, str(self.repo_path / "test.py"),
                 "--input-dir", str(input_dir),
                 "--output-dir", str(output_dir),
                 "--config-path", str(self.repo_path / "config" / "chord.yaml")],
                capture_output=True, text=True, cwd=str(self.repo_path),
                timeout=300, env=env,
            )

            if result.returncode != 0:
                raise RuntimeError(f"CHORD test.py failed: {result.stderr[-300:]}")

            maps = {}
            # CHORD saves to: output_dir/{image_stem}/basecolor.png etc.
            subdirs = [d for d in output_dir.iterdir() if d.is_dir()]
            search_dir = subdirs[0] if subdirs else output_dir
            for name, suffix in [("color", "basecolor"), ("normal", "normal"),
                                  ("roughness", "roughness"), ("metallic", "metalness")]:
                p = search_dir / f"{suffix}.png"
                if p.exists():
                    maps[name] = Image.open(str(p))

            if "color" not in maps:
                maps["color"] = image

            maps.setdefault("normal", Image.new("RGB", image.size, (128, 128, 255)))
            maps.setdefault("roughness", Image.new("L", image.size, 128))
            maps.setdefault("metallic", Image.new("L", image.size, 0))
            return maps


class _FallbackChordPipeline:
    """Fallback when CHORD model is not available."""
    def __call__(self, image: Image.Image) -> dict:
        w, h = image.size
        return {
            "color": image.copy(),
            "normal": Image.new("RGB", (w, h), (128, 128, 255)),
            "roughness": Image.new("L", (w, h), 128),
            "metallic": Image.new("L", (w, h), 0),
        }


class MaterialRequest(BaseModel):
    image_b64: str
    tile_size: int = 512


def _to_b64(image: Image.Image, mode: str = "RGB") -> str:
    buf = io.BytesIO()
    image.convert(mode).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def _run_in_thread(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


_gpu_sem = asyncio.Semaphore(2)


@app.get("/health")
async def health():
    return {"status": "ok", "model": HF_MODEL_ID}


@app.post("/extract")
async def extract_material(req: MaterialRequest):
    if _gpu_sem.locked():
        return JSONResponse({"error": "GPU busy, retry later"}, status_code=503)
    async with _gpu_sem:
        pipe = get_pipeline()
        image_bytes = base64.b64decode(req.image_b64)
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        image = image.resize((req.tile_size, req.tile_size))

        result = await _run_in_thread(pipe, image)

        return JSONResponse({
            "color_b64": _to_b64(result["color"]),
            "normal_b64": _to_b64(result["normal"]),
            "roughness_b64": _to_b64(result["roughness"], "L"),
            "metallic_b64": _to_b64(result["metallic"], "L"),
        })
