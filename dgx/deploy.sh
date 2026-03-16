#!/bin/bash
# Deploy all DGX Spark services for Washing Room Designer
# Usage: ./deploy.sh [all|flux|chord|trellis|splatter|blender|status] [--build]
#        ./deploy.sh build-blender   # One-time: build Blender 5.0.1 from source on DGX
#
# Requires: ssh access to luok@192.168.0.200

set -euo pipefail

DGX_HOST="luok@192.168.0.200"
DGX_BASE="~/Projects"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

service="${1:-all}"
build_flag="${2:-}"

deploy_service() {
    local name="$1"
    local local_dir="$2"
    local remote_dir="$3"

    echo "=== Deploying $name ==="
    echo "  Syncing files..."
    rsync -av --exclude='__pycache__' "$local_dir/" "$DGX_HOST:$remote_dir/"

    # Ensure .env exists
    ssh "$DGX_HOST" "touch $remote_dir/.env"

    if [ "$build_flag" = "--build" ]; then
        echo "  Building and starting container..."
        ssh "$DGX_HOST" "cd $remote_dir && docker compose up -d --build"
    else
        echo "  Restarting container..."
        ssh "$DGX_HOST" "cd $remote_dir && docker compose up -d"
    fi

    echo "  Done."
}

check_health() {
    local name="$1"
    local port="$2"
    echo -n "  Health check $name (port $port): "
    if curl -sf "http://192.168.0.200:$port/health" 2>/dev/null; then
        echo ""
    else
        echo "not ready yet (container may still be starting)"
    fi
}

case "$service" in
    flux)
        deploy_service "Flux (Chroma + ControlNet + Preview)" \
            "$SCRIPT_DIR/flux-render" "$DGX_BASE/flux-render"
        check_health "Flux" 8001
        ;;
    chord)
        deploy_service "CHORD (Material extraction)" \
            "$SCRIPT_DIR/chord-material" "$DGX_BASE/chord-material"
        check_health "CHORD" 8002
        ;;
    trellis)
        deploy_service "TRELLIS.2 (3D furniture)" \
            "$SCRIPT_DIR/trellis" "$DGX_BASE/trellis"
        check_health "TRELLIS" 8003
        ;;
    splatter)
        deploy_service "DN-Splatter (3D reconstruction)" \
            "$SCRIPT_DIR/dn-splatter" "$DGX_BASE/dn-splatter"
        check_health "DN-Splatter" 8004
        ;;
    build-blender)
        echo "=== Building Blender 5.0.1 from source on DGX Spark ==="
        echo "  This takes ~1 hour on first run. Uses ccache for rebuilds."
        echo ""
        # Copy build script to DGX and run it
        scp "$SCRIPT_DIR/blender-render/build-blender.sh" "$DGX_HOST:~/build-blender.sh"
        ssh -t "$DGX_HOST" "bash ~/build-blender.sh"
        echo ""
        echo "=== Blender build complete ==="
        echo "  Now run: ./deploy.sh blender --build"
        ;;
    blender)
        # Verify Blender is built on DGX
        if ! ssh "$DGX_HOST" "test -f ~/opt/blender-5.0.1/blender" 2>/dev/null; then
            echo "ERROR: Blender not built on DGX yet."
            echo "  Run first: ./deploy.sh build-blender"
            exit 1
        fi
        # Copy blender_scene.py into the build context before deploying
        cp "$SCRIPT_DIR/../backend/blender_scene.py" "$SCRIPT_DIR/blender-render/blender_scene.py"
        deploy_service "Blender (3D render with CUDA GPU)" \
            "$SCRIPT_DIR/blender-render" "$DGX_BASE/blender-render"
        check_health "Blender" 8005
        ;;
    all)
        # Copy blender_scene.py into the build context
        cp "$SCRIPT_DIR/../backend/blender_scene.py" "$SCRIPT_DIR/blender-render/blender_scene.py"

        # Warn if Blender not built yet (non-fatal — other services still deploy)
        if ! ssh "$DGX_HOST" "test -f ~/opt/blender-5.0.1/blender" 2>/dev/null; then
            echo "WARNING: Blender not built on DGX yet. Skipping blender service."
            echo "  Run: ./deploy.sh build-blender"
            SKIP_BLENDER=1
        else
            SKIP_BLENDER=0
        fi

        deploy_service "Flux (Chroma + ControlNet + Preview)" \
            "$SCRIPT_DIR/flux-render" "$DGX_BASE/flux-render"
        deploy_service "CHORD (Material extraction)" \
            "$SCRIPT_DIR/chord-material" "$DGX_BASE/chord-material"
        deploy_service "TRELLIS.2 (3D furniture)" \
            "$SCRIPT_DIR/trellis" "$DGX_BASE/trellis"
        deploy_service "DN-Splatter (3D reconstruction)" \
            "$SCRIPT_DIR/dn-splatter" "$DGX_BASE/dn-splatter"
        if [ "${SKIP_BLENDER:-0}" = "0" ]; then
            deploy_service "Blender (3D render with CUDA GPU)" \
                "$SCRIPT_DIR/blender-render" "$DGX_BASE/blender-render"
        fi

        echo ""
        echo "=== Health checks ==="
        sleep 3
        check_health "Flux" 8001
        check_health "CHORD" 8002
        check_health "TRELLIS" 8003
        check_health "DN-Splatter" 8004
        check_health "Blender" 8005

        echo ""
        echo "=== VRAM ==="
        ssh "$DGX_HOST" "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader"
        ;;
    status)
        echo "=== Service status ==="
        check_health "Flux" 8001
        check_health "CHORD" 8002
        check_health "TRELLIS" 8003
        check_health "DN-Splatter" 8004
        check_health "Blender" 8005
        echo ""
        echo "=== VRAM ==="
        ssh "$DGX_HOST" "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader"
        ;;
    *)
        echo "Usage: $0 [all|flux|chord|trellis|splatter|blender|build-blender|status] [--build]"
        exit 1
        ;;
esac
