import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import websockets

WORKFLOW_URI = os.environ.get("WORKFLOW_URI", "ws://127.0.0.1:8003/ws")


def b64_file(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode("utf-8")


async def recv_json(ws, timeout=60):
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    try:
        return json.loads(raw)
    except Exception:
        return {"op": "_raw", "raw": raw}


async def main():
    if len(sys.argv) < 2:
        print("Usage: python test_pipeline_ws.py /path/to/audio.wav_or_m4a")
        sys.exit(1)

    audio_path = Path(sys.argv[1]).expanduser().resolve()
    if not audio_path.exists():
        raise SystemExit(f"Audio file not found: {audio_path}")

    # Use audio_b64 to mimic AR-glasses payload behavior.
    audio_b64 = b64_file(audio_path)

    async with websockets.connect(WORKFLOW_URI, max_size=256 * 1024 * 1024) as ws:
        # Server sends a ready frame first.
        ready = await recv_json(ws, timeout=30)
        print("\n[ready]\n", json.dumps(ready, indent=2, ensure_ascii=False))

        # 1) start_session
        await ws.send(json.dumps({"op": "start_session", "request_id": "s1"}))
        r1 = await recv_json(ws, timeout=30)
        print("\n[start_session response]\n", json.dumps(r1, indent=2, ensure_ascii=False))

        session_id = r1.get("session_id")
        if not session_id:
            raise SystemExit("No session_id returned. Check server_pipeline.py")

        # 2) process_audio
        payload = {
            "op": "process_audio",
            "request_id": "t1",
            "session_id": session_id,
            "audio_b64": audio_b64,
            "sample_rate": 16000,
            "return": ["note_full", "advice_full", "summary_turn", "running_summary"],
        }
        await ws.send(json.dumps(payload))
        r2 = await recv_json(ws, timeout=180)
        print("\n[process_audio response]\n", json.dumps(r2, indent=2, ensure_ascii=False))

        # 2.1) on-demand get_latest checks
        for what in ["note_full", "advice_full", "summary_turn", "running_summary"]:
            await ws.send(
                json.dumps(
                    {
                        "op": "get_latest",
                        "request_id": f"g-{what}",
                        "session_id": session_id,
                        "what": what,
                    }
                )
            )
            rg = await recv_json(ws, timeout=180)
            print(f"\n[get_latest:{what} response]\n", json.dumps(rg, indent=2, ensure_ascii=False))

        # 3) end_session
        await ws.send(
            json.dumps(
                {
                    "op": "end_session",
                    "request_id": "e1",
                    "session_id": session_id,
                    "include_transcript": True,
                }
            )
        )
        r3 = await recv_json(ws, timeout=180)
        print("\n[end_session response]\n", json.dumps(r3, indent=2, ensure_ascii=False))

        # Quick view
        print("\n=== QUICK VIEW ===")
        print("Turn note_full:", r2.get("note_full"))
        print("Turn advice_full:", r2.get("advice_full"))
        print("Turn summary_turn:", r2.get("summary_turn"))
        print("Running summary:", r2.get("running_summary"))


asyncio.run(main())
