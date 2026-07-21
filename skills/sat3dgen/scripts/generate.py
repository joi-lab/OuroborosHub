#!/usr/bin/env python3
"""
Sat3DGen skill — generate a 3D scene from a satellite image
via the HuggingFace Space qian43/Sat3DGen (Gradio Client).

Outputs are confined to OUROBOROS_SKILL_STATE_DIR.
"""
import argparse
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

SPACE_ID = "qian43/Sat3DGen"
VALID_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp'}

VIEWER_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Sat3DGen — 3D Viewer</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0a0a12; overflow: hidden; font-family: system-ui, sans-serif; }
  canvas { display: block; }
  #info {
    position: absolute; top: 16px; left: 16px; color: #ccc;
    font-size: 13px; pointer-events: none; text-shadow: 0 1px 3px rgba(0,0,0,.8);
  }
</style>
</head>
<body>
<div id="info">Sat3DGen 3D Viewer — drag to rotate, scroll to zoom</div>
<script type="importmap">
{ "imports": {
    "three": "https://cdn.jsdelivr.net/npm/three@0.164.1/build/three.module.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.164.1/examples/jsm/"
}}
</script>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { OBJLoader } from 'three/addons/loaders/OBJLoader.js';

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0a12);
scene.fog = new THREE.Fog(0x0a0a12, 80, 200);

const camera = new THREE.PerspectiveCamera(55, innerWidth / innerHeight, 0.1, 500);
camera.position.set(30, 25, 30);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(devicePixelRatio);
renderer.toneMapping = THREE.ACESFilmicToneMapping;
document.body.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.target.set(0, 3, 0);

scene.add(new THREE.AmbientLight(0xffffff, 0.6));
const dir = new THREE.DirectionalLight(0xffffff, 1.2);
dir.position.set(20, 40, 20);
scene.add(dir);
scene.add(new THREE.HemisphereLight(0x8899bb, 0x443322, 0.5));

const grid = new THREE.GridHelper(100, 50, 0x333344, 0x222233);
scene.add(grid);

// Load OBJ from a sibling file via fetch
fetch('__OBJ_FILENAME__')
  .then(r => r.text())
  .then(objText => {
    const loader = new OBJLoader();
    const obj = loader.parse(objText);
    const box = new THREE.Box3().setFromObject(obj);
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    obj.position.sub(center);
    obj.position.y += size.y / 2;
    obj.traverse(child => {
      if (child.isMesh) {
        const geo = child.geometry;
        if (!geo.attributes.color) {
          child.material = new THREE.MeshStandardMaterial({
            color: 0x8899aa, roughness: 0.6, metalness: 0.1, flatShading: true
          });
        } else {
          child.material = new THREE.MeshStandardMaterial({
            vertexColors: true, roughness: 0.5, metalness: 0.05, flatShading: true
          });
        }
      }
    });
    scene.add(obj);
    controls.target.copy(new THREE.Vector3(0, size.y / 2, 0));
  })
  .catch(err => {
    document.getElementById('info').textContent = 'Failed to load OBJ: ' + err;
  });

addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});

