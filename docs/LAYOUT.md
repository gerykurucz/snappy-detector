# SnappyAI Portable Layout Specification

This document describes the canonical folder layout for the packaged application.

## Root

```
SnappyAI/
├── SnappyAI.exe          # Launcher (C, ~24 KB, no Python deps)
└── main/                 # Real Nuitka build
    ├── SnappyAI.exe      # Real executable
    ├── python314.dll     # Python runtime
    ├── vcruntime140.dll  # MSVC runtime
    ├── *.pyd             # Compiled extensions
    ├── cv2/              # OpenCV package
    ├── numpy/            # NumPy package
    ├── onnxruntime/      # ONNX Runtime
    ├── PIL/              # Pillow
    ├── dxcam/            # DXcam capture backend
    ├── windows_capture/  # WGC capture backend
    ├── tcl.zip, tk.zip   # Tkinter data
    ├── tcl90.dll, tcl9tk90.dll
    └── ...               # All other runtime files
```

The launcher:
- Sets `SNAPPYAI_HOME` to the root folder
- Prepends `main\` to `PATH`
- Spawns `main\SnappyAI.exe` with the same command line
- Waits and returns the child's exit code

## User Data Folders (App-Root Relative)

| Folder | Purpose |
|--------|---------|
| `configs/` | `config.json`, `config.ini`, `reticle_presets.json` |
| `recordings/` | `macros.json`, `pad_macros.json` |
| `models/` | Downloaded ONNX models (`yolo11n-pose.onnx`, etc.) |
| `capture/` | Capture backend packages + `interception.dll` |
| `driver/` | `ViGEmClient.dll`, `interception.dll` |
| `docs/` | Documentation (`README.md`, `LAYOUT.md`) |
| `logs/` | `snappyai.log` + rotated `.oldN.log` |
| `reports/` | Lattool `report.html`, `report.md`, `manifest.json`, `chain.json` |
| `scripts/exec/` | `apply.ps1`, `maintenance.ps1`, `clean.ps1`, `redists.ps1`, `memory.ps1`, `power.ps1`, `debloat.ps1`, `registry.ps1` |
| `scripts/undo/` | `undo.ps1` |
| `cache/` | Browser auth profiles, temporary PS1 scripts, TensorRT cache |

## System Folders (Outside App Root)

| Path | Content |
|------|---------|
| `%LOCALAPPDATA%\SnappyAI\session.json` | Encrypted session (DPAPI) |
| `%LOCALAPPDATA%\SnappyAI\authbrowser\` | **Legacy** – moved to `cache\authbrowser\` on first run |

## Migration

On first launch after an update, the launcher calls `ensure_app_layout()` which:

1. Creates all folders above.
2. Moves legacy flat files (`config.json`, `config.ini`, `reticle_presets.json`, `macros.json`, `pad_macros.json`) from the app root into `configs/` or `recordings/`.
3. Merges `%LOCALAPPDATA%\SnappyAI\logs\` → `logs\`
4. Merges `%LOCALAPPDATA%\SnappyAI\models\` → `models\`
5. Deletes `%LOCALAPPDATA%\SnappyAI\authbrowser\` (disposable cache).

## DLL Search Order

`general_utils.dll_search_paths()` now checks:
1. `driver\` (for `interception.dll`, `ViGEmClient.dll`)
2. `main\` (for Python runtime + extension modules)
3. `System32`

The launcher prepends `main\` to `PATH` so Windows loader finds `python314.dll` there.

## Capturing Models

`hpose_detector.cache_dir()` returns `models\` by default (writable check). Falls back to `cache\models\`.

## Logs

`attach_app_log()` writes to `logs\snappyai.log`. Rotation keeps 7 generations.

## Lattool Outputs

- Reports → `reports\`
- Apply script → `scripts\exec\apply.ps1`
- Undo script → `scripts\undo\undo.ps1`
- Snapshot → `scripts\exec\snapshot.json` (referenced relatively from undo)
- Maintenance scripts → `scripts\exec\<kind>.ps1`
- Diagnostics → `reports\diagnose\`
- Chain measurement → `reports\chain.json`