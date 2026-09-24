#!/usr/bin/env python3
"""T4（#5）jev 素材可信度判断点：独立客户端模块（本仓独立实现）。

只负责一件事：把候选（candidate）的素材描述发给 jev（TypeSafe System
One）打 0-10 分，把结果变成可降级的数据。鉴权走 env（绝不打印 key）、
超时 5s、失败重试 1 次、每次拍板限次（默认 20 次，env JEV_CALL_LIMIT
可调）。任何不可用（无 key / 超时 / 5xx / 4xx / 坏回包 / 超限 / 关闭）
都不抛异常，返回 degraded 结果让调用方退回启发式孪生（宿主 AI 自行
评估）并显式记录降级。

与 travel-guide 版的关键差异：**默认开启**（JEV_POINT_MATERIAL=0 才关
闭）——本 skill 核心价值依赖筛选；评分对象是采集管线产出的候选。

jev 契约：POST /v1/systemone，body 必含 model:"jev-latest" 与
questions:{credibility:{type:"score"}}（缺 model 会被 422 拒，真机验证过）。
"""

from __future__ import annotations

import json
import os
import http.client
import urllib.error
import urllib.request
from typing import Any, Callable

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"  # 必填：缺 model 字段会被 422 拒掉
QUESTION_KEY = "credibility"
# 4 档 criteria 真机验证（jev 按 0-3 档插值返回 raw score，展示分 = raw/3*10）
SCORE_INSTRUCTIONS = "这个美食候选作为「今晚吃什么」拍板推荐的素材匹配度与可信度评分（1-10）。"
SCORE_CRITERIA = [
    "1-3 描述空洞、明显广告或与地点无关",
    "4-6 有提及但证据弱、辨识度低",
    "7-8 描述具体、本地认可、有真实素材支撑",
    "9-10 高度具体且多来源佐证、招牌级",
]
DEFAULT_TIMEOUT = 5.0
DEFAULT_RETRIES = 1  # 首次失败后重试 1 次（每次尝试都计入限次预算）
DEFAULT_CALL_LIMIT = 20
DEFAULT_THRESHOLD = 7.0

# 降级原因（写入 pipeline.jev.reason / verdict.degraded[]）
NO_KEY = "no_key"
LIMIT_REACHED = "limit_reached"
TIMEOUT = "timeout"
NETWORK = "network"
HTTP_5XX = "http_5xx"
HTTP_4XX = "http_4xx"
INVALID_RESPONSE = "invalid_response"
DISABLED = "disabled"  # JEV_POINT_MATERIAL=0 显式关闭

# (method, url, payload, headers, timeout) -> (status, body_text)
Transport = Callable[[str, str, bytes, dict, float], "tuple[int, str]"]


def material_point_enabled() -> bool:
    """默认开启（本 skill 核心价值依赖筛选）；JEV_POINT_MATERIAL=0 才关闭。"""
    return os.environ.get("JEV_POINT_MATERIAL", "1") != "0"


def call_limit() -> int:
    try:
        return int(os.environ.get("JEV_CALL_LIMIT", DEFAULT_CALL_LIMIT))
    except ValueError:
        return DEFAULT_CALL_LIMIT


def score_threshold() -> float:
    try:
        return float(os.environ.get("JEV_SCORE_THRESHOLD", DEFAULT_THRESHOLD))
    except ValueError:
        return DEFAULT_THRESHOLD


