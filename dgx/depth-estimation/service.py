"""
Depth estimation service on DGX Spark.
Uses Depth Anything V2 Large to produce per-pixel depth maps from single images.

Output: uint16 PNG depth map (0=nearest, 65535=farthest) + min/max values.
VRAM: ~2GB fp16. Inference: ~0.3-0.5s per image.
"""

import asyncio
import base64
import io
import os

import numpy as np
import torch
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image

app = FastAPI(title="Depth Estimation Service")

MODEL_ID = os.environ.get("DEPTH_MODEL", "depth-anything/Depth-Anything-V2-Large-hf")
_model = None
_processor = None
_lock = asyncio.Semaphore(2)


def _load_model():
    global _model, _processor
    if _model is None:
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation
        print(f"[depth] Loading {MODEL_ID}...", flush=True)
        _processor = AutoImageProcessor.from_pretrained(MODEL_ID)
        _model = AutoModelForDepthEstimation.from_pretrained(MODEL_ID).to("cuda").half()
        _model.eval()
        print(f"[depth] Model loaded", flush=True)
    return _model, _processor


async def _run_in_thread(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


class EstimateRequest(BaseModel):
    image_b64: str


@app.get("/health")
async def health():
    loaded = _model is not None
    return {
        "status": "ok",
        "model": MODEL_ID,
        "loaded": loaded,
    }


def _estimate_depth(image_b64: str) -> dict:
    """Run depth estimation. Returns depth as uint16 PNG + metadata."""
    model, processor = _load_model()

    # Decode image
    img_bytes = base64.b64decode(image_b64)
    image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    orig_w, orig_h = image.size

    # Preprocess
    inputs = processor(images=image, return_tensors="pt").to("cuda")
    # Convert pixel_values to fp16 to match model
    if "pixel_values" in inputs:
        inputs["pixel_values"] = inputs["pixel_values"].half()

    # Inference
    with torch.no_grad():
        outputs = model(**inputs)
        predicted_depth = outputs.predicted_depth

    # Interpolate to original size
    depth = torch.nn.functional.interpolate(
        predicted_depth.unsqueeze(1).float(),
        size=(orig_h, orig_w),
        mode="bicubic",
        align_corners=False,
    ).squeeze().cpu().numpy()

    # Normalize to 0..1 range
    min_val = float(depth.min())
    max_val = float(depth.max())
    if max_val - min_val > 1e-6:
        depth_norm = (depth - min_val) / (max_val - min_val)
    else:
        depth_norm = np.zeros_like(depth)

    # Encode as uint16 PNG (0=nearest, 65535=farthest for DA2 inverse depth)
    depth_uint16 = (depth_norm * 65535).astype(np.uint16)
    depth_img = Image.fromarray(depth_uint16, mode="I;16")

    buf = io.BytesIO()
    depth_img.save(buf, format="PNG")
    depth_b64 = base64.b64encode(buf.getvalue()).decode()

    return {
        "depth_b64": depth_b64,
        "width": orig_w,
        "height": orig_h,
        "min_depth": min_val,
        "max_depth": max_val,
    }


@app.post("/estimate")
async def estimate(req: EstimateRequest):
    if _lock.locked():
        return JSONResponse({"error": "GPU busy, retry later"}, status_code=503)
    async with _lock:
        try:
            result = await _run_in_thread(_estimate_depth, req.image_b64)
            return JSONResponse(result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, status_code=500)
