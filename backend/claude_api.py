"""Shared Anthropic API client for room analysis and modification.

Uses the Anthropic SDK when ANTHROPIC_API_KEY is set.
Falls back to `claude -p` CLI (which uses OAuth) when no API key is available.
"""
import base64
import json
import os
import re
import subprocess
import tempfile

_client = None
_use_sdk = None
MODEL = "claude-sonnet-4-6"


def _sdk_available() -> bool:
    global _use_sdk
    if _use_sdk is None:
        _use_sdk = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return _use_sdk


def get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


# ── CLI fallback ──────────────────────────────────────────────────────────────

def _run_cli(prompt: str, tools: list[str] | None = None) -> str:
    """Call `claude -p` via subprocess (uses OAuth, no API key needed)."""
    cmd = ["claude", "-p", "--output-format", "text"]
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        input=prompt, timeout=300,
        env={**os.environ, "CLAUDECODE": ""},
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI error: {result.stderr[:300]}")
    return result.stdout.strip()


# ── Public API ────────────────────────────────────────────────────────────────

def ask_text(prompt: str, max_tokens: int = 8192) -> str:
    """Send a text-only prompt, return the text response."""
    if _sdk_available():
        resp = get_client().messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text
    return _run_cli(prompt)


def ask_with_image(prompt: str, image_path: str, max_tokens: int = 8192) -> str:
    """Send a prompt with an image file, return the text response."""
    if _sdk_available():
        with open(image_path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode("utf-8")
        ext = image_path.rsplit(".", 1)[-1].lower()
        media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                      "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
        resp = get_client().messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
                {"type": "text", "text": prompt},
            ]}],
        )
        return resp.content[0].text

    # CLI fallback: resize large images for faster processing, then have Claude read
    resized = _maybe_resize_for_cli(image_path)
    full_prompt = f"Look at the image at {resized} and answer:\n\n{prompt}"
    return _run_cli(full_prompt, tools=["Read"])


def ask_with_image_b64(prompt: str, image_b64: str, media_type: str = "image/png",
                       max_tokens: int = 8192) -> str:
    """Send a prompt with base64-encoded image data, return the text response."""
    if _sdk_available():
        resp = get_client().messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": prompt},
            ]}],
        )
        return resp.content[0].text

    # CLI fallback: write image to temp file, have Claude read it
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(media_type, ".png")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(base64.b64decode(image_b64))
        tmp_path = tmp.name
    try:
        full_prompt = f"Look at the image at {tmp_path} and answer:\n\n{prompt}"
        return _run_cli(full_prompt, tools=["Read"])
    finally:
        os.unlink(tmp_path)


def _maybe_resize_for_cli(image_path: str, max_size: int = 800) -> str:
    """Resize large images to speed up CLI processing. Returns path to resized image."""
    try:
        from PIL import Image
        img = Image.open(image_path)
        w, h = img.size
        if max(w, h) <= max_size:
            return image_path
        scale = max_size / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        resized_path = image_path.rsplit(".", 1)[0] + "_resized.jpg"
        img.save(resized_path, "JPEG", quality=80)
        return resized_path
    except Exception:
        return image_path


def _extract_json_str(raw: str) -> str:
    """Strip markdown fences and find the JSON object/array in the response."""
    # Remove all markdown code fences
    raw = re.sub(r"```(?:json)?\s*", "", raw)
    raw = re.sub(r"```", "", raw)
    raw = raw.strip()
    # Try to find the outermost JSON object or array
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start = raw.find(start_char)
        if start == -1:
            continue
        # Find the matching closing bracket
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(raw)):
            c = raw[i]
            if escape:
                escape = False
                continue
            if c == '\\' and in_string:
                escape = True
                continue
            if c == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == start_char:
                depth += 1
            elif c == end_char:
                depth -= 1
                if depth == 0:
                    return raw[start:i + 1]
    return raw


def parse_json(raw: str) -> dict:
    """Extract JSON object from response that may have markdown fences or surrounding text."""
    return json.loads(_extract_json_str(raw))


def parse_json_array(raw: str) -> list:
    """Extract JSON array from response that may have markdown fences or surrounding text."""
    return json.loads(_extract_json_str(raw))
