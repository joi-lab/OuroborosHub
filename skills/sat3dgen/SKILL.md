---
name: sat3dgen
version: 1.0.0
description: "Generate 3D scenes from satellite imagery using Sat3DGen (ICLR 2026). Sends a satellite image to the HuggingFace Space API and returns a textured 3D mesh (.obj) plus an interactive Three.js viewer."
type: script
scripts:
  - name: generate.py
    description: "Generate a 3D mesh and interactive viewer from a satellite image"
runtime: python3
timeout_sec: 300
dependencies:
  - "gradio_client>=1.0"
permissions:
  - net
trigger: "User provides a satellite image and wants to generate a 3D street-level scene, 3D mesh, or 3D reconstruction from it."
env_from_settings: []
---

# Sat3DGen — Satellite Image to 3D Scene

Generates a 3D street-level scene from a single satellite/aerial image using the [Sat3DGen](https://github.com/qianmingduowan/Sat3DGen) model (ICLR 2026).

## How it works

1. Accepts a satellite image path as input (PNG/JPG, zoom level ~20 recommended)
2. Connects to the live HuggingFace Space `qian43/Sat3DGen` via Gradio Client
3. Submits the image for 3D mesh generation (Marching Cubes at configurable resolution)
4. Downloads the resulting `.obj` mesh
5. Generates an interactive Three.js HTML viewer for the mesh

## Usage

```
skill_exec sat3dgen generate.py <image_path> [--resolution 256]
```

### Arguments
- `image_path` — path to the satellite image (PNG or JPG)
- `--resolution` — mesh voxel resolution: 128 or 256 (default: 256)

### Outputs (written to skill state directory)
- `<name>_mesh.obj` — textured 3D mesh
- `<name>_viewer.html` — interactive Three.js viewer

## Requirements
- `gradio_client` (installed automatically via dependencies)
- Internet access to reach HuggingFace Spaces (`net` permission)
