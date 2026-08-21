"""Strict, side-effect-free parsing of responsibility notices from model output."""

from __future__ import annotations

import json
import re

from ci_owner_agent.schemas import CiResponsibilityNotice


def parse_notice(raw: str) -> CiResponsibilityNotice | None:
    """Return the first valid notice using the Agent's established fallback order."""
    for text in notice_candidates(raw):
        notice = parse_notice_candidate(text)
        if notice is not None:
            return notice
    return None


def parse_notice_candidate(text: str) -> CiResponsibilityNotice | None:
    try:
        return CiResponsibilityNotice.model_validate_json(text)
    except Exception:
        try:
            return CiResponsibilityNotice.model_validate(json.loads(text))
        except Exception:
            return None


def notice_candidates(raw: str) -> list[str]:
    candidates = [raw.strip()]
    stripped = strip_code_fence(raw)
    if stripped not in candidates:
        candidates.append(stripped)
    for match in re.finditer(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.S | re.I):
        fenced = match.group(1).strip()
        if fenced and fenced not in candidates:
            candidates.append(fenced)
    json_object = extract_first_json_object(raw)
    if json_object and json_object not in candidates:
        candidates.append(json_object)
    return candidates


def extract_first_json_object(raw: str) -> str | None:
    decoder = json.JSONDecoder()
    for idx, char in enumerate(raw):
        if char != "{":
            continue
        try:
            _obj, end = decoder.raw_decode(raw[idx:])
        except json.JSONDecodeError:
            continue
        return raw[idx : idx + end]
    return None


def strip_code_fence(raw: str) -> str:
    text = raw.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.S | re.I)
    return match.group(1).strip() if match else text
