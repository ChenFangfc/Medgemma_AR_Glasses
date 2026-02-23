import asyncio
import base64
import binascii
import difflib
import io
import json
import logging
import os
import re
import time
import uuid
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

ASR_WS_URL = os.environ.get("ASR_WS_URL", "ws://127.0.0.1:8001/ws")
GEMMA_WS_URL = os.environ.get("GEMMA_WS_URL", "ws://127.0.0.1:8002/ws")
WORKFLOW_TIMEOUT_S = float(os.environ.get("WORKFLOW_TIMEOUT_S", "240"))
WORKFLOW_CONCURRENCY = max(1, int(os.environ.get("WORKFLOW_CONCURRENCY", "1")))
UPSTREAM_MAX_SIZE = int(os.environ.get("WORKFLOW_UPSTREAM_MAX_SIZE", str(64 * 1024 * 1024)))
PATIENTS_ROOT = Path(os.environ.get("PATIENTS_ROOT", "/srv/local/chenf3/patients"))
WORKFLOW_LOG_LEVEL = os.environ.get("WORKFLOW_LOG_LEVEL", "INFO").upper()
TURN_STREAM_MAX_BYTES = int(
    os.environ.get("WORKFLOW_TURN_STREAM_MAX_BYTES", str(10 * 1024 * 1024))
)
TURN_STREAM_TIMEOUT_S = float(os.environ.get("WORKFLOW_TURN_STREAM_TIMEOUT_S", "60"))
WORKFLOW_IMAGE_MAX_BYTES = int(
    os.environ.get("WORKFLOW_IMAGE_MAX_BYTES", str(8 * 1024 * 1024))
)

DEFAULT_NOTE_MAX_NEW_TOKENS = int(os.environ.get("WORKFLOW_NOTE_MAX_NEW_TOKENS", "768"))
DEFAULT_ADVICE_MAX_NEW_TOKENS = int(
    os.environ.get("WORKFLOW_ADVICE_MAX_NEW_TOKENS", "768")
)
DEFAULT_SESSION_NOTE_MAX_NEW_TOKENS = int(
    os.environ.get("WORKFLOW_SESSION_NOTE_MAX_NEW_TOKENS", "384")
)
DEFAULT_SESSION_ADVICE_MAX_NEW_TOKENS = int(
    os.environ.get("WORKFLOW_SESSION_ADVICE_MAX_NEW_TOKENS", "384")
)
DEFAULT_SUMMARY_COMPRESS_MAX_NEW_TOKENS = int(
    os.environ.get("WORKFLOW_SUMMARY_COMPRESS_MAX_NEW_TOKENS", "256")
)
MAX_SUMMARY_CHARS = int(os.environ.get("WORKFLOW_MAX_SUMMARY_CHARS", "6000"))
NOTE_CONTENT_MAX_BULLETS = int(os.environ.get("WORKFLOW_NOTE_CONTENT_MAX_BULLETS", "48"))
ADVICE_CONTENT_MAX_BULLETS = int(
    os.environ.get("WORKFLOW_ADVICE_CONTENT_MAX_BULLETS", "48")
)
CONTENT_BULLET_MAX_CHARS = int(os.environ.get("WORKFLOW_CONTENT_BULLET_MAX_CHARS", "420"))
SUMMARY_TURN_MAX_BULLETS = int(os.environ.get("WORKFLOW_SUMMARY_TURN_MAX_BULLETS", "16"))
RUNNING_SUMMARY_MAX_BULLETS = int(
    os.environ.get("WORKFLOW_RUNNING_SUMMARY_MAX_BULLETS", "120")
)

ALLOWED_RESULT_FIELDS = ("note_full", "advice_full", "summary_turn", "running_summary")
ALLOWED_LATEST_FIELDS = ALLOWED_RESULT_FIELDS + ("asr_text",)
DEFAULT_RETURN_FIELDS = ALLOWED_RESULT_FIELDS
LEGACY_RETURN_FIELD_ALIASES = {
    "note_short": "note_full",
    "advice_short": "advice_full",
}

NOTE_FULL_KEYS = (
    "chief_complaint",
    "hpi",
    "relevant_history",
    "meds",
    "allergies",
    "red_flags",
    "questions_to_clarify",
)

ADVICE_FULL_KEYS = (
    "top_differentials",
    "recommended_questions",
    "recommended_exam_or_tests",
    "red_flags_and_actions",
    "initial_plan_considerations",
)

ADVICE_FULL_LIMITS = {
    "top_differentials": 3,
    "recommended_questions": 5,
    "recommended_exam_or_tests": 5,
    "red_flags_and_actions": 5,
    "initial_plan_considerations": 5,
}
ADVICE_NOTE_SIMILARITY_MAX = float(
    os.environ.get("WORKFLOW_ADVICE_NOTE_SIMILARITY_MAX", "0.85")
)

NOTE_FULL_SECTIONS = (
    ("Patient concerns", ("patient concerns", "chief complaint", "chief concern", "concern")),
    ("Current symptoms and timeline", ("current symptoms", "timeline", "hpi", "symptoms")),
    ("Relevant history and context", ("history", "context", "background", "relevant history")),
    ("Meds and allergies", ("meds", "medications", "allergies", "allergy")),
    ("Red flags", ("red flags", "warning signs", "urgent signs")),
    ("Uncertainties / follow-up", ("uncertainties", "follow-up", "questions", "to clarify")),
)

ADVICE_FULL_SECTIONS = (
    (
        "Differential considerations",
        ("differential", "possible causes", "considerations", "top differentials"),
    ),
    (
        "Red flags and immediate actions",
        ("red flags", "urgent actions", "immediate actions", "safety"),
    ),
    ("Questions to clarify", ("questions", "clarify", "ask next")),
    ("Exam/tests to consider", ("exam", "tests", "workup", "diagnostics")),
    (
        "Initial management ideas",
        ("initial management", "plan", "next steps", "management"),
    ),
)
KNOWN_SECTION_SLUGS: set[str] = set()
for _title, _aliases in NOTE_FULL_SECTIONS + ADVICE_FULL_SECTIONS:
    KNOWN_SECTION_SLUGS.add(re.sub(r"[^a-z0-9]+", " ", _title.lower()).strip())
    for _alias in _aliases:
        KNOWN_SECTION_SLUGS.add(re.sub(r"[^a-z0-9]+", " ", _alias.lower()).strip())
KNOWN_SECTION_SLUGS.update(
    {
        "patient concerns",
        "differential considerations",
        "red flags and immediate actions",
        "questions to clarify",
        "exam tests to consider",
        "initial management ideas",
        "turn summary",
        "running summary",
        "clinical note",
        "clinical advice",
    }
)


