import argparse
import asyncio
import os
import re
from pathlib import Path
from typing import Optional

# Keep MedGemma on the first physical GPU by default.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", os.environ.get("MEDGEMMA_GPU", "0"))

import torch
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from PIL import Image
from transformers import AutoModelForImageTextToText, AutoProcessor

MODEL_ID = "google/medgemma-1.5-4b-it"
DEFAULT_MAX_NEW_TOKENS = 256


def clean_output(text: str) -> str:
    text = re.sub(r"<unused\d+>\s*", "", text)
    text = re.sub(r"^\s*thought\s*\n", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"^Here'?s a thinking process.*?\n\n",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return text.strip()


def load_runtime():
    cuda_available = torch.cuda.is_available()
    dtype = (
        torch.bfloat16
        if cuda_available and torch.cuda.is_bf16_supported()
        else (torch.float16 if cuda_available else torch.float32)
    )
    model = AutoModelForImageTextToText.from_pretrained(
        MODEL_ID,
        torch_dtype=dtype,
        device_map="auto" if cuda_available else None,
    )
    if not cuda_available:
        model = model.to("cpu")
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    device_name = torch.cuda.get_device_name(0) if cuda_available else "cpu"
    return model, processor, dtype, cuda_available, device_name


MODEL, PROCESSOR, DTYPE, CUDA_AVAILABLE, DEVICE_NAME = load_runtime()
INFER_LOCK = asyncio.Lock()

app = FastAPI(title="MedGemma WebSocket Server")


def run_once(prompt: str, image_path: Optional[str], max_new_tokens: int) -> str:
    image = None
    if image_path:
        path = Path(image_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {path}")
        image = Image.open(path).convert("RGB")

    prompt_text = (
        "Answer as a helpful medical assistant. "
        "Do NOT reveal chain-of-thought, reasoning steps, or internal notes. "
        "Only give the final answer.\n\n"
        + prompt
    )

    content = []
    if image is not None:
        content.append({"type": "image", "image": image})
    content.append({"type": "text", "text": prompt_text})
    messages = [{"role": "user", "content": content}]

    inputs = PROCESSOR.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    ).to(MODEL.device)

    if DTYPE in (torch.bfloat16, torch.float16):
        inputs = {
            key: value.to(dtype=DTYPE) if value.is_floating_point() else value
            for key, value in inputs.items()
        }

    input_len = inputs["input_ids"].shape[-1]
    with torch.inference_mode():
        generated = MODEL.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            temperature=0.0,
            repetition_penalty=1.05,
        )
    generated = generated[0][input_len:]
    decoded = PROCESSOR.decode(generated, skip_special_tokens=True)
    return clean_output(decoded)


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

            op = payload.get("op", "generate")
            if op == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if op != "generate":
                await websocket.send_json(
                    {"type": "error", "error": f"Unsupported op: {op}"}
                )
                continue

            prompt = str(payload.get("prompt", "")).strip()
            if not prompt:
                await websocket.send_json(
                    {"type": "error", "error": "Field 'prompt' is required."}
                )
                continue

            image_path = payload.get("image_path")
            max_new_tokens = int(payload.get("max_new_tokens", DEFAULT_MAX_NEW_TOKENS))
            max_new_tokens = max(1, min(max_new_tokens, 1024))

            async with INFER_LOCK:
                try:
                    answer = await asyncio.to_thread(
                        run_once, prompt, image_path, max_new_tokens
                    )
                except Exception as exc:
                    await websocket.send_json(
                        {"type": "error", "error": f"Inference failed: {exc}"}
                    )
                    continue

            await websocket.send_json({"type": "result", "text": answer})
    except WebSocketDisconnect:
        return


def parse_args():
    parser = argparse.ArgumentParser(description="MedGemma websocket server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8001, type=int)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
