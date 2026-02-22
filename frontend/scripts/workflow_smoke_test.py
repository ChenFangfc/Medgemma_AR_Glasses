#!/usr/bin/env python3
"""Protocol-level smoke test for the workflow WebSocket service."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
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


def build_tone_wav(sample_rate: int, seconds: float, frequency_hz: float) -> bytes:
    sample_count = max(1, int(sample_rate * seconds))
    pcm = bytearray()

    for i in range(sample_count):
        sample = int(0.20 * 32767.0 * math.sin(2.0 * math.pi * frequency_hz * i / sample_rate))
        pcm += struct.pack("<h", sample)

    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(bytes(pcm))

    return out.getvalue()


def load_audio_bytes(path: str) -> bytes:
    data = Path(path).expanduser().resolve().read_bytes()
    if not data:
        raise SmokeTestError(f"Audio file is empty: {path}")
    return data


async def receive_until(
    ws,
    *,
    expected_ops: set[str],
    request_id: str,
    timeout_seconds: float,
    transcript: list[dict],
    verbose: bool,
) -> dict:
    deadline = time.monotonic() + timeout_seconds

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SmokeTestError(
                f"Timed out waiting for op in {sorted(expected_ops)} for request_id={request_id}"
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

        if msg_request_id != request_id:
            continue

        if op in expected_ops:
            return message


def build_ssl_context(ws_url: str, insecure: bool):
    if not ws_url.lower().startswith("wss://"):
        return None

    if not insecure:
        return ssl.create_default_context()

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def run_smoke_test(args: argparse.Namespace) -> int:
    ws_url = args.ws_url or os.getenv("WS_URL", "").strip()
    if not ws_url:
        print("Missing --ws-url (or set WS_URL).", file=sys.stderr)
        return 2

    if args.audio_file:
        wav_bytes = load_audio_bytes(args.audio_file)
    else:
        wav_bytes = build_tone_wav(args.sample_rate, args.tone_seconds, args.tone_frequency)

    audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
    transcript: list[dict] = []
    ssl_context = build_ssl_context(ws_url, args.insecure)

    print(f"Connecting: {ws_url}")
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

            process_request_id = "t1"
            process_payload = {
                "op": "process_audio",
                "request_id": process_request_id,
                "session_id": session_id,
                "audio_b64": audio_b64,
                "sample_rate": args.sample_rate,
                "return": ["note_short"],
            }
            if args.verbose:
                print(f"--> process_audio payload bytes={len(wav_bytes)}")
            await ws.send(json.dumps(process_payload))
            turn_result = await receive_until(
                ws,
                expected_ops={"turn_result"},
                request_id=process_request_id,
                timeout_seconds=args.timeout,
                transcript=transcript,
                verbose=args.verbose,
            )

            note_short = str(turn_result.get("note_short", ""))
            if not note_short:
                print("WARN turn_result note_short is empty")
            print(
                "PASS process_audio",
                f"turn_index={turn_result.get('turn_index', 'n/a')}",
                f"note_short_len={len(note_short)}",
            )

            fetch_fields = [item.strip() for item in args.fetch_fields.split(",") if item.strip()]
            for idx, what in enumerate(fetch_fields, start=1):
                request_id = f"g{idx}"
                get_payload = {
                    "op": "get_latest",
                    "request_id": request_id,
                    "session_id": session_id,
                    "what": what,
                }
                if args.verbose:
                    print(f"--> {json.dumps(get_payload)}")
                await ws.send(json.dumps(get_payload))
                get_result = await receive_until(
                    ws,
                    expected_ops={"get_result"},
                    request_id=request_id,
                    timeout_seconds=args.timeout,
                    transcript=transcript,
                    verbose=args.verbose,
                )

                returned_what = str(get_result.get("what", ""))
                if returned_what != what:
                    raise SmokeTestError(
                        f"get_result what mismatch: expected={what} actual={returned_what} payload={json.dumps(get_result)}"
                    )
                print(f"PASS get_latest what={what}")

            if args.end_session:
                end_request_id = "e1"
                end_payload = {
                    "op": "end_session",
                    "request_id": end_request_id,
                    "session_id": session_id,
                    "include_transcript": True,
                }
                if args.verbose:
                    print(f"--> {json.dumps(end_payload)}")
                await ws.send(json.dumps(end_payload))
                summary = await receive_until(
                    ws,
                    expected_ops={"session_summary"},
                    request_id=end_request_id,
                    timeout_seconds=args.timeout,
                    transcript=transcript,
                    verbose=args.verbose,
                )
                print(f"PASS end_session turn_count={summary.get('turn_count', 'n/a')}")

    except (OSError, asyncio.TimeoutError, SmokeTestError) as exc:
        print(f"FAIL workflow smoke test: {exc}", file=sys.stderr)
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

    print("PASS workflow smoke test complete")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run protocol-level smoke tests against workflow WS service")
    parser.add_argument("--ws-url", default="", help="Workflow WS URL, e.g. ws://<ip>:8003/ws")
    parser.add_argument("--patient-id", default="p_smoke_001", help="Patient id used for start_session")
    parser.add_argument("--sample-rate", type=int, default=16000, help="Sample rate for generated tone")
    parser.add_argument("--tone-seconds", type=float, default=1.0, help="Generated tone duration when no --audio-file")
    parser.add_argument("--tone-frequency", type=float, default=440.0, help="Generated tone frequency in Hz")
    parser.add_argument("--audio-file", default="", help="Optional audio file path to send instead of generated tone")
    parser.add_argument("--fetch-fields", default="note_full,advice_short,advice_full", help="Comma-separated get_latest fields")
    parser.add_argument("--timeout", type=float, default=45.0, help="Per-request timeout seconds")
    parser.add_argument("--end-session", action="store_true", help="Also test end_session")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS cert verification for wss:// URLs")
    parser.add_argument("--save-json", default="", help="Optional output path for all received messages")
    parser.add_argument("--verbose", action="store_true", help="Print sent/received operation traces")
    return parser.parse_args()


def main() -> int:
    return asyncio.run(run_smoke_test(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