LOGGER = logging.getLogger("medworkflow")
if not LOGGER.handlers:
    logging.basicConfig(
        level=getattr(logging, WORKFLOW_LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
LOGGER.setLevel(getattr(logging, WORKFLOW_LOG_LEVEL, logging.INFO))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _now_compact_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _sanitize_id(raw: str, default_value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (raw or "").strip()).strip("_")
    return cleaned or default_value


def _new_request_id() -> str:
    return uuid.uuid4().hex


def _new_auto_patient_id() -> str:
    PATIENTS_ROOT.mkdir(parents=True, exist_ok=True)
    max_idx = 0
    for child in PATIENTS_ROOT.iterdir():
        if not child.is_dir():
            continue
        match = re.match(r"^p_(\d+)_\d{8}_\d{6}$", child.name)
        if not match:
            continue
        max_idx = max(max_idx, int(match.group(1)))
    return f"p_{max_idx + 1:03d}_{_now_compact_stamp()}"


def _clamp_max_tokens(raw_value: Any, default_value: int) -> int:
    try:
        value = int(raw_value)
    except Exception:
        value = default_value
    return max(1, min(value, 1024))


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = int(value)
    except Exception:
        return None
    if parsed <= 0:
        return None
    return parsed


def _decode_image_b64_payload(raw_image_b64: Any) -> tuple[str, bytes, str | None]:
    raw = str(raw_image_b64 or "").strip()
    if not raw:
        raise ValueError("image_b64 is empty")
    data_uri_mime: str | None = None
    if raw.startswith("data:") and "," in raw:
        header, payload = raw.split(",", 1)
        mime_match = re.match(r"^data:([^;]+);base64$", header.strip(), flags=re.IGNORECASE)
        if mime_match:
            data_uri_mime = mime_match.group(1).strip().lower()
        raw = payload
    normalized = re.sub(r"\s+", "", raw)
    if not normalized:
        raise ValueError("image_b64 is empty")
    try:
        image_bytes = base64.b64decode(normalized, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64 in image_b64") from exc
    if not image_bytes:
        raise ValueError("image_b64 decoded to empty bytes")
    if len(image_bytes) > WORKFLOW_IMAGE_MAX_BYTES:
        raise ValueError(
            f"image exceeds max bytes ({WORKFLOW_IMAGE_MAX_BYTES})"
        )
    return normalized, image_bytes, data_uri_mime


def _parse_optional_image_payload(
    msg: dict[str, Any],
) -> tuple[str | None, str | None, bytes | None, dict[str, Any] | None]:
    raw_image_b64 = msg.get("image_b64")
    if raw_image_b64 is None:
        return None, None, None, None
    if isinstance(raw_image_b64, str) and not raw_image_b64.strip():
        return None, None, None, None

    normalized_b64, image_bytes, data_uri_mime = _decode_image_b64_payload(raw_image_b64)
    image_mime = str(msg.get("image_mime", "")).strip().lower() or data_uri_mime
    width = _optional_positive_int(msg.get("image_width"))
    height = _optional_positive_int(msg.get("image_height"))
    image_meta: dict[str, Any] = {
        "bytes": len(image_bytes),
    }
    if image_mime:
        image_meta["mime"] = image_mime
    if width is not None:
        image_meta["width"] = width
    if height is not None:
        image_meta["height"] = height
    return normalized_b64, image_mime, image_bytes, image_meta


def _image_suffix(image_bytes: bytes, image_mime: str | None) -> str:
    mime = (image_mime or "").strip().lower()
    if mime == "image/jpeg" or mime == "image/jpg":
        return ".jpg"
    if mime == "image/png":
        return ".png"
    if mime == "image/webp":
        return ".webp"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return ".webp"
    return ".img"


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _extract_json(text: str) -> str:
    match = re.search(r"\{.*\}", text, flags=re.S)
    return match.group(0) if match else text


def safe_json_load(text: str) -> dict[str, Any]:
    raw = _extract_json((text or "").strip()).strip()
    try:
        data = json.loads(raw)
    except Exception as exc:
        snippet = (text or "")[:240].replace("\n", " ")
        raise RuntimeError(f"invalid JSON from model: {snippet}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("invalid JSON from model: expected object")
    return data


def _clean_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("</s>", " ")
    text = re.sub(r"\b(uhm|uh|um)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_leading_bullets(text: str) -> str:
    return re.sub(r"^[\-\*\u2022\u00b7]+\s*", "", text).strip()


def _is_unknown_text(text: str) -> bool:
    t = _clean_text(text).lower()
    return t in {"", "unknown", "none", "n/a", "na"}


def _value_to_text(value: Any) -> str:
    if isinstance(value, list):
        items = [_clean_text(v) for v in value if not _is_unknown_text(_clean_text(v))]
        return "; ".join(items)
    return _clean_text(value)


def _truncate_words(text: str, max_words: int) -> str:
    words = _clean_text(text).split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words])


def _extract_fact_chunks(transcript_text: str, max_items: int = 6) -> list[str]:
    cleaned = _clean_text(transcript_text)
    parts = re.split(r"[.;,]", cleaned)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        chunk = _clean_text(part)
        if len(chunk) < 12 or _is_unknown_text(chunk):
            continue
        key = chunk.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(chunk)
        if len(out) >= max_items:
            break
    return out


def _format_short(head: str, bullets: list[str], warn: str | None = None) -> str:
    clean_head = _clean_text(head) or "Visit summary"
    clean_head = clean_head[:80]

    seen: set[str] = set()
    clean_bullets: list[str] = []
    for bullet in bullets:
        b = _truncate_words(bullet, 16)
        if not b or _is_unknown_text(b):
            continue
        key = b.lower()
        if key in seen:
            continue
        seen.add(key)
        clean_bullets.append(f"• {b}")
        if len(clean_bullets) >= 3:
            break

    while len(clean_bullets) < 3:
        clean_bullets.append(f"• {_truncate_words(clean_head, 16)}")

    out = [clean_head] + clean_bullets[:3]
    if warn and _clean_text(warn):
        out.append(f"⚠ {_truncate_words(warn, 16)}")
    return "\n".join(out[:6])


def _normalize_str_list(value: Any, max_items: int) -> list[str]:
    if isinstance(value, list):
        cleaned = [str(v).strip() for v in value if str(v).strip()]
    elif value is None:
        cleaned = []
    else:
        text = str(value).strip()
        cleaned = [text] if text else []
    return cleaned[:max_items]


def _normalize_note_full(raw_value: Any) -> dict[str, Any]:
    raw = raw_value if isinstance(raw_value, dict) else {}
    out: dict[str, Any] = {}
    for key in NOTE_FULL_KEYS:
        value = raw.get(key, "Unknown")
        if isinstance(value, list):
            cleaned = [str(v).strip() for v in value if str(v).strip()]
            out[key] = cleaned if cleaned else ["Unknown"]
        else:
            text = str(value).strip() if value is not None else ""
            out[key] = text or "Unknown"
    return out


def _normalize_advice_full(raw_value: Any) -> dict[str, list[str]]:
    raw = raw_value if isinstance(raw_value, dict) else {}
    out: dict[str, list[str]] = {}
    for key in ADVICE_FULL_KEYS:
        out[key] = _normalize_str_list(raw.get(key), ADVICE_FULL_LIMITS[key])
    return out


def _render_note_full_text(note_full: dict[str, Any]) -> str:
    fields = [
        ("Chief complaint", note_full.get("chief_complaint", "Unknown")),
        ("HPI", note_full.get("hpi", "Unknown")),
        ("Relevant history", note_full.get("relevant_history", "Unknown")),
        ("Meds", note_full.get("meds", "Unknown")),
        ("Allergies", note_full.get("allergies", "Unknown")),
        ("Red flags", note_full.get("red_flags", "Unknown")),
        ("Questions to clarify", note_full.get("questions_to_clarify", "Unknown")),
    ]
    lines: list[str] = []
    for label, value in fields:
        text = _value_to_text(value)
        if not text:
            text = "Unknown"
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


def _render_advice_full_text(advice_full: dict[str, list[str]]) -> str:
    sections = [
        ("Top differentials", advice_full.get("top_differentials", [])),
        ("Recommended questions", advice_full.get("recommended_questions", [])),
        ("Recommended exam or tests", advice_full.get("recommended_exam_or_tests", [])),
        ("Red flags and actions", advice_full.get("red_flags_and_actions", [])),
        ("Initial plan considerations", advice_full.get("initial_plan_considerations", [])),
    ]
    lines: list[str] = []
    for title, items in sections:
        lines.append(f"{title}:")
        if items:
            for item in items:
                lines.append(f"- {_clean_text(item)}")
        else:
            lines.append("- Unknown")
    return "\n".join(lines)


def _build_note_short_from_content(
    note_full: dict[str, Any],
    transcript_text: str,
    model_short: str = "",
) -> str:
    facts = _extract_fact_chunks(transcript_text, max_items=6)
    head = _value_to_text(note_full.get("chief_complaint"))
    if _is_unknown_text(head):
        head = _value_to_text(note_full.get("hpi"))
    if _is_unknown_text(head) and facts:
        head = facts[0]

    bullets: list[str] = []
    for key in ("hpi", "relevant_history", "red_flags", "questions_to_clarify"):
        txt = _value_to_text(note_full.get(key))
        if not _is_unknown_text(txt):
            bullets.append(txt)

    for fact in facts:
        bullets.append(fact)

    warn = None
    red_flags_text = _value_to_text(note_full.get("red_flags"))
    if any(
        k in red_flags_text.lower()
        for k in ["seizure", "chest pain", "shortness of breath", "stroke", "bleeding"]
    ):
        warn = red_flags_text

    if model_short and "• " in model_short:
        model_lines = [ln.strip() for ln in model_short.splitlines() if ln.strip()]
        model_head = model_lines[0] if model_lines else ""
        model_bullets = [ln[2:].strip() for ln in model_lines[1:] if ln.startswith("• ")]
        if model_head:
            head = model_head
        bullets = model_bullets + bullets

    return _format_short(head, bullets, warn=warn)


def _build_advice_short_from_content(
    advice_full: dict[str, list[str]],
    note_full: dict[str, Any],
    model_short: str = "",
) -> str:
    top_diff = advice_full.get("top_differentials", [])
    red_actions = advice_full.get("red_flags_and_actions", [])
    rec_questions = advice_full.get("recommended_questions", [])
    rec_tests = advice_full.get("recommended_exam_or_tests", [])

    head = (
        f"Priority concern: {top_diff[0]}"
        if top_diff
        else _value_to_text(note_full.get("chief_complaint"))
    )
    if _is_unknown_text(head):
        head = _value_to_text(note_full.get("hpi"))
    if _is_unknown_text(head):
        head = "Patient-reported concern requires clinical follow-up."

    bullets: list[str] = []
    if red_actions:
        bullets.append(red_actions[0])
    if rec_questions:
        bullets.append(rec_questions[0])
    if rec_tests:
        bullets.append(rec_tests[0])
    if top_diff:
        bullets.extend(top_diff[1:])

    warn = red_actions[0] if red_actions else None

    if model_short and "• " in model_short:
        model_lines = [ln.strip() for ln in model_short.splitlines() if ln.strip()]
        model_head = model_lines[0] if model_lines else ""
        model_bullets = [ln[2:].strip() for ln in model_lines[1:] if ln.startswith("• ")]
        if model_head and not _is_unknown_text(model_head):
            head = model_head
        bullets = model_bullets + bullets

    return _format_short(head, bullets, warn=warn)


def _note_full_is_mostly_unknown(note_full: dict[str, Any]) -> bool:
    non_unknown_count = 0
    for value in note_full.values():
        if isinstance(value, list):
            for item in value:
                if str(item).strip() and str(item).strip().lower() != "unknown":
                    non_unknown_count += 1
        else:
            text = str(value).strip()
            if text and text.lower() != "unknown":
                non_unknown_count += 1
    return non_unknown_count <= 1


def _fallback_note_from_transcript(transcript_text: str) -> tuple[str, dict[str, Any]]:
    seed = " ".join(transcript_text.replace("</s>", "").split())
    seed_words = " ".join(seed.split()[:14]).strip() or "Patient concern from transcript."
    chief = _truncate_words(seed, 12) if seed else "Unknown"
    facts = _extract_fact_chunks(seed, max_items=3)
    relevant = facts[1] if len(facts) > 1 else (facts[0] if facts else "Unknown")
    note_full = _normalize_note_full(
        {
            "chief_complaint": chief,
            "hpi": seed[:500] if seed else "Unknown",
            "relevant_history": relevant,
            "meds": "Unknown",
            "allergies": "Unknown",
            "red_flags": "Unknown",
            "questions_to_clarify": "Unknown",
        }
    )
    note_short = _format_short(
        seed_words,
        [
            seed_words,
            _value_to_text(note_full.get("hpi")),
            _value_to_text(note_full.get("chief_complaint")),
        ],
    )
    return note_short, note_full


def _fallback_advice_full_from_note(note_full: dict[str, Any]) -> dict[str, list[str]]:
    red_flags = _value_to_text(note_full.get("red_flags"))
    chief = _value_to_text(note_full.get("chief_complaint"))
    hpi = _value_to_text(note_full.get("hpi"))

    if "seizure" in red_flags.lower() or "seizure" in hpi.lower():
        top = ["Possible neurologic etiology", "Seizure disorder", "Intracranial pathology"]
        urgent = ["Urgent neurologic assessment is recommended."]
    else:
        top = [chief if not _is_unknown_text(chief) else "Undifferentiated symptom complex"]
        urgent = ["Escalate urgently if symptoms worsen rapidly."]

    questions = _normalize_str_list(note_full.get("questions_to_clarify"), 5)
    if not questions:
        questions = ["Clarify onset, duration, and progression."]

    return _normalize_advice_full(
        {
            "top_differentials": top,
            "recommended_questions": questions,
            "recommended_exam_or_tests": ["Focused clinical exam", "Targeted diagnostic workup"],
            "red_flags_and_actions": urgent,
            "initial_plan_considerations": [
                "Document key symptoms and timeline.",
                "Reassess based on progression and red flags.",
            ],
        }
    )


def _parse_return_fields(raw_value: Any) -> list[str]:
    if raw_value is None:
        return list(DEFAULT_RETURN_FIELDS)

    if isinstance(raw_value, str):
        requested = [raw_value]
    elif isinstance(raw_value, list):
        requested = raw_value
    else:
        raise ValueError("field 'return' must be a list of field names")

    out: list[str] = []
    for item in requested:
        key = str(item).strip()
        key = LEGACY_RETURN_FIELD_ALIASES.get(key, key)
        if key not in ALLOWED_RESULT_FIELDS:
            raise ValueError(f"unsupported return field: {key}")
        if key not in out:
            out.append(key)

    if not out:
        return list(DEFAULT_RETURN_FIELDS)
    return out


def _build_note_prompt(transcript_text: str) -> str:
    return f"""You are a clinical documentation assistant for AR glasses.

Hard rules:
- Use ONLY facts from the transcript. If unknown, write "Unknown" or omit.
- Output MUST be valid JSON only. No markdown. No extra text.
- Do NOT include internal reasoning, checklists, confidence, or meta commentary.

note_short format:
- Max 6 lines total.
- Line 1: one sentence <= 80 characters.
- Then EXACTLY 3 bullet lines, each starts with "• " and <= 16 words.
- Optional last line begins with "⚠ " only if urgent red flag; otherwise omit.

note_full format (JSON object):
- Keys: chief_complaint, hpi, relevant_history, meds, allergies, red_flags, questions_to_clarify
- Strings or arrays of strings. Keep concise.

Transcript:
{transcript_text}

Return JSON with keys: note_short, note_full.
"""


def _build_advice_prompt(note_full_json: str) -> str:
    return f"""You are assisting a licensed clinician. Provide decision-support suggestions, not a diagnosis.

Hard rules:
- Base your output ONLY on the provided clinical note facts. Do not invent.
- No medication dosing. No definitive diagnosis.
- Output MUST be valid JSON only. No markdown. No extra text.
- No internal reasoning or meta text.

advice_short format:
- Max 6 lines total.
- Line 1: one sentence <= 80 characters.
- Then EXACTLY 3 bullets with "• " and <= 16 words each.
- Optional "⚠ " red-flag action line if urgent.

advice_full format (JSON object):
- Keys:
  top_differentials (array up to 3),
  recommended_questions (array up to 5),
  recommended_exam_or_tests (array up to 5),
  red_flags_and_actions (array up to 5),
  initial_plan_considerations (array up to 5)

Clinical note facts (JSON):
{note_full_json}

Return JSON with keys: advice_short, advice_full.
"""


def _build_repair_prompt(
    *,
    target_keys: list[str],
    source_label: str,
    source_content: str,
) -> str:
    keys_text = ", ".join(target_keys)
    return f"""Return ONLY a valid JSON object.

Rules:
- No markdown.
- No comments.
- No explanations.
- No reasoning text.
- Top-level keys MUST be exactly: {keys_text}

{source_label}:
{source_content}
"""


def _trim_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


def _value_to_lines(value: Any) -> list[str]:
    if isinstance(value, dict):
        out: list[str] = []
        for k, v in value.items():
            v_text = (
                _value_to_text(v)
                if not isinstance(v, (dict, list))
                else "; ".join(_value_to_lines(v))
            )
            line = _clean_text(f"{k}: {v_text}")
            if line:
                out.append(line)
        return out
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, (dict, list)):
                out.extend(_value_to_lines(item))
            else:
                line = _strip_leading_bullets(_clean_text(item))
                if line:
                    out.append(line)
        return out
    text = str(value or "")
    out: list[str] = []
    expanded = text.replace("•", "·")
    expanded = re.sub(r"\s+·\s+", "\n· ", expanded)
    expanded = re.sub(r"\s+\*\s+", "\n* ", expanded)
    expanded = re.sub(r"\s+-\s+(?=[A-Za-z])", "\n- ", expanded)
    for raw_line in expanded.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        stripped = _clean_text(re.sub(r"^[\-\*\u2022\u00b7]+\s*", "", line))
        if stripped:
            out.append(stripped)
    if not out:
        single = _clean_text(text)
        if single:
            out.append(single)
    return out


def _is_header_line(text: str) -> bool:
    line = text.strip()
    if not line or line.startswith(("·", "-", "*", "•")):
        return False
    if not line.endswith(":"):
        return False
    return len(line) <= 120


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _parse_section_map(value: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if isinstance(value, dict):
        for key, raw_val in value.items():
            section = str(key).strip() or "General"
            lines = _value_to_lines(raw_val)
            if lines:
                out.setdefault(section, []).extend(lines)
        return out
    if isinstance(value, list):
        lines = _value_to_lines(value)
        if lines:
            out["General"] = lines
        return out

    current = "General"
    for raw in str(value or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _is_header_line(line):
            current = line[:-1].strip() or "General"
            out.setdefault(current, [])
            continue
        content = _clean_text(re.sub(r"^[\-\*\u2022\u00b7]+\s*", "", line))
        if content:
            pieces = _value_to_lines(content)
            if pieces:
                out.setdefault(current, []).extend(pieces)
            else:
                out.setdefault(current, []).append(content)

    if not out:
        lines = _value_to_lines(value)
        if lines:
            out["General"] = lines
    return out


def _collect_section_bullets(
    section_map: dict[str, list[str]],
    aliases: tuple[str, ...],
) -> list[str]:
    alias_slugs = [_slug(a) for a in aliases if _slug(a)]
    collected: list[str] = []
    for key, values in section_map.items():
        key_slug = _slug(key)
        if not key_slug:
            continue
        matched = any(
            key_slug == alias
            or key_slug in alias
            or alias in key_slug
            for alias in alias_slugs
        )
        if matched:
            collected.extend(values)
    return collected


def _is_placeholder_text(text: str) -> bool:
    slug = _slug(text)
    if not slug:
        return True
    if slug in {
        "unknown",
        "not stated",
        "n a",
        "na",
        "none",
        "not available",
        "not provided",
        "no information",
        "no info",
        "unspecified",
        "nil",
        "not mentioned",
        "nothing reported",
    }:
        return True
    if slug.startswith("not stated "):
        return True
    if slug.startswith("unknown "):
        return True
    return False


def _strip_known_section_prefix(text: str) -> str:
    if ":" not in text:
        return text
    head, tail = text.split(":", 1)
    tail = tail.strip()
    if not tail:
        return text
    head_slug = _slug(head)
    if head_slug in KNOWN_SECTION_SLUGS:
        return tail
    if len(head_slug.split()) <= 5 and len(head_slug) <= 40:
        return tail
    if len(head_slug.split()) <= 4 and head_slug.endswith("history"):
        return tail
    return text


def _normalize_content_only_bullets(value: Any, *, max_bullets: int) -> str:
    section_map = _parse_section_map(value)
    out: list[str] = []
    seen: set[str] = set()
    for _, items in section_map.items():
        for raw in items:
            for piece in _value_to_lines(raw):
                text = _strip_known_section_prefix(
                    _strip_leading_bullets(_clean_text(piece))
                )
                text = _trim_text(text, CONTENT_BULLET_MAX_CHARS)
                if not text:
                    continue
                if _is_header_line(text):
                    continue
                if _is_placeholder_text(text):
                    continue
                key = _slug(text)
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(f"· {text}")
                if len(out) >= max_bullets:
                    return "\n".join(out)
    return "\n".join(out)


def _render_required_sections(
    value: Any,
    sections: tuple[tuple[str, tuple[str, ...]], ...],
    *,
    max_bullets_per_section: int,
) -> str:
    section_map = _parse_section_map(value)
    general = list(section_map.get("General", []))
    lines: list[str] = []
    global_seen: set[str] = set()

    for idx, (title, aliases) in enumerate(sections):
        candidates = _collect_section_bullets(section_map, aliases)
        if not candidates and idx == 0 and general:
            candidates = general
        section_bullets: list[str] = []
        for item in candidates:
            text = _trim_text(
                _strip_leading_bullets(_clean_text(item)),
                CONTENT_BULLET_MAX_CHARS,
            )
            if not text:
                continue
            key = _slug(text)
            if not key or key in global_seen:
                continue
            global_seen.add(key)
            section_bullets.append(text)
            if len(section_bullets) >= max_bullets_per_section:
                break
        if not section_bullets:
            section_bullets = ["Not stated."]

        lines.append(f"{title}:")
        for bullet in section_bullets:
            lines.append(f"· {bullet}")
    return "\n".join(lines)


def _render_single_section(value: Any, title: str, *, max_bullets: int) -> str:
    section_map = _parse_section_map(value)
    candidates = _collect_section_bullets(section_map, (title,)) or section_map.get("General", [])
    bullets: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        text = _trim_text(
            _strip_leading_bullets(_clean_text(item)),
            CONTENT_BULLET_MAX_CHARS,
        )
        if not text:
            continue
        key = _slug(text)
        if not key or key in seen or key == _slug(title):
            continue
        seen.add(key)
        bullets.append(text)
        if len(bullets) >= max_bullets:
            break
    if not bullets:
        bullets = ["Not stated."]
    return "\n".join([f"{title}:", *[f"· {b}" for b in bullets]])


def _normalize_note_full_text(value: Any) -> str:
    text = _normalize_content_only_bullets(value, max_bullets=NOTE_CONTENT_MAX_BULLETS)
    return text


def _normalize_advice_full_text(value: Any) -> str:
    text = _normalize_content_only_bullets(value, max_bullets=ADVICE_CONTENT_MAX_BULLETS)
    return text


def _normalize_summary_turn_text(value: Any) -> str:
    return _render_single_section(value, "Turn summary", max_bullets=SUMMARY_TURN_MAX_BULLETS)


def _normalize_running_summary_text(value: Any) -> str:
    return _render_single_section(
        value,
        "Running summary",
        max_bullets=RUNNING_SUMMARY_MAX_BULLETS,
    )


def _flatten_for_similarity(text: str) -> str:
    parts: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _is_header_line(line):
            continue
        line = re.sub(r"^[\-\*\u2022\u00b7]+\s*", "", line)
        line = _clean_text(line).lower()
        if line and line != "not stated." and line != "not stated":
            parts.append(line)
    return " ".join(parts)


def _text_similarity(a: str, b: str) -> float:
    x = _flatten_for_similarity(a)
    y = _flatten_for_similarity(b)
    if not x or not y:
        return 0.0
    return difflib.SequenceMatcher(a=x, b=y).ratio()


def _build_advice_only_prompt(
    *,
    running_summary: str,
    asr_text: str,
    note_full: str,
    summary_turn: str,
) -> str:
    return f"""You are a clinical decision-support assistant.

Hard rules:
- Use ONLY facts from RUNNING_SUMMARY, CURRENT TURN, and NOTE_FULL.
- Output MUST be valid JSON only with exactly one key: advice_full.
- advice_full must NOT restate note_full. Focus on differential, red flags, questions, tests, and management ideas.
- No medication dosing. No definitive diagnosis.

FORMAT:
- advice_full must be bullet-only content.
- Every non-empty line MUST start with "· ".
- Keep the output slide-like: use short category tags inside bullets (for example: "· [Differential] ...", "· [Red flag] ...", "· [Question] ...", "· [Test] ...", "· [Plan] ...").
- One clinical idea per bullet. No strict line-count requirement.
- Do not include section headers or labels such as "Differential:".
- No paragraphs, no markdown, no numbered lists.
- Do NOT output placeholders like "Unknown", "Not stated", "N/A".
- If information is missing, omit it.

CONTENT:
- Cover as many clinically relevant known facts as possible from the provided context.
- Be concise by merging duplicates, not by dropping meaningful details.

SPEAKER NOTE:
- Try to distinguish doctor vs patient statements.
- Do not treat doctor questions/recommendations as patient symptoms.
- If unsure, avoid guessing and keep wording neutral.

RUNNING_SUMMARY:
{running_summary}

CURRENT TURN:
{asr_text}

NOTE_FULL:
{note_full}

SUMMARY_TURN:
{summary_turn}
"""


def _fallback_turn_outputs(asr_text: str) -> tuple[str, str, str]:
    facts = _extract_fact_chunks(asr_text, max_items=6)
    if not facts:
        fallback = _truncate_words(asr_text, 40) or "No transcript content."
        facts = [fallback]
    note_full = _normalize_note_full_text(
        {
            "Patient concerns": facts[0],
            "Current symptoms and timeline": facts,
            "Relevant history and context": facts[1:] or ["Not stated."],
            "Meds and allergies": "Not stated.",
            "Red flags": "Not stated.",
            "Uncertainties / follow-up": "Not stated.",
        }
    )
    advice_seed = [
        "Review history and symptom timeline with the patient.",
        "Perform focused exam and targeted diagnostics based on complaints.",
        "Escalate promptly if red flags are present or symptoms worsen.",
    ]
    advice_full = _normalize_advice_full_text(
        {
            "Differential considerations": "Not stated.",
            "Red flags and immediate actions": advice_seed[-1],
            "Questions to clarify": advice_seed[0],
            "Exam/tests to consider": advice_seed[1],
            "Initial management ideas": advice_seed,
        }
    )
    summary_turn = _normalize_summary_turn_text(facts[:4])
    return note_full, advice_full, summary_turn


def _build_turn_prompt(running_summary: str, asr_text: str) -> str:
    return f"""You are a clinical documentation assistant for an AR glasses workflow.

Hard rules:
- Use ONLY facts from the provided transcript and running summary.
- Output MUST be valid JSON only. No markdown, no prose outside JSON, no meta commentary.
- Output keys MUST be exactly: note_full, advice_full, summary_turn.
- advice_full must not restate note_full. advice_full should focus on differential, red flags, questions, tests, and management ideas.

FORMAT:
- note_full and advice_full must be content-only bullet lists, not templates.
- Every content line MUST start with "· ".
- Keep output slide-like by using concise category tags inside bullets when helpful (for example: "· [Symptoms] ...", "· [Timeline] ...", "· [History] ...", "· [Differential] ...", "· [Plan] ...").
- One clinical fact per bullet. No strict line-count requirement.
- Do not include label lines like "Patient concerns:" or "Differential considerations:".
- No paragraphs, no markdown, no numbered lists.
- Do NOT output placeholders like "Unknown", "Not stated", "N/A".
- If information is missing, omit it (do not add filler lines).

CONTENT:
- Include all clinically relevant facts from RUNNING_SUMMARY + CURRENT TURN.
- Be concise by merging repeated facts, not by omitting known details.

SPEAKER NOTE:
- Try to distinguish doctor vs patient statements.
- Do not treat doctor questions/recommendations as patient symptoms.
- If unsure, avoid guessing and keep wording neutral.

Running summary so far:
{running_summary or "No prior running summary available."}

Current ASR transcript:
{asr_text}
"""


def _build_summary_compress_prompt(running_summary: str) -> str:
    return f"""You are compressing a longitudinal clinical running summary.

Hard rules:
- Preserve critical clinical facts and timeline.
- Remove repetition and low-value detail.
- Output MUST be valid JSON only with exactly one key: running_summary.
- running_summary must be a bullet string:
  - Every line starts with "· "
  - No section headers
  - No placeholders like "Unknown"/"Not stated"

Input running summary:
{running_summary}
"""


def _merge_running_summary(existing: str, summary_turn: str) -> str:
    existing_lines = _value_to_lines(existing)
    turn_lines = _value_to_lines(summary_turn)
    merged: list[str] = []
    seen: set[str] = set()
    for line in existing_lines + turn_lines:
        short = _trim_text(
            _strip_leading_bullets(_clean_text(line)),
            CONTENT_BULLET_MAX_CHARS,
        )
        if not short:
            continue
        key = short.lower().rstrip(":")
        if key in {
            "running summary",
            "turn summary",
            "clinical note",
            "clinical advice",
            "none yet",
            "none yet.",
            "not stated",
            "not stated.",
        }:
            continue
        if key in seen:
            continue
        seen.add(key)
        merged.append(short)
    if not merged:
        merged = ["Not stated."]
    return "\n".join(["Running summary:", *[f"· {line}" for line in merged]])


async def _compress_running_summary(
    *,
    running_summary: str,
    request_id_prefix: str,
) -> tuple[str, int | None, bool]:
    if len(running_summary) <= MAX_SUMMARY_CHARS:
        return running_summary, None, False

    prompt = _build_summary_compress_prompt(running_summary)
    gemma_response = await _call_gemma(
        prompt=prompt,
        max_new_tokens=DEFAULT_SUMMARY_COMPRESS_MAX_NEW_TOKENS,
        request_id=f"{request_id_prefix}:summary-compress",
    )
    latency = gemma_response.get("latency_ms")
    try:
        parsed = safe_json_load(str(gemma_response.get("text", "")))
        compressed = _normalize_running_summary_text(parsed.get("running_summary", ""))
    except Exception:
        compressed = _normalize_running_summary_text(running_summary)
    compressed = _trim_text(compressed, MAX_SUMMARY_CHARS)
    return compressed, latency, True


async def _generate_turn_outputs(
    *,
    running_summary: str,
    asr_text: str,
    max_new_tokens: int,
    request_id_prefix: str,
    image_b64: str | None = None,
    image_mime: str | None = None,
) -> tuple[str, str, str, int | None, bool, float]:
    repair_source = (
        f"Running summary:\n{running_summary or 'No prior running summary available.'}\n\n"
        f"Current ASR transcript:\n{asr_text}"
    )
    prompts = [
        _build_turn_prompt(running_summary, asr_text),
        _build_repair_prompt(
            target_keys=["note_full", "advice_full", "summary_turn"],
            source_label="Clinical context",
            source_content=repair_source,
        ),
    ]
    last_latency: int | None = None
    advice_regenerated = False
    note_advice_similarity = 0.0
    total_latency_ms = 0
    for idx, prompt in enumerate(prompts):
        gemma_response = await _call_gemma(
            prompt=prompt,
            max_new_tokens=max_new_tokens,
            request_id=f"{request_id_prefix}:turn:{idx+1}",
            image_b64=image_b64,
            image_mime=image_mime,
        )
        last_latency = gemma_response.get("latency_ms")
        if isinstance(last_latency, int):
            total_latency_ms += last_latency
        try:
            parsed = safe_json_load(str(gemma_response.get("text", "")))
            note_full = _normalize_note_full_text(parsed.get("note_full", ""))
            advice_full = _normalize_advice_full_text(parsed.get("advice_full", ""))
            summary_turn = _normalize_summary_turn_text(parsed.get("summary_turn", ""))
            fallback_note, fallback_advice, fallback_summary = _fallback_turn_outputs(asr_text)
            if not note_full:
                note_full = fallback_note
            if not summary_turn:
                summary_turn = fallback_summary
            if not advice_full:
                advice_full = fallback_advice
            note_advice_similarity = _text_similarity(note_full, advice_full)
            if note_advice_similarity > ADVICE_NOTE_SIMILARITY_MAX:
                regen_response = await _call_gemma(
                    prompt=_build_advice_only_prompt(
                        running_summary=running_summary,
                        asr_text=asr_text,
                        note_full=note_full,
                        summary_turn=summary_turn,
                    ),
                    max_new_tokens=max_new_tokens,
                    request_id=f"{request_id_prefix}:advice-regenerate",
                    image_b64=image_b64,
                    image_mime=image_mime,
                )
                regen_latency = regen_response.get("latency_ms")
                if isinstance(regen_latency, int):
                    total_latency_ms += regen_latency
                regen_parsed = safe_json_load(str(regen_response.get("text", "")))
                regen_advice = _normalize_advice_full_text(regen_parsed.get("advice_full", ""))
                if not regen_advice:
                    regen_advice = fallback_advice
                regen_similarity = _text_similarity(note_full, regen_advice)
                if regen_similarity <= note_advice_similarity:
                    advice_full = regen_advice
                    note_advice_similarity = regen_similarity
                    advice_regenerated = True
            return (
                note_full,
                advice_full,
                summary_turn,
                total_latency_ms or last_latency,
                advice_regenerated,
                note_advice_similarity,
            )
        except Exception:
            continue

    note_full, advice_full, summary_turn = _fallback_turn_outputs(asr_text)
    note_advice_similarity = _text_similarity(note_full, advice_full)
    return (
        note_full,
        advice_full,
        summary_turn,
        total_latency_ms or last_latency,
        advice_regenerated,
        note_advice_similarity,
    )


def _parse_json_payload(raw: Any, label: str) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        raise RuntimeError(f"{label}: expected text payload")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label}: invalid JSON payload: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label}: expected JSON object")
    return data


async def _recv_json(client: Any, label: str) -> dict[str, Any]:
    raw = await client.recv()
    return _parse_json_payload(raw, label)


async def _call_asr(payload: dict[str, Any]) -> dict[str, Any]:
    async with websockets.connect(ASR_WS_URL, max_size=UPSTREAM_MAX_SIZE) as client:
        ready = await _recv_json(client, "asr-ready")
        if ready.get("op") != "ready":
            raise RuntimeError(f"ASR not ready: {ready}")
        await client.send(json.dumps(payload))
        response = await _recv_json(client, "asr-response")
        if response.get("op") == "error":
            raise RuntimeError(str(response.get("error", "ASR error")))
        if response.get("op") != "asr_result":
            raise RuntimeError(f"Unexpected ASR response: {response}")
        return response


async def _call_gemma(
    prompt: str,
    max_new_tokens: int,
    request_id: str,
    image_b64: str | None = None,
    image_mime: str | None = None,
) -> dict[str, Any]:
    payload = {
        "op": "generate",
        "request_id": request_id,
        "prompt": prompt,
        "max_new_tokens": max_new_tokens,
    }
    if image_b64:
        payload["image_b64"] = image_b64
    if image_mime:
        payload["image_mime"] = image_mime
    async with websockets.connect(GEMMA_WS_URL, max_size=UPSTREAM_MAX_SIZE) as client:
        ready = await _recv_json(client, "gemma-ready")
        if ready.get("op") != "ready":
            raise RuntimeError(f"Gemma not ready: {ready}")
        await client.send(json.dumps(payload))
        response = await _recv_json(client, "gemma-response")
        if response.get("op") == "error":
            raise RuntimeError(str(response.get("error", "Gemma error")))
        if response.get("op") != "gemma_result":
            raise RuntimeError(f"Unexpected Gemma response: {response}")
        return response


@dataclass
class SessionState:
    session_id: str
    patient_id: str
    created_at_s: float
    created_at_iso: str
    metadata: dict[str, Any]
    session_dir: Path
    session_meta_path: Path
    running_summary_path: Path
    transcript_path: Path
    turns_dir: Path
    turns: list[dict[str, Any]] = field(default_factory=list)
    latest: dict[str, Any] = field(default_factory=dict)
    running_summary: str = "Running summary:\n· Not stated."
    summary_turns: list[str] = field(default_factory=list)
    ended_at_iso: str | None = None


@dataclass
class TurnStreamState:
    session_id: str
    turn_id: str
    sample_rate: int
    channels: int
    pcm_format: str
    chunk_ms: int
    expected_seq: int = 1
    pcm_buffer: bytearray = field(default_factory=bytearray)
    created_at_s: float = field(default_factory=time.time)
    last_update_time: float = field(default_factory=time.time)


def _ensure_session_dirs(session: SessionState) -> None:
    session.turns_dir.mkdir(parents=True, exist_ok=True)
    session.transcript_path.parent.mkdir(parents=True, exist_ok=True)
    if not session.running_summary_path.exists():
        session.running_summary_path.write_text(
            session.running_summary, encoding="utf-8"
        )
    if not session.session_meta_path.exists():
        payload = {
            "session_id": session.session_id,
            "patient_id": session.patient_id,
            "start_time": session.created_at_iso,
            "end_time": None,
            "metadata": session.metadata,
            "turn_count": 0,
            "running_summary_chars": len(session.running_summary),
        }
        session.session_meta_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _write_session_meta(session: SessionState) -> None:
    payload = {
        "session_id": session.session_id,
        "patient_id": session.patient_id,
        "start_time": session.created_at_iso,
        "end_time": session.ended_at_iso,
        "metadata": session.metadata,
        "turn_count": len(session.turns),
        "running_summary_chars": len(session.running_summary),
        "latest_turn_index": session.latest.get("turn_index"),
    }
    session.session_meta_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _append_turn_files(session: SessionState, turn_record: dict[str, Any]) -> None:
    transcript_item = {
        "timestamp_utc": turn_record["timestamp_utc"],
        "turn_index": turn_record["turn_index"],
        "asr_text": turn_record["asr_text"],
    }
    with session.transcript_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(transcript_item, ensure_ascii=False) + "\n")

    turn_path = session.turns_dir / f"{int(turn_record['turn_index']):04d}.json"
    turn_path.write_text(
        json.dumps(turn_record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    session.running_summary_path.write_text(session.running_summary, encoding="utf-8")
    _write_session_meta(session)


def _write_session_summary(
    session: SessionState,
    summary_payload: dict[str, Any],
) -> None:
    summary_path = session.session_dir / "session_summary.json"
    summary_path.write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _new_session_state(
    session_id: str,
    patient_id: str,
    metadata: dict[str, Any],
) -> SessionState:
    created_iso = _now_iso()
    session_dir = PATIENTS_ROOT / patient_id / "sessions" / session_id
    session_meta_path = session_dir / "session_meta.json"
    running_summary_path = session_dir / "running_summary.txt"
    transcript_path = session_dir / "transcript.jsonl"
    turns_dir = session_dir / "turns"
    session = SessionState(
        session_id=session_id,
        patient_id=patient_id,
        created_at_s=time.time(),
        created_at_iso=created_iso,
        metadata=metadata,
        session_dir=session_dir,
        session_meta_path=session_meta_path,
        running_summary_path=running_summary_path,
        transcript_path=transcript_path,
        turns_dir=turns_dir,
    )
    _ensure_session_dirs(session)
    _write_session_meta(session)
    return session


async def _generate_note_outputs(
    transcript_text: str,
    max_new_tokens: int,
    request_id_prefix: str,
) -> tuple[str, dict[str, Any], str, int | None]:
    prompts = [
        _build_note_prompt(transcript_text),
        _build_repair_prompt(
            target_keys=["note_short", "note_full"],
            source_label="Transcript",
            source_content=transcript_text,
        ),
    ]
    last_exc: Exception | None = None
    last_latency: int | None = None

    for idx, prompt in enumerate(prompts):
        gemma_response = await _call_gemma(
            prompt=prompt,
            max_new_tokens=max_new_tokens,
            request_id=f"{request_id_prefix}:note:{idx+1}",
        )
        last_latency = gemma_response.get("latency_ms")
        try:
            parsed = safe_json_load(str(gemma_response.get("text", "")))
            model_note_short = str(parsed.get("note_short", ""))
            note_full = _normalize_note_full(parsed.get("note_full", {}))
            if _note_full_is_mostly_unknown(note_full):
                _, fallback_full = _fallback_note_from_transcript(transcript_text)
                note_full = fallback_full
            note_short = _build_note_short_from_content(
                note_full=note_full,
                transcript_text=transcript_text,
                model_short=model_note_short,
            )
            note_full_text = _render_note_full_text(note_full)
            return note_short, note_full, note_full_text, last_latency
        except Exception as exc:
            last_exc = exc
            continue

    # Final safety fallback to keep workflow alive if model formatting fails.
    note_short, note_full = _fallback_note_from_transcript(transcript_text)
    note_full_text = _render_note_full_text(note_full)
    _ = last_exc  # reserved for future structured logging
    return note_short, note_full, note_full_text, last_latency


async def _generate_advice_outputs(
    note_full: dict[str, Any],
    max_new_tokens: int,
    request_id_prefix: str,
) -> tuple[str, dict[str, list[str]], str, int | None]:
    note_full_json = json.dumps(note_full, ensure_ascii=False)
    prompts = [
        _build_advice_prompt(note_full_json),
        _build_repair_prompt(
            target_keys=["advice_short", "advice_full"],
            source_label="Clinical note facts (JSON)",
            source_content=note_full_json,
        ),
    ]
    last_exc: Exception | None = None
    last_latency: int | None = None

    for idx, prompt in enumerate(prompts):
        gemma_response = await _call_gemma(
            prompt=prompt,
            max_new_tokens=max_new_tokens,
            request_id=f"{request_id_prefix}:advice:{idx+1}",
        )
        last_latency = gemma_response.get("latency_ms")
        try:
            parsed = safe_json_load(str(gemma_response.get("text", "")))
            model_advice_short = str(parsed.get("advice_short", ""))
            advice_full = _normalize_advice_full(parsed.get("advice_full", {}))
            if not any(advice_full.values()):
                advice_full = _fallback_advice_full_from_note(note_full)
            advice_short = _build_advice_short_from_content(
                advice_full=advice_full,
                note_full=note_full,
                model_short=model_advice_short,
            )
            advice_full_text = _render_advice_full_text(advice_full)
            return advice_short, advice_full, advice_full_text, last_latency
        except Exception as exc:
            last_exc = exc
            continue

    # Final safety fallback.
    advice_full = _fallback_advice_full_from_note(note_full)
    advice_short = _build_advice_short_from_content(
        advice_full=advice_full,
        note_full=note_full,
        model_short="",
    )
    advice_full_text = _render_advice_full_text(advice_full)
    _ = last_exc  # reserved for future structured logging
    return advice_short, advice_full, advice_full_text, last_latency


def _canonical_pcm_format(raw_value: Any) -> str:
    text = str(raw_value or "pcm_s16le").strip().lower()
    aliases = {
        "pcm_s16le": "pcm_s16le",
        "pcm16": "pcm_s16le",
        "s16le": "pcm_s16le",
    }
    return aliases.get(text, "")


def _stream_key(session_id: str, turn_id: str) -> tuple[str, str]:
    return (session_id, turn_id)


async def _cleanup_stale_turn_streams() -> int:
    now_s = time.time()
    stale_keys: list[tuple[str, str]] = []
    async with TURN_STREAMS_LOCK:
        for key, stream in TURN_STREAMS.items():
            if now_s - stream.last_update_time > TURN_STREAM_TIMEOUT_S:
                stale_keys.append(key)
        for key in stale_keys:
            TURN_STREAMS.pop(key, None)
    if stale_keys:
        LOGGER.warning("turn_stream_cleanup expired=%s", len(stale_keys))
    return len(stale_keys)


async def _drop_turn_streams_for_session(session_id: str) -> int:
    dropped = 0
    async with TURN_STREAMS_LOCK:
        for key in list(TURN_STREAMS.keys()):
            if key[0] == session_id:
                TURN_STREAMS.pop(key, None)
                dropped += 1
    return dropped


def _pcm16le_to_wav_bytes(*, pcm_bytes: bytes, sample_rate: int, channels: int) -> bytes:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be > 0")
    if channels <= 0:
        raise ValueError("channels must be > 0")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_fh:
        wav_fh.setnchannels(channels)
        wav_fh.setsampwidth(2)
        wav_fh.setframerate(sample_rate)
        wav_fh.writeframes(pcm_bytes)
    return buf.getvalue()


async def _run_turn_pipeline(
    *,
    session_id: str,
    request_id: str,
    return_fields: list[str],
    asr_payload: dict[str, Any],
    note_max_new_tokens: int,
    advice_max_new_tokens: int,
    stream_meta: dict[str, Any] | None = None,
    image_b64: str | None = None,
    image_mime: str | None = None,
    image_bytes: bytes | None = None,
    image_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with SESSIONS_LOCK:
        session = SESSIONS.get(session_id)
        if session is None:
            raise KeyError(f"unknown session_id: {session_id}")
        running_summary_before = session.running_summary
        patient_id = session.patient_id

    t0 = time.time()
    async with WORKFLOW_SEM:
        asr_response = await asyncio.wait_for(
            _call_asr(asr_payload),
            timeout=WORKFLOW_TIMEOUT_S,
        )
        asr_text = str(asr_response.get("text", "")).strip()
        if not asr_text:
            raise RuntimeError("ASR returned empty text")

        turn_max_new_tokens = max(note_max_new_tokens, advice_max_new_tokens)
        (
            note_full,
            advice_full,
            summary_turn,
            gemma_latency_ms,
            advice_regenerated,
            note_advice_similarity,
        ) = await asyncio.wait_for(
            _generate_turn_outputs(
                running_summary=running_summary_before,
                asr_text=asr_text,
                max_new_tokens=turn_max_new_tokens,
                request_id_prefix=request_id,
                image_b64=image_b64,
                image_mime=image_mime,
            ),
            timeout=WORKFLOW_TIMEOUT_S,
        )
        running_summary_after = _merge_running_summary(running_summary_before, summary_turn)
        (
            running_summary_after,
            summary_compress_latency_ms,
            summary_compressed,
        ) = await asyncio.wait_for(
            _compress_running_summary(
                running_summary=running_summary_after,
                request_id_prefix=request_id,
            ),
            timeout=WORKFLOW_TIMEOUT_S,
        )

    workflow_latency_ms = int((time.time() - t0) * 1000)
    turn_timestamp = _now_iso()
    async with SESSIONS_LOCK:
        session = SESSIONS.get(session_id)
        if session is None:
            raise KeyError(f"unknown session_id: {session_id}")

        turn_index = len(session.turns) + 1
        turn_record = {
            "timestamp_utc": turn_timestamp,
            "turn_index": turn_index,
            "request_id": request_id,
            "patient_id": session.patient_id,
            "asr_text": asr_text,
            "note_full": note_full,
            "advice_full": advice_full,
            "summary_turn": summary_turn,
            "running_summary_before": running_summary_before,
            "running_summary_after": running_summary_after,
            "latency_ms": {
                "asr": asr_response.get("latency_ms"),
                "gemma": gemma_latency_ms,
                "summary_compress": summary_compress_latency_ms,
                "workflow_total": workflow_latency_ms,
            },
            "asr_source": asr_response.get("source"),
            "asr_fallback_used": asr_response.get("fallback_used"),
            "summary_compressed": summary_compressed,
            "advice_regenerated": advice_regenerated,
            "note_advice_similarity": round(note_advice_similarity, 4),
        }
        if stream_meta:
            turn_record["stream"] = stream_meta
        if image_meta:
            turn_record["image"] = dict(image_meta)
        if image_bytes is not None:
            image_name = f"{turn_index:04d}_image{_image_suffix(image_bytes, image_mime)}"
            image_path = session.turns_dir / image_name
            try:
                image_path.write_bytes(image_bytes)
                turn_record.setdefault("image", {})["file"] = image_name
            except Exception as exc:
                LOGGER.warning(
                    "turn_image_save_failed session_id=%s request_id=%s turn_index=%s error=%s",
                    session_id,
                    request_id,
                    turn_index,
                    exc,
                )

        session.running_summary = running_summary_after
        session.summary_turns.append(summary_turn)
        session.ended_at_iso = None
        session.turns.append(turn_record)
        session.latest = {
            "asr_text": asr_text,
            "note_full": note_full,
            "advice_full": advice_full,
            "summary_turn": summary_turn,
            "running_summary": running_summary_after,
            "turn_index": turn_index,
            "timestamp_utc": turn_timestamp,
        }
        _append_turn_files(session, turn_record)

        response = {
            "op": "turn_result",
            "request_id": request_id,
            "session_id": session_id,
            "patient_id": patient_id,
            "turn_index": turn_index,
            "asr_text": asr_text,
            "note_full": note_full,
            "advice_full": advice_full,
            "summary_turn": summary_turn,
            "running_summary": running_summary_after,
            "returned": return_fields,
            "workflow_latency_ms": workflow_latency_ms,
            "asr_latency_ms": asr_response.get("latency_ms"),
            "gemma_latency_ms": gemma_latency_ms,
            "summary_compress_latency_ms": summary_compress_latency_ms,
            "asr_source": asr_response.get("source"),
            "asr_fallback_used": asr_response.get("fallback_used"),
            "summary_compressed": summary_compressed,
            "advice_regenerated": advice_regenerated,
            "note_advice_similarity": round(note_advice_similarity, 4),
        }
        if stream_meta and stream_meta.get("turn_id"):
            response["turn_id"] = stream_meta["turn_id"]

    return response


app = FastAPI(title="Med Workflow WebSocket Server")
WORKFLOW_SEM = asyncio.Semaphore(WORKFLOW_CONCURRENCY)
SESSIONS: dict[str, SessionState] = {}
SESSIONS_LOCK = asyncio.Lock()
TURN_STREAMS: dict[tuple[str, str], TurnStreamState] = {}
TURN_STREAMS_LOCK = asyncio.Lock()


def _client_addr(ws: WebSocket) -> str:
    client = getattr(ws, "client", None)
    if client is None:
        return "unknown"
    host = getattr(client, "host", "unknown")
    port = getattr(client, "port", "unknown")
    return f"{host}:{port}"


async def _send_error(ws: WebSocket, request_id: str, message: str) -> None:
    LOGGER.warning(
        "send_error client=%s request_id=%s error=%s",
        _client_addr(ws),
        request_id,
        message,
    )
    try:
        await ws.send_json(
            {
                "op": "error",
                "request_id": request_id,
                "error": message,
                "message": message,
            }
        )
    except RuntimeError:
        return


@app.get("/health")
async def health() -> dict[str, Any]:
    async with SESSIONS_LOCK:
        session_count = len(SESSIONS)
    async with TURN_STREAMS_LOCK:
        turn_stream_count = len(TURN_STREAMS)
    return {
        "ok": True,
        "service": "med-workflow",
        "asr_ws_url": ASR_WS_URL,
        "gemma_ws_url": GEMMA_WS_URL,
        "workflow_timeout_s": WORKFLOW_TIMEOUT_S,
        "workflow_concurrency": WORKFLOW_CONCURRENCY,
        "active_sessions": session_count,
        "active_turn_streams": turn_stream_count,
        "patients_root": str(PATIENTS_ROOT),
    }


@app.websocket("/ws")
async def ws_workflow(ws: WebSocket) -> None:
    await ws.accept()
    print(f"[WS CONNECT] client={_client_addr(ws)}", flush=True)
    LOGGER.info("ws_connect client=%s", _client_addr(ws))
    await ws.send_json(
        {
            "op": "ready",
            "service": "med-workflow",
            "asr_ws_url": ASR_WS_URL,
            "gemma_ws_url": GEMMA_WS_URL,
            "result_fields": list(ALLOWED_RESULT_FIELDS),
            "stream_ops": ["audio_begin", "audio_chunk", "audio_end"],
        }
    )
    LOGGER.info("ws_ready_sent client=%s", _client_addr(ws))

    try:
        while True:
            try:
                msg = await ws.receive_json()
            except WebSocketDisconnect:
                print(f"[WS DISCONNECT] client={_client_addr(ws)}", flush=True)
                LOGGER.info("ws_disconnect client=%s", _client_addr(ws))
                return
            except Exception:
                LOGGER.exception("ws_receive_error client=%s", _client_addr(ws))
                await _send_error(ws, "", "expected JSON payload")
                continue

            if not isinstance(msg, dict):
                await _send_error(ws, "", "expected JSON object payload")
                continue

            op = str(msg.get("op", "")).strip()
            request_id = str(msg.get("request_id", "")).strip() or _new_request_id()
            LOGGER.info(
                "ws_request client=%s op=%s request_id=%s",
                _client_addr(ws),
                op,
                request_id,
            )
            await _cleanup_stale_turn_streams()

            if op == "ping":
                await ws.send_json({"op": "pong", "request_id": request_id})
                LOGGER.info(
                    "ws_pong client=%s request_id=%s",
                    _client_addr(ws),
                    request_id,
                )
                continue

            if op == "start_session":
                requested_id = _sanitize_id(str(msg.get("session_id", "")), "")
                session_id = requested_id or _new_request_id()
                raw_patient_id = str(msg.get("patient_id", "")).strip()
                metadata = msg.get("metadata") or {}
                if not isinstance(metadata, dict):
                    await _send_error(ws, request_id, "metadata must be an object")
                    continue

                async with SESSIONS_LOCK:
                    existing = SESSIONS.get(session_id)
                    created = existing is None
                    if created:
                        if raw_patient_id:
                            patient_id = _sanitize_id(raw_patient_id, _new_auto_patient_id())
                        else:
                            patient_id = _new_auto_patient_id()
                        session = _new_session_state(
                            session_id=session_id,
                            patient_id=patient_id,
                            metadata=metadata,
                        )
                        SESSIONS[session_id] = session
                        turn_count = 0
                        session_dir = str(session.session_dir)
                    else:
                        turn_count = len(existing.turns)
                        session_dir = str(existing.session_dir)
                        patient_id = existing.patient_id

                await ws.send_json(
                    {
                        "op": "session_started",
                        "request_id": request_id,
                        "session_id": session_id,
                        "patient_id": patient_id,
                        "created": created,
                        "turn_count": turn_count,
                        "session_dir": session_dir,
                        "running_summary": (
                            session.running_summary if created else existing.running_summary
                        ),
                    }
                )
                LOGGER.info(
                    "session_started client=%s request_id=%s session_id=%s patient_id=%s created=%s turn_count=%s",
                    _client_addr(ws),
                    request_id,
                    session_id,
                    patient_id,
                    created,
                    turn_count,
                )
                continue

            if op == "audio_begin":
                session_id = str(msg.get("session_id", "")).strip()
                if not session_id:
                    await _send_error(ws, request_id, "missing session_id")
                    continue
                turn_id = _sanitize_id(str(msg.get("turn_id", "")), "") or _new_request_id()
                try:
                    sample_rate = int(msg.get("sample_rate", 16000))
                    channels = int(msg.get("channels", 1))
                    chunk_ms = int(msg.get("chunk_ms", 500))
                    seq_start = int(msg.get("seq_start", 1))
                except Exception:
                    await _send_error(
                        ws,
                        request_id,
                        "sample_rate/channels/chunk_ms/seq_start must be integers",
                    )
                    continue
                if sample_rate <= 0 or channels <= 0 or chunk_ms <= 0 or seq_start < 0:
                    await _send_error(
                        ws,
                        request_id,
                        "sample_rate/channels/chunk_ms must be > 0 and seq_start must be >= 0",
                    )
                    continue
                pcm_format = _canonical_pcm_format(
                    msg.get(
                        "audio_format",
                        msg.get("format", msg.get("pcm_format", "pcm_s16le")),
                    )
                )
                if not pcm_format:
                    await _send_error(
                        ws,
                        request_id,
                        "unsupported format; use pcm_s16le",
                    )
                    continue

                async with SESSIONS_LOCK:
                    if SESSIONS.get(session_id) is None:
                        await _send_error(ws, request_id, f"unknown session_id: {session_id}")
                        continue

                key = _stream_key(session_id, turn_id)
                async with TURN_STREAMS_LOCK:
                    if key in TURN_STREAMS:
                        await _send_error(
                            ws,
                            request_id,
                            f"stream already exists for session_id={session_id} turn_id={turn_id}",
                        )
                        continue
                    TURN_STREAMS[key] = TurnStreamState(
                        session_id=session_id,
                        turn_id=turn_id,
                        sample_rate=sample_rate,
                        channels=channels,
                        pcm_format=pcm_format,
                        chunk_ms=chunk_ms,
                        expected_seq=seq_start,
                    )

                await ws.send_json(
                    {
                        "op": "audio_ack",
                        "request_id": request_id,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "accepted": True,
                        "expected_seq": seq_start,
                        "sample_rate": sample_rate,
                        "channels": channels,
                        "audio_format": pcm_format,
                        "format": pcm_format,
                        "chunk_ms": chunk_ms,
                        "max_bytes": TURN_STREAM_MAX_BYTES,
                        "timeout_s": TURN_STREAM_TIMEOUT_S,
                    }
                )
                LOGGER.info(
                    "audio_begin_done client=%s request_id=%s session_id=%s turn_id=%s sample_rate=%s channels=%s chunk_ms=%s",
                    _client_addr(ws),
                    request_id,
                    session_id,
                    turn_id,
                    sample_rate,
                    channels,
                    chunk_ms,
                )
                continue

            if op == "audio_chunk":
                session_id = str(msg.get("session_id", "")).strip()
                turn_id = str(msg.get("turn_id", "")).strip()
                chunk_b64 = (
                    msg.get("audio_b64")
                    or msg.get("chunk_b64")
                    or msg.get("chunk_base64")
                    or msg.get("data_b64")
                )
                if not session_id:
                    await _send_error(ws, request_id, "missing session_id")
                    continue
                if not turn_id:
                    await _send_error(ws, request_id, "missing turn_id")
                    continue
                if chunk_b64 is None:
                    await _send_error(ws, request_id, "missing audio_b64")
                    continue
                try:
                    seq = int(msg.get("seq"))
                except Exception:
                    await _send_error(ws, request_id, "missing or invalid seq")
                    continue
                try:
                    chunk_bytes = base64.b64decode(str(chunk_b64), validate=True)
                except (ValueError, binascii.Error):
                    await _send_error(ws, request_id, "invalid base64 in audio_b64")
                    continue

                key = _stream_key(session_id, turn_id)
                async with TURN_STREAMS_LOCK:
                    stream = TURN_STREAMS.get(key)
                    if stream is None:
                        await _send_error(
                            ws,
                            request_id,
                            f"unknown stream for session_id={session_id} turn_id={turn_id}",
                        )
                        continue
                    if seq != stream.expected_seq:
                        await _send_error(
                            ws,
                            request_id,
                            f"out-of-order chunk: got seq={seq}, expected={stream.expected_seq}",
                        )
                        continue
                    next_size = len(stream.pcm_buffer) + len(chunk_bytes)
                    if next_size > TURN_STREAM_MAX_BYTES:
                        TURN_STREAMS.pop(key, None)
                        await _send_error(
                            ws,
                            request_id,
                            f"stream exceeded max bytes ({TURN_STREAM_MAX_BYTES}) and was dropped",
                        )
                        continue
                    stream.pcm_buffer.extend(chunk_bytes)
                    stream.expected_seq += 1
                    stream.last_update_time = time.time()
                    expected_seq = stream.expected_seq
                    total_bytes = len(stream.pcm_buffer)

                if _as_bool(msg.get("ack"), default=True):
                    await ws.send_json(
                        {
                            "op": "chunk_ack",
                            "request_id": request_id,
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "accepted": True,
                            "seq": seq,
                            "expected_seq": expected_seq,
                            "received_bytes": total_bytes,
                        }
                    )
                continue

            if op == "audio_end":
                session_id = str(msg.get("session_id", "")).strip()
                turn_id = str(msg.get("turn_id", "")).strip()
                if not session_id:
                    await _send_error(ws, request_id, "missing session_id")
                    continue
                if not turn_id:
                    await _send_error(ws, request_id, "missing turn_id")
                    continue
                try:
                    return_fields = _parse_return_fields(msg.get("return"))
                except ValueError as exc:
                    await _send_error(ws, request_id, str(exc))
                    continue

                try:
                    image_b64, image_mime, image_bytes, image_meta = _parse_optional_image_payload(
                        msg
                    )
                except ValueError as exc:
                    await _send_error(ws, request_id, str(exc))
                    continue

                key = _stream_key(session_id, turn_id)
                async with TURN_STREAMS_LOCK:
                    stream = TURN_STREAMS.pop(key, None)
                if stream is None:
                    await _send_error(
                        ws,
                        request_id,
                        f"unknown stream for session_id={session_id} turn_id={turn_id}",
                    )
                    continue
                if not stream.pcm_buffer:
                    await _send_error(ws, request_id, "audio stream is empty")
                    continue
                if len(stream.pcm_buffer) % 2 != 0:
                    stream.pcm_buffer = stream.pcm_buffer[:-1]
                if not stream.pcm_buffer:
                    await _send_error(ws, request_id, "audio stream is empty")
                    continue

                try:
                    chunk_length_s = float(msg.get("chunk_length_s", 20.0))
                    stride_length_s = float(msg.get("stride_length_s", 2.0))
                except Exception:
                    await _send_error(
                        ws,
                        request_id,
                        "chunk_length_s/stride_length_s must be numbers",
                    )
                    continue

                note_max_new_tokens = _clamp_max_tokens(
                    msg.get("note_max_new_tokens", DEFAULT_NOTE_MAX_NEW_TOKENS),
                    DEFAULT_NOTE_MAX_NEW_TOKENS,
                )
                advice_max_new_tokens = _clamp_max_tokens(
                    msg.get("advice_max_new_tokens", DEFAULT_ADVICE_MAX_NEW_TOKENS),
                    DEFAULT_ADVICE_MAX_NEW_TOKENS,
                )

                pcm_bytes = bytes(stream.pcm_buffer)
                try:
                    wav_bytes = _pcm16le_to_wav_bytes(
                        pcm_bytes=pcm_bytes,
                        sample_rate=stream.sample_rate,
                        channels=stream.channels,
                    )
                except Exception as exc:
                    await _send_error(ws, request_id, f"failed to encode wav: {exc}")
                    continue

                asr_payload: dict[str, Any] = {
                    "op": "transcribe",
                    "request_id": f"{request_id}:asr",
                    "audio_b64": base64.b64encode(wav_bytes).decode("utf-8"),
                    "sample_rate": stream.sample_rate,
                    "chunk_length_s": chunk_length_s,
                    "stride_length_s": stride_length_s,
                }
                stream_duration_ms = int(
                    1000 * len(pcm_bytes) / max(1, stream.sample_rate * stream.channels * 2)
                )
                stream_meta = {
                    "turn_id": turn_id,
                    "sample_rate": stream.sample_rate,
                    "channels": stream.channels,
                    "format": stream.pcm_format,
                    "chunk_ms": stream.chunk_ms,
                    "chunk_count": stream.expected_seq,
                    "pcm_bytes": len(pcm_bytes),
                    "duration_ms_estimate": stream_duration_ms,
                }
                LOGGER.info(
                    "audio_end_start client=%s request_id=%s session_id=%s turn_id=%s chunk_count=%s pcm_bytes=%s has_image_b64=%s image_mime=%s image_bytes=%s return_fields=%s",
                    _client_addr(ws),
                    request_id,
                    session_id,
                    turn_id,
                    stream.expected_seq,
                    len(pcm_bytes),
                    bool(image_b64),
                    image_mime or "",
                    len(image_bytes) if image_bytes is not None else 0,
                    return_fields,
                )
                try:
                    response = await _run_turn_pipeline(
                        session_id=session_id,
                        request_id=request_id,
                        return_fields=return_fields,
                        asr_payload=asr_payload,
                        note_max_new_tokens=note_max_new_tokens,
                        advice_max_new_tokens=advice_max_new_tokens,
                        stream_meta=stream_meta,
                        image_b64=image_b64,
                        image_mime=image_mime,
                        image_bytes=image_bytes,
                        image_meta=image_meta,
                    )
                except asyncio.TimeoutError:
                    LOGGER.warning(
                        "audio_end_timeout client=%s request_id=%s session_id=%s turn_id=%s timeout_s=%s",
                        _client_addr(ws),
                        request_id,
                        session_id,
                        turn_id,
                        WORKFLOW_TIMEOUT_S,
                    )
                    await _send_error(
                        ws,
                        request_id,
                        f"workflow timeout after {WORKFLOW_TIMEOUT_S}s",
                    )
                    continue
                except KeyError as exc:
                    await _send_error(ws, request_id, str(exc.args[0]))
                    continue
                except Exception as exc:
                    LOGGER.exception(
                        "audio_end_failed client=%s request_id=%s session_id=%s turn_id=%s error=%s",
                        _client_addr(ws),
                        request_id,
                        session_id,
                        turn_id,
                        exc,
                    )
                    await _send_error(ws, request_id, f"workflow failed: {exc}")
                    continue

                await ws.send_json(response)
                LOGGER.info(
                    "audio_end_done client=%s request_id=%s session_id=%s turn_id=%s turn_index=%s asr_latency_ms=%s gemma_latency_ms=%s summary_compress_latency_ms=%s workflow_latency_ms=%s",
                    _client_addr(ws),
                    request_id,
                    session_id,
                    turn_id,
                    response.get("turn_index"),
                    response.get("asr_latency_ms"),
                    response.get("gemma_latency_ms"),
                    response.get("summary_compress_latency_ms"),
                    response.get("workflow_latency_ms"),
                )
                continue

            if op == "process_audio":
                session_id = str(msg.get("session_id", "")).strip()
                if not session_id:
                    await _send_error(ws, request_id, "missing session_id")
                    continue

                try:
                    return_fields = _parse_return_fields(msg.get("return"))
                except ValueError as exc:
                    await _send_error(ws, request_id, str(exc))
                    continue

                async with SESSIONS_LOCK:
                    if SESSIONS.get(session_id) is None:
                        await _send_error(ws, request_id, f"unknown session_id: {session_id}")
                        continue

                audio_b64 = msg.get("audio_b64")
                audio_path = msg.get("audio_path")
                if not audio_b64 and not audio_path:
                    await _send_error(ws, request_id, "missing audio_b64 or audio_path")
                    continue

                try:
                    image_b64, image_mime, image_bytes, image_meta = _parse_optional_image_payload(
                        msg
                    )
                except ValueError as exc:
                    await _send_error(ws, request_id, str(exc))
                    continue

                LOGGER.info(
                    "process_audio_start client=%s request_id=%s session_id=%s has_audio_b64=%s has_audio_path=%s has_image_b64=%s image_mime=%s image_bytes=%s return_fields=%s",
                    _client_addr(ws),
                    request_id,
                    session_id,
                    bool(audio_b64),
                    bool(audio_path),
                    bool(image_b64),
                    image_mime or "",
                    len(image_bytes) if image_bytes is not None else 0,
                    return_fields,
                )

                try:
                    sample_rate = int(msg.get("sample_rate", 16000))
                    chunk_length_s = float(msg.get("chunk_length_s", 20.0))
                    stride_length_s = float(msg.get("stride_length_s", 2.0))
                except Exception:
                    await _send_error(
                        ws,
                        request_id,
                        "sample_rate/chunk_length_s/stride_length_s must be numeric",
                    )
                    continue

                note_max_new_tokens = _clamp_max_tokens(
                    msg.get("note_max_new_tokens", DEFAULT_NOTE_MAX_NEW_TOKENS),
                    DEFAULT_NOTE_MAX_NEW_TOKENS,
                )
                advice_max_new_tokens = _clamp_max_tokens(
                    msg.get("advice_max_new_tokens", DEFAULT_ADVICE_MAX_NEW_TOKENS),
                    DEFAULT_ADVICE_MAX_NEW_TOKENS,
                )

                asr_payload: dict[str, Any] = {
                    "op": "transcribe",
                    "request_id": f"{request_id}:asr",
                    "sample_rate": sample_rate,
                    "chunk_length_s": chunk_length_s,
                    "stride_length_s": stride_length_s,
                }
                if audio_b64:
                    asr_payload["audio_b64"] = audio_b64
                if audio_path:
                    asr_payload["audio_path"] = audio_path

                try:
                    response = await _run_turn_pipeline(
                        session_id=session_id,
                        request_id=request_id,
                        return_fields=return_fields,
                        asr_payload=asr_payload,
                        note_max_new_tokens=note_max_new_tokens,
                        advice_max_new_tokens=advice_max_new_tokens,
                        image_b64=image_b64,
                        image_mime=image_mime,
                        image_bytes=image_bytes,
                        image_meta=image_meta,
                    )
                except asyncio.TimeoutError:
                    LOGGER.warning(
                        "process_audio_timeout client=%s request_id=%s session_id=%s timeout_s=%s",
                        _client_addr(ws),
                        request_id,
                        session_id,
                        WORKFLOW_TIMEOUT_S,
                    )
                    await _send_error(
                        ws,
                        request_id,
                        f"workflow timeout after {WORKFLOW_TIMEOUT_S}s",
                    )
                    continue
                except KeyError as exc:
                    await _send_error(ws, request_id, str(exc.args[0]))
                    continue
                except Exception as exc:
                    LOGGER.exception(
                        "process_audio_failed client=%s request_id=%s session_id=%s error=%s",
                        _client_addr(ws),
                        request_id,
                        session_id,
                        exc,
                    )
                    await _send_error(ws, request_id, f"workflow failed: {exc}")
                    continue

                await ws.send_json(response)
                LOGGER.info(
                    "process_audio_done client=%s request_id=%s session_id=%s turn_index=%s asr_latency_ms=%s gemma_latency_ms=%s summary_compress_latency_ms=%s workflow_latency_ms=%s",
                    _client_addr(ws),
                    request_id,
                    session_id,
                    response.get("turn_index"),
                    response.get("asr_latency_ms"),
                    response.get("gemma_latency_ms"),
                    response.get("summary_compress_latency_ms"),
                    response.get("workflow_latency_ms"),
                )
                continue

            if op == "get_latest":
                session_id = str(msg.get("session_id", "")).strip()
                what = str(msg.get("what", "")).strip()
                if not session_id:
                    await _send_error(ws, request_id, "missing session_id")
                    continue
                if what not in ALLOWED_LATEST_FIELDS:
                    await _send_error(
                        ws,
                        request_id,
                        f"unsupported field in 'what': {what}",
                    )
                    continue

                async with SESSIONS_LOCK:
                    session = SESSIONS.get(session_id)
                    if session is None:
                        await _send_error(ws, request_id, f"unknown session_id: {session_id}")
                        continue
                    value = session.latest.get(what)
                    turn_index = session.latest.get("turn_index")

                if value is None:
                    await _send_error(
                        ws,
                        request_id,
                        f"no latest value for field: {what}",
                    )
                    continue

                payload = {
                    "op": "get_result",
                    "request_id": request_id,
                    "session_id": session_id,
                    "turn_index": turn_index,
                    "what": what,
                }
                if isinstance(value, str):
                    payload["text"] = value
                else:
                    payload["data"] = value

                await ws.send_json(payload)
                LOGGER.info(
                    "get_latest_done client=%s request_id=%s session_id=%s what=%s turn_index=%s",
                    _client_addr(ws),
                    request_id,
                    session_id,
                    what,
                    turn_index,
                )
                continue

            if op in {"summarize_session", "end_session"}:
                session_id = str(msg.get("session_id", "")).strip()
                if not session_id:
                    await _send_error(ws, request_id, "missing session_id")
                    continue

                close_session = _as_bool(
                    msg.get("close_session"),
                    default=(op == "end_session"),
                )
                include_transcript = _as_bool(msg.get("include_transcript"), default=False)
                async with SESSIONS_LOCK:
                    session = SESSIONS.get(session_id)
                    if session is None:
                        await _send_error(ws, request_id, f"unknown session_id: {session_id}")
                        continue

                    turns_snapshot = list(session.turns)
                    latest = dict(session.latest)
                    patient_id = session.patient_id
                    now_iso = _now_iso()
                    if close_session:
                        session.ended_at_iso = now_iso
                    _write_session_meta(session)

                    summary_payload = {
                        "timestamp_utc": now_iso,
                        "session_id": session_id,
                        "patient_id": patient_id,
                        "turn_count": len(turns_snapshot),
                        "running_summary": session.running_summary,
                        "latest": latest,
                        "session_closed": close_session,
                    }
                    _write_session_summary(session, summary_payload)
                    if close_session:
                        SESSIONS.pop(session_id, None)

                dropped_streams = 0
                if close_session:
                    dropped_streams = await _drop_turn_streams_for_session(session_id)

                response = {
                    "op": "session_summary",
                    "request_id": request_id,
                    "session_id": session_id,
                    "patient_id": patient_id,
                    "turn_count": len(turns_snapshot),
                    "running_summary": summary_payload["running_summary"],
                    "note_full": latest.get("note_full", ""),
                    "advice_full": latest.get("advice_full", ""),
                    "summary_turn": latest.get("summary_turn", ""),
                    "session_closed": close_session,
                }
                if include_transcript:
                    response["transcript_turns"] = [item["asr_text"] for item in turns_snapshot]
                if dropped_streams:
                    response["dropped_streams"] = dropped_streams

                await ws.send_json(response)
                LOGGER.info(
                    "session_summary_done client=%s request_id=%s session_id=%s turn_count=%s session_closed=%s dropped_streams=%s",
                    _client_addr(ws),
                    request_id,
                    session_id,
                    len(turns_snapshot),
                    close_session,
                    dropped_streams,
                )
                continue

            if op == "discard_session":
                session_id = str(msg.get("session_id", "")).strip()
                if not session_id:
                    await _send_error(ws, request_id, "missing session_id")
                    continue

                async with SESSIONS_LOCK:
                    existed = SESSIONS.pop(session_id, None) is not None
                dropped_streams = await _drop_turn_streams_for_session(session_id)

                await ws.send_json(
                    {
                        "op": "session_discarded",
                        "request_id": request_id,
                        "session_id": session_id,
                        "existed": existed,
                        "dropped_streams": dropped_streams,
                    }
                )
                LOGGER.info(
                    "session_discarded client=%s request_id=%s session_id=%s existed=%s dropped_streams=%s",
                    _client_addr(ws),
                    request_id,
                    session_id,
                    existed,
                    dropped_streams,
                )
                continue

            await _send_error(ws, request_id, f"unknown op: {op}")
    except WebSocketDisconnect:
        print(f"[WS DISCONNECT] client={_client_addr(ws)}", flush=True)
        LOGGER.info("ws_disconnect client=%s", _client_addr(ws))
        return
