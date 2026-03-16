FROM nvcr.io/nvidia/pytorch:25.12-py3

WORKDIR /app

# System deps: COLMAP build + video processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    cmake \
    ninja-build \
    build-essential \
    libboost-program-options-dev \
    libboost-filesystem-dev \
    libboost-graph-dev \
    libboost-system-dev \
    libeigen3-dev \
    libflann-dev \
    libfreeimage-dev \
    libmetis-dev \
    libgoogle-glog-dev \
    libgtest-dev \
    libsqlite3-dev \
    libglew-dev \
    qtbase5-dev \
    libqt5opengl5-dev \
    libcgal-dev \
    libceres-dev \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Build COLMAP from source with CUDA
RUN git clone --depth 1 --branch 3.11.1 https://github.com/colmap/colmap.git /tmp/colmap && \
    cd /tmp/colmap && \
    mkdir build && cd build && \
    cmake .. -GNinja \
        -DCMAKE_CUDA_ARCHITECTURES=all-major \
        -DCMAKE_BUILD_TYPE=Release && \
    ninja -j$(nproc) && \
    ninja install && \
    rm -rf /tmp/colmap

# Python deps (required)
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    python-multipart \
    Pillow \
    trimesh \
    pygltflib \
    scipy

# Open3D (optional — Poisson meshing fallback)
RUN pip install --no-cache-dir open3d-cpu 2>/dev/null \
    || pip install --no-cache-dir open3d 2>/dev/null \
    || echo "open3d not available"

COPY service.py .

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8000"]
