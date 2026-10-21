# Model weights

These two files are the only things hpose loads from disk.

| file | size | what it does |
|---|---|---|
| `yolo11n-pose.onnx` | 12 MB | finds people and their 17 body joints |
| `face_detection_yunet_2023mar.onnx` | 0.2 MB | finds faces - the answer to "is that body turned towards the camera?" |

They are resolved relative to this folder, so a copy of the project
works anywhere.  If one is missing it is downloaded into this folder
the first time it is needed, which makes a partial copy self-healing.

The trained heads live one folder up, in `hpose/hpose_bundle.npz`.

`yolo11s-pose.onnx` is a better but slower alternative; download it
into this folder to use it:

    curl -L -o yolo11s-pose.onnx \
      https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11s-pose.onnx

Then pick it in the app: *Experimental* -> *Person detector*.
