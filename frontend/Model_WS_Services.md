# Model WebSocket Services

This workspace provides three websocket services:

- `server_asr.py` on `ws://127.0.0.1:8001/ws` (MedASR on GPU 0)
- `server_gemma.py` on `ws://127.0.0.1:8002/ws` (MedGemma on GPU 1)
- `server_pipeline.py` on `ws://127.0.0.1:8003/ws` (server-side workflow: ASR -> Gemma)

`server_pipeline.py` is the backend workflow entrypoint for:

- doctor starts a patient session
- each audio turn is transcribed by ASR then summarized by Gemma
- session end returns a full conversation summary for documentation

## Install Dependencies

```bash
cd /srv/local/chenf3
bash install_ws_server_deps.sh
```

## Option A: systemd user service (preferred if available)

```bash
cd /srv/local/chenf3
bash install_systemd_model_ws_services.sh
```

Useful commands:

```bash
systemctl --user status medasr-ws.service
systemctl --user status medgemma-ws.service
systemctl --user status medworkflow-ws.service
systemctl --user restart medasr-ws.service
systemctl --user restart medgemma-ws.service
systemctl --user restart medworkflow-ws.service
journalctl --user -u medasr-ws.service -f
journalctl --user -u medgemma-ws.service -f
journalctl --user -u medworkflow-ws.service -f
```

If user bus is unavailable, use one of the fallbacks below.

## Option B: systemd system service (requires sudo)

```bash
cd /srv/local/chenf3
bash install_systemd_root_model_ws_services.sh
```

Then:

```bash
sudo systemctl status medasr.service
sudo systemctl status medgemma.service
sudo systemctl status medworkflow.service
```

## Option C: tmux fallback (no sudo, no user-systemd bus)

```bash
cd /srv/local/chenf3
bash start_model_ws_tmux.sh
bash status_model_ws_tmux.sh
```

`start_model_ws_tmux.sh` wraps each uvicorn process in a restart loop, so crashes are auto-restarted.

Stop:

```bash
bash stop_model_ws_tmux.sh
```

## Background Script Controls

You can still use the nohup helpers:

```bash
bash start_model_ws_services.sh
bash status_model_ws_services.sh
bash stop_model_ws_services.sh
```

Logs are written to:

- `/srv/local/chenf3/.logs/medasr_ws.log`
- `/srv/local/chenf3/.logs/medgemma_ws.log`
- `/srv/local/chenf3/.logs/medworkflow_ws.log`

## WebSocket Contract

### Workflow Service (`ws://<server-ip>:8003/ws`) - recommended client entrypoint

Standard result fields:

- `note_short`
- `note_full`
- `advice_short`
- `advice_full`

`op="start_session"` request:

```json
{
  "op": "start_session",
  "request_id": "s1",
  "patient_id": "p_chen_001"
}
```

Response:

```json
{
  "op": "session_started",
  "request_id": "s1",
  "session_id": "generated-session-id",
  "patient_id": "p_chen_001",
  "created": true,
  "turn_count": 0,
  "session_dir": "/srv/local/chenf3/patients/p_chen_001/sessions/..."
}
```

`op="process_audio"` request (`audio_b64` or `audio_path`).  
By default, response returns only `note_short`.

```json
{
  "op": "process_audio",
  "request_id": "t1",
  "session_id": "generated-session-id",
  "audio_b64": "<base64 audio bytes>",
  "sample_rate": 16000,
  "return": ["note_short"]
}
```

Response:

```json
{
  "op": "turn_result",
  "request_id": "t1",
  "session_id": "generated-session-id",
  "turn_index": 1,
  "returned": ["note_short"],
  "note_short": "..."
}
```

On-demand fetch for UI buttons:

`op="get_latest"` request:

```json
{
  "op": "get_latest",
  "request_id": "g1",
  "session_id": "generated-session-id",
  "what": "advice_full"
}
```

Response:

```json
{
  "op": "get_result",
  "request_id": "g1",
  "session_id": "generated-session-id",
  "turn_index": 1,
  "what": "advice_full",
  "data": {
    "top_differentials": [],
    "recommended_questions": []
  }
}
```

`op="end_session"` request:

```json
{
  "op": "end_session",
  "request_id": "e1",
  "session_id": "generated-session-id",
  "include_transcript": true
}
```

Response:

```json
{
  "op": "session_summary",
  "request_id": "e1",
  "session_id": "generated-session-id",
  "turn_count": 3,
  "note_short": "...",
  "note_full": {},
  "advice_short": "...",
  "advice_full": {},
  "text": "...",
  "session_closed": true
}
```

### MedASR (`ws://<server-ip>:8001/ws`)

Request:

```json
{
  "op": "transcribe",
  "request_id": "abc-1",
  "audio_b64": "<base64-encoded audio file bytes>",
  "sample_rate": 16000
}
```

You can also send a local file path:

```json
{
  "op": "transcribe",
  "request_id": "abc-2",
  "audio_path": "/absolute/path/to/audio.m4a"
}
```

Response:

```json
{
  "op": "asr_result",
  "request_id": "abc-1",
  "text": "...",
  "latency_ms": 1234
}
```

### MedGemma (`ws://<server-ip>:8002/ws`)

Request:

```json
{
  "op": "generate",
  "request_id": "xyz-1",
  "prompt": "Summarize this finding",
  "max_new_tokens": 256
}
```

Response:

```json
{
  "op": "gemma_result",
  "request_id": "xyz-1",
  "text": "...",
  "latency_ms": 987
}
```
