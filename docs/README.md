# SnappyAI

Active mouse assistance for competitive gaming.

## Quick Start

1. Run `SnappyAI.exe` (the launcher).
2. Sign in with Discord when prompted.
3. Configure your settings in the **Settings** tab.
4. Use the **Latency** tab to run scans and apply optimizations.

## Folder Layout

```
SnappyAI/
├── SnappyAI.exe          # Launcher (~24 KB)
├── main/                 # Real application + runtime
│   ├── SnappyAI.exe
│   ├── python314.dll
│   ├── *.pyd
│   ├── cv2/, numpy/, onnxruntime/, etc.
│   └── ...
├── configs/              # User settings
│   ├── config.json
│   ├── config.ini
│   └── reticle_presets.json
├── recordings/           # Macro recordings
│   ├── macros.json
│   └── pad_macros.json
├── models/               # ONNX pose models
├── capture/              # Screen capture backends
│   ├── dxcam/
│   ├── windows_capture/
│   └── interception.dll
├── driver/               # Input drivers
│   ├── ViGEmClient.dll
│   └── interception.dll
├── docs/                 # Documentation
├── logs/                 # Application logs
├── reports/              # Lattool reports (HTML/Markdown)
├── scripts/
│   ├── exec/             # Apply/maintenance scripts
│   └── undo/             # Undo scripts
└── cache/                # Temporary files (browser profiles, etc.)
```

## Configuration

Edit `configs/config.json` or `configs/config.ini` (INI overrides JSON).

## Session Data

Only `session.json` (encrypted credentials) lives in `%LOCALAPPDATA%\SnappyAI\`. Everything else stays in the app folder.

## Building from Source

```cmd
build.bat
```

Requires Python 3.14+, Nuitka, and Visual Studio Build Tools.

## License

Proprietary. See LICENSE.txt.