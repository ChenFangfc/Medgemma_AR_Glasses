import asyncio
import base64
import binascii
import io
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

# Pin MedGemma to GPU1 by default.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", os.environ.get("MEDGEMMA_GPU", "1"))

import torch
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from PIL import Image, ImageOps, UnidentifiedImageError
from transformers import AutoModelForImageTextToText, AutoProcessor

MODEL_ID = "google/medgemma-1.5-4b-it"
DEFAULT_MAX_NEW_TOKENS = 256
GEMMA_TIMEOUT_S = float(os.environ.get("GEMMA_TIMEOUT_S", "180"))
GEMMA_CONCURRENCY = max(1, int(os.environ.get("GEMMA_CONCURRENCY", "1")))
GEMMA_IMAGE_SIZE = int(os.environ.get("GEMMA_IMAGE_SIZE", "896"))
GEMMA_IMAGE_MAX_BYTES = int(
    os.environ.get("GEMMA_IMAGE_MAX_BYTES", str(8 * 1024 * 1024))
)
RESAMPLE_BICUBIC = (
    Image.Resampling.BICUBIC if hasattr(Image, "Resampling") else Image.BICUBIC
)

app = FastAPI(title="MedGemma WebSocket Server")
GEMMA_SEM = asyncio.Semaphore(GEMMA_CONCURRENCY)
LOGGER = logging.getLogger("medgemma_ws")
if not LOGGER.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
LOGGER.setLevel(logging.INFO)

GEMMA_MODEL = None
GEMMA_PROC = None
MODEL_DTYPE = torch.float32
CUDA_AVAILABLE = False
DEVICE_NAME = "cpu"


def _clean_output(text: str) -> str:
    text = re.sub(r"<unused\d+>\s*", "", text)
    text = re.sub(r"^\s*thought\s*\n", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"^Here'?s a thinking process.*?\n\n",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return text.strip()


def load_gemma_model() -> None:
    global GEMMA_MODEL, GEMMA_PROC, MODEL_DTYPE, CUDA_AVAILABLE, DEVICE_NAME

    CUDA_AVAILABLE = torch.cuda.is_available()
    MODEL_DTYPE = (
        torch.bfloat16
        if CUDA_AVAILABLE and torch.cuda.is_bf16_supported()
        else (torch.float16 if CUDA_AVAILABLE else torch.float32)
    )
    GEMMA_MODEL = AutoModelForImageTextToText.from_pretrained(
        MODEL_ID,
        torch_dtype=MODEL_DTYPE,
        device_map="auto" if CUDA_AVAILABLE else None,
    )
    if not CUDA_AVAILABLE:
        GEMMA_MODEL = GEMMA_MODEL.to("cpu")
    GEMMA_PROC = AutoProcessor.from_pretrained(MODEL_ID)
    DEVICE_NAME = torch.cuda.get_device_name(0) if CUDA_AVAILABLE else "cpu"


def _decode_image_b64(image_b64: str) -> tuple[bytes, str | None]:
    raw = str(image_b64 or "").strip()
    data_uri_mime: str | None = None
    if raw.startswith("data:") and "," in raw:
        header, payload = raw.split(",", 1)
        mime_match = re.match(r"^data:([^;]+);base64$", header.strip(), flags=re.IGNORECASE)
        if mime_match:
            data_uri_mime = mime_match.group(1).strip().lower()
        raw = payload
    raw = re.sub(r"\s+", "", raw)
    if not raw:
        raise ValueError("image_b64 is empty")
    try:
        image_bytes = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64 in image_b64") from exc
    if not image_bytes:
        raise ValueError("image_b64 decoded to empty bytes")
    if len(image_bytes) > GEMMA_IMAGE_MAX_BYTES:
        raise ValueError(
            f"image exceeds max bytes ({GEMMA_IMAGE_MAX_BYTES})"
        )
    return image_bytes, data_uri_mime


def _normalize_image(image: Image.Image) -> Image.Image:
    rgb = image.convert("RGB")
    return ImageOps.pad(
        rgb,
        (GEMMA_IMAGE_SIZE, GEMMA_IMAGE_SIZE),
        method=RESAMPLE_BICUBIC,
        color=(0, 0, 0),
        centering=(0.5, 0.5),
    )


def _load_optional_image(
    *,
    image_path: Optional[str],
    image_b64: Optional[str],
    image_mime: Optional[str],
) -> tuple[Image.Image | None, dict[str, Any]]:
    if image_b64:
        image_bytes, mime_from_data_uri = _decode_image_b64(image_b64)
        effective_mime = (image_mime or mime_from_data_uri or "").strip().lower()
        LOGGER.info(
            "gemma_image_input source=b64 bytes=%s mime=%s",
            len(image_bytes),
            effective_mime or "unknown",
        )
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                normalized = _normalize_image(img)
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError(f"invalid image payload: {exc}") from exc
        LOGGER.info(
            "gemma_image_normalized source=b64 size=%sx%s",
            normalized.width,
            normalized.height,
        )
        return normalized, {
            "source": "b64",
            "bytes": len(image_bytes),
            "mime": effective_mime,
        }

    if image_path:
        path = Path(str(image_path)).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Image file not found: {path}")
        raw_bytes = path.read_bytes()
        LOGGER.info(
            "gemma_image_input source=path bytes=%s path=%s",
            len(raw_bytes),
            path,
        )
        if len(raw_bytes) > GEMMA_IMAGE_MAX_BYTES:
            raise ValueError(
                f"image exceeds max bytes ({GEMMA_IMAGE_MAX_BYTES})"
            )
        try:
            with Image.open(path) as img:
                normalized = _normalize_image(img)
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError(f"invalid image payload: {exc}") from exc
        LOGGER.info(
            "gemma_image_normalized source=path size=%sx%s",
            normalized.width,
            normalized.height,
        )
        return normalized, {
            "source": "path",
            "bytes": len(raw_bytes),
            "path": str(path),
        }

    return None, {}


def gemma_generate(
    prompt: str,
    max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
    image_path: Optional[str] = None,
    image_b64: Optional[str] = None,
    image_mime: Optional[str] = None,
) -> str:
    image, _ = _load_optional_image(
        image_path=image_path,
        image_b64=image_b64,
        image_mime=image_mime,
    )

    content = []
    if image is not None:
        content.append({"type": "image", "image": image})
    content.append({"type": "text", "text": prompt})
    messages = [{"role": "user", "content": content}]

    inputs = GEMMA_PROC.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    ).to(GEMMA_MODEL.device)

    if MODEL_DTYPE in (torch.bfloat16, torch.float16):
        inputs = {
            key: value.to(dtype=MODEL_DTYPE) if value.is_floating_point() else value
            for key, value in inputs.items()
        }

    input_len = inputs["input_ids"].shape[-1]
    with torch.inference_mode():
        out = GEMMA_MODEL.generate(
            **inputs,
            max_new_tokens=max(1, min(int(max_new_tokens), 1024)),
            do_sample=False,
            temperature=0.0,
            repetition_penalty=1.05,
        )
    generated = out[0][input_len:]
    text = GEMMA_PROC.decode(generated, skip_special_tokens=True)
    return _clean_output(text)


