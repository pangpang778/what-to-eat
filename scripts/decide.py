#!/usr/bin/env python3
"""T5（#6）拍板引擎：collect → jev 筛选 → 记忆过滤 → 拍板 → 自动记录。

编排 docs/SCHEMA.md 全管线，唯一数据缝是拍板 schema。任何失败显式降级
（verdict.degraded[]），产物永不失败——本模块不抛异常。

拍板规则：候选池内按 jev_score × 记忆权重排序，同分加确定性抖动
（location+日期+名字 的 sha256 → 同日多次拍板可复现，但同分能轮换）。
mode="alternate" 排除最近一次同地点拍板后出次优。

降级语义（与 jev_client 契约一致）：
- collect 失败/零候选 → 启发式孪生（heuristics_fallback，诚实标注非实时采集）
- jev 显式关闭（disabled）→ 全池交宿主 AI 复核，degraded 记录
- jev 不可用（no_key/limit/timeout...）→ 全池交宿主 AI 复核，degraded 记录
- jev 全拒（达标数 0 且未降级）→ all_candidates_rejected + 规则孪生拍板
  （低分素材不入 verdict——untrusted/低分素材铁律）
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from scripts import collect_eats, jev_client
from scripts import memory as memory_mod

ADVISORY = "advisory"


def heuristics_fallback(request: dict[str, Any], reason: str) -> dict[str, Any]:
    """无候选时的规则化兜底 verdict（诚实标注「非实时采集」）。"""
    return {
        "pick": {"name": "（宿主 AI 建议：本地家常菜一份）", "category": "未采集"},
        "reason": "实时采集不可用，已降级为宿主 AI 建议（非实时采集）",
        "alternates_hint": [],
        "degraded": [reason],
    }


def run_pipeline(
    request: dict[str, Any],
    workdir: str | Any,
    memory_path: str | Any,
    today: date | None = None,
) -> dict[str, Any]:
    """完整拍板编排，返回 docs/SCHEMA.md 缝形状：
    {request, candidates, verdict, memory, alerts, pipeline{collect, jev}}。
    """
    today = today or date.today()
    alerts: list[dict[str, Any]] = []
    degraded: list[str] = []
    location = str(request.get("location", "")).strip()

    # 1. 采集（collect 自身承诺不抛；双保险）
    try:
        collected = collect_eats.collect(location, workdir)
    except Exception as exc:
        collected = {
            "ok": False,
            "candidates": [],
            "notes": 0,
            "images": 0,
            "tool": collect_eats.TOOL,
            "degraded": collect_eats.DEGRADED,
            "message": str(exc)[:200],
        }
    raw_candidates = collected.get("candidates") or []
    candidates = [
        cand
        for cand in raw_candidates
        if isinstance(cand, dict) and str(cand.get("name", "")).strip()
    ]
    if len(candidates) != len(raw_candidates):
        degraded.append("collect_invalid_candidates: 已剔除无效候选")
    if not collected.get("ok") or not candidates:
        return _fallback(
            request, collected, "collect_failed: 启发式孪生",
            degraded, alerts, summary=None,
            today=today, location=location, memory_path=memory_path,
        )

    # 2. jev 素材筛选
    enabled = jev_client.material_point_enabled()
    all_rejected = False
    summary: dict[str, Any]
    if enabled:
        client = jev_client.JevClient()
        pool: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for cand in candidates:
            try:
                result = jev_client.score_candidate(client, cand)
            except Exception:
                result = {"jev_score": None, "adopted": False, "degraded": "internal_error"}
                client.degraded = True
                client.reason = client.reason or "internal_error"
            image = cand.get("image")
            if isinstance(image, dict):
                if result.get("jev_score") is not None:
                    image["jev_score"] = result["jev_score"]
                    image["adopted"] = bool(result["adopted"])
                else:
                    # 未评上分的素材不得保留达标标记（schema：仅 jev 达标 adopted=true）
                    image.pop("jev_score", None)
                    image.pop("adopted", None)
            (pool if result["adopted"] else rejected).append(cand)
        summary = jev_client.jev_summary(client, len(pool))
        if client.degraded:
            # jev 不可用 → 全池交宿主 AI 复核（不按低分误杀）
            degraded.append("jev_{}: 宿主 AI 复核".format(client.reason))
            pool = list(candidates)
        else:
            for cand in rejected:
                image = cand.get("image") or {}
                alerts.append({
                    "source": "JEV",
                    "type": "素材筛选",
                    "level": ADVISORY,
                    "title": "「{}」素材 jev {} < {} 未达标，已从候选剔除".format(
                        cand.get("name", ""), _fmt(image.get("jev_score")),
                        _fmt(client.threshold),
                    ),
                    "detail": "候选池 {} 个，{} 个达标；阈值 {}，限次 {}，实际调用 {} 次。".format(
                        len(candidates), len(pool), _fmt(client.threshold),
                        client.limit, client.calls_used,
                    ),
                })
            if not pool:
                # 全拒 → 启发式孪生从全量候选里按规则挑（低分素材不入 verdict）
                all_rejected = True
                degraded.append("all_candidates_rejected: 启发式孪生")
                pool = list(candidates)
    else:
        degraded.append("jev_disabled: 宿主 AI 复核")
        pool = list(candidates)
        for cand in pool:  # jev 未参与：剥掉任何遗留达标标记
            image = cand.get("image")
            if isinstance(image, dict):
                image.pop("jev_score", None)
                image.pop("adopted", None)
        summary = jev_client.jev_summary(jev_client.JevClient(), 0, enabled=False)

    # 3. 记忆过滤（忌口硬过滤含 request.constraints.taboos / 近 3 天 / 低权重）
    memory = memory_mod.load_memory(memory_path)
    effective = dict(memory)
    request_taboos = [
        str(t) for t in ((request.get("constraints") or {}).get("taboos") or [])
    ]
    effective["taboos"] = list(dict.fromkeys(
        [str(t) for t in memory.get("taboos", [])] + request_taboos
    ))
    pool, filtered_note = memory_mod.filter_candidates(pool, effective, location, today)
    if filtered_note:
        alerts.append({
            "source": "RULE",
            "type": "记忆过滤",
            "level": ADVISORY,
            "title": "记忆过滤剔除部分候选",
            "detail": filtered_note,
        })

    # 5. alternate 模式：排除最近一次同地点拍板
    prev_pick = None
    if request.get("mode") == "alternate":
        for entry in reversed(memory.get("eaten_log", [])):
            if entry.get("location") == location and entry.get("pick"):
                prev_pick = str(entry["pick"])
                break
        if prev_pick:
            pool = [c for c in pool if c.get("name") != prev_pick]

    if not pool:
        reason = (
            "alternate_exhausted: 启发式孪生"
            if prev_pick else "memory_filtered_all: 启发式孪生"
        )
        return _fallback(
            request, collected, reason, degraded, alerts, summary=summary,
            today=today, location=location, memory_path=memory_path,
        )

    # 4. 拍板：jev_score × weight 降序 + 确定性抖动
    weights = memory.get("weights", {})

    def rank(cand: dict[str, Any]) -> tuple[float, str]:
        score = (cand.get("image") or {}).get("jev_score")
        base = float(score) if isinstance(score, (int, float)) else 0.0
        weight = float(weights.get(str(cand.get("name", "")), 1.0))
        key = base * weight + _jitter(location, today, str(cand.get("name", "")))
        return (-key, str(cand.get("name", "")))

    pool.sort(key=rank)
    picked = pool[0]
    alternates = [str(c.get("name", "")) for c in pool[1:4]]
    picked_score = (picked.get("image") or {}).get("jev_score")
    picked_weight = float(weights.get(str(picked.get("name", "")), 1.0))
    image = picked.get("image") if (picked.get("image") or {}).get("adopted") else None

    verdict: dict[str, Any] = {
        "pick": {
            key: picked[key]
            for key in ("name", "category", "description")
            if picked.get(key)
        },
        "reason": _reason(
            picked_score, picked_weight, all_rejected, prev_pick
        ),
        "alternates_hint": alternates,
        "degraded": degraded,
    }
    if image:
        verdict["image"] = image
    if all_rejected:
        verdict["all_candidates_rejected"] = True

    # 6. 拍板自动记录
    try:
        memory_mod.record_verdict(memory_path, location, verdict, today)
    except Exception:
        degraded.append("memory_write_failed: 拍板记录失败")

    return {
        "request": request,
        "candidates": candidates,
        "verdict": verdict,
        "memory": memory,
        "alerts": alerts,
        "pipeline": {"collect": collected, "jev": summary},
    }


def _fallback(
    request: dict[str, Any],
    collected: dict[str, Any],
    reason: str,
    degraded: list[str],
    alerts: list[dict[str, Any]],
    summary: dict[str, Any] | None,
    today: date,
    location: str,
    memory_path: str | Any,
) -> dict[str, Any]:
    """兜底信封：启发式孪生 verdict + 显式降级披露。"""
    degraded = [reason] + [d for d in degraded if d != reason]
    if not jev_client.material_point_enabled():
        degraded.append("jev_disabled: 宿主 AI 复核")
    verdict = heuristics_fallback(request, reason)
    verdict["degraded"] = degraded
    if summary is None:
        base = {
            "ok": False,
            "calls": 0,
            "limit": jev_client.call_limit(),
            "threshold": jev_client.score_threshold(),
            "adopted": 0,
            "degraded": True,
        }
        if not jev_client.material_point_enabled():
            base["reason"] = jev_client.DISABLED
        else:
            base["reason"] = "not_run"
        summary = base
    alerts = alerts + [{
        "source": "RULE",
        "type": "降级",
        "level": ADVISORY,
        "title": "拍板已降级为宿主 AI 建议",
        "detail": "；".join(degraded),
    }]
    memory = memory_mod.load_memory(memory_path)
    try:
        memory_mod.record_verdict(memory_path, location, verdict, today)
    except Exception:
        verdict["degraded"].append("memory_write_failed: 拍板记录失败")
    return {
        "request": request,
        "candidates": collected.get("candidates") or [],
        "verdict": verdict,
        "memory": memory,
        "alerts": alerts,
        "pipeline": {"collect": collected, "jev": summary},
    }


def _reason(
    score: Any, weight: float, all_rejected: bool, prev_pick: str | None
) -> str:
    """理由只引用 schema 内真实字段（jev_score/weights/近期记录），无编造。"""
    if all_rejected:
        text = "候选均未通过 jev 素材筛选，已按偏好权重降级挑选（宿主 AI 可复核）"
    elif isinstance(score, (int, float)):
        text = "jev 评分最高（{}），且未在近期吃过记录中".format(_fmt(score))
        if weight != 1.0:
            text += "，偏好权重 {}".format(_fmt(weight))
    else:
        text = "jev 未参与评分，已按偏好权重挑选（宿主 AI 复核素材）"
    if prev_pick:
        text += "；已避开上一次拍板「{}」".format(prev_pick)
    return text


def _jitter(location: str, today: date, name: str) -> float:
    """确定性抖动（0-0.0099）：同日同输入可复现，同分候选能轮换。"""
    seed = "{}|{}|{}".format(location, today.isoformat(), name)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest, 16) % 100 / 10000.0


def _fmt(value: Any) -> str:
    try:
        return "{:g}".format(float(value))
    except (TypeError, ValueError):
        return str(value)
