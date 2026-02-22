import asyncio
import base64
import binascii
import os
import subprocess
import tempfile
import time
import warnings
from pathlib import Path

# Pin MedASR to GPU0 by default.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", os.environ.get("MEDASR_GPU", "0"))

import numpy as np
import torch
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from huggingface_hub import hf_hub_download
from transformers import pipeline

MODEL_ID = "google/medasr"
DEFAULT_CHUNK_LENGTH_S = float(os.environ.get("ASR_CHUNK_LENGTH_S", "20"))
DEFAULT_STRIDE_LENGTH_S = float(os.environ.get("ASR_STRIDE_LENGTH_S", "2"))
FALLBACK_SAMPLE_RATE = 16000
ASR_TIMEOUT_S = float(os.environ.get("ASR_TIMEOUT_S", "180"))
ASR_CONCURRENCY = max(1, int(os.environ.get("ASR_CONCURRENCY", "1")))

app = FastAPI(title="MedASR WebSocket Server")
ASR_SEM = asyncio.Semaphore(ASR_CONCURRENCY)

ASR_PIPE = None
CUDA_AVAILABLE = False
DEVICE_NAME = "cpu"


def load_asr_model() -> None:
    global ASR_PIPE, CUDA_AVAILABLE, DEVICE_NAME
    device = 0 if torch.cuda.is_available() else -1
    ASR_PIPE = pipeline("automatic-speech-recognition", model=MODEL_ID, device=device)
    CUDA_AVAILABLE = torch.cuda.is_available()
    DEVICE_NAME = torch.cuda.get_device_name(0) if CUDA_AVAILABLE else "cpu"


def _run_ffmpeg_decode(
    input_source: str, sample_rate: int, audio_bytes: bytes | None = None
) -> tuple[int, bytes, str]:
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-i",
        input_source,
        "-f",
        "f32le",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "pipe:1",
    ]
    proc = subprocess.run(
        cmd,
        input=audio_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    err = proc.stderr.decode("utf-8", errors="ignore").strip()
    return proc.returncode, proc.stdout, err


def _decode_with_ffmpeg(audio_bytes: bytes, sample_rate: int) -> tuple[np.ndarray, bool]:
    # Try stdin first for low overhead.
    rc, out, err = _run_ffmpeg_decode("pipe:0", sample_rate=sample_rate, audio_bytes=audio_bytes)
    audio = np.frombuffer(out, dtype=np.float32)
    if rc == 0 and audio.size > 0:
        return audio, False

    # Some containers (for example M4A/MP4 variants) decode unreliably from a pipe.
    # Retry from a temp file so ffmpeg can seek while probing.
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".audio", prefix="medasr_", delete=False
        ) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        rc_file, out_file, err_file = _run_ffmpeg_decode(
            tmp_path, sample_rate=sample_rate, audio_bytes=None
        )
        audio = np.frombuffer(out_file, dtype=np.float32)
        if rc_file != 0:
            detail = err_file or err or f"ffmpeg decode failed with code {rc_file}"
            raise RuntimeError(detail)
        if audio.size == 0:
            detail = err_file or err or "decoded audio is empty"
            raise RuntimeError(detail)
        return audio, True
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _transcribe_array(
    audio: np.ndarray, sample_rate: int, chunk_length_s: float, stride_length_s: float
) -> str:
    result = ASR_PIPE(
        {"array": audio, "sampling_rate": sample_rate},
        chunk_length_s=chunk_length_s,
        stride_length_s=stride_length_s,
    )
    return result.get("text", "").strip() if isinstance(result, dict) else str(result).strip()