@app.on_event("startup")
def _startup() -> None:
    load_gemma_model()


@app.get("/health")
def health():
    return {
        "ok": True,
        "model_id": MODEL_ID,
        "model_loaded": GEMMA_MODEL is not None,
        "cuda_available": CUDA_AVAILABLE,
        "device": DEVICE_NAME,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "concurrency_limit": GEMMA_CONCURRENCY,
        "timeout_s": GEMMA_TIMEOUT_S,
    }


@app.websocket("/ws")
async def ws_gemma(ws: WebSocket):
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

            op = msg.get("op", "generate")
            if op == "ping":
                await ws.send_json({"op": "pong"})
                continue

            req_id = str(msg.get("request_id", ""))
            if op != "generate":
                await ws.send_json(
                    {
                        "op": "error",
                        "request_id": req_id,
                        "error": f"unknown op: {op}",
                    }
                )
                continue

            prompt = str(msg.get("prompt", "")).strip()
            if not prompt:
                await ws.send_json(
                    {
                        "op": "error",
                        "request_id": req_id,
                        "error": "missing prompt",
                    }
                )
                continue

            max_new_tokens = int(msg.get("max_new_tokens", DEFAULT_MAX_NEW_TOKENS))
            image_path = msg.get("image_path")
            image_b64 = msg.get("image_b64")
            image_mime = str(msg.get("image_mime", "")).strip() or None
            LOGGER.info(
                "gemma_generate_start request_id=%s has_image_b64=%s has_image_path=%s image_mime=%s max_new_tokens=%s",
                req_id,
                bool(image_b64),
                bool(image_path),
                image_mime or "",
                max_new_tokens,
            )

            t0 = time.time()
            try:
                async with GEMMA_SEM:
                    text = await asyncio.wait_for(
                        asyncio.to_thread(
                            gemma_generate,
                            prompt,
                            max_new_tokens,
                            image_path,
                            image_b64,
                            image_mime,
                        ),
                        timeout=GEMMA_TIMEOUT_S,
                    )
            except asyncio.TimeoutError:
                await ws.send_json(
                    {
                        "op": "error",
                        "request_id": req_id,
                        "error": f"inference timeout after {GEMMA_TIMEOUT_S}s",
                    }
                )
                continue
            except Exception as exc:
                await ws.send_json(
                    {
                        "op": "error",
                        "request_id": req_id,
                        "error": f"generation failed: {exc}",
                    }
                )
                continue

            await ws.send_json(
                {
                    "op": "gemma_result",
                    "request_id": req_id,
                    "text": text,
                    "latency_ms": int((time.time() - t0) * 1000),
                }
            )
    except WebSocketDisconnect:
        return
