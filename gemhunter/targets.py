"""Optional local target table.

The built-in knowledge module remains the default brain. If a local
targets.yaml exists, this module merges those editable targets in without
requiring a code change.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.lower()]
    return [str(v).lower() for v in value if str(v).strip()]


def _normalize_rare(raw: dict) -> dict | None:
    label = str(raw.get("label") or raw.get("name") or "").strip()
    brand_any = _as_list(raw.get("brand_any") or raw.get("brand_terms"))
    must_any = _as_list(raw.get("must_any") or raw.get("must_terms") or raw.get("terms"))
    if not label or not brand_any or not must_any:
        return None
    return {"label": label, "brand_any": brand_any, "must_any": must_any}


@lru_cache(maxsize=4)
def load_targets(path: str | os.PathLike | None = None) -> dict:
    """Load optional targets.yaml. Bad/missing files produce an empty table."""
    raw_path = path or os.getenv("GEMHUNTER_TARGETS", "targets.yaml")
    target_path = Path(raw_path)
    if not target_path.exists():
        return {"rare": []}
    try:
        raw = yaml.safe_load(target_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"rare": []}
    rare = []
    for item in raw.get("rare", []) or []:
        if isinstance(item, dict):
            normalized = _normalize_rare(item)
            if normalized:
                rare.append(normalized)
    return {"rare": rare}


def rare_targets(defaults: list[dict]) -> list[dict]:
    seen, merged = set(), []
    for target in defaults + load_targets().get("rare", []):
        key = (target["label"].lower(), tuple(target["brand_any"]), tuple(target["must_any"]))
        if key in seen:
            continue
        seen.add(key)
        merged.append(target)
    return merged
