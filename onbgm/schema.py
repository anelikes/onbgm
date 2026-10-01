"""乐谱格式的唯一标准：JSON Schema + 各织体可用的字段。

校验错误会被翻译成写给 agent 看的话：错在哪、应该怎么写、附一个例子；拼错的字段会猜"是不是想写 xx"。
`onbgm schema` 导出同一份 schema，编辑器和 agent 都可以直接用它校验。
"""
from __future__ import annotations

import difflib
from functools import lru_cache

from jsonschema import Draft202012Validator

# 各织体除公共字段外能用的字段；by_section 里能覆盖的字段
COMMON_TRACK_FIELDS = {"sound", "texture", "in", "gain", "by_section"}
TEXTURE_FIELDS = {
    "arpeggio": {"low"},
    "pad": {"range"},
    "bass-sustain": {"low"},
    "bass-pizz": {"low"},
    "timpani": {"low", "roll_into"},
    "crash": set(),
    "melody": {"notes", "gate", "articulation"},
    "line": {"notes", "gate", "articulation"},
    "groove": {"pattern", "laid_back"},
    "bassline": {"rhythm", "low", "articulation"},
    "comp": {"rhythm", "range", "roll"},
    "strum": {"pattern", "spread"},
}
SECTION_OVERRIDES = {"groove": {"pattern"}, "bassline": {"rhythm"}, "comp": {"rhythm"}, "strum": {"pattern"}}

TIME = {
    "anyOf": [{"type": "number", "minimum": 0}, {"type": "string", "pattern": r"^\s*(\d+:)?\d+(\.\d+)?\s*$"}],
    "description": "秒数，或 \"分:秒\" 字符串",
    "x-format": "秒数，或 \"分:秒\" 字符串",
    "examples": ["0:32", 12.5],
}
ENERGY = {
    "anyOf": [
        {"type": "number", "minimum": 0, "maximum": 1},
        {"type": "string", "pattern": r"^\s*(0(\.\d+)?|1(\.0+)?)\s*->\s*(0(\.\d+)?|1(\.0+)?)\s*$"},
    ],
    "description": "0~1 的能量，或段内渐变 \"起->止\"",
    "x-format": "0~1 的数字，或 \"起->止\" 渐变",
    "examples": [0.6, "0.4->0.9"],
}
STEPS = {
    "type": "string",
    "description": "节奏型：每小节 8/12/16/24/32 个字符（16 个即一个字符一个十六分音符），| 分小节，空格可随意加",
    "examples": ["x... ..x. ..x. ...."],
}


