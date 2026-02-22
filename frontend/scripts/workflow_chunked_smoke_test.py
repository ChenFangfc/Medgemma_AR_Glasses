#!/usr/bin/env python3
"""Chunked protocol smoke test for the workflow WebSocket service."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path

try:
    import ssl
    import websockets
except ImportError as exc:  # pragma: no cover
    print(f"Missing dependency: {exc}. Install with: python3 -m pip install websockets", file=sys.stderr)
    sys.exit(2)


class SmokeTestError(RuntimeError):
    """Raised when smoke test expectations are not met."""


def build_tone_pcm16(sample_rate: int, seconds: float, frequency_hz: float) -> bytes:
    sample_count = max(1, int(sample_rate * seconds))
    pcm = bytearray()

    for i in range(sample_count):
        sample = int(0.20 * 32767.0 * math.sin(2.0 * math.pi * frequency_hz * i / sample_rate))
        pcm += struct.pack("<h", sample)

    return bytes(pcm)


def load_wav_as_pcm16_mono(path: str, target_sample_rate: int) -> bytes:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise SmokeTestError(f"Audio file not found: {resolved}")

    with wave.open(str(resolved), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frame_count = wav.getnframes()
        frames = wav.readframes(frame_count)

    if not frames:
        raise SmokeTestError(f"Audio file is empty: {resolved}")

    mono = decode_wav_frames_to_mono_f32(frames, sample_width, channels)

    if sample_rate != target_sample_rate:
        mono = resample_linear(mono, sample_rate, target_sample_rate)

    return float_mono_to_pcm16le(mono)


def decode_wav_frames_to_mono_f32(frames: bytes, sample_width: int, channels: int) -> list[float]:
    if sample_width not in (1, 2, 3, 4):
        raise SmokeTestError(f"Unsupported WAV sample width: {sample_width}")

    if channels <= 0:
        raise SmokeTestError(f"Invalid WAV channel count: {channels}")

    bytes_per_frame = sample_width * channels
    if bytes_per_frame <= 0 or len(frames) % bytes_per_frame != 0:
        raise SmokeTestError("Corrupt WAV frame layout.")

    frame_count = len(frames) // bytes_per_frame
    mono: list[float] = []

    for frame_index in range(frame_count):
        base = frame_index * bytes_per_frame
        mixed = 0.0
        for ch in range(channels):
            offset = base + ch * sample_width
            mixed += decode_one_sample(frames, offset, sample_width)
        mono.append(mixed / channels)

    return mono


def decode_one_sample(frames: bytes, offset: int, sample_width: int) -> float:
    if sample_width == 1:
        # 8-bit PCM in WAV is unsigned [0,255].
        unsigned = frames[offset]
        return max(-1.0, min(1.0, (unsigned - 128) / 128.0))

    if sample_width == 2:
        value = int.from_bytes(frames[offset : offset + 2], byteorder="little", signed=True)
        return max(-1.0, min(1.0, value / 32768.0))

    if sample_width == 3:
        raw = frames[offset : offset + 3]
        sign = b"\xff" if (raw[2] & 0x80) else b"\x00"
        value = int.from_bytes(raw + sign, byteorder="little", signed=True)
        return max(-1.0, min(1.0, value / 8388608.0))

    value = int.from_bytes(frames[offset : offset + 4], byteorder="little", signed=True)
    return max(-1.0, min(1.0, value / 2147483648.0))


def resample_linear(samples: list[float], src_rate: int, dst_rate: int) -> list[float]:
    if not samples:
        return []
    if src_rate <= 0 or dst_rate <= 0:
        raise SmokeTestError(f"Invalid sample rate conversion: {src_rate} -> {dst_rate}")
    if src_rate == dst_rate:
        return samples

    dst_count = max(1, int(round(len(samples) * (dst_rate / src_rate))))
    last_src = len(samples) - 1
    out: list[float] = []

    for i in range(dst_count):
        src_pos = i * (src_rate / dst_rate)
        i0 = int(math.floor(src_pos))
        i1 = min(i0 + 1, last_src)
        frac = src_pos - i0
        value = (samples[i0] * (1.0 - frac)) + (samples[i1] * frac)
        out.append(value)

    return out


def float_mono_to_pcm16le(samples: list[float]) -> bytes:
    pcm = bytearray()
    for sample in samples:
        clamped = max(-1.0, min(1.0, sample))
        value = int(round(clamped * 32767.0))
        value = max(-32768, min(32767, value))
        pcm += struct.pack("<h", value)
    return bytes(pcm)


def build_ssl_context(ws_url: str, insecure: bool):
    if not ws_url.lower().startswith("wss://"):
        return None

    if not insecure:
        return ssl.create_default_context()

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def receive_until(
    ws,
    *,
    expected_ops: set[str],
    request_id: str | None,
    timeout_seconds: float,
    transcript: list[dict],
    verbose: bool,
) -> dict:
    deadline = time.monotonic() + timeout_seconds

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SmokeTestError(
                f"Timed out waiting for op in {sorted(expected_ops)} for request_id={request_id or '(any)'}"
            )

        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)

        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SmokeTestError(f"Received non-JSON message: {raw!r}") from exc

        transcript.append(message)
        op = str(message.get("op", ""))
        msg_request_id = str(message.get("request_id", ""))
        if verbose:
            print(f"<-- op={op} request_id={msg_request_id}")

        if op == "ready":
            continue

        if op == "error":
            raise SmokeTestError(f"Server error for request_id={msg_request_id}: {json.dumps(message)}")

        if request_id is not None and msg_request_id != request_id:
            continue

        if op in expected_ops:
            return message


def chunk_bytes(payload: bytes, chunk_size: int) -> list[bytes]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    return [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]


async def run_smoke_test(args: argparse.Namespace) -> int:
    ws_url = args.ws_url or os.getenv("WS_URL", "").strip()
    if not ws_url:
        print("Missing --ws-url (or set WS_URL).", file=sys.stderr)
        return 2

    if args.audio_file:
        pcm16 = load_wav_as_pcm16_mono(args.audio_file, args.sample_rate)
    else:
        pcm16 = build_tone_pcm16(args.sample_rate, args.tone_seconds, args.tone_frequency)

    chunk_size_bytes = max(1, int(args.sample_rate * 2 * (args.chunk_ms / 1000.0)))
    chunks = chunk_bytes(pcm16, chunk_size_bytes)
    if not chunks:
        print("No chunks generated from audio payload.", file=sys.stderr)
        return 2

    transcript: list[dict] = []
    ssl_context = build_ssl_context(ws_url, args.insecure)

    print(f"Connecting: {ws_url}")
    print(f"Audio bytes={len(pcm16)} chunks={len(chunks)} chunk_ms={args.chunk_ms}")
    if args.insecure and ws_url.lower().startswith("wss://"):
        print("TLS verification disabled (--insecure).")

    try:
        async with websockets.connect(ws_url, ssl=ssl_context, max_size=32_000_000) as ws:
            start_request_id = "s1"
            start_payload = {
                "op": "start_session",
                "request_id": start_request_id,
                "patient_id": args.patient_id,
            }
            if args.verbose:
                print(f"--> {json.dumps(start_payload)}")
            await ws.send(json.dumps(start_payload))
            started = await receive_until(
                ws,
                expected_ops={"session_started"},
                request_id=start_request_id,
                timeout_seconds=args.timeout,
                transcript=transcript,
                verbose=args.verbose,
            )

            session_id = str(started.get("session_id", "")).strip()
            if not session_id:
                raise SmokeTestError(f"session_started missing session_id: {json.dumps(started)}")
            print(f"PASS start_session session_id={session_id}")

            turn_id = f"t_{int(time.time() * 1000)}"
            begin_request_id = "ab1"
            begin_payload = {
                "op": "audio_begin",
                "request_id": begin_request_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "audio_format": "pcm_s16le",
                "sample_rate": args.sample_rate,
                "channels": 1,
                "chunk_ms": args.chunk_ms,
            }
            if args.verbose:
                print(f"--> {json.dumps(begin_payload)}")
            await ws.send(json.dumps(begin_payload))
            begin_ack = await receive_until(
                ws,
                expected_ops={"audio_ack"},
                request_id=begin_request_id,
                timeout_seconds=args.timeout,
                transcript=transcript,
                verbose=args.verbose,
            )

            if not bool(begin_ack.get("accepted", True)):
                raise SmokeTestError(f"audio_begin rejected: {json.dumps(begin_ack)}")
            print(f"PASS audio_begin turn_id={turn_id}")

            for seq, chunk in enumerate(chunks, start=1):
                chunk_request_id = f"ac_{seq:04d}"
                chunk_payload = {
                    "op": "audio_chunk",
                    "request_id": chunk_request_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "seq": seq,
                    "audio_b64": base64.b64encode(chunk).decode("ascii"),
                }
                if args.verbose:
                    print(f"--> audio_chunk seq={seq} bytes={len(chunk)}")
                await ws.send(json.dumps(chunk_payload))

                if args.expect_chunk_ack:
                    ack = await receive_until(
                        ws,
                        expected_ops={"chunk_ack"},
                        request_id=None,
                        timeout_seconds=args.timeout,
                        transcript=transcript,
                        verbose=args.verbose,
                    )
                    ack_seq = int(ack.get("seq", -1))
                    if ack_seq != seq:
                        raise SmokeTestError(f"chunk_ack seq mismatch: expected={seq} actual={ack_seq}")

            print(f"PASS audio_chunk sent={len(chunks)}")

            end_request_id = "ae1"
            end_payload = {
                "op": "audio_end",
                "request_id": end_request_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "return": [args.return_field],
            }
            if args.verbose:
                print(f"--> {json.dumps(end_payload)}")
            await ws.send(json.dumps(end_payload))
            turn_result = await receive_until(
                ws,
                expected_ops={"turn_result"},
                request_id=end_request_id,
                timeout_seconds=args.timeout,
                transcript=transcript,
                verbose=args.verbose,
            )

            text = str(turn_result.get(args.return_field, ""))
            if not text:
                raise SmokeTestError(f"turn_result missing '{args.return_field}': {json.dumps(turn_result)}")
            print(
                "PASS audio_end/turn_result",
                f"turn_index={turn_result.get('turn_index', 'n/a')}",
                f"{args.return_field}_len={len(text)}",
            )

    except (OSError, asyncio.TimeoutError, SmokeTestError) as exc:
        print(f"FAIL chunked workflow smoke test: {exc}", file=sys.stderr)
        if args.save_json:
            path = Path(args.save_json)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"messages": transcript}, ensure_ascii=True, indent=2), encoding="utf-8")
            print(f"Saved transcript: {path}", file=sys.stderr)
        return 1

    if args.save_json:
        path = Path(args.save_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"messages": transcript}, ensure_ascii=True, indent=2), encoding="utf-8")
        print(f"Saved transcript: {path}")

    print("PASS chunked workflow smoke test complete")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run chunked protocol smoke test against workflow WS service")
    parser.add_argument("--ws-url", default="", help="Workflow WS URL, e.g. ws://<ip>:8003/ws")
    parser.add_argument("--patient-id", default="p_smoke_chunked_001", help="Patient id used for start_session")
    parser.add_argument("--sample-rate", type=int, default=16000, help="PCM sample rate")
    parser.add_argument("--chunk-ms", type=int, default=500, help="Chunk duration in milliseconds")
    parser.add_argument("--audio-file", default="", help="Optional WAV file path (mono/stereo supported)")
    parser.add_argument("--tone-seconds", type=float, default=3.0, help="Tone duration when --audio-file is not set")
    parser.add_argument("--tone-frequency", type=float, default=440.0, help="Tone frequency when --audio-file is not set")
    parser.add_argument("--return-field", default="note_short", help="Field requested in audio_end return list")
    parser.add_argument("--timeout", type=float, default=60.0, help="Request timeout in seconds")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification for wss:// endpoint")
    parser.add_argument("--expect-chunk-ack", action="store_true", help="Require chunk_ack after every chunk")
    parser.add_argument("--save-json", default="", help="Optional path to save received WS messages")
    parser.add_argument("--verbose", action="store_true", help="Print per-message traffic")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(run_smoke_test(args))


if __name__ == "__main__":
    raise SystemExit(main())
