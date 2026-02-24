# Medgemm AR Glasses Backend

Backend services for real-time clinical workflow:

- MedASR (speech-to-text)
- MedGemma (LLM generation, optional multimodal image input)
- Workflow orchestrator (session/memory/persistence + ASR -> Gemma pipeline)

This README reflects the **current active backend code path**:

- `server_asr.py`
- `server_gemma.py`
- `server_pipeline.py`

## 1) Architecture

### Services

1. ASR service (`server_asr.py`)
- Port: `8001`
- WS endpoint: `ws://<host>:8001/ws`
- Health: `http://<host>:8001/health`
- Model: `google/medasr`

2. Gemma service (`server_gemma.py`)
- Port: `8002`
- WS endpoint: `ws://<host>:8002/ws`
- Health: `http://<host>:8002/health`
- Model: `google/medgemma-1.5-4b-it`
- Optional image input (`image_b64`, `image_mime`)

3. Workflow service (`server_pipeline.py`) (recommended client entrypoint)
- Port: `8003`
- WS endpoint: `ws://<host>:8003/ws`
- Health: `http://<host>:8003/health`
- Orchestrates ASR -> Gemma
- Manages session memory and persistence

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

## 2) Runtime and Dependencies

## 2.1 Python envs

- ASR service runs in `miniforge3/envs/medasr`
- Gemma service runs in `miniforge3/envs/medgemma`
- Workflow service runs in `miniforge3/envs/medasr`

## 2.2 Install dependencies

```bash
cd /srv/local/chenf3/Medgemm_AR_Glasses/backend
bash install_ws_server_deps.sh
```

Note:
- `ffmpeg` is required by ASR decoding and chunked smoke tests.

## 2.3 GPU placement

Default GPU mapping from launch scripts:
- ASR: `MEDASR_GPU=0`
- Gemma: `MEDGEMMA_GPU=1`

## 3) Start/Stop Backend

## 3.1 tmux (recommended fallback)

```bash
cd /srv/local/chenf3/Medgemm_AR_Glasses/backend
bash start_model_ws_tmux.sh
bash status_model_ws_tmux.sh
```

Stop:

```bash
bash stop_model_ws_tmux.sh
```

## 3.2 user systemd

```bash
cd /srv/local/chenf3/Medgemm_AR_Glasses/backend
bash install_systemd_model_ws_services.sh
```

## 3.3 root systemd

```bash
cd /srv/local/chenf3/Medgemm_AR_Glasses/backend
bash install_systemd_root_model_ws_services.sh
```

## 3.4 nohup scripts

```bash
bash start_model_ws_services.sh
bash status_model_ws_services.sh
bash stop_model_ws_services.sh
```

Logs are written under `.logs/` when using nohup scripts.

## 4) Workflow API (Port 8003)

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

## 4.1 Session lifecycle

### `start_session`

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

### `end_session`

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

## 4.2 One-shot audio turn

### `process_audio` (audio only)

```json
{
  "op": "process_audio",
  "request_id": "t1",
  "session_id": "....",
  "audio_b64": "<base64 file bytes>",
  "sample_rate": 16000
}
```

### `process_audio` (audio + optional image)

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

## 4.3 Chunked audio turn

### `audio_begin`

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

### `audio_chunk`

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

### `audio_end` (optional image supported)

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

## 4.4 On-demand fetch

### `get_latest`

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

## 4.5 Error format

Server returns:

```json
{
  "op": "error",
  "request_id": "...",
  "error": "...",
  "message": "..."
}
```

## 5) Memory and Context Behavior

Per session:
- Each turn generates `summary_turn`.
- `running_summary` is updated by merging prior summary + current turn summary.
- Next turn prompt uses `running_summary + current ASR transcript`.

Important:
- Memory is scoped to `session_id`.
- Closing session (`end_session` default behavior) removes in-memory state for that session.
- Keep one `session_id` across multi-turn conversation for one patient.

## 6) Image Handling (Gemma + Workflow)

When image is provided:
- Workflow validates/decode bounds (`WORKFLOW_IMAGE_MAX_BYTES`, default 8MB).
- Gemma service decodes and normalizes image to `896x896` via center-pad letterbox.
- Same prompt pipeline is used; image is optional context enhancer.
- Workflow persists image file as `turns/000N_image.<ext>`.

If image is omitted:
- behavior is identical to current audio-only flow.

## 7) Persistence Layout

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

## 8) Validation / Smoke Tests

## 8.1 One-shot workflow test

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

## 8.2 Chunked workflow test

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

## 8.3 Manual pipeline test client

```bash
python3 test_pipeline_ws.py /srv/local/chenf3/medasr_test001.m4a
```

## 9) Key Environment Variables

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

## 10) Operational Notes

1. Keep each service at one worker (`--workers 1`) unless you redesign shared session state.
2. If exposing externally (ngrok/public), use `wss://` and enforce auth/rate limits upstream.
3. If session continuity seems broken, verify client is not creating a new `session_id` per turn.
4. If chunked fails at start, check:
- `session_id` exists
- `turn_id` consistent across begin/chunk/end
- `seq` strictly increments as expected
