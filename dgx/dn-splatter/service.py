"""
3D reconstruction service on DGX Spark.
Takes video frames and reconstructs a 3D mesh of the room.

Pipeline: frames → COLMAP SfM → dense → Poisson mesh → GLB
VRAM: ~4-8 GB (freed after reconstruction)
Time: 2-10 minutes depending on frame count
"""

import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from fastapi import FastAPI
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel
from PIL import Image

app = FastAPI(title="3D Reconstruction Service")


class ReconstructFromB64Request(BaseModel):
    frames_b64: list[str]
    output_name: str = "reconstruction"


@app.get("/health")
async def health():
    colmap_ok = shutil.which("colmap") is not None
    try:
        import open3d
        o3d_ok = True
    except ImportError:
        o3d_ok = False
    return {
        "status": "ok" if colmap_ok else "partial",
        "colmap": colmap_ok,
        "open3d": o3d_ok,
    }


@app.post("/reconstruct")
async def reconstruct(req: ReconstructFromB64Request):
    """Run full reconstruction: frames → COLMAP → dense → mesh → GLB."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _reconstruct_sync, req.frames_b64, req.output_name)
        return JSONResponse(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


def _reconstruct_sync(frames_b64: list[str], output_name: str) -> dict:
    """Synchronous reconstruction pipeline."""
    with tempfile.TemporaryDirectory(prefix="recon_") as workdir:
        workdir = Path(workdir)

        # Save frames
        frames_dir = workdir / "images"
        frames_dir.mkdir()
        for i, b64 in enumerate(frames_b64):
            img_bytes = base64.b64decode(b64)
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            img.save(frames_dir / f"frame_{i:04d}.jpg")
        print(f"[RECON] Saved {len(frames_b64)} frames", flush=True)

        # Step 1: COLMAP sparse reconstruction
        sparse_dir = workdir / "sparse"
        _run_colmap_sparse(frames_dir, workdir, sparse_dir)

        # Step 2: COLMAP dense reconstruction
        dense_dir = workdir / "dense"
        _run_colmap_dense(frames_dir, sparse_dir, dense_dir)

        # Step 3: Poisson mesh reconstruction
        mesh_path = workdir / "mesh.glb"
        _reconstruct_mesh(dense_dir, mesh_path)

        # Step 4: Extract room dimensions
        room_data = _extract_room_data(mesh_path)

        # Read GLB and encode
        glb_bytes = mesh_path.read_bytes()
        glb_b64 = base64.b64encode(glb_bytes).decode()

        return {
            "room_data": room_data,
            "mesh_b64": glb_b64,
            "mesh_size": len(glb_bytes),
        }


def _run_colmap_sparse(frames_dir: Path, workdir: Path, sparse_dir: Path):
    """COLMAP SfM: feature extraction → matching → mapping."""
    db_path = workdir / "database.db"
    sparse_dir.mkdir(exist_ok=True)

    print("[RECON] COLMAP feature extraction...", flush=True)
    subprocess.run([
        "colmap", "feature_extractor",
        "--database_path", str(db_path),
        "--image_path", str(frames_dir),
        "--ImageReader.camera_model", "SIMPLE_RADIAL",
        "--ImageReader.single_camera", "1",
        "--SiftExtraction.use_gpu", "1",
    ], check=True, capture_output=True, timeout=300)

    print("[RECON] COLMAP matching...", flush=True)
    subprocess.run([
        "colmap", "sequential_matcher",
        "--database_path", str(db_path),
        "--SiftMatching.use_gpu", "1",
    ], check=True, capture_output=True, timeout=300)

    print("[RECON] COLMAP mapping...", flush=True)
    subprocess.run([
        "colmap", "mapper",
        "--database_path", str(db_path),
        "--image_path", str(frames_dir),
        "--output_path", str(sparse_dir),
    ], check=True, capture_output=True, timeout=600)

    # Use the largest reconstruction (usually 0/)
    recons = sorted(sparse_dir.iterdir())
    if not recons:
        raise RuntimeError("COLMAP mapping produced no reconstruction")
    best = recons[0]
    # Move best to sparse_dir root if in subdir
    if best.name != "cameras.bin":
        for f in best.iterdir():
            shutil.move(str(f), str(sparse_dir / f.name))
        best.rmdir()
    print(f"[RECON] Sparse reconstruction done", flush=True)


def _run_colmap_dense(frames_dir: Path, sparse_dir: Path, dense_dir: Path):
    """COLMAP dense reconstruction: undistort → stereo → fusion."""
    dense_dir.mkdir(exist_ok=True)

    print("[RECON] COLMAP undistort...", flush=True)
    subprocess.run([
        "colmap", "image_undistorter",
        "--image_path", str(frames_dir),
        "--input_path", str(sparse_dir),
        "--output_path", str(dense_dir),
        "--output_type", "COLMAP",
    ], check=True, capture_output=True, timeout=300)

    print("[RECON] COLMAP patch_match_stereo...", flush=True)
    subprocess.run([
        "colmap", "patch_match_stereo",
        "--workspace_path", str(dense_dir),
        "--PatchMatchStereo.geom_consistency", "true",
    ], check=True, capture_output=True, timeout=600)

    print("[RECON] COLMAP stereo_fusion...", flush=True)
    subprocess.run([
        "colmap", "stereo_fusion",
        "--workspace_path", str(dense_dir),
        "--output_path", str(dense_dir / "fused.ply"),
    ], check=True, capture_output=True, timeout=300)

    if not (dense_dir / "fused.ply").exists():
        raise RuntimeError("Dense fusion produced no output")
    print(f"[RECON] Dense reconstruction done", flush=True)


def _reconstruct_mesh(dense_dir: Path, output_path: Path):
    """Poisson surface reconstruction from dense point cloud."""
    ply_path = dense_dir / "fused.ply"

    # Try COLMAP's built-in Poisson mesher first
    try:
        print("[RECON] COLMAP Poisson mesher...", flush=True)
        mesh_ply = dense_dir / "meshed.ply"
        subprocess.run([
            "colmap", "poisson_mesher",
            "--input_path", str(ply_path),
            "--output_path", str(mesh_ply),
        ], check=True, capture_output=True, timeout=300)

        import trimesh
        mesh = trimesh.load(str(mesh_ply))
        mesh.export(str(output_path))
        print(f"[RECON] Mesh: {len(mesh.faces)} faces", flush=True)
        return
    except Exception as e:
        print(f"[RECON] COLMAP Poisson failed: {e}", flush=True)

    # Fallback: Open3D Poisson reconstruction
    try:
        import open3d as o3d
        print("[RECON] Open3D Poisson...", flush=True)
        pcd = o3d.io.read_point_cloud(str(ply_path))
        pcd.estimate_normals()
        pcd.orient_normals_consistent_tangent_plane(k=15)
        mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pcd, depth=9)

        # Remove low-density vertices (artifacts)
        densities = np.asarray(densities)
        threshold = np.quantile(densities, 0.05)
        vertices_to_remove = densities < threshold
        mesh.remove_vertices_by_mask(vertices_to_remove)

        o3d.io.write_triangle_mesh(str(output_path.with_suffix('.ply')), mesh)
        import trimesh
        tm = trimesh.load(str(output_path.with_suffix('.ply')))
        tm.export(str(output_path))
        output_path.with_suffix('.ply').unlink(missing_ok=True)
        print(f"[RECON] Mesh: {len(tm.faces)} faces", flush=True)
        return
    except ImportError:
        print("[RECON] Open3D not available", flush=True)

    # Last fallback: convert point cloud directly to GLB
    import trimesh
    pcd = trimesh.load(str(ply_path))
    pcd.export(str(output_path))
    print(f"[RECON] Exported point cloud as GLB (no mesh)", flush=True)


def _extract_room_data(mesh_path: Path) -> dict:
    """Extract room dimensions from mesh bounding box."""
    try:
        import trimesh
        mesh = trimesh.load(str(mesh_path))
        bounds = mesh.bounds
        dims = bounds[1] - bounds[0]
        return {
            "room": {
                "width": round(float(dims[0]), 2),
                "depth": round(float(dims[1]), 2),
                "height": round(float(dims[2]), 2),
            },
            "source": "colmap-reconstruction",
            "mesh_path": str(mesh_path),
        }
    except Exception:
        return {
            "room": {"width": 3.0, "depth": 3.0, "height": 2.5},
            "source": "reconstruction-fallback",
        }