@lru_cache(maxsize=None)
def score_schema() -> dict:
    from .arrange import DRUM_KEYS, SOUNDS, TEXTURES  # 延迟导入，避免循环依赖

    option_schemas = {
        "notes": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "按段落逐音写旋律：{段落名: \"音符 音符 | 下一小节 …\"}；音符后加 ' 为断奏、> 为重音",
            "examples": [{"chorus": "G5/4 E5/8 G5/8 r/2 | A5/1"}],
        },
        "pattern": {
            "anyOf": [
                STEPS,
                {"type": "object", "propertyNames": {"enum": sorted(DRUM_KEYS)}, "additionalProperties": STEPS},
            ],
            "description": "strum：一行扫弦节奏型；groove：{鼓件名: 节奏型}",
            "examples": ["D... x.U. D.U. x.U.", {"kick": "x... ...x ..x. ....", "snare": ".... x... .... x..."}],
        },
        "rhythm": STEPS,
        "low": {"type": "integer", "minimum": 0, "maximum": 127, "description": "最低音区起点（MIDI 音高，60 = C4）"},
        "range": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 127},
                  "minItems": 2, "maxItems": 2, "description": "音区 [最低, 最高]（MIDI 音高）", "examples": [[48, 67]]},
        "roll": {"type": "number", "minimum": 0, "description": "和弦从低到高依次拨开的间隔（拍）"},
        "roll_into": {"type": "string", "description": "在这个段落之前一小节做渐强滚奏"},
        "gate": {"type": "number", "exclusiveMinimum": 0, "maximum": 1.5, "description": "音符实际长度占时值的比例"},
        "articulation": {"enum": ["normal", "staccato"],
                         "description": "整个声部默认的演奏法；staccato 为断奏（弦乐会改用跳弓采样）。单个音也可以用 ' 标断奏"},
        "laid_back": {"type": "number", "description": "军鼓/拍手往后拖的拍数"},
        "spread": {"type": "number", "minimum": 0, "description": "扫弦相邻两根弦的时间差（秒）"},
    }
    track_props = {
        "sound": {"enum": sorted(SOUNDS), "description": "音色"},
        "texture": {"enum": sorted(TEXTURES), "description": "织体：这个声部怎么演奏"},
        "in": {"anyOf": [{"const": "all"}, {"type": "array", "items": {"type": "string"}}],
               "description": "出现在哪些段落", "x-format": "段落名列表，或 \"all\"",
               "examples": [["verse", "chorus"]]},
        "gain": {"type": "number", "description": "在自动混音结果上再加减的音量（dB）"},
        "by_section": {
            "type": "object",
            "additionalProperties": {"type": "object"},
            "description": "某些段落换一套节奏型：{段落名: {pattern 或 rhythm: …}}",
            "examples": [{"chorus": {"rhythm": "1-1- 1-1- 1-1- 5-8-"}}],
        },
        **option_schemas,
    }
    track = {
        "type": "object",
        "required": ["sound"],
        "additionalProperties": False,
        "properties": track_props,
        "allOf": [
            {"if": {"required": ["texture"], "properties": {"texture": {"const": t}}},
             "then": {"propertyNames": {"enum": sorted(COMMON_TRACK_FIELDS | fields)}}}
            for t, fields in TEXTURE_FIELDS.items()
        ],
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "onbgm score",
        "description": "onbgm 乐谱：LLM 逐音编写、引擎渲染成纯器乐 BGM",
        "type": "object",
        "additionalProperties": False,
        "required": ["meta", "sections", "harmony"],
        "properties": {
            "title": {"type": "string", "description": "曲名"},
            "meta": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "bpm": {"anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"const": "auto"}],
                            "description": "速度；写 auto 则由 length 或段落的 at 反推", "examples": [90, "auto"]},
                    "length": {**TIME, "description": "总时长约束（配合 bpm: auto）"},
                    "key": {"type": "string", "description": "调性，仅作记录", "examples": ["D minor"]},
                    "loop": {"type": "boolean", "description": "true：输出可无缝循环的 loop.wav；false：输出 song.wav"},
                    "seed": {"type": "integer", "description": "人性化随机种子"},
                    "swing": {"type": "number", "minimum": 0.5, "maximum": 0.75,
                              "description": "摇摆：0.5 平直，0.58 左右是 lo-fi 的晃动感"},
                    "swing_unit": {"enum": [8, 16], "description": "16：十六分音符摇摆；8：八分音符摇摆"},
                    "tail": {**TIME, "description": "不循环时，乐谱结束后再留几秒余音"},
                },
            },
            "fx": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"lofi": {"type": "number", "minimum": 0, "maximum": 1,
                                        "description": "lo-fi 质感：磁带抖动、低通、轻微失真、黑胶底噪"}},
            },
            "sections": {
                "type": "array",
                "minItems": 1,
                "description": "段落，按播放顺序",
                "examples": [[{"name": "intro", "bars": 4, "energy": 0.3}, {"name": "chorus", "bars": 8, "energy": 0.9, "at": "0:12"}]],
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "bars"],
                    "properties": {
                        "name": {"type": "string"},
                        "bars": {"type": "integer", "minimum": 1},
                        "energy": ENERGY,
                        "at": {**TIME, "description": "这一段必须从这个时间开始"},
                    },
                },
            },
            "harmony": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "每段一行和弦：{段落名: \"C | Am | F G | G7\"}，| 分小节，同小节多个和弦平分",
                "examples": [{"verse": "G | D/F# | Em | C"}],
            },
            "tracks": {"type": "object", "additionalProperties": track},
        },
    }


