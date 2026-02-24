# EgoMedAgent: Agentic MedGemma Augmented Reality (AR)

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Unity](https://img.shields.io/badge/Unity-2022.3.21f1-000000?logo=unity&logoColor=white)](https://unity.com/)
[![Protocol](https://img.shields.io/badge/Protocol-WebSocket-0A7EA4)](#workflow-api-port-8003)
[![GPU](https://img.shields.io/badge/Runtime-CUDA-76B900?logo=nvidia&logoColor=white)](#runtime-and-dependencies)

> [!NOTE]
> This repository currently documents both backend and frontend. The backend workflow is the production-critical path, and the Unity section is included for integration reference.

## Quick Navigation

- [Overview](#overview)
- [Architecture](#architecture)
- [Runtime and Dependencies](#runtime-and-dependencies)
- [Start and Stop Backend](#start-and-stop-backend)
- [Workflow API (Port 8003)](#workflow-api-port-8003)
- [Memory and Context Behavior](#memory-and-context-behavior)
- [Image Handling (Gemma + Workflow)](#image-handling-gemma--workflow)
- [Persistence Layout](#persistence-layout)
- [Validation and Smoke Tests](#validation-and-smoke-tests)
- [Key Environment Variables](#key-environment-variables)
- [Operational Notes](#operational-notes)
- [Frontend (Unity)](#frontend-unity)

## Overview

Backend services for real-time clinical workflow:

- MedASR (speech-to-text)
- MedGemma (LLM generation, optional multimodal image input)
- Workflow orchestrator (session/memory/persistence + ASR -> Gemma pipeline)

This README reflects the **current active backend code path**:

- `server_asr.py`
- `server_gemma.py`
- `server_pipeline.py`

## Architecture

### Services

| Service | Script | Port | WS Endpoint | Health Endpoint | Model | Notes |
|---|---|---:|---|---|---|---|
| ASR | `server_asr.py` | `8001` | `ws://<host>:8001/ws` | `http://<host>:8001/health` | `google/medasr` | Speech-to-text |
| Gemma | `server_gemma.py` | `8002` | `ws://<host>:8002/ws` | `http://<host>:8002/health` | `google/medgemma-1.5-4b-it` | Supports optional `image_b64`, `image_mime` |
| Workflow (recommended client entrypoint) | `server_pipeline.py` | `8003` | `ws://<host>:8003/ws` | `http://<host>:8003/health` | Orchestrator | ASR -> Gemma, session memory, persistence |

### Active vs Legacy Files

Active runtime uses:

- `server_asr.py`
- `server_gemma.py`
- `server_pipeline.py`

Legacy/utility files (not used by startup scripts):

- `medasr_ws_server.py`
- `medgemma_ws_server.py`
- `medasr_chat.py`
- `medgemma_chat.py`

## Runtime and Dependencies

### Python envs

- ASR service runs in `miniforge3/envs/medasr`
- Gemma service runs in `miniforge3/envs/medgemma`
- Workflow service runs in `miniforge3/envs/medasr`

### Install dependencies

```bash
cd /srv/local/chenf3/Medgemm_AR_Glasses/backend
bash install_ws_server_deps.sh
```

Note:

- `ffmpeg` is required by ASR decoding and chunked smoke tests.

### GPU placement

Default GPU mapping from launch scripts:

- ASR: `MEDASR_GPU=0`
- Gemma: `MEDGEMMA_GPU=1`

## Start and Stop Backend

### tmux (recommended fallback)

```bash
cd /srv/local/chenf3/Medgemm_AR_Glasses/backend
bash start_model_ws_tmux.sh
bash status_model_ws_tmux.sh
```

Stop:

```bash
bash stop_model_ws_tmux.sh
```

### user systemd

```bash
cd /srv/local/chenf3/Medgemm_AR_Glasses/backend
bash install_systemd_model_ws_services.sh
```

### root systemd

```bash
cd /srv/local/chenf3/Medgemm_AR_Glasses/backend
bash install_systemd_root_model_ws_services.sh
```

### nohup scripts

```bash
bash start_model_ws_services.sh
bash status_model_ws_services.sh
bash stop_model_ws_services.sh
```

Logs are written under `.logs/` when using nohup scripts.

## Workflow API (Port 8003)

Client should connect to `ws://<host>:8003/ws` (or `wss://.../ws` behind TLS proxy/ngrok).

On connect, server sends:

```json
{
  "op": "ready",
  "service": "med-workflow",
  "result_fields": ["note_full", "advice_full", "summary_turn", "running_summary"],
  "stream_ops": ["audio_begin", "audio_chunk", "audio_end"]
}
```

### Session lifecycle

#### `start_session`

Request:

```json
{
  "op": "start_session",
  "request_id": "s1",
  "patient_id": "p_001_20260222_120000"
}
```

Response:

```json
{
  "op": "session_started",
  "request_id": "s1",
  "session_id": "....",
  "patient_id": "p_001_20260222_120000",
  "created": true,
  "turn_count": 0,
  "session_dir": ".../patients/<patient_id>/sessions/<session_id>",
  "running_summary": "..."
}
```

Notes:

- If `patient_id` is omitted, server auto-generates (`p_###_YYYYMMDD_HHMMSS`).
- Reusing the same `session_id` keeps same in-memory context.

#### `end_session`

Request:

```json
{
  "op": "end_session",
  "request_id": "e1",
  "session_id": "....",
  "include_transcript": false
}
```

Response:

```json
{
  "op": "session_summary",
  "request_id": "e1",
  "session_id": "....",
  "patient_id": "...",
  "turn_count": 3,
  "running_summary": "...",
  "note_full": "...",
  "advice_full": "...",
  "summary_turn": "...",
  "session_closed": true
}
```

### One-shot audio turn

#### `process_audio` (audio only)

```json
{
  "op": "process_audio",
  "request_id": "t1",
  "session_id": "....",
  "audio_b64": "<base64 file bytes>",
  "sample_rate": 16000
}
```

#### `process_audio` (audio + optional image)

```json
{
  "op": "process_audio",
  "request_id": "t2",
  "session_id": "....",
  "audio_b64": "<base64 file bytes>",
  "sample_rate": 16000,
  "image_b64": "<base64 image bytes>",
  "image_mime": "image/jpeg",
  "image_width": 1024,
  "image_height": 768
}
```

Successful response (`turn_result`) includes:

- `note_full`
- `advice_full`
- `summary_turn`
- `running_summary`
- `turn_index`, `latency` fields, etc.

Default returned fields are all of the above. You can also specify `return`.

### Chunked audio turn

#### `audio_begin`

```json
{
  "op": "audio_begin",
  "request_id": "b1",
  "session_id": "....",
  "turn_id": "turn_0001",
  "audio_format": "pcm_s16le",
  "sample_rate": 16000,
  "channels": 1,
  "chunk_ms": 500,
  "seq_start": 1
}
```

#### `audio_chunk`

```json
{
  "op": "audio_chunk",
  "request_id": "c1",
  "session_id": "....",
  "turn_id": "turn_0001",
  "seq": 1,
  "audio_b64": "<base64 pcm chunk>",
  "ack": true
}
```

#### `audio_end` (optional image supported)

```json
{
  "op": "audio_end",
  "request_id": "e1",
  "session_id": "....",
  "turn_id": "turn_0001",
  "image_b64": "<optional base64 image>",
  "image_mime": "image/png"
}
```

### On-demand fetch

#### `get_latest`

Request:

```json
{
  "op": "get_latest",
  "request_id": "g1",
  "session_id": "....",
  "what": "note_full"
}
```

`what` supports:

- `note_full`
- `advice_full`
- `summary_turn`
- `running_summary`
- `asr_text`

### Error format

Server returns:

```json
{
  "op": "error",
  "request_id": "...",
  "error": "...",
  "message": "..."
}
```

## Memory and Context Behavior

Per session:

- Each turn generates `summary_turn`.
- `running_summary` is updated by merging prior summary + current turn summary.
- Next turn prompt uses `running_summary + current ASR transcript`.

Important:

- Memory is scoped to `session_id`.
- Closing session (`end_session` default behavior) removes in-memory state for that session.
- Keep one `session_id` across multi-turn conversation for one patient.

## Image Handling (Gemma + Workflow)

When image is provided:

- Workflow validates/decode bounds (`WORKFLOW_IMAGE_MAX_BYTES`, default 8MB).
- Gemma service decodes and normalizes image to `896x896` via center-pad letterbox.
- Same prompt pipeline is used; image is optional context enhancer.
- Workflow persists image file as `turns/000N_image.<ext>`.

If image is omitted:

- behavior is identical to current audio-only flow.

## Persistence Layout

Default root:

```text
/srv/local/chenf3/patients
```

Configurable via `PATIENTS_ROOT` env.

Per session layout:

```text
patients/<patient_id>/sessions/<session_id>/
  session_meta.json
  running_summary.txt
  transcript.jsonl
  session_summary.json            # after summarize/end session
  turns/
    0001.json
    0001_image.jpg               # optional
    0002.json
```

## Validation and Smoke Tests

### One-shot workflow test

```bash
python3 scripts/workflow_smoke_test.py \
  --ws-url ws://127.0.0.1:8003/ws \
  --audio /srv/local/chenf3/medasr_test001.m4a \
  --verbose
```

With image:

```bash
python3 scripts/workflow_smoke_test.py \
  --ws-url ws://127.0.0.1:8003/ws \
  --audio /srv/local/chenf3/medasr_test001.m4a \
  --image /path/to/image.jpg
```

### Chunked workflow test

```bash
python3 scripts/workflow_chunked_smoke_test.py \
  --ws-url ws://127.0.0.1:8003/ws \
  --chunk-ms 500 \
  --verbose
```

With image:

```bash
python3 scripts/workflow_chunked_smoke_test.py \
  --ws-url ws://127.0.0.1:8003/ws \
  --chunk-ms 500 \
  --image /path/to/image.png
```

### Manual pipeline test client

```bash
python3 test_pipeline_ws.py /srv/local/chenf3/medasr_test001.m4a
```

## Key Environment Variables

Workflow:

- `ASR_WS_URL`
- `GEMMA_WS_URL`
- `WORKFLOW_TIMEOUT_S`
- `WORKFLOW_CONCURRENCY`
- `PATIENTS_ROOT`
- `WORKFLOW_IMAGE_MAX_BYTES`
- `WORKFLOW_MAX_SUMMARY_CHARS`
- `WORKFLOW_NOTE_MAX_NEW_TOKENS`
- `WORKFLOW_ADVICE_MAX_NEW_TOKENS`

ASR:

- `MEDASR_GPU`
- `ASR_TIMEOUT_S`
- `ASR_CONCURRENCY`
- `ASR_CHUNK_LENGTH_S`
- `ASR_STRIDE_LENGTH_S`

Gemma:

- `MEDGEMMA_GPU`
- `GEMMA_TIMEOUT_S`
- `GEMMA_CONCURRENCY`
- `GEMMA_IMAGE_SIZE`
- `GEMMA_IMAGE_MAX_BYTES`

## Operational Notes

1. Keep each service at one worker (`--workers 1`) unless you redesign shared session state.
2. If exposing externally (ngrok/public), use `wss://` and enforce auth/rate limits upstream.
3. If session continuity seems broken, verify client is not creating a new `session_id` per turn.
4. If chunked fails at start, check:
- `session_id` exists
- `turn_id` consistent across begin/chunk/end
- `seq` strictly increments as expected

---

## Frontend (Unity)

This section documents the Unity AR glasses client.

### Project scope

Frontend responsibilities:

- Capture wheel input and voice commands
- Record microphone audio
- Send workflow requests over WebSocket
- Render note/advice views and runtime status
- Optional photo capture and upload enhancement

Main Unity scripts:

- `Assets/Scripts/ArGlassesClinicalAssistant.cs`
- `Assets/Scripts/ArWorkflowWebSocket.cs`
- `Assets/Scripts/ArAudioRecorder.cs`
- `Assets/Scripts/ArWheelInputRouter.cs`
- `Assets/Scripts/ArGlassesUiPresenter.cs`
- `Assets/Scripts/ArPhotoCapture.cs`
- `Assets/Scripts/ArDigiOSVoiceUiBridge.cs`
- `Assets/Scripts/AndroidRuntimePermissions.cs`
- `Assets/Scripts/ArSimpleJson.cs`

Unity version:

- `2022.3.21f1`

Main scene:

- `Assets/Scenes/SampleScene.unity`

### Input mapping

Current mapping:

- Single press (`Return`): toggle view `NOTE <-> ADVICE`
- Long press (`Menu`): start/stop recording
- Scroll (`LeftArrow`/`RightArrow`): smooth body scroll
- Double click on ARGO (`Escape`): quit app

Quit behavior:

- Best-effort `end_session` (`close_session=true`)
- Then `Application.Quit()`

### Session and patient lifecycle

Client behavior:

1. On app launch, generate a fresh `patient_id` (default enabled)
2. Connect to workflow WS
3. Send `start_session` once per app run
4. Store returned `session_id` and reuse it for all turns in this app run
5. On reconnect, continue with the same `session_id`
6. On exit, send `end_session`, then quit
7. Reopen app => new `patient_id` => new patient/session on server

### Audio workflow

Default mode is chunked streaming:

1. Start recording -> `audio_begin`
2. During recording -> repeated `audio_chunk`
3. Stop recording -> flush + `audio_end`
4. Receive `turn_result`

Fallback:

- On chunk failure, client can fallback to one-shot `process_audio`

Requested result fields:

- `note_full`
- `advice_full`
- `summary_turn`
- `running_summary`

UI behavior:

- After each turn result, auto-show `note_full`
- Single press toggles to `advice_full` and back

### UI text fields

In `UiRoot`, the presenter controls:

- `header`: current mode (`NOTE`, `ADVICE`, `PHOTO`)
- `status`: runtime state (`Connecting`, `Recording`, `Processing`, errors)
- `connectivity`: `Online` / `Offline`
- `recording`: `REC mm:ss` with pulse animation

### Optional photo enhancement

Photo path is optional and non-breaking for audio-only usage.

Flow:

1. After turn result, prompt `Add photo?`
2. If no selection within 3s, auto-select `No`
3. If `Yes`, open live camera preview
4. Capture photo
5. Review page shows captured image with `Retake / Upload / Back`
6. Upload sends last-turn audio + image via `process_audio`

Optional image fields sent:

- `image_b64`
- `image_mime`
- `image_width`
- `image_height`

### Build and run (frontend)

1. Open Unity project root (`Assets/`, `Packages/`, `ProjectSettings/`)
2. Open `Assets/Scenes/SampleScene.unity`
3. Set workflow URL in `ArGlassesClinicalAssistant`
4. Build Android APK and install on glasses

Useful runtime log filter:

```bash
PKG="com.DefaultCompany.Medgemma_glasses"
PID="$(adb shell pidof "$PKG" | tr -d '\r' | awk '{print $1}')"
adb logcat -c
adb logcat --pid="$PID" -v time | grep -E "ArGlassesClinicalAssistant|ArWheelInputRouter|audio_begin|audio_chunk|audio_end|turn_result|session_started|session_summary|CAM|Photo"
```

### Frontend troubleshooting

1. Immediate `Processing failed` after recording starts:
- Ensure server supports `audio_begin/audio_chunk/audio_end`

2. Scroll appears not working:
- If content does not exceed viewport height, there is no overflow to scroll

3. Camera preview not visible:
- Check camera permission and `ArPhotoCapture` initialization

4. Server processed but glasses show no result:
- Verify client receives/parses `turn_result` and reads `note_full/advice_full`

5. Device cannot connect to backend:
- Use reachable LAN/WSS URL, not local loopback unless using `adb reverse`

### Repo structure note

This repository currently has a second Unity-like tree under `frontend/`.
The active Unity implementation is the root project (`Assets/...`, `Packages/...`, `ProjectSettings/...`).