def load_api_key() -> str:
    """从 env（TYPESAFE_API_KEY / JEV_API_KEY）或 JEV_ENV_FILE 指向的 .env 读 key，绝不输出。"""
    for name in ("TYPESAFE_API_KEY", "JEV_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    path = os.environ.get("JEV_ENV_FILE", "")
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("TYPESAFE_API_KEY") and "=" in line:
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value
    except OSError:
        return ""
    return ""


def _urllib_transport(method, url, payload, headers, timeout):
    request = urllib.request.Request(url, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            try:
                raw = response.read()
            except http.client.HTTPException:
                raise OSError("incomplete read")
            return response.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:  # 4xx/5xx 以 HTTPError 形态出现
        return error.code, error.read().decode("utf-8", "replace")


class JevClient:
    """每次拍板一个实例；calls_used 是全部 HTTP 尝试数（含重试）。"""

    def __init__(
        self,
        api_key: str | None = None,
        limit: int | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        threshold: float | None = None,
        url: str = API_URL,
        transport: Transport | None = None,
    ) -> None:
        self.api_key = load_api_key() if api_key is None else api_key
        self.limit = call_limit() if limit is None else limit
        self.timeout = DEFAULT_TIMEOUT if timeout is None else timeout
        self.retries = DEFAULT_RETRIES if retries is None else retries
        self.threshold = score_threshold() if threshold is None else threshold
        self.url = url
        self._transport = transport or _urllib_transport
        self.calls_used = 0
        self.degraded = False  # 任一次调用降级即置位（显式降级记录的依据）
        self.reason: str | None = None  # 最近一次降级原因

    def score(self, state: str) -> dict[str, Any]:
        """对一段素材描述打 0-10 分；失败路径一律返回 degraded 结果。"""
        if not self.api_key:
            return self._degrade(NO_KEY)
        if self.calls_used >= self.limit:
            return self._degrade(LIMIT_REACHED)

        payload = json.dumps(
            {
                "state": state,
                "model": MODEL,
                "questions": {
                    QUESTION_KEY: {
                        "type": "score",
                        "instructions": SCORE_INSTRUCTIONS,
                        "criteria": SCORE_CRITERIA,
                    }
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }

        reason = NETWORK
        for _ in range(self.retries + 1):
            if self.calls_used >= self.limit:  # 重试也吃预算；预算尽则带着真实失败原因降级
                break
            self.calls_used += 1
            try:
                status, body = self._transport(
                    "POST", self.url, payload, headers, self.timeout
                )
            except TimeoutError:
                reason = TIMEOUT
                continue
            except OSError:
                reason = NETWORK
                continue
            if 500 <= status < 600:
                reason = HTTP_5XX
                continue
            if status >= 400:  # 4xx（如 422 缺字段）不重试
                return self._degrade(HTTP_4XX)
            parsed = self._parse_score(body)
            if parsed is None:
                reason = INVALID_RESPONSE
                continue
            return {"ok": True, "score": parsed, "degraded": False, "reason": None}
        return self._degrade(reason)

    def _degrade(self, reason: str) -> dict[str, Any]:
        self.degraded = True
        self.reason = reason
        return {"ok": False, "score": None, "degraded": True, "reason": reason}

    @staticmethod
    def _parse_score(body: str) -> float | None:
        """兼容 answers.{key} 为数字或 {score: n} 两种回包形态，钳到 0-10。"""
        try:
            data = json.loads(body)
        except ValueError:
            return None
        answers = data.get("answers")
        if not isinstance(answers, dict):
            result = data.get("result")
            answers = result.get("answers") if isinstance(result, dict) else None
        if not isinstance(answers, dict):
            return None
        value = answers.get(QUESTION_KEY)
        if isinstance(value, dict):
            value = value.get("score")
        try:
            raw = float(value)
        except (TypeError, ValueError):
            return None
        # jev 对 4 档 criteria 做 0-3 档插值（真机验证），归一化到 0-10 展示分
        return max(0.0, min(10.0, raw / 3.0 * 10.0))


def score_candidate(client: JevClient, candidate: dict[str, Any]) -> dict[str, Any]:
    """对单个候选评分（docs/SCHEMA.md 候选形状）。

    把 name/category/description/image.alt_description 拼成评分描述发给
    jev；score ≥ threshold → adopted。返回
    {jev_score: float|None, adopted: bool, degraded?: str}，不抛异常。
    关闭（JEV_POINT_MATERIAL=0）时零 HTTP 调用，degraded="disabled"。
    """
    if not material_point_enabled():
        return {"jev_score": None, "adopted": False, "degraded": DISABLED}
    image = candidate.get("image") or {}
    parts = (
        str(candidate.get("name", "")),
        str(candidate.get("category", "")),
        str(candidate.get("description", "")),
        str(image.get("alt_description", "")),
    )
    state = " / ".join(part for part in parts if part)
    result = client.score(state)
    if result["degraded"]:
        return {
            "jev_score": None,
            "adopted": False,
            "degraded": result["reason"],
        }
    return {"jev_score": result["score"], "adopted": result["score"] >= client.threshold}


def jev_summary(
    client: JevClient, adopted: int, enabled: bool | None = None
) -> dict[str, Any]:
    """pipeline.jev 形状的 summary（docs/SCHEMA.md）：供管线接线披露。

    关闭时 reason="disabled"；成功 ok=True degraded=False reason=None。
    """
    if enabled is None:
        enabled = material_point_enabled()
    if not enabled:
        return {
            "ok": False,
            "calls": 0,
            "limit": client.limit,
            "threshold": client.threshold,
            "adopted": 0,
            "degraded": True,
            "reason": DISABLED,
        }
    return {
        "ok": not client.degraded,
        "calls": client.calls_used,
        "limit": client.limit,
        "threshold": client.threshold,
        "adopted": adopted,
        "degraded": client.degraded,
        "reason": client.reason,
    }
