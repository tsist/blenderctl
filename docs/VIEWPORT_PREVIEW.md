# Viewport Preview (`blenderctl preview`)

`blenderctl preview` captures real Blender 5.3.0 3D viewport panels on a private, hidden Windows Desktop (`CreateDesktopW`) without flashing windows, stealing focus, or touching open Blender sessions or source `.blend` bytes.

## Capabilities

- **One-command focus & framing (`-Y` front convention)**:
  - Focus whole scene, `--objects A,B`, or `--collection Col`.
  - Default keeps surrounding scene context (for checking intersections, gaps, and cast shadows); add `--isolate` to solo target geometry while keeping scene lights and world intact.
  - Built-in layouts: `quad` (default: `TOP/FRONT/RIGHT` ortho + `FRONT_3Q` 50mm persp), `orbit`, `3x3`, `single`, custom `--views`, or `camera:<Name>`.
  - Reuse exact framing across revisions with `--framing-from prior_preview.json`.
- **Full 3D viewport display modes**:
  - `wireframe` (with `--xray` / `--xray-alpha` / `--wire-color`)
  - `solid` (Studio + Cavity + Specular; `--solid-color single|material|object|random`)
  - `solid_wire` (default: solid shading + native semi-transparent wireframe overlay, `--wire-opacity 0.05..1.0`, no edge-count cap)
  - `matcap` (high-gloss surface flow check; default `metal_carpaint.exr`)
  - `face_orientation` (backface/normal check)
  - `material` (EEVEE Material Preview / LookDev, studio HDRI or `--use-scene-lights`)
  - `rendered` (Cycles GPU viewport preview with **NVIDIA DLSS Ray Reconstruction** preferred, or `--render-engine eevee` for EEVEE Next)
- **Orthogonal overlays & multi-stage boards**:
  - `--wire-overlay 0.35` layers native semi-transparent wireframe onto any display mode (`material`, `rendered`, `matcap`, etc.).
  - `--compare-modes solid,solid_wire,material,rendered --view front_3q` produces a matched 2x2 stage comparison sheet.
- **DLSS Ray Reconstruction & explicit fallback**:
  - Blender 5.3 Cycles viewport DLSS (`preview_denoiser='DLSS'`) requires an RTX GPU (Compute Capability ≥ 7.5) and **NVIDIA driver ≥ 620**.
  - Default `--denoiser auto` activates `DLSS` (`--dlss-quality balanced`, 8 spp) when supported, and falls back explicitly to GPU `OPTIX` denoising when unavailable — always labeled in both the image badge and `preview.json`.
  - Pass `--denoiser dlss --strict-denoiser` to fail hard instead of falling back.
  - Unlit asset files receive soft temporary three-point fill lights in `rendered` mode (disable with `--strict-scene-lights`).
- **One-shot & hot session modes**:
  - Default is one-shot (launch hidden desktop → capture → exit & destroy desktop handle, zero residue).
  - Optional `preview session start / status / stop / list` reuses a warm hidden-desktop Blender process across rapid iterations with automatic SHA reload and idle auto-exit.

## Quick Examples

```powershell
# Default Quad 4-view solid + 45% semi-transparent wireframe
./blenderctl.ps1 --compact preview asset.blend --output quad.png

# Focus a sub-part (keep context vs isolate)
./blenderctl.ps1 --compact preview scene.blend --objects "Drawer" --mode solid_wire
./blenderctl.ps1 --compact preview scene.blend --objects "Drawer" --isolate --mode matcap

# Same-view 4-stage comparison (solid / solid_wire / material / Cycles DLSS)
./blenderctl.ps1 --compact preview asset.blend --compare-modes solid,solid_wire,material,rendered --view front_3q

# Strict DLSS 4-view render preview (requires Windows Blender 5.3 + NVIDIA driver >= 620)
./blenderctl.ps1 --compact preview asset.blend --mode rendered --denoiser dlss --strict-denoiser --dlss-quality balanced --samples 8

# EEVEE Next render preview & LookDev with semi-transparent wireframe overlay
./blenderctl.ps1 --compact preview asset.blend --mode rendered --render-engine eevee --layout orbit
./blenderctl.ps1 --compact preview asset.blend --mode material --wire-overlay 0.35 --layout orbit

# Optional hot session reuse
$sid = (./blenderctl.ps1 preview session start --idle-timeout 180 | ConvertFrom-Json).data.session_id
./blenderctl.ps1 --compact preview asset.blend --session $sid --mode solid_wire --output v1.png
./blenderctl.ps1 --compact preview asset.blend --session $sid --mode rendered --framing-from v1.json --output v2.png
./blenderctl.ps1 preview session stop $sid
```

## Boundaries

- Isolated-desktop capture uses Win32 `CreateDesktopW` and requires **Windows** with Blender 5.3.0. Host-only tests run cross-platform (Windows and Linux) without launching Blender.
- Output contact sheets are automatically capped at `<= 2048px` on the longest edge for fast visual review; source `.blend` files are opened with `load_ui=False, use_scripts=False` and never saved.
