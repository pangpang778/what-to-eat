"""T2（#3）记忆层：跨会话本地 JSON 状态文件。

- 冷启动：文件不存在或损坏 JSON → 默认空记忆（损坏时不覆盖原文件，留人工抢救机会）
- 过滤：忌口硬过滤 / 近 N 天同 location 去重 / 低权重剔除
- 记录：拍板自动 append；评分调权（好评 +0.2 封顶 2.0，差评 -0.4 地板 0）

契约见 docs/SCHEMA.md「记忆」节；文件人类可读可手改（ensure_ascii=False, indent=2）。
"""

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

WEIGHT_DEFAULT = 1.0
WEIGHT_CEIL = 2.0
WEIGHT_FLOOR = 0.0
WEIGHT_FILTER_BELOW = 0.5
RATING_UP_BONUS = 0.2
RATING_DOWN_PENALTY = 0.4

def _cold_start() -> dict[str, Any]:
    return {"eaten_log": [], "taboos": [], "weights": {}}


def load_memory(path: str | Path) -> dict[str, Any]:
    """读记忆文件；不存在或损坏 JSON → 冷启动默认（不覆盖原文件）。"""
    p = Path(path)
    if not p.exists():
        return _cold_start()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return _cold_start()
    if not isinstance(data, dict):
        return _cold_start()
    memory = _cold_start()
    if isinstance(data.get("eaten_log"), list):
        memory["eaten_log"] = data["eaten_log"]
    if isinstance(data.get("taboos"), list):
        memory["taboos"] = data["taboos"]
    if isinstance(data.get("weights"), dict):
        memory["weights"] = data["weights"]
    return memory


def save_memory(path: str | Path, memory: dict[str, Any]) -> None:
    """原子写（tmp + rename），ensure_ascii=False 保持中文可读。"""
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(
        json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, p)


def filter_candidates(
    candidates: list[dict[str, Any]],
    memory: dict[str, Any],
    location: str,
    today: date | None = None,
    recent_days: int = 3,
) -> tuple[list[dict[str, Any]], str]:
    """拍板前过滤候选。返回 (合格候选, 被过滤原因摘要)。

    三条规则：忌口词命中 name/category/description 任一即剔除；
    近 recent_days 天内同 location 吃过的 pick 剔除（异 location 不误杀）；
    weights < 0.5 的剔除（未记录过的候选默认 1.0）。
    """
    today = today or date.today()
    cutoff = today - timedelta(days=recent_days - 1)
    taboos = [str(t) for t in memory.get("taboos", [])]
    recent = {
        entry.get("pick")
        for entry in memory.get("eaten_log", [])
        if entry.get("location") == location
        and _parse_date(entry.get("date")) is not None
        and cutoff <= _parse_date(entry.get("date")) <= today
    }
    weights = memory.get("weights", {})

    eligible: list[dict[str, Any]] = []
    by_taboo: list[str] = []
    by_recent: list[str] = []
    by_weight: list[str] = []
    for cand in candidates:
        name = str(cand.get("name", ""))
        haystack = " ".join(
            str(cand.get(key, "")) for key in ("name", "category", "description")
        )
        if any(t in haystack for t in taboos):
            by_taboo.append(name)
        elif name in recent:
            by_recent.append(name)
        elif float(weights.get(name, WEIGHT_DEFAULT)) < WEIGHT_FILTER_BELOW:
            by_weight.append(name)
        else:
            eligible.append(cand)

    notes: list[str] = []
    if by_taboo:
        notes.append("忌口过滤: " + "、".join(by_taboo))
    if by_recent:
        notes.append(f"近 {recent_days} 天同地点吃过: " + "、".join(by_recent))
    if by_weight:
        notes.append("低权重剔除: " + "、".join(by_weight))
    return eligible, "；".join(notes)


def record_verdict(
    path: str | Path,
    location: str,
    verdict: dict[str, Any],
    today: date | None = None,
) -> None:
    """拍板自动 append：verdict.pick 进 eaten_log（date=今天 ISO）。"""
    memory = load_memory(path)
    pick = verdict.get("pick", {})
    pick_name = pick.get("name", "") if isinstance(pick, dict) else str(pick)
    today = today or date.today()
    memory["eaten_log"].append(
        {"location": location, "pick": pick_name, "date": today.isoformat()}
    )
    save_memory(path, memory)


def apply_rating(
    path: str | Path,
    pick: str,
    rating: str,
    location: str | None = None,
    today: date | None = None,
) -> None:
    """评分调权：up → +0.2（封顶 2.0）；down → -0.4（地板 0）。

    同时把 rating 回写到该 pick 最近一条未评分的 eaten_log 记录（有则）。
    """
    if rating not in ("up", "down"):
        raise ValueError(f"rating 必须是 up/down，得到: {rating!r}")
    memory = load_memory(path)
    delta = RATING_UP_BONUS if rating == "up" else -RATING_DOWN_PENALTY
    current = float(memory["weights"].get(pick, WEIGHT_DEFAULT))
    memory["weights"][pick] = round(
        min(WEIGHT_CEIL, max(WEIGHT_FLOOR, current + delta)), 2
    )
    for entry in reversed(memory["eaten_log"]):
        if entry.get("pick") != pick or entry.get("rating"):
            continue
        if location is not None and entry.get("location") != location:
            continue
        entry["rating"] = rating
        break
    save_memory(path, memory)


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