_TYPE_NAMES = {"object": "键值对（映射）", "array": "列表", "string": "字符串", "number": "数字",
               "integer": "整数", "boolean": "true/false"}
# 写错位置的常见字段：顶层 → 应在 meta 里，meta 里 → 应在顶层
_MISPLACED = {("", "bpm"): " meta 里", ("", "key"): " meta 里", ("", "loop"): " meta 里", ("", "length"): " meta 里",
              ("", "swing"): " meta 里", ("meta", "title"): "顶层", ("meta", "fx"): "顶层", ("meta", "lofi"): " fx 里"}


def _path(err) -> str:
    return ".".join(str(p) for p in err.absolute_path)


def _example(schema: dict) -> str:
    ex = schema.get("examples")
    return f"，例如 {_dump(ex[0])}" if ex else ""


def _dump(v) -> str:
    import json
    return json.dumps(v, ensure_ascii=False)


def _kind(v) -> str:
    if isinstance(v, bool):
        return "true/false"
    return {dict: "键值对", list: "列表", str: "字符串", int: "整数", float: "数字"}.get(type(v), type(v).__name__)


def _track_texture(raw: dict, path: str) -> str | None:
    """路径是 tracks.<名字> 时，返回这个声部的织体。"""
    parts = path.split(".")
    if len(parts) != 2 or parts[0] != "tracks":
        return None
    cfg = (raw.get("tracks") or {}).get(parts[1])
    if not isinstance(cfg, dict):
        return None
    texture = cfg.get("texture", "melody" if "notes" in cfg else None)
    return texture if texture in TEXTURE_FIELDS else None


def _friendly(err, raw: dict) -> list[str]:
    path, schema, v = _path(err), err.schema, err.validator
    where = path or "顶层"
    texture = _track_texture(raw, path)
    if v == "enum" and "propertyNames" in err.schema_path and texture:
        # 某个织体不该用的字段（字段本身存在于其他织体）；完全不认识的字段由 additionalProperties 报告
        key = err.instance
        if key not in score_schema()["properties"]["tracks"]["additionalProperties"]["properties"]:
            return []
        users = [t for t, f in TEXTURE_FIELDS.items() if key in f]
        allowed = sorted(COMMON_TRACK_FIELDS | TEXTURE_FIELDS[texture])
        return [f"{where}: {texture} 织体不使用字段 '{key}'（{'、'.join(users)} 才用）；{texture} 可用: {', '.join(allowed)}"]
    if v == "additionalProperties" and isinstance(err.instance, dict):
        allowed = sorted(schema.get("properties", {}))
        if texture:
            allowed = sorted(COMMON_TRACK_FIELDS | TEXTURE_FIELDS[texture])
        out = []
        for key in err.instance:
            if key in schema.get("properties", {}):
                continue
            if (path, key) in _MISPLACED:
                out.append(f"{where}: '{key}' 应该写在{_MISPLACED[(path, key)]}")
                continue
            guess = difflib.get_close_matches(key, allowed, n=1)
            out.append(f"{where}: 不认识字段 '{key}'" + (f"，是不是想写 '{guess[0]}'？" if guess else "")
                       + f"（可用: {', '.join(allowed)}）")
        return out
    if v == "required":
        missing = err.message.split("'")[1]
        sub = schema.get("properties", {}).get(missing, {})
        return [f"{where}: 缺少字段 '{missing}'" + (f"（{sub['description']}）" if "description" in sub else "")
                + _example(sub)]
    if v == "type":
        expected = err.validator_value if isinstance(err.validator_value, str) else err.validator_value[0]
        return [f"{where}: 应为{_TYPE_NAMES.get(expected, expected)}，当前是{_kind(err.instance)}"
                + (f"。{schema['description']}" if "description" in schema else "") + _example(schema)]
    if v == "enum" or v == "const":
        options = err.validator_value if v == "enum" else [err.validator_value]
        guess = difflib.get_close_matches(str(err.instance), [str(o) for o in options], n=1)
        return [f"{where}: 不认识 {_dump(err.instance)}" + (f"，是不是想写 {guess[0]}？" if guess else "")
                + f"（可用: {', '.join(str(o) for o in options)}）"]
    if v in ("anyOf", "oneOf"):
        # 实例类型对上了某个分支、但在更深处出错（如鼓件名拼错）：报那个具体错误
        deeper = [e for e in err.context if e.validator not in ("type", "pattern", "const")]
        if deeper:
            return [m for e in deeper for m in _friendly(e, raw)]
        fmt = schema.get("x-format") or schema.get("description")
        return [f"{where}: 格式不对" + (f"，应为{fmt}" if fmt else "")
                + _example(schema) + f"（当前是 {_dump(err.instance)}）"]
    if v in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        sign = {"minimum": "≥", "maximum": "≤", "exclusiveMinimum": ">", "exclusiveMaximum": "<"}[v]
        return [f"{where}: 须 {sign} {err.validator_value}，当前是 {err.instance}"]
    if v in ("minItems", "maxItems"):
        return [f"{where}: 项数不对" + (f"，应为{schema['description']}" if "description" in schema else "")
                + _example(schema)]
    return [f"{where}: {err.message}"]


