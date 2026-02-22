import os
import shlex
import warnings
from pathlib import Path

# Default MedASR to the second physical GPU unless caller overrides it.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", os.environ.get("MEDASR_GPU", "1"))

import torch
from huggingface_hub import hf_hub_download
from transformers import pipeline

MODEL_ID = "google/medasr"
DEFAULT_CHUNK_LENGTH_S = 20.0
DEFAULT_STRIDE_LENGTH_S = 2.0
FALLBACK_SAMPLE_RATE = 16000


def print_help() -> None:
    print("\nCommands:")
    print("  /file PATH [CHUNK] [STRIDE]   transcribe a local audio file")
    print("  /sample                        transcribe MedASR sample audio")
    print("  /help                          show commands")
    print("  /quit                          exit\n")


def transcribe_once(
    asr_pipe,
    audio_path: str,
    chunk_length_s: float = DEFAULT_CHUNK_LENGTH_S,
    stride_length_s: float = DEFAULT_STRIDE_LENGTH_S,
) -> None:
    path = Path(audio_path).expanduser()
    if not path.exists():
        print(f"File not found: {path}\n")
        return

    result = None
    direct_exc = None
    try:
        result = asr_pipe(
            str(path),
            chunk_length_s=chunk_length_s,
            stride_length_s=stride_length_s,
        )
    except Exception as exc:
        direct_exc = exc

    if result is None and direct_exc is not None:
        # Some containers/codecs (e.g. m4a) fail in SoundFile; fallback to librosa.
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import librosa
                audio, sr = librosa.load(str(path), sr=FALLBACK_SAMPLE_RATE, mono=True)
            result = asr_pipe(
                {"array": audio, "sampling_rate": sr},
                chunk_length_s=chunk_length_s,
                stride_length_s=stride_length_s,
            )
            print("[decoded via librosa fallback]")
        except Exception:
            print(f"Transcription failed: {direct_exc}\n")
            return

    text = result.get("text", "").strip() if isinstance(result, dict) else str(result).strip()
    print("MedASR>", text if text else "[empty transcription]", "\n")


def main() -> None:
    cuda_available = torch.cuda.is_available()
    device = 0 if cuda_available else -1

    print("Loading model:", MODEL_ID)
    if cuda_available:
        print("CUDA_VISIBLE_DEVICES=", os.environ.get("CUDA_VISIBLE_DEVICES", ""))
        print("Using logical cuda:0 =", torch.cuda.get_device_name(0))
    else:
        print("CUDA not available; running on CPU.")

    try:
        asr_pipe = pipeline(
            "automatic-speech-recognition",
            model=MODEL_ID,
            device=device,
        )
    except Exception as exc:
        print("Could not load MedASR:", exc)
        return

    print("\nReady.")
    print_help()

    while True:
        try:
            raw = input("Audio> ").strip()
        except EOFError:
            print("\n[stdin closed - exiting]")
            break

        if not raw:
            continue
        if raw in ("/quit", "/exit"):
            break
        if raw == "/help":
            print_help()
            continue
        if raw == "/sample":
            try:
                sample_path = hf_hub_download(MODEL_ID, filename="test_audio.wav")
            except Exception as exc:
                print(f"Could not download sample audio: {exc}\n")
                continue
            print(f"[sample audio: {sample_path}]")
            transcribe_once(asr_pipe, sample_path)
            continue
        if raw.startswith("/file "):
            args = shlex.split(raw[len("/file "):])
            if not args:
                print("Usage: /file PATH [CHUNK] [STRIDE]\n")
                continue
            path = args[0]
            chunk = DEFAULT_CHUNK_LENGTH_S
            stride = DEFAULT_STRIDE_LENGTH_S
            if len(args) >= 2:
                try:
                    chunk = float(args[1])
                except ValueError:
                    print("CHUNK must be a number.\n")
                    continue
            if len(args) >= 3:
                try:
                    stride = float(args[2])
                except ValueError:
                    print("STRIDE must be a number.\n")
                    continue
            transcribe_once(asr_pipe, path, chunk, stride)
            continue

        # If no command is provided, treat input as a file path.
        transcribe_once(asr_pipe, raw)


if __name__ == "__main__":
    main()
