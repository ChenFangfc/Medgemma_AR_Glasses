#!/usr/bin/env python3
import argparse
import asyncio
import base64
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import websockets


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chunked WS smoke test for med workflow")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8003/ws")
    parser.add_argument("--audio", default="/srv/local/chenf3/medasr_test001.m4a")
    parser.add_argument("--chunk-ms", type=int, default=500)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--seq-start", type=int, default=1)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def _ffmpeg_decode_pcm16le(audio_path: Path, sample_rate: int) -> bytes:
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(audio_path),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"ffmpeg decode failed: {stderr.strip()}")
    if not proc.stdout:
        raise RuntimeError("ffmpeg decode produced no audio")
    return proc.stdout


def _chunks(data: bytes, chunk_bytes: int) -> list[bytes]:
    return [data[i : i + chunk_bytes] for i in range(0, len(data), chunk_bytes)]


async def _recv_json(ws: Any, timeout_s: float = 30.0) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return json.loads(raw)


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise RuntimeError(message)


async def _run(args: argparse.Namespace) -> None:
    audio_path = Path(args.audio).expanduser().resolve()
    if not audio_path.exists():
        raise RuntimeError(f"audio file not found: {audio_path}")
    if args.chunk_ms <= 0:
        raise RuntimeError("--chunk-ms must be > 0")
    if args.sample_rate <= 0:
        raise RuntimeError("--sample-rate must be > 0")
    if args.seq_start < 0:
        raise RuntimeError("--seq-start must be >= 0")

    pcm_bytes = _ffmpeg_decode_pcm16le(audio_path, args.sample_rate)
    bytes_per_ms = max(1, int(args.sample_rate * 2 / 1000))
    chunk_bytes = max(320, bytes_per_ms * args.chunk_ms)
    chunks = _chunks(pcm_bytes, chunk_bytes)
    _require(bool(chunks), "decoded audio is empty")

    async with websockets.connect(args.ws_url, max_size=256 * 1024 * 1024) as ws:
        ready = await _recv_json(ws, timeout_s=30)
        _require(ready.get("op") == "ready", f"expected ready, got: {ready}")

        await ws.send(
            json.dumps(
                {
                    "op": "start_session",
                    "request_id": "s1",
                    "patient_id": "p_smoke_001",
                }
            )
        )
        start = await _recv_json(ws, timeout_s=30)
        _require(start.get("op") == "session_started", f"start_session failed: {start}")
        session_id = str(start.get("session_id", ""))
        _require(bool(session_id), f"missing session_id in start_session response: {start}")
        print(f"PASS start_session session_id={session_id}")

        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        await ws.send(
            json.dumps(
                {
                    "op": "audio_begin",
                    "request_id": "b1",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "audio_format": "pcm_s16le",
                    "sample_rate": args.sample_rate,
                    "channels": 1,
                    "chunk_ms": args.chunk_ms,
                    "seq_start": args.seq_start,
                }
            )
        )
        begin = await _recv_json(ws, timeout_s=30)
        _require(begin.get("op") == "audio_ack", f"audio_begin failed: {begin}")
        _require(begin.get("accepted") is True, f"audio_begin not accepted: {begin}")
        print(f"PASS audio_begin turn_id={turn_id}")

        for idx, chunk in enumerate(chunks, start=args.seq_start):
            await ws.send(
                json.dumps(
                    {
                        "op": "audio_chunk",
                        "request_id": f"c{idx}",
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "seq": idx,
                        "audio_b64": base64.b64encode(chunk).decode("utf-8"),
                        "ack": True,
                    }
                )
            )
            ack = await _recv_json(ws, timeout_s=30)
            _require(ack.get("op") == "chunk_ack", f"audio_chunk failed at seq {idx}: {ack}")
            _require(ack.get("accepted") is True, f"chunk not accepted at seq {idx}: {ack}")
        print(f"PASS audio_chunk sent={len(chunks)} total_pcm_bytes={len(pcm_bytes)}")

        await ws.send(
            json.dumps(
                {
                    "op": "audio_end",
                    "request_id": "e1",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "return": ["note_full", "advice_full", "summary_turn", "running_summary"],
                }
            )
        )
        end = await _recv_json(ws, timeout_s=240)
        _require(end.get("op") == "turn_result", f"audio_end failed: {end}")
        _require(bool(str(end.get("note_full", "")).strip()), "turn_result missing note_full")
        _require(bool(str(end.get("advice_full", "")).strip()), "turn_result missing advice_full")
        _require(bool(str(end.get("summary_turn", "")).strip()), "turn_result missing summary_turn")
        _require(
            bool(str(end.get("running_summary", "")).strip()),
            "turn_result missing running_summary",
        )
        print("PASS audio_end/turn_result")

        if args.verbose:
            print(json.dumps(end, ensure_ascii=False, indent=2))

        await ws.send(
            json.dumps(
                {
                    "op": "discard_session",
                    "request_id": "d1",
                    "session_id": session_id,
                }
            )
        )
        _ = await _recv_json(ws, timeout_s=30)


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"FAIL {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
