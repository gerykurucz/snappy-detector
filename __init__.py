"""hpose - human pose, view and motion analysis.

A self-contained stack that turns raw pixels into answers about the
people in a frame:

* :mod:`hpose.detector` - YOLO-pose person detection + YuNet faces
* :mod:`hpose.posefit` - geometric body fit: how far the body is turned
* :mod:`hpose.features` - keypoints to feature vectors
* :mod:`hpose.view` - which way the body faces (front, back, side, 45)
* :mod:`hpose.stance` - standing, walking, crouching, prone or jumping
* :mod:`hpose.tracker` - multi-object tracking with temporal smoothing
* :mod:`hpose.motion` - velocity and lead prediction for moving targets
* :mod:`hpose.analyzer` - the high level API that ties it together
* :mod:`hpose.synth` - the synthetic body generator used for training
* :mod:`hpose.train` - training the heads
"""

__version__ = "1.0.0"

__all__ = [
    "analyzer", "bundle", "classifier", "diagnose", "evaluate",
    "features", "heuristics", "keypoints", "makecoco", "model3d",
    "motion", "posefit", "render", "selftest", "stance", "synth",
    "train", "tracker", "validate_coco", "view",
]
