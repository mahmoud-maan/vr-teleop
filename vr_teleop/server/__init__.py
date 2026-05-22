"""HTTP servers used by the Godot XR client.

`camera_server` exposes camera feeds over `multipart/x-mixed-replace` (MJPEG)
so the Godot client can consume them with a plain HTTPClient + Image decode —
no WebRTC GDExtension required.
"""

from vr_teleop.server.camera_server import (
    CameraServer,
    setup_camera_server,
)

__all__ = ["CameraServer", "setup_camera_server"]
