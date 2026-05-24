"""HTTP MJPEG server that streams camera frames to the Godot XR client.

Endpoints
---------
- ``GET /``                         – tiny landing page (helpful when debugging).
- ``GET /cameras``                  – JSON list of available camera names.
- ``GET /cameras/<name>/mjpeg``     – ``multipart/x-mixed-replace`` MJPEG stream.
- ``GET /cameras/<name>/snapshot``  – Single ``image/jpeg`` of the latest frame.
- ``GET /dataset_configured``       – ``{"configured": true|false}``.

Why MJPEG instead of WebRTC?
----------------------------
Godot 4 has no built-in WebRTC video track consumer for native (Android) builds
and shipping a GDExtension for it is heavy. MJPEG-over-HTTP is decoded with the
standard ``HTTPClient`` + ``Image.load_jpg_from_buffer`` in a few lines of GDScript
and Quest 3 handles 30 fps at 640×480 trivially.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import cv2
import numpy as np
from aiohttp import web

from vr_teleop.logs import log


class _LatestFrame:
    """Thread-safe holder for the latest JPEG-encoded camera frame."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jpeg: Optional[bytes] = None
        self._updated_at: float = 0.0

    def update(self, jpeg: bytes) -> None:
        with self._lock:
            self._jpeg = jpeg
            self._updated_at = time.monotonic()

    def get(self) -> tuple[Optional[bytes], float]:
        with self._lock:
            return self._jpeg, self._updated_at


class CameraServer:
    """HTTP server that serves multiple camera streams as MJPEG over HTTP."""

    BOUNDARY = "vrtelopmjpegboundary"

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        ssl_context: ssl.SSLContext | None = None,
        dataset_configured: bool = False,
        jpeg_quality: int = 80,
        target_fps: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.dataset_configured = dataset_configured
        self.jpeg_quality = int(np.clip(jpeg_quality, 1, 100))
        self.target_fps = max(1, int(target_fps))
        self.frame_period = 1.0 / self.target_fps

        self._frames: Dict[str, _LatestFrame] = {}
        self._app = web.Application()
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/cameras", self._list_cameras)
        self._app.router.add_get(
            "/cameras/{name}/mjpeg", self._stream_camera
        )
        self._app.router.add_get(
            "/cameras/{name}/snapshot", self._snapshot_camera
        )
        self._app.router.add_get(
            "/dataset_configured", self._dataset_configured
        )

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready_event = threading.Event()

        logging.basicConfig(level=logging.WARNING)
        self.logger = logging.getLogger("camera_server")

    # ── public API ────────────────────────────────────────────────────────────
    def add_camera(self, name: str) -> None:
        """Register a camera so it can be streamed."""
        if name not in self._frames:
            self._frames[name] = _LatestFrame()

    def update_camera_frame(self, name: str, frame: np.ndarray | None) -> None:
        """Push a new BGR frame for ``name``. No-op if the camera isn't known."""
        if frame is None or name not in self._frames:
            return
        # OpenCV frames are BGR; JPEG decoders (Godot, browsers) expect RGB.
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        ok, buf = cv2.imencode(".jpg", rgb, encode_params)
        if not ok:
            return
        self._frames[name].update(buf.tobytes())

    def cameras(self) -> list[str]:
        return list(self._frames.keys())

    def start_in_thread(self) -> None:
        """Start serving in a daemon thread, blocking until ready."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready_event.wait()

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(
            runner, self.host, self.port, ssl_context=self.ssl_context
        )
        await site.start()
        self._ready_event.set()
        # Run forever until cancelled.
        await asyncio.Event().wait()

    # ── handlers ──────────────────────────────────────────────────────────────
    async def _index(self, _request: web.Request) -> web.Response:
        names = "".join(
            f'<li><a href="/cameras/{n}/mjpeg">{n}</a></li>' for n in self.cameras()
        )
        html = (
            "<html><head><title>vr_teleop camera server</title></head>"
            "<body><h1>vr_teleop camera server</h1>"
            f"<h2>Cameras</h2><ul>{names or '<li>no cameras configured</li>'}</ul>"
            "</body></html>"
        )
        return web.Response(text=html, content_type="text/html")

    async def _list_cameras(self, _request: web.Request) -> web.Response:
        return web.json_response(self.cameras())

    async def _dataset_configured(self, _request: web.Request) -> web.Response:
        return web.json_response({"configured": bool(self.dataset_configured)})

    async def _snapshot_camera(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in self._frames:
            return web.Response(status=404, text=f"unknown camera: {name}")
        jpeg, _ts = self._frames[name].get()
        if jpeg is None:
            return web.Response(status=503, text=f"no frame yet for {name}")
        return web.Response(body=jpeg, content_type="image/jpeg")

    async def _stream_camera(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if name not in self._frames:
            return web.Response(status=404, text=f"unknown camera: {name}")

        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": (
                    f"multipart/x-mixed-replace; boundary={self.BOUNDARY}"
                ),
                "Cache-Control": "no-cache, private",
                "Pragma": "no-cache",
                "Connection": "close",
            },
        )
        await response.prepare(request)

        last_ts = 0.0
        try:
            while True:
                jpeg, ts = self._frames[name].get()
                if jpeg is not None and ts != last_ts:
                    last_ts = ts
                    chunk = (
                        f"--{self.BOUNDARY}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n"
                    ).encode("ascii") + jpeg + b"\r\n"
                    await response.write(chunk)
                await asyncio.sleep(self.frame_period)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response


def _create_ssl_context(cert_file: Path, key_file: Path) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert_file, key_file)
    return ssl_context_with_warning(ctx)


def ssl_context_with_warning(ctx: ssl.SSLContext) -> ssl.SSLContext:
    # Helper to make it easy to chain configuration later if needed.
    return ctx


def setup_camera_server(
    robot,
    settings,
    logger,
    cert_file: str | Path | None = "ssl_cert/server.crt",
    key_file: str | Path | None = "ssl_cert/server.key",
) -> CameraServer:
    """Build, register all cameras on the robot, and start the camera server."""
    ssl_context: ssl.SSLContext | None = None
    if cert_file and key_file:
        cert_path = Path(cert_file)
        key_path = Path(key_file)
        if cert_path.exists() and key_path.exists():
            ssl_context = _create_ssl_context(cert_path, key_path)
            log(f"🔒 HTTPS enabled with {cert_path}", logger)
        else:
            log(
                f"⚠️  SSL certs not found ({cert_path}, {key_path}) — "
                "camera server will run on plain HTTP.",
                logger,
            )

    server = CameraServer(
        host=settings.camera_server.host,
        port=settings.camera_server.port,
        ssl_context=ssl_context,
        dataset_configured=settings.dataset is not None,
        jpeg_quality=settings.camera_server.jpeg_quality,
        target_fps=settings.camera_server.fps,
    )

    for cam_name in getattr(robot, "cameras", {}) or {}:
        server.add_camera(cam_name)

    server.start_in_thread()
    proto = "https" if ssl_context else "http"
    log(
        f"🎥 Camera server listening on {proto}://{settings.camera_server.host}:"
        f"{settings.camera_server.port}",
        logger,
    )
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
        log(
            f"   Quest URL: {proto}://{local_ip}:{settings.camera_server.port}/cameras",
            logger,
        )
    except Exception:
        pass
    return server
