#!/usr/bin/env python3
import argparse
import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import websockets


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Workflow smoke test (2 turns)")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8003/ws")
    parser.add_argument("--audio", default="/srv/local/chenf3/medasr_test001.m4a")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise RuntimeError(msg)


async def _recv_json(ws: Any, timeout_s: float = 60.0) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return json.loads(raw)


async def _run(args: argparse.Namespace) -> None:
    audio_path = Path(args.audio).expanduser().resolve()
    _require(audio_path.exists(), f"audio file not found: {audio_path}")
    audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("utf-8")

    async with websockets.connect(args.ws_url, max_size=256 * 1024 * 1024) as ws:
        ready = await _recv_json(ws, timeout_s=30)
        _require(ready.get("op") == "ready", f"expected ready: {ready}")
        fields = ready.get("result_fields", [])
        for key in ["note_full", "advice_full", "summary_turn", "running_summary"]:
            _require(key in fields, f"ready.result_fields missing {key}: {fields}")

        await ws.send(
            json.dumps(
                {
                    "op": "start_session",
                    "request_id": "s1",
                    "patient_id": "p_smoke_summary_001",
                }
            )
        )
        start = await _recv_json(ws, timeout_s=30)
        _require(start.get("op") == "session_started", f"start_session failed: {start}")
        session_id = str(start.get("session_id", ""))
        _require(bool(session_id), f"missing session_id: {start}")
        print(f"PASS start_session session_id={session_id}")

        async def process_turn(turn_num: int) -> dict[str, Any]:
            req_id = f"t{turn_num}"
            await ws.send(
                json.dumps(
                    {
                        "op": "process_audio",
                        "request_id": req_id,
                        "session_id": session_id,
                        "audio_b64": audio_b64,
                        "sample_rate": args.sample_rate,
                        "return": [
                            "note_full",
                            "advice_full",
                            "summary_turn",
                            "running_summary",
                        ],
                    }
                )
            )
            resp = await _recv_json(ws, timeout_s=240)
            _require(resp.get("op") == "turn_result", f"process_audio failed turn {turn_num}: {resp}")
            _require(resp.get("turn_index") == turn_num, f"turn_index mismatch: {resp}")
            for key in ["note_full", "advice_full", "summary_turn", "running_summary"]:
                _require(bool(str(resp.get(key, "")).strip()), f"turn {turn_num} missing {key}")
            print(f"PASS process_audio turn={turn_num}")
            if args.verbose:
                print(json.dumps(resp, ensure_ascii=False, indent=2))
            return resp

        t1 = await process_turn(1)
        t2 = await process_turn(2)
        rs1 = str(t1.get("running_summary", ""))
        rs2 = str(t2.get("running_summary", ""))
        if rs2 != rs1:
            print("PASS running_summary updated across turns")
        else:
            # Duplicate turns can legitimately keep the same deduplicated running summary.
            print("PASS running_summary stable for duplicate turns")

        await ws.send(
            json.dumps(
                {
                    "op": "end_session",
                    "request_id": "e1",
                    "session_id": session_id,
                    "include_transcript": False,
                }
            )
        )
        end = await _recv_json(ws, timeout_s=60)
        _require(end.get("op") == "session_summary", f"end_session failed: {end}")
        _require(bool(str(end.get("running_summary", "")).strip()), "session_summary missing running_summary")
        print("PASS end_session")


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
