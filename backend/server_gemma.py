import asyncio
import os
import re
import time
from pathlib import Path
from typing import Optional

# Pin MedGemma to GPU1 by default.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", os.environ.get("MEDGEMMA_GPU", "1"))

import torch
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from PIL import Image
from transformers import AutoModelForImageTextToText, AutoProcessor

MODEL_ID = "google/medgemma-1.5-4b-it"
DEFAULT_MAX_NEW_TOKENS = 256
GEMMA_TIMEOUT_S = float(os.environ.get("GEMMA_TIMEOUT_S", "180"))
GEMMA_CONCURRENCY = max(1, int(os.environ.get("GEMMA_CONCURRENCY", "1")))

app = FastAPI(title="MedGemma WebSocket Server")
GEMMA_SEM = asyncio.Semaphore(GEMMA_CONCURRENCY)

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


def gemma_generate(
    prompt: str,
    max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
    image_path: Optional[str] = None,
) -> str:
    image = None
    if image_path:
        path = Path(image_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Image file not found: {path}")
        image = Image.open(path).convert("RGB")

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

            t0 = time.time()
            try:
                async with GEMMA_SEM:
                    text = await asyncio.wait_for(
                        asyncio.to_thread(gemma_generate, prompt, max_new_tokens, image_path),
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
