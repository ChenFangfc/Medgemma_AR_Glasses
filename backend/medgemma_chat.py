import os
import re

# Default MedGemma to the first physical GPU unless caller overrides it.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", os.environ.get("MEDGEMMA_GPU", "0"))

import torch
from transformers import AutoProcessor, AutoModelForImageTextToText
from PIL import Image

model_id = "google/medgemma-1.5-4b-it"

cuda_available = torch.cuda.is_available()
dtype = (
    torch.bfloat16
    if cuda_available and torch.cuda.is_bf16_supported()
    else (torch.float16 if cuda_available else torch.float32)
)
print("Loading model... dtype=", dtype)
if cuda_available:
    print("CUDA_VISIBLE_DEVICES=", os.environ.get("CUDA_VISIBLE_DEVICES", ""))
    print("Using logical cuda:0 =", torch.cuda.get_device_name(0))
else:
    print("CUDA not available; running on CPU.")

model = AutoModelForImageTextToText.from_pretrained(
    model_id,
    torch_dtype=dtype,
    device_map="auto" if cuda_available else None,
)
if not cuda_available:
    model = model.to("cpu")
processor = AutoProcessor.from_pretrained(model_id)

print("\nReady. Commands:")
print("  /image PATH   attach an image for the next question")
print("  /reset        clear image")
print("  /quit         exit\n")

current_image = None


def clean_output(text: str) -> str:
    # Remove special "<unused...>" tokens that sometimes leak
    text = re.sub(r"<unused\d+>\s*", "", text)

    # Drop leading "thought" block / meta reasoning headers if present
    text = re.sub(r"^\s*thought\s*\n", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"^Here'?s a thinking process.*?\n\n",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return text.strip()


def run_once(user_text: str, image):
    # Nudge model to output final answer only
    user_text = (
        "Answer as a helpful medical assistant. "
        "Do NOT reveal chain-of-thought, reasoning steps, or internal notes. "
        "Only give the final answer.\n\n"
        + user_text
    )

    content = []
    if image is not None:
        content.append({"type": "image", "image": image})
    content.append({"type": "text", "text": user_text})
    messages = [{"role": "user", "content": content}]

    inputs = processor.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    ).to(model.device)

    # match dtype
    if dtype in (torch.bfloat16, torch.float16):
        inputs = {
            k: v.to(dtype=dtype) if v.is_floating_point() else v
            for k, v in inputs.items()
        }

    input_len = inputs["input_ids"].shape[-1]

    with torch.inference_mode():
        gen = model.generate(
            **inputs,
            max_new_tokens=256,
            do_sample=False,
            temperature=0.0,
            repetition_penalty=1.05,
        )
        gen = gen[0][input_len:]

    decoded = processor.decode(gen, skip_special_tokens=True)
    return clean_output(decoded)


while True:
    try:
        s = input("You> ").strip()
    except EOFError:
        print("\n[stdin closed — exiting]")
        break

    if not s:
        continue
    if s in ("/quit", "/exit"):
        break

    if s.startswith("/image "):
        path = s[len("/image "):].strip()
        try:
            current_image = Image.open(path).convert("RGB")
            print(f"[attached image: {path}]")
        except Exception as e:
            print("Could not open image:", e)
        continue

    if s == "/reset":
        current_image = None
        print("[reset image]")
        continue

    ans = run_once(s, current_image)
    print("MedGemma>", ans, "\n")
