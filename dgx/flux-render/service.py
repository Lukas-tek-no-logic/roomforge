"""
Flux image generation service on DGX Spark.
Provides endpoints for text-to-image, image-to-image, depth-controlled render,
inpainting, and quick preview.

Models:
- Chroma (Apache 2.0 fork of FLUX.1-schnell) — supports negative prompts
- FLUX.1-Depth-V3 ControlNet — structure-aware depth conditioning
- FLUX.2 [klein] 4B — fast preview generation
"""

import asyncio
import os
import base64
import torch
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from PIL import Image

app = FastAPI(title="Flux Render Service")


async def _run_in_thread(fn, *args, **kwargs):
    """Run a blocking function in a thread to avoid blocking the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

MODEL_ID = os.environ.get("FLUX_MODEL", "black-forest-labs/FLUX.1-schnell")
DEPTH_CONTROLNET_ID = os.environ.get("FLUX_DEPTH_MODEL", "black-forest-labs/FLUX.1-Depth-V3")
PREVIEW_MODEL_ID = os.environ.get("FLUX_PREVIEW_MODEL", "black-forest-labs/FLUX.2-klein")
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")

# -- Pipelines (lazy load, shared components) --------------------------------
#
# Memory optimization: load ONE base FluxPipeline via from_pretrained(),
# then derive img2img / inpaint / controlnet via from_pipe() which shares
# the transformer (~23 GB) and T5 encoder (~10 GB) at zero extra cost.
# Preview (FLUX.2-klein) is a different model and stays separate.
#
# Before: ~102 GB (4 x base model + klein)
# After:  ~38 GB  (1 x base model + klein)

_base_pipe = None
_txt2img_pipe = None
_img2img_pipe = None
_controlled_pipe = None
_controlnet_model = None
_inpaint_pipe = None
_preview_pipe = None


def _load_base_pipe():
    """Load the single base FluxPipeline that all other pipelines derive from."""
    global _base_pipe
    if _base_pipe is not None:
        return _base_pipe
    from diffusers import FluxPipeline
    print(f"[flux] Loading base FluxPipeline from {MODEL_ID}", flush=True)
    try:
        _base_pipe = FluxPipeline.from_pretrained(
            MODEL_ID, torch_dtype=torch.bfloat16, token=HF_TOKEN,
        ).to("cuda")
    except (ValueError, KeyError):
        # Chroma and some forks lack optional components -- load with ignore
        _base_pipe = FluxPipeline.from_pretrained(
            MODEL_ID, torch_dtype=torch.bfloat16, token=HF_TOKEN,
            text_encoder_2=None, tokenizer_2=None,
            image_encoder=None, feature_extractor=None,
        ).to("cuda")
    return _base_pipe


def get_txt2img_pipe():
    global _txt2img_pipe
    if _txt2img_pipe is None:
        # The base pipe IS a FluxPipeline (txt2img) -- reuse directly
        _txt2img_pipe = _load_base_pipe()
        # GB10 has 128GB unified memory -- keep on GPU, no offload needed
    return _txt2img_pipe


def get_img2img_pipe():
    global _img2img_pipe
    if _img2img_pipe is None:
        from diffusers import FluxImg2ImgPipeline
        base = _load_base_pipe()
        print("[flux] Creating FluxImg2ImgPipeline from_pipe (shared weights)", flush=True)
        _img2img_pipe = FluxImg2ImgPipeline.from_pipe(base)
        # GB10 has 128GB unified memory -- keep on GPU
    return _img2img_pipe


def get_controlled_pipe():
    """ControlNet depth-conditioned pipeline for structure-aware finalization."""
    global _controlled_pipe, _controlnet_model
    if _controlled_pipe is None:
        from diffusers import FluxControlNetModel, FluxControlNetImg2ImgPipeline
        base = _load_base_pipe()
        print(f"[flux] Loading ControlNet model from {DEPTH_CONTROLNET_ID}", flush=True)
        _controlnet_model = FluxControlNetModel.from_pretrained(
            DEPTH_CONTROLNET_ID, torch_dtype=torch.bfloat16, token=HF_TOKEN,
        ).to("cuda")
        print("[flux] Creating FluxControlNetImg2ImgPipeline from_pipe (shared weights)", flush=True)
        _controlled_pipe = FluxControlNetImg2ImgPipeline.from_pipe(
            base, controlnet=_controlnet_model,
        )
        # GB10 has 128GB unified memory -- keep on GPU
    return _controlled_pipe


def get_inpaint_pipe():
    """Inpainting pipeline for selective region regeneration."""
    global _inpaint_pipe
    if _inpaint_pipe is None:
        from diffusers import FluxInpaintPipeline
        base = _load_base_pipe()
        print("[flux] Creating FluxInpaintPipeline from_pipe (shared weights)", flush=True)
        _inpaint_pipe = FluxInpaintPipeline.from_pipe(base)
        # GB10 has 128GB unified memory -- keep on GPU
    return _inpaint_pipe


def get_preview_pipe():
    """Lightweight fast-preview pipeline. Falls back to img2img if no preview model."""
    global _preview_pipe
    if _preview_pipe is None:
        if not PREVIEW_MODEL_ID:
            # No preview model configured -- use img2img pipe as fallback
            return get_img2img_pipe()
        from diffusers import FluxImg2ImgPipeline
        # Preview is a DIFFERENT model (FLUX.2-klein) -- must load separately
        print(f"[flux] Loading preview FluxImg2ImgPipeline from {PREVIEW_MODEL_ID}", flush=True)
        try:
            _preview_pipe = FluxImg2ImgPipeline.from_pretrained(
                PREVIEW_MODEL_ID, torch_dtype=torch.bfloat16, token=HF_TOKEN,
            ).to("cuda")
        except (ValueError, KeyError):
            _preview_pipe = FluxImg2ImgPipeline.from_pretrained(
                PREVIEW_MODEL_ID, torch_dtype=torch.bfloat16, token=HF_TOKEN,
                text_encoder_2=None, tokenizer_2=None,
                image_encoder=None, feature_extractor=None,
            ).to("cuda")
        # GB10 has 128GB unified memory -- keep on GPU
    return _preview_pipe


# -- Request models -----------------------------------------------------------

class GenerateRequest(BaseModel):
    prompt: str
    negative_prompt: str = ""
    width: int = 1280
    height: int = 720
    steps: int = 4
    guidance: float = 0.0
    seed: int = -1


class Img2ImgRequest(BaseModel):
    image_b64: str
    prompt: str
    negative_prompt: str = ""
    strength: float = 0.65
    steps: int = 20
    guidance: float = 3.5
    seed: int = -1


class ControlledRenderRequest(BaseModel):
    image_b64: str      # Blender render (base64 PNG)
    depth_b64: str      # Depth map (base64 PNG, grayscale)
    prompt: str
    negative_prompt: str = ""
    strength: float = 0.90
    controlnet_conditioning_scale: float = 0.6
    steps: int = 20
    guidance: float = 3.5
    seed: int = -1


class InpaintRequest(BaseModel):
    image_b64: str      # Current render (base64 PNG)
    mask_b64: str       # Mask: white=change, black=keep (base64 PNG)
    prompt: str
    negative_prompt: str = ""
    strength: float = 0.85
    steps: int = 20
    guidance: float = 3.5
    seed: int = -1


class QuickPreviewRequest(BaseModel):
    image_b64: str      # Blender render (base64 PNG)
    depth_b64: str = "" # Optional depth map
    prompt: str
    negative_prompt: str = ""
    steps: int = 4
    guidance: float = 0.0
    seed: int = -1


# -- Helper -------------------------------------------------------------------

def _decode_image(b64: str, size: tuple[int, int] = (1280, 720)) -> Image.Image:
    image_bytes = base64.b64decode(b64)
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    return img.resize(size)


def _to_png_response(image: Image.Image) -> Response:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


# -- Concurrency guard (prevent OOM from parallel GPU requests) ---------------

_gpu_sem = asyncio.Semaphore(1)

# -- Endpoints ----------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_ID, "features": [
        "negative_prompt", "controlnet_depth", "inpaint", "quick_preview"
    ]}


def _run_pipe(pipe, kwargs):
    """Run pipeline synchronously -- called from thread."""
    neg = kwargs.pop("_negative_prompt", "")
    if neg:
        try:
            kwargs["negative_prompt"] = neg
            return pipe(**kwargs).images[0]
        except TypeError:
            kwargs.pop("negative_prompt", None)
    return pipe(**kwargs).images[0]


@app.post("/generate")
async def generate(req: GenerateRequest):
    if _gpu_sem.locked():
        return JSONResponse({"error": "GPU busy, retry later"}, status_code=503)
    async with _gpu_sem:
        pipe = get_txt2img_pipe()
        generator = torch.Generator("cuda").manual_seed(req.seed) if req.seed >= 0 else None

        kwargs = dict(
            prompt=req.prompt,
            width=req.width,
            height=req.height,
            num_inference_steps=req.steps,
            guidance_scale=req.guidance,
            generator=generator,
            _negative_prompt=req.negative_prompt,
        )
        result = await _run_in_thread(_run_pipe, pipe, kwargs)
        return _to_png_response(result)


@app.post("/img2img")
async def img2img(req: Img2ImgRequest):
    if _gpu_sem.locked():
        return JSONResponse({"error": "GPU busy, retry later"}, status_code=503)
    async with _gpu_sem:
        pipe = get_img2img_pipe()
        init_image = _decode_image(req.image_b64)
        generator = torch.Generator("cuda").manual_seed(req.seed) if req.seed >= 0 else None

        kwargs = dict(
            prompt=req.prompt,
            image=init_image,
            strength=req.strength,
            num_inference_steps=req.steps,
            guidance_scale=req.guidance,
            generator=generator,
            _negative_prompt=req.negative_prompt,
        )
        result = await _run_in_thread(_run_pipe, pipe, kwargs)
        return _to_png_response(result)


@app.post("/controlled-render")
async def controlled_render(req: ControlledRenderRequest):
    """Phase 1: Depth-conditioned img2img -- preserves room layout while maximizing photorealism."""
    if _gpu_sem.locked():
        return JSONResponse({"error": "GPU busy, retry later"}, status_code=503)
    async with _gpu_sem:
        pipe = get_controlled_pipe()
        init_image = _decode_image(req.image_b64)
        depth_image = _decode_image(req.depth_b64)
        generator = torch.Generator("cuda").manual_seed(req.seed) if req.seed >= 0 else None

        kwargs = dict(
            prompt=req.prompt,
            image=init_image,
            control_image=depth_image,
            controlnet_conditioning_scale=req.controlnet_conditioning_scale,
            strength=req.strength,
            num_inference_steps=req.steps,
            guidance_scale=req.guidance,
            generator=generator,
            _negative_prompt=req.negative_prompt,
        )
        result = await _run_in_thread(_run_pipe, pipe, kwargs)
        return _to_png_response(result)


@app.post("/inpaint")
async def inpaint(req: InpaintRequest):
    """Phase 2: Selective region inpainting -- regenerate only masked area."""
    if _gpu_sem.locked():
        return JSONResponse({"error": "GPU busy, retry later"}, status_code=503)
    async with _gpu_sem:
        pipe = get_inpaint_pipe()
        init_image = _decode_image(req.image_b64)
        mask_image = _decode_image(req.mask_b64)
        generator = torch.Generator("cuda").manual_seed(req.seed) if req.seed >= 0 else None

        kwargs = dict(
            prompt=req.prompt,
            image=init_image,
            mask_image=mask_image,
            strength=req.strength,
            num_inference_steps=req.steps,
            guidance_scale=req.guidance,
            generator=generator,
            _negative_prompt=req.negative_prompt,
        )
        result = await _run_in_thread(_run_pipe, pipe, kwargs)
        return _to_png_response(result)


@app.post("/quick-preview")
async def quick_preview(req: QuickPreviewRequest):
    """Phase 6: Fast photorealistic preview using lightweight model (~1-2s)."""
    if _gpu_sem.locked():
        return JSONResponse({"error": "GPU busy, retry later"}, status_code=503)
    async with _gpu_sem:
        pipe = get_preview_pipe()
        init_image = _decode_image(req.image_b64)
        generator = torch.Generator("cuda").manual_seed(req.seed) if req.seed >= 0 else None

        # Phase 6: Preview at 256x256 for speed (~5s on GB10)
        preview_size = (256, 256)
        init_image = init_image.resize(preview_size)

        kwargs = dict(
            prompt=req.prompt,
            image=init_image,
            strength=0.55,
            num_inference_steps=req.steps,
            guidance_scale=req.guidance,
            generator=generator,
            _negative_prompt=req.negative_prompt,
        )
        result = await _run_in_thread(_run_pipe, pipe, kwargs)
        # Upscale back to display size
        result = result.resize((1280, 720), Image.LANCZOS)
        return _to_png_response(result)