def validate(raw) -> list[str]:
    """按 schema 校验乐谱的结构；返回写给 agent 看的问题列表（空列表表示通过）。"""
    if not isinstance(raw, dict):
        return ["乐谱应是一个 YAML 映射，至少包含 meta、sections、harmony"]
    validator = Draft202012Validator(score_schema())
    problems = []
    for err in sorted(validator.iter_errors(raw), key=lambda e: (list(map(str, e.absolute_path)), e.validator)):
        if err.validator == "allOf":
            continue  # 由内部的 propertyNames 错误报告
        for msg in _friendly(err, raw):
            if msg not in problems:
                problems.append(msg)
    problems += _check_texture_specifics(raw)
    return problems


def _check_texture_specifics(raw: dict) -> list[str]:
    """schema 不方便表达的部分：织体缺省、by_section 的段落名和可覆盖字段、pattern 在不同织体下的类型。"""
    names = [s.get("name") for s in raw.get("sections") or [] if isinstance(s, dict)]
    out = []
    for name, cfg in (raw.get("tracks") or {}).items():
        if not isinstance(cfg, dict):
            continue
        where = f"tracks.{name}"
        texture = cfg.get("texture", "melody" if "notes" in cfg else None)
        if texture is None:
            out.append(f"{where}: 缺少 texture（或用 notes 逐音写旋律）；可用织体: {', '.join(TEXTURE_FIELDS)}")
            continue
        if texture not in TEXTURE_FIELDS:
            continue  # 已由 enum 报告
        pattern = cfg.get("pattern")
        if texture == "groove" and pattern is not None and not isinstance(pattern, dict):
            out.append(f"{where}.pattern: groove 的 pattern 应是 {{鼓件名: 节奏型}}，例如 "
                       f"{{\"kick\": \"x... ...x ..x. ....\", \"snare\": \".... x... .... x...\"}}")
        if texture == "strum" and pattern is not None and not isinstance(pattern, str):
            out.append(f"{where}.pattern: strum 的 pattern 应是一行扫弦节奏型，例如 \"D... x.U. D.U. x.U.\"")
        for sec, override in (cfg.get("by_section") or {}).items():
            if sec not in names:
                out.append(f"{where}.by_section.{sec}: 没有这个段落（现有: {', '.join(map(str, names))}）")
            allowed = SECTION_OVERRIDES.get(texture, set())
            for key in override or {}:
                if key not in allowed:
                    out.append(f"{where}.by_section.{sec}: {texture} 的段落覆盖里不能写 '{key}'"
                               + (f"（只能写 {', '.join(sorted(allowed))}）" if allowed else "（这个织体没有可按段落覆盖的字段）"))
    return out
