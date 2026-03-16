#!/bin/bash
# Build Blender 5.0.1 from source on DGX Spark (aarch64 + CUDA)
#
# Run this ONCE on the DGX host:
#   ssh luok@192.168.0.200
#   cd ~/Projects/blender-render
#   bash build-blender.sh
#
# Result: /opt/blender-5.0.1/ with working Blender binary
# Time: ~1 hour first build, uses ccache for rebuilds
#
# Based on: https://github.com/mvalancy/blender-nvidia-gb10

set -euo pipefail

BLENDER_VERSION="5.0.1"
BLENDER_TAG="v${BLENDER_VERSION}"
BUILD_DIR="$HOME/blender-build"
INSTALL_DIR="$HOME/opt/blender-${BLENDER_VERSION}"
PATCH_REPO="https://github.com/mvalancy/blender-nvidia-gb10.git"
JOBS=$(nproc)
# Use fewer jobs for ninja (CUDA kernel compilation uses lots of RAM)
NINJA_JOBS=$((JOBS > 10 ? 10 : JOBS))

# Ensure CUDA is on PATH
export CUDA_PATH=/usr/local/cuda
export CUDAToolkit_ROOT=/usr/local/cuda
export PATH=/usr/local/cuda/bin:$PATH

echo "=== Building Blender ${BLENDER_VERSION} for aarch64 + CUDA ==="
echo "  Build dir: ${BUILD_DIR}"
echo "  Install dir: ${INSTALL_DIR}"
echo "  Jobs: ${JOBS}"
echo ""

# ── Step 1: Install build dependencies ────────────────────────────────
echo "=== Step 1/6: Installing build dependencies ==="
sudo apt-get update
sudo apt-get install -y \
    build-essential git git-lfs cmake ninja-build ccache \
    patch autoconf automake libtool autopoint \
    bison flex gettext texinfo help2man yasm wget patchelf meson \
    python3-dev python3-mako python3-yaml \
    libx11-dev libxxf86vm-dev libxcursor-dev libxi-dev libxrandr-dev \
    libxinerama-dev libxkbcommon-dev libxkbcommon-x11-dev libxshmfence-dev \
    libwayland-dev libdecor-0-dev wayland-protocols \
    libdbus-1-dev libgl-dev libegl-dev mesa-common-dev libglu1-mesa-dev libxt-dev \
    libdrm-dev libgbm-dev libudev-dev libinput-dev libevdev-dev \
    libasound2-dev libpulse-dev libjack-jackd2-dev \
    zlib1g-dev libncurses-dev libexpat1-dev \
    libcairo2-dev libpixman-1-dev libffi-dev \
    tcl perl libxml2-dev \
    libxcb-randr0-dev libxcb-dri2-0-dev libxcb-dri3-dev \
    libxcb-present-dev libxcb-sync-dev libxcb-glx0-dev \
    libxcb-shm0-dev libxcb-xfixes0-dev libx11-xcb-dev \
    libgles2-mesa-dev

# ── Step 2: Clone Blender source ──────────────────────────────────────
echo "=== Step 2/6: Cloning Blender source ==="
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

if [ ! -d "blender" ]; then
    GIT_LFS_SKIP_SMUDGE=1 git clone -b "${BLENDER_TAG}" --depth 1 \
        https://github.com/blender/blender.git
    cd blender
    # LFS data is required — without it Blender segfaults
    git config lfs.url https://projects.blender.org/blender/blender.git/info/lfs
    git lfs pull --include="release/datafiles/*"
else
    echo "  Blender source already cloned, skipping"
    cd blender
fi

BLENDER_SRC="${BUILD_DIR}/blender"

# ── Step 3: Get and apply aarch64 patches ─────────────────────────────
echo "=== Step 3/6: Applying aarch64 patches ==="

if [ ! -d "${BUILD_DIR}/gb10-patches" ]; then
    git clone --depth 1 "${PATCH_REPO}" "${BUILD_DIR}/gb10-patches"
fi