(function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
})();
</script>
</body>
</html>"""


def _resolve_output_dir():
    """Return a confined output directory inside the skill state dir."""
    state_dir = os.environ.get("OUROBOROS_SKILL_STATE_DIR", "")
    if not state_dir:
        # Fallback: use a temp-like path inside the skill directory
        state_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".output")
    out = Path(state_dir) / "results"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _download_url(url, dest):
    """Download a URL to a local file path."""
    print(f"Downloading mesh from URL: {url}")
    urllib.request.urlretrieve(url, str(dest))


def main():
    parser = argparse.ArgumentParser(description="Sat3DGen: satellite image to 3D mesh")
    parser.add_argument("image", help="Path to satellite image (PNG/JPG)")
    parser.add_argument("--resolution", type=int, default=256, choices=[128, 256],
                        help="Mesh voxel resolution (default: 256)")
    args = parser.parse_args()

    image_path = Path(args.image)
    if not image_path.is_file():
        print(f"ERROR: Image not found: {image_path}", file=sys.stderr)
        sys.exit(1)

    if image_path.suffix.lower() not in VALID_EXTENSIONS:
        print(f"ERROR: Unsupported image format '{image_path.suffix}'. "
              f"Supported: {', '.join(sorted(VALID_EXTENSIONS))}", file=sys.stderr)
        sys.exit(1)

    out_dir = _resolve_output_dir()
    stem = image_path.stem

    # --- Preflight: check gradio_client ---
    try:
        from gradio_client import Client, handle_file
    except ImportError:
        print("ERROR: gradio_client not installed. "
              "Run: pip install gradio_client", file=sys.stderr)
        sys.exit(1)

    # --- Connect to HF Space (with error handling) ---
    print(f"Connecting to HuggingFace Space: {SPACE_ID} ...")
    try:
        client = Client(SPACE_ID)
    except Exception as e:
        print(f"ERROR: Could not connect to HuggingFace Space "
              f"{SPACE_ID}: {e}", file=sys.stderr)
        sys.exit(1)

    # --- Preflight: verify API surface ---
    try:
        api_info = client.view_api(return_format="dict")
        print(f"Space API endpoints available: {len(api_info.get('named_endpoints', {}))} named, "
              f"{len(api_info.get('unnamed_endpoints', {}))} unnamed")
    except Exception as e:
        print(f"WARNING: Could not inspect Space API: {e}", file=sys.stderr)

    # --- Submit for 3D mesh generation ---
    print(f"Submitting image: {image_path} (resolution={args.resolution}) ...")
    try:
        result = client.predict(
            handle_file(str(image_path)),
            args.resolution,
            api_name="/generate_mesh"
        )
    except Exception as e:
        print(f"Named endpoint /generate_mesh failed ({e}), trying fn_index=0 ...")
        try:
            result = client.predict(
                handle_file(str(image_path)),
                args.resolution,
                fn_index=0
            )
        except Exception as e2:
            print(f"ERROR: Generation failed: {e2}", file=sys.stderr)
            sys.exit(1)

    print(f"Result type: {type(result).__name__}")

    # --- Extract the 3D file from the result ---
    mesh_source = None
    is_url = False

    def _check_candidate(val):
        """Check if a string is a usable mesh path or URL."""
        if not isinstance(val, str):
            return None, False
        if val.startswith(('http://', 'https://')):
            for ext in ('.obj', '.glb', '.ply'):
                if ext in val.lower():
                    return val, True
            return None, False
        if val.endswith(('.obj', '.glb', '.ply')) or os.path.isfile(val):
            return val, False
        return None, False

    if isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, str):
                mesh_source, is_url = _check_candidate(item)
                if mesh_source:
                    break
            if isinstance(item, dict):
                for key in ('path', 'url', 'value', 'name'):
                    val = item.get(key)
                    mesh_source, is_url = _check_candidate(val)
                    if mesh_source:
                        break
            if mesh_source:
                break
    elif isinstance(result, str):
        mesh_source, is_url = _check_candidate(result)
    elif isinstance(result, dict):
        for key in ('path', 'url', 'value', 'name'):
            val = result.get(key)
            mesh_source, is_url = _check_candidate(val)
            if mesh_source:
                break

    if not mesh_source:
        print("WARNING: Could not locate mesh in result.", file=sys.stderr)
        debug_path = out_dir / f"{stem}_raw_result.json"
        debug_path.write_text(json.dumps(result, default=str, indent=2))
        print(f"Raw result saved to: {debug_path}")
        sys.exit(1)

    # --- Copy or download mesh to output ---
    if is_url:
        mesh_ext = '.obj'
        for ext in ('.obj', '.glb', '.ply'):
            if ext in mesh_source.lower():
                mesh_ext = ext
                break
    else:
        mesh_ext = Path(mesh_source).suffix or '.obj'

    mesh_filename = f"{stem}_mesh{mesh_ext}"
    mesh_out = out_dir / mesh_filename

    if is_url:
        try:
            _download_url(mesh_source, mesh_out)
        except Exception as e:
            print(f"ERROR: Failed to download mesh from {mesh_source}: {e}",
                  file=sys.stderr)
            sys.exit(1)
    else:
        shutil.copy2(mesh_source, mesh_out)

    print(f"3D mesh saved: {mesh_out}")

    # --- Generate Three.js viewer (external .obj reference, not inline) ---
    if mesh_ext == '.obj':
        try:
            viewer_html = VIEWER_TEMPLATE.replace('__OBJ_FILENAME__', mesh_filename)
            viewer_path = out_dir / f"{stem}_viewer.html"
            viewer_path.write_text(viewer_html)
            print(f"Interactive viewer: {viewer_path}")
            print(f"  (open viewer and mesh from the same directory)")
        except Exception as e:
            print(f"WARNING: Could not generate viewer: {e}", file=sys.stderr)
    else:
        print(f"Mesh format is {mesh_ext} — OBJ viewer skipped")

    # --- Summary ---
    print(f"\n=== Sat3DGen Complete ===")
    print(f"  Mesh: {mesh_out}")
    if mesh_ext == '.obj':
        print(f"  Viewer: {out_dir / f'{stem}_viewer.html'}")
    print(f"  Resolution: {args.resolution}")
    print(f"  Output dir: {out_dir}")


if __name__ == "__main__":
    main()
