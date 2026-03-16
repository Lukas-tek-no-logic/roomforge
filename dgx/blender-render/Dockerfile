FROM nvcr.io/nvidia/pytorch:25.12-py3

WORKDIR /app

# Blender runtime dependencies (not build deps — Blender is pre-built on host)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libxi6 \
    libxxf86vm1 libxfixes3 libxkbcommon0 libgomp1 \
    libxcursor1 libxrandr2 libxinerama1 libdecor-0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps for the FastAPI service
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    Pillow

# Copy service files
COPY service.py .
COPY blender_scene.py .

# Persistent cache for Polyhaven textures and HDRIs
RUN mkdir -p /data/texture_cache /data/hdri_cache

# Blender binary is mounted from host at /opt/blender
# Built via: bash build-blender.sh (run once on DGX host)
ENV BLENDER_BIN=/opt/blender/blender

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8000"]