# Apply patches if not already applied
PATCH_DIR="${BUILD_DIR}/gb10-patches/patches"
if [ -d "${PATCH_DIR}" ]; then
    cd "${BLENDER_SRC}"
    for patch_file in "${PATCH_DIR}"/*.patch; do
        if [ -f "$patch_file" ]; then
            echo "  Applying: $(basename "$patch_file")"
            git apply --check "$patch_file" 2>/dev/null && \
                git apply "$patch_file" || \
                echo "  (already applied or N/A)"
        fi
    done
fi

# Fix ISPC multiarch headers (aarch64-specific)
for dir in bits gnu asm; do
    if [ -d "/usr/include/aarch64-linux-gnu/$dir" ] && [ ! -e "/usr/include/$dir" ]; then
        sudo ln -sf "/usr/include/aarch64-linux-gnu/$dir" "/usr/include/$dir"
    fi
done

# Fix CUDA 13 missing header
CUDA_STUB="/usr/local/cuda/include/texture_fetch_functions.h"
if [ ! -f "${CUDA_STUB}" ]; then
    sudo touch "${CUDA_STUB}"
fi

# ── Step 4: Build third-party dependencies ────────────────────────────
echo "=== Step 4/6: Building third-party dependencies (~30-45 min) ==="
cd "${BLENDER_SRC}"

if [ ! -d "lib/linux_arm64" ] || [ ! -f "lib/linux_arm64/boost/lib/libboost_system.a" ]; then
    make deps -j${JOBS}

    # Fix aarch64 lib path: Meson installs to lib/aarch64-linux-gnu/ not lib64/
    echo "  Fixing aarch64 lib paths..."
    DEPS_RELEASE="${BUILD_DIR}/build_linux/deps_arm64/Release"
    for dir in wayland mesa; do
        if [ -d "${DEPS_RELEASE}/${dir}/lib/aarch64-linux-gnu" ] && [ ! -d "${DEPS_RELEASE}/${dir}/lib64" ]; then
            mkdir -p "${DEPS_RELEASE}/${dir}/lib64"
            cp -a "${DEPS_RELEASE}/${dir}/lib/aarch64-linux-gnu/"* "${DEPS_RELEASE}/${dir}/lib64/"
            echo "    Fixed: ${dir}/lib64"
        fi
    done

    # Re-run harvest (quick — just copies)
    make deps -j${JOBS}
else
    echo "  Dependencies already built, skipping"
fi

# ── Step 5: Build Blender ─────────────────────────────────────────────
echo "=== Step 5/6: Building Blender (~10-15 min) ==="
mkdir -p "${BUILD_DIR}/build"
cd "${BUILD_DIR}/build"

# Detect CUDA compute capability for the local GPU
CUDA_ARCH=""
if command -v nvidia-smi &>/dev/null; then
    # Get compute capability (e.g., "8.9" → "sm_89")
    CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d '.')
    if [ -n "$CC" ]; then
        CUDA_ARCH="sm_${CC}"
        echo "  Detected GPU compute capability: ${CUDA_ARCH}"
    fi
fi

cmake \
    -C "${BLENDER_SRC}/build_files/cmake/config/blender_release.cmake" \
    -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="${INSTALL_DIR}" \
    -DWITH_LIBS_PRECOMPILED=ON \
    -DLIBDIR="${BLENDER_SRC}/lib/linux_arm64" \
    -DWITH_INSTALL_PORTABLE=ON \
    -DWITH_CYCLES=ON \
    -DWITH_CYCLES_CUDA_BINARIES=ON \
    ${CUDA_ARCH:+-DCYCLES_CUDA_BINARIES_ARCH="${CUDA_ARCH}"} \
    -DWITH_CYCLES_DEVICE_OPTIX=ON \
    -DCMAKE_C_COMPILER_LAUNCHER=ccache \
    -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
    "${BLENDER_SRC}"

ninja -j${NINJA_JOBS}

# ── Step 6: Install ──────────────────────────────────────────────────
echo "=== Step 6/6: Installing to ${INSTALL_DIR} ==="
ninja install

# Verify
echo ""
echo "=== Verification ==="
"${INSTALL_DIR}/blender" --version
echo ""
echo "=== Done! ==="
echo "Blender installed to: ${INSTALL_DIR}"
echo "Binary: ${INSTALL_DIR}/blender"
echo ""
echo "To use with the Docker service:"
echo "  docker compose up -d --build"
echo ""
echo "Disk usage:"
du -sh "${INSTALL_DIR}"
du -sh "${BUILD_DIR}"
echo ""
echo "You can remove the build dir to free space:"
echo "  rm -rf ${BUILD_DIR}"
