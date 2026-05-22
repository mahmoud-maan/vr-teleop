"""WebSocket server that receives FramePackets from the Godot XR app.

Runs an asyncio WebSocket loop in a daemon thread; the main thread polls
`last_frame` whenever it wants the most recent controller state. This means
the teleop loop can run at its own cadence (e.g. 30 Hz) independent of how
fast the Quest is pushing frames.
"""

import asyncio
import json
import threading

import websockets


class QuestSocket:
    """Background-thread WebSocket server.

    Exposes the latest received frame as `last_frame`. Frames are JSON
    `FramePacket`s of the shape::

        {
          "action": "none" | "reset",
          "left":   { "pos": [x, y, z], "rot": [x, y, z, w], "joystickX": f, "enabled": b } | null,
          "right":  { ... }                                                                | null,
        }
    """

    name = "quest_socket"

    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        self.connected = False
        self.last_frame: dict | None = None
        self._server: websockets.WebSocketServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready_event = threading.Event()

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def _on_frame(self, websocket):
        try:
            async for message in websocket:
                self.last_frame = json.loads(message)
        except websockets.ConnectionClosedOK:
            print("🔌 Quest disconnected normally.")
        except websockets.ConnectionClosedError as e:
            print(f"⚠️ Quest connection closed with error: {e}")
        except Exception as e:
            print(f"❌ Unexpected error while receiving frame: {e}")

    async def _serve(self):
        print(f"🌐 Quest socket listening on ws://{self.host}:{self.port}")
        self._server = await websockets.serve(self._on_frame, self.host, self.port)
        self._ready_event.set()
        await self._server.wait_closed()

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    def connect(self) -> None:
        """Start the server in a daemon thread; blocks until it's accepting."""
        print("🚀 Starting quest socket thread...")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready_event.wait()
        self.connected = True
        print(f"✅ Quest socket ready on ws://{self.host}:{self.port}")

    def disconnect(self) -> None:
        if self._server and self._loop and self._loop.is_running():
            print("🛑 Shutting down quest socket...")
            self._loop.call_soon_threadsafe(self._server.close)
        self.connected = False


def open_quest_socket(host: str = "0.0.0.0", port: int = 8080) -> QuestSocket:
    """Create, start, and return a `QuestSocket` ready to receive frames."""
    socket = QuestSocket(host=host, port=port)
    socket.connect()
    return socket
