"""目录：可复用的写法（鼓组、扫弦、低音、和弦伴奏、和弦进行）和引擎提供的构件（织体、音色、鼓件、和弦类型、示例）。

`onbgm catalog --query "慵懒的鼓"` 按关键词搜索。中文会按字和双字切分，不需要精确匹配。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
KINDS = ["drums", "strum", "bassline", "comp", "arp", "progression", "sfx", "texture", "sound", "drum", "chord", "example"]


@lru_cache(maxsize=None)
def items() -> list[dict]:
    from .arrange import DRUM_KEYS, SOUNDS, TEXTURES
    from .library import HQ
    from .synth import PRESETS
    from .schema import COMMON_TRACK_FIELDS, TEXTURE_FIELDS
    from .theory import QUALITIES, pitch_name

    out = [dict(x) | {"tags": [str(t) for t in x.get("tags", [])]}  # YAML 会把 808 这类标签读成数字
           for x in yaml.safe_load((HERE / "catalog.yaml").read_text(encoding="utf-8"))]
    for name, fn in TEXTURES.items():
        doc = (fn.__doc__ or "").strip().splitlines()
        fields = ", ".join(sorted(TEXTURE_FIELDS[name] - COMMON_TRACK_FIELDS)) or "（无）"
        out.append({"name": name, "kind": "texture", "tags": ["织体", "texture"],
                    "description": f"{doc[0] if doc else ''} 专属字段: {fields}"})
    for name, s in SOUNDS.items():
        if s.drum:
            out.append({"name": name, "kind": "sound", "tags": ["鼓组", "drums", "kit"], "description": "鼓组音色"})
            continue
        quality = "内置合成器" if name in PRESETS else "高质量采样" if name in HQ else "GM"
        out.append({"name": name, "kind": "sound", "tags": ["音色", "sound"],
                    "description": f"音域 {pitch_name(s.lo)}–{pitch_name(s.hi)}，{'持续音' if s.sustained else '衰减音'}，{quality}"})
    for name in DRUM_KEYS:
        out.append({"name": name, "kind": "drum", "tags": ["鼓件", "drum"], "description": "groove 的 pattern 里可用的鼓件"})
    for q in QUALITIES:
        out.append({"name": f"C{q}", "kind": "chord", "tags": ["和弦", "chord"],
                    "description": f"和弦类型 '{q or '(大三)'}'，例如 C{q}、F#{q}、Bb{q}"})
    for path in sorted((HERE / "examples").glob("*.yaml")):
        first = next((ln.lstrip("# ").strip() for ln in path.read_text(encoding="utf-8").splitlines()
                      if ln.startswith("#")), "")
        out.append({"name": path.stem, "kind": "example", "tags": ["示例", "example"],
                    "description": f"{first}（onbgm example {path.stem}）"})
    return out


def _terms(query: str) -> tuple[list[str], list[str]]:
    """切词，返回 (词, 单字)。拉丁字母按词切；中文连续段切成双字词（"慵懒的鼓" → 慵懒、懒的、的鼓），
    单字另算：权重低、只匹配名字和标签，避免"追逐"里的"逐"命中"逐音写旋律"这类误报。"""
    words, chars = [], []
    for tok in re.findall(r"[A-Za-z0-9+\-#]+|[一-鿿]+", query.lower()):
        if re.match(r"[一-鿿]", tok):
            words += [tok[i:i + 2] for i in range(len(tok) - 1)] or [tok]
            chars += [c for c in tok if c not in "的和与了"]
        else:
            words.append(tok)
    return words, chars


def search(query: str | None = None, kind: str | None = None, limit: int = 8) -> list[dict]:
    pool = [x for x in items() if kind is None or x["kind"] == kind]
    if not query:
        return pool
    words, chars = _terms(query)
    scored = []
    for x in pool:
        name, tags, desc = x["name"].lower(), " ".join(x.get("tags", [])).lower(), x.get("description", "").lower()
        score = sum(3 * (t in name) + 2 * (t in tags) + (t in desc) for t in words)
        score += sum(0.5 * ((c in name) + (c in tags)) for c in chars)
        if score:
            scored.append((score, x))
    scored.sort(key=lambda s: -s[0])
    best = scored[0][0] if scored else 0
    return [x for score, x in scored[:limit] if score >= best * 0.3]  # 远低于最佳结果的不要


def format_items(found: list[dict], with_snippets: bool = True) -> str:
    lines = []
    for x in found:
        lines.append(f"[{x['kind']}] {x['name']} — {x.get('description', '')}")
        if with_snippets and x.get("snippet"):
            lines += ["    " + ln for ln in x["snippet"].rstrip().splitlines()]
    return "\n".join(lines)
