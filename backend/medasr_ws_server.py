import argparse
import asyncio
import os
import warnings
from pathlib import Path

# Keep MedASR on the second physical GPU by default.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", os.environ.get("MEDASR_GPU", "1"))

import torch
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from huggingface_hub import hf_hub_download
from transformers import pipeline

MODEL_ID = "google/medasr"
DEFAULT_CHUNK_LENGTH_S = 20.0
DEFAULT_STRIDE_LENGTH_S = 2.0
FALLBACK_SAMPLE_RATE = 16000


def load_runtime():
    cuda_available = torch.cuda.is_available()
    device = 0 if cuda_available else -1
    asr_pipe = pipeline("automatic-speech-recognition", model=MODEL_ID, device=device)
    device_name = torch.cuda.get_device_name(0) if cuda_available else "cpu"
    return asr_pipe, cuda_available, device_name


ASR_PIPE, CUDA_AVAILABLE, DEVICE_NAME = load_runtime()
INFER_LOCK = asyncio.Lock()
app = FastAPI(title="MedASR WebSocket Server")


def transcribe_path(audio_path: str, chunk_length_s: float, stride_length_s: float):
    path = Path(audio_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {path}")

    fallback_used = False
    try:
        result = ASR_PIPE(
            str(path),
            chunk_length_s=chunk_length_s,
            stride_length_s=stride_length_s,
        )
    except Exception:
        import librosa

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            audio, sample_rate = librosa.load(
                str(path), sr=FALLBACK_SAMPLE_RATE, mono=True
            )
        result = ASR_PIPE(
            {"array": audio, "sampling_rate": sample_rate},
            chunk_length_s=chunk_length_s,
            stride_length_s=stride_length_s,
        )
        fallback_used = True

    text = result.get("text", "").strip() if isinstance(result, dict) else str(result).strip()
    return text, fallback_used


@app.get("/health")
def health():
    return {
        "ok": True,
        "model_id": MODEL_ID,
        "cuda_available": CUDA_AVAILABLE,
        "device": DEVICE_NAME,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json(
        {
            "type": "ready",
            "model_id": MODEL_ID,
            "device": DEVICE_NAME,
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        }
    )
    try:
        while True:
            try:
                payload = await websocket.receive_json()
            except Exception:
                await websocket.send_json(
                    {"type": "error", "error": "Expected JSON payload."}
                )
                continue

            op = payload.get("op", "transcribe")
            if op == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if op == "sample":
                try:
                    sample_path = hf_hub_download(MODEL_ID, filename="test_audio.wav")
                    audio_path = sample_path
                except Exception as exc:
                    await websocket.send_json(
                        {"type": "error", "error": f"Could not fetch sample: {exc}"}
                    )
                    continue
            elif op == "transcribe":
                audio_path = str(payload.get("audio_path", "")).strip()
                if not audio_path:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "error": "Field 'audio_path' is required for op='transcribe'.",
                        }
                    )
                    continue
            else:
                await websocket.send_json(
                    {"type": "error", "error": f"Unsupported op: {op}"}
                )
                continue

            chunk_length_s = float(payload.get("chunk_length_s", DEFAULT_CHUNK_LENGTH_S))
            stride_length_s = float(payload.get("stride_length_s", DEFAULT_STRIDE_LENGTH_S))

            async with INFER_LOCK:
                try:
                    text, fallback_used = await asyncio.to_thread(
                        transcribe_path, audio_path, chunk_length_s, stride_length_s
                    )
                except Exception as exc:
                    await websocket.send_json(
                        {"type": "error", "error": f"Transcription failed: {exc}"}
                    )
                    continue

            await websocket.send_json(
                {
                    "type": "result",
                    "text": text,
                    "audio_path": audio_path,
                    "fallback_used": fallback_used,
                }
            )
    except WebSocketDisconnect:
        return


def parse_args():
    parser = argparse.ArgumentParser(description="MedASR websocket server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8002, type=int)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
