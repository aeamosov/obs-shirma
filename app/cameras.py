"""Список камер так, как их видит OBS: DirectShow на Windows, AVFoundation на macOS."""
import sys

if sys.platform == "darwin":
    from cameras_mac import Camera, list_cameras  # noqa: F401
else:
    from cameras_win import Camera, list_cameras  # noqa: F401

if __name__ == "__main__":
    for c in list_cameras():
        print(f"{c.name}\t{c.obs_id}")
