# vr_teleop

Server side of the Godot/Meta Quest 3 VR teleoperation app. Takes 6-DoF
controller poses over WebSocket, runs them through an IK pipeline, drives an
SO-101 arm, optionally records episodes to a LeRobot dataset, and streams the
robot's cameras back to the headset via MJPEG.

## Install

Install [`uv`](https://docs.astral.sh/uv/) if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then from the project root:

```bash
uv sync
```

## Configure

Pick a template under `conf/` and edit it to match your hardware:

| Setup | File |
|---|---|
| Single arm | `conf/arm.yaml` |
| Dual arms (left + right) | `conf/arm.dual.yaml` |

Key fields:

- `arms.<side>.port` — serial device for the Feetech bus (e.g. `/dev/ttyACM0`).
- `arms.<side>.regularization` — IK smoothness (higher = smoother, less responsive).
- `arms.<side>.end_effector_step_sizes` — meters of arm travel per meter of controller travel.
- `transport.port` — WebSocket port the Godot client streams FramePackets to.
- `camera_server.port` — HTTP/MJPEG port the Godot client pulls camera streams from.
- `cameras.<name>` — declared cameras (with OpenCV index + resolution + fps).
- `arms.<side>.cameras` — which declared cameras are mounted on each arm.
- `dataset` — set `repo_id`, `single_task`, `push_to_hub` to record LeRobot datasets.

## Optional: HTTPS for the camera server

Quest 3's secure XR contexts work fine with `ws://` and `http://`, but if you
need TLS (e.g. some browser-based tooling alongside the Quest), generate a
self-signed cert and the camera server will auto-pick it up:

```bash
mkdir -p ssl_cert && openssl req -x509 -newkey rsa:4096 \
  -keyout ssl_cert/server.key -out ssl_cert/server.crt \
  -days 365 -nodes -subj '/CN=localhost'
```

## Run

```bash
uv run python scripts/run_teleop.py
# or, via the installed console-script:
uv run vr-teleop -c conf/arm.dual.yaml
```

## Wire format

The Godot app sends one JSON FramePacket per tick over the WebSocket:

```json
{
  "action": "none",
  "left":  { "pos": [x, y, z], "rot": [x, y, z, w], "joystickX": 0.0, "enabled": false },
  "right": { "pos": [x, y, z], "rot": [x, y, z, w], "joystickX": 0.0, "enabled": false }
}
```

- `pos` / `rot` use Godot convention (Y-up, right-handed). `vr_teleop` remaps
  them into the robot's base frame.
- `enabled = true` while the grip button is held (the arm only follows when enabled).
- `joystickX` is mapped to gripper open/close velocity.
- `action` is one of:
  - `"none"` — normal teleop (the default each tick).
  - `"reset"` — return the arm to its initial pose.
  - `"start_episode"` — begin recording (requires `dataset` configured).
  - `"stop_episode"` — finalize the current episode.
  - `"save_dataset"` — flush everything to disk / push to Hub.

## Camera HTTP endpoints

The MJPEG server (`camera_server.port`, default `8765`) exposes:

| Endpoint | Description |
|---|---|
| `GET /` | Tiny landing page listing the cameras. |
| `GET /cameras` | JSON array of camera names. |
| `GET /cameras/<name>/mjpeg` | `multipart/x-mixed-replace` MJPEG stream. |
| `GET /cameras/<name>/snapshot` | Single `image/jpeg` of the latest frame. |
| `GET /dataset_configured` | `{"configured": true|false}` — drives the dataset UI gating. |

## Dataset recording

If `dataset.repo_id` and `dataset.single_task` are set, recording is gated on
the action commands from the Quest:

1. Press **Record Episode** → countdown → `start_episode`.
2. Perform the task.
3. Press **Save Episode** → `stop_episode` (the arm also resets here).
4. Repeat for as many episodes as you need.
5. Press **Save Dataset** → `save_dataset` (finalize + optional Hub push).

## Delete episodes from a dataset

```bash
uv run vr-teleop delete-episodes \
  --repo-id <YOUR_HF_USERNAME>/vr_test_dual_arm \
  --episodes "[0, 2, 5]" \
  --push-to-hub
```

## Layout

```
conf/                 YAML configs
assets/so101/         URDF + meshes for the kinematic solver
scripts/              Runnable entry-points
vr_teleop/
  transport/          WebSocket server (Quest frame intake)
  server/             HTTP MJPEG camera server
  stages/             Pipeline stages: remap → ref/delta → bounds → gripper → IK
  arm/                URDF-backed IK solver + hardware passthrough
  dataset/            LeRobotDataset record / save / push / delete helpers
  runtime/            Per-frame session dispatch + main loop
  settings.py         YAML loader → dataclasses + Robot
  logs.py             Logging helpers
```

## Calibration

The first time you connect, follow the prompts in the terminal to calibrate
each joint (mid → max → min). Calibration values are persisted by LeRobot and
re-used on subsequent runs.