def _transcribe_path(
    audio_path: str, chunk_length_s: float, stride_length_s: float
) -> tuple[str, bool]:
    path = Path(audio_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {path}")

    try:
        result = ASR_PIPE(
            str(path),
            chunk_length_s=chunk_length_s,
            stride_length_s=stride_length_s,
        )
        text = (
            result.get("text", "").strip() if isinstance(result, dict) else str(result).strip()
        )
        return text, False
    except Exception:
        import librosa

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            audio, sr = librosa.load(str(path), sr=FALLBACK_SAMPLE_RATE, mono=True)
        text = _transcribe_array(audio, sr, chunk_length_s, stride_length_s)
        return text, True


def asr_infer(
    audio_b64: str | None,
    audio_path: str | None,
    sample_rate: int,
    chunk_length_s: float,
    stride_length_s: float,
) -> tuple[str, dict]:
    if audio_b64:
        try:
            audio_bytes = base64.b64decode(audio_b64)
        except binascii.Error as exc:
            raise ValueError(f"invalid base64 audio payload: {exc}") from exc
        if sample_rate <= 0:
            raise ValueError(f"invalid sample_rate: {sample_rate}")
        audio, decode_fallback_used = _decode_with_ffmpeg(audio_bytes, sample_rate=sample_rate)
        text = _transcribe_array(
            audio,
            sample_rate=sample_rate,
            chunk_length_s=chunk_length_s,
            stride_length_s=stride_length_s,
        )
        return text, {"source": "audio_b64", "fallback_used": decode_fallback_used}

    if audio_path:
        text, fallback = _transcribe_path(audio_path, chunk_length_s, stride_length_s)
        return text, {"source": "audio_path", "fallback_used": fallback}

    raise ValueError("missing audio_b64 or audio_path")


@app.on_event("startup")
def _startup() -> None:
    load_asr_model()


@app.get("/health")
def health():
    return {
        "ok": True,
        "model_id": MODEL_ID,
        "model_loaded": ASR_PIPE is not None,
        "cuda_available": CUDA_AVAILABLE,
        "device": DEVICE_NAME,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "concurrency_limit": ASR_CONCURRENCY,
        "timeout_s": ASR_TIMEOUT_S,
    }


@app.websocket("/ws")
async def ws_asr(ws: WebSocket):
    await ws.accept()
    await ws.send_json(
        {
            "op": "ready",
            "model_id": MODEL_ID,
            "device": DEVICE_NAME,
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        }
    )

    try:
        while True:
            try:
                msg = await ws.receive_json()
            except Exception:
                await ws.send_json({"op": "error", "error": "expected JSON payload"})
                continue

            op = msg.get("op", "transcribe")
            if op == "ping":
                await ws.send_json({"op": "pong"})
                continue

            req_id = str(msg.get("request_id", ""))

            if op == "sample":
                try:
                    sample_path = hf_hub_download(MODEL_ID, filename="test_audio.wav")
                except Exception as exc:
                    await ws.send_json(
                        {
                            "op": "error",
                            "request_id": req_id,
                            "error": f"failed to fetch sample audio: {exc}",
                        }
                    )
                    continue
                msg = {
                    **msg,
                    "op": "transcribe",
                    "audio_path": sample_path,
                }
                op = "transcribe"

            if op != "transcribe":
                await ws.send_json(
                    {
                        "op": "error",
                        "request_id": req_id,
                        "error": f"unknown op: {op}",
                    }
                )
                continue

            sample_rate = int(msg.get("sample_rate", FALLBACK_SAMPLE_RATE))
            chunk_length_s = float(msg.get("chunk_length_s", DEFAULT_CHUNK_LENGTH_S))
            stride_length_s = float(msg.get("stride_length_s", DEFAULT_STRIDE_LENGTH_S))
            audio_b64 = msg.get("audio_b64")
            audio_path = msg.get("audio_path")

            t0 = time.time()
            try:
                async with ASR_SEM:
                    text, meta = await asyncio.wait_for(
                        asyncio.to_thread(
                            asr_infer,
                            audio_b64,
                            audio_path,
                            sample_rate,
                            chunk_length_s,
                            stride_length_s,
                        ),
                        timeout=ASR_TIMEOUT_S,
                    )
            except asyncio.TimeoutError:
                await ws.send_json(
                    {
                        "op": "error",
                        "request_id": req_id,
                        "error": f"inference timeout after {ASR_TIMEOUT_S}s",
                    }
                )
                continue
            except Exception as exc:
                await ws.send_json(
                    {
                        "op": "error",
                        "request_id": req_id,
                        "error": f"transcription failed: {exc}",
                    }
                )
                continue

            await ws.send_json(
                {
                    "op": "asr_result",
                    "request_id": req_id,
                    "text": text,
                    "latency_ms": int((time.time() - t0) * 1000),
                    **meta,
                }
            )
    except WebSocketDisconnect:
        return
