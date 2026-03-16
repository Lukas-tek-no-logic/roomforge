FROM nvcr.io/nvidia/pytorch:25.12-py3

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    libjpeg-dev \
    && rm -rf /var/lib/apt/lists/*

# TRELLIS.2 Python dependencies
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    python-multipart \
    Pillow \
    trimesh \
    pygltflib \
    scipy \
    imageio \
    imageio-ffmpeg \
    easydict \
    opencv-python-headless \
    ninja \
    transformers \
    kornia \
    timm \
    lpips \
    zstandard \
    fast-simplification

# Optional TRELLIS.2 extensions — all non-fatal (fallback always works)
RUN pip install --no-cache-dir \
    git+https://github.com/EasternJournalist/utils3d.git@9a4eb15e4021b67b12c460c7057d642626897ec8 \
    2>/dev/null || echo "[TRELLIS] utils3d build failed (optional)"

RUN pip install --no-cache-dir \
    git+https://github.com/NVlabs/nvdiffrast.git@v0.4.0 --no-build-isolation \
    2>/dev/null || echo "[TRELLIS] nvdiffrast build failed (optional)"

# Clone TRELLIS v1 (no DINOv3 dependency) and v2 (for when DINOv3 is approved)
RUN git clone --depth 1 --recursive https://github.com/microsoft/TRELLIS.git /app/trellis_v1 \
    2>/dev/null || echo "[TRELLIS] v1 clone failed"

RUN git clone --depth 1 --recursive https://github.com/microsoft/TRELLIS.2.git /app/trellis_repo \
    2>/dev/null || echo "[TRELLIS] v2 clone failed"

RUN cd /app/trellis_repo 2>/dev/null && \
    (pip install ./cumesh --no-build-isolation 2>/dev/null || echo "[TRELLIS] cumesh failed") && \
    (pip install ./o-voxel --no-build-isolation 2>/dev/null || echo "[TRELLIS] o-voxel failed") && \
    (pip install ./flexgemm --no-build-isolation 2>/dev/null || echo "[TRELLIS] flexgemm failed") \
    || echo "[TRELLIS] extensions skipped"

COPY service.py .

ENV HF_HOME=/root/.cache/huggingface
ENV TRELLIS_REPO=/app/trellis_repo
ENV ATTN_BACKEND=sdpa

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8000"]
