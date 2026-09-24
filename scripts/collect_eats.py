#!/usr/bin/env python3
"""T3（#4）采集管线：OpenCLI × 小红书 → 拍板 schema 候选素材。

薄适配器，不自己爬平台：OpenCLI 拥有浏览器会话与登录态（key/登录态从
环境读取，本脚本绝不读取/打印任何密钥值）。规则解析只负责把素材与
结构化笔记内容带回来——候选的深度提取是宿主 AI（skill 运行时）的事。

任何失败路径（opencli 不在 PATH/超时/零笔记/零候选）显式降级
（degraded="collect_failed"），不抛异常——产物永不失败是 schema 铁律。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.parse
import http.client
import ipaddress
import socket
from urllib.parse import urlparse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 120
IMAGE_TIMEOUT = 30
TOOL = "opencli"
DEGRADED = "collect_failed"
CREDIBILITY = "untrusted · 小红书 UGC（OpenCLI 采集）"

# 双关键词搜索策略（CONTEXT.md「采集」）：两词都搜，结果按 url 去重合并
SEARCH_SUFFIXES = ("美食攻略", "必吃")

# ponytail: --window background --site-session persistent 真机实测返回空结果（会话态问题），
# 最小 flag 集（-f json）实测有结果；若未来需要后台窗口再单测恢复
OPENCLI_FLAGS = ("-f", "json")

# 规则解析用品类关键词表（命中即停；未命中归「美食」）
CATEGORY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("火锅", "火锅"),
    ("串串", "串串"),
    ("烧烤", "烧烤"),
    ("烤肉", "烤肉"),
    ("小龙虾", "小龙虾"),
    ("胡辣汤", "汤食"),
    ("烩面", "面食"),
    ("拌面", "面食"),
    ("拉面", "面食"),
    ("米线", "米线"),
    ("湘菜", "湘菜"),
    ("川菜", "川菜"),
    ("粤菜", "粤菜"),
    ("日料", "日料"),
    ("寿司", "日料"),
    ("烤鸭", "烤鸭"),
    ("奶茶", "饮品"),
    ("咖啡", "饮品"),
    ("甜品", "甜品"),
)

_QUOTE_RE = re.compile(r"[「【]([^「」【】]{2,20})[」】]")


def _resolve_command(name: str) -> str | None:
    """Find commands installed by venvs or npm on Windows（travel-guide 真机同款）."""
    found = shutil.which(name)
    if found:
        found_path = Path(found)
        if found_path.suffix.lower() == ".cmd":
            powershell_wrapper = found_path.with_suffix(".ps1")
            if powershell_wrapper.exists():
                return str(powershell_wrapper)
        return found
    candidates = [
        Path.home() / ".agent-reach-venv" / "Scripts" / "{}.exe".format(name),
        Path.home() / "AppData" / "Roaming" / "npm" / "{}.cmd".format(name),
        Path.home() / "AppData" / "Roaming" / "npm" / "{}.exe".format(name),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _json_from_output(raw: str) -> Any:
    """Parse JSON even when a runtime prepends warnings to stdout."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
            return value
        except json.JSONDecodeError:
            continue
    return None


def _run_json(command: list[str], timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run a local JSON-producing command; never raises, returns a stable envelope."""
    resolved = _resolve_command(command[0])
    if not resolved:
        return {"ok": False, "error": "command_not_found", "message": command[0]}
    command = [resolved] + command[1:]
    if Path(resolved).suffix.lower() == ".ps1":
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if shell:
            command = [
                shell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                resolved,
            ] + command[1:]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "command_not_found", "message": command[0]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "message": command[0]}

    data = _json_from_output(completed.stdout)
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()[:500]
        return {"ok": False, "error": "command_failed", "message": message}
    if data is None:
        return {"ok": False, "error": "invalid_json", "message": completed.stdout.strip()[:500]}
    if isinstance(data, dict) and data.get("ok") is False:
        return {
            "ok": False,
            "error": data.get("error") or "tool_error",
            "message": str(data.get("message") or data.get("help") or "tool error")[:500],
        }
    return {"ok": True, "data": data}


def _records(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def search_keywords(location: str, constraints: dict[str, Any] | None = None) -> list[str]:
    """反问轮 v2：意图合成搜索词（docs/SCHEMA.md 反问轮 v2 契约）。

    位置锚点/场景（外卖、一人食）/预算 → 附加词；no_preference 维度不进搜索词。
    返回双关键词（SEARCH_SUFFIXES 各拼一条）。
    """
    head = str(location or "").strip()
    extras: list[str] = []
    cons = constraints if isinstance(constraints, dict) else {}
    anchor = cons.get("location_anchor")
    if isinstance(anchor, str) and anchor.strip():
        extras.append(anchor.strip())
    party = cons.get("party")
    if isinstance(party, str) and party.strip():
        extras.append(party.strip())
    budget = cons.get("budget")
    if isinstance(budget, str) and budget.strip():
        extras.append(budget.strip())
    if extras:
        head = "{} {}".format(head, " ".join(extras))
    return ["{} {}".format(head, suffix) for suffix in SEARCH_SUFFIXES]


def _search_notes(
    location: str,
    search_limit: int,
    timeout: int,
    keywords: list[str] | None = None,
) -> "tuple[list[dict[str, Any]], dict[str, Any] | None]":
    """双关键词搜索并按 url 去重；返回 (notes, error)，error 非 None 即失败路径。

    部分成功（一个关键词出笔记、另一个失败）不算失败——有素材就带回去。
    """
    seen: set[str] = set()
    notes: list[dict[str, Any]] = []
    last_error: dict[str, Any] | None = None
    use_keywords = keywords or search_keywords(location)
    for query in use_keywords:
        result = _run_json(
            [
                "opencli",
                "xiaohongshu",
                "search",
                query,
                "--limit",
                str(search_limit),
                *OPENCLI_FLAGS,
            ],
            timeout=timeout,
        )
        if not result["ok"]:
            last_error = result
            continue
        last_error = None
        for record in _records(result.get("data"), "notes", "results", "data", "items"):
            url = str(record.get("url") or record.get("link") or "")
            if url and url in seen:
                continue
            seen.add(url)
            notes.append(record)
    if not notes:
        return [], last_error or {"error": "empty_result", "message": "search returned no notes"}
    return notes[:search_limit], None


def _read_note(url: str, timeout: int) -> dict[str, Any]:
    return _run_json(["opencli", "xiaohongshu", "note", url, *OPENCLI_FLAGS], timeout=timeout)


def _detail_fields(data: Any) -> "tuple[str, list[dict[str, Any]], str]":
    """Extract note full text, image list, and engagement line from an
    OpenCLI note payload. engagement 是 likes/collects 人气细节（jev 9-10
    档 criteria 的证据），拼进评分描述。"""
    text = ""
    images: list[dict[str, Any]] = []
    engagement = ""
    # opencli note -f json 回 field-value 行数组；先归一成 dict 再取字段
    if isinstance(data, list) and data and all(
        isinstance(item, dict) and "field" in item for item in data
    ):
        data = {item["field"]: item.get("value") for item in data}
    if isinstance(data, dict):
        for key in ("desc", "text", "content"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
        raw = data.get("images")
        if isinstance(raw, list):
            images = [item for item in raw if isinstance(item, dict)]
        likes = str(data.get("likes") or "").strip()
        collects = str(data.get("collects") or "").strip()
        parts = []
        if likes.isdigit():
            parts.append("点赞 {}".format(likes))
        if collects.isdigit():
            parts.append("收藏 {}".format(collects))
        engagement = " · ".join(parts)
    return text, images, engagement


def _image_url(image: dict[str, Any]) -> str:
    for key in ("url", "img", "image"):
        value = str(image.get(key) or "")
        if value.startswith("http"):
            return value
    return ""


def _image_alt(image: dict[str, Any]) -> str:
    for key in ("alt_description", "alt", "description"):
        value = str(image.get(key) or "").strip()
        if value:
            return value
    return ""


def _download(url: str, dest: Path, timeout: int = IMAGE_TIMEOUT) -> bool:
    """下载图片到本地；只允许 https 公网地址（untrusted UGC 提供的 URL 不可信）。"""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        # 拒绝私网/环回/链路本地（SSRF 防护：URL 来自 untrusted 素材）
        infos = socket.getaddrinfo(parsed.hostname, None)
        for info in infos:
            if ipaddress.ip_address(info[4][0]).is_private or ipaddress.ip_address(info[4][0]).is_loopback                     or ipaddress.ip_address(info[4][0]).is_link_local:
                return False
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response, open(dest, "wb") as handle:
            handle.write(response.read())
        return True
    except (OSError, ValueError, http.client.HTTPException):
        return False


def _category(text: str) -> str:
    for keyword, category in CATEGORY_KEYWORDS:
        if keyword in text:
            return category
    return "美食"


# ponytail: 每篇笔记只本地化首图（候选 image 是单图槽）；需要图集时扩成多图
def _localize_images(
    raw_images: list[dict[str, Any]], note_index: int, workdir: Path, source: str
) -> list[dict[str, Any]]:
    images_dir = workdir / "images"
    localized: list[dict[str, Any]] = []
    for offset, image in enumerate(raw_images):
        url = _image_url(image)
        if not url:
            continue
        ext = Path(urllib.parse.urlparse(url).path).suffix
        if not ext or len(ext) > 5 or not ext.startswith("."):
            ext = ".jpg"
        filename = "eats-{}-{}{}".format(note_index, offset + 1, ext)
        dest = images_dir / filename
        if not _download(url, dest):
            continue
        localized.append(
            {
                "url": "images/{}".format(filename),
                "source": source,
                "credibility": CREDIBILITY,
                "alt_description": _image_alt(image),
            }
        )
    return localized


def collect(
    location: str,
    workdir: str | Path,
    search_limit: int = 8,
    timeout: int = DEFAULT_TIMEOUT,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """采集小红书美食笔记，产出拍板 schema 的候选素材。

    Returns {ok, candidates, notes, images, tool, degraded?, message?}：
    - candidates: name/category/description/image/note_source（untrusted 铁律）
    - notes: 采集到的笔记数；images: 本地化落盘的图片数
    - 任何失败路径显式降级（degraded="collect_failed"），不抛异常
    """
    failed: dict[str, Any] = {
        "ok": False,
        "candidates": [],
        "notes": 0,
        "images": 0,
        "tool": TOOL,
        "degraded": DEGRADED,
    }
    location = str(location or "").strip()
    if not location:
        failed["message"] = "location is empty"
        return failed
    try:
        return _collect(str(location), Path(workdir), search_limit, timeout, failed, constraints)
    except Exception as exc:  # 兜底：产物永不失败
        failed["message"] = "unexpected: {}".format(exc)
        return failed


def _collect(
    location: str,
    workdir: Path,
    search_limit: int,
    timeout: int,
    failed: dict[str, Any],
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)  # 根因修：工作目录由 collect 自建，调用方不用记得 mkdir
    keywords = search_keywords(location, constraints)
    notes, error = _search_notes(location, search_limit, timeout, keywords)
    if error is not None:
        failed["message"] = str(error.get("message") or error.get("error") or "search failed")
        return failed

    candidates: list[dict[str, Any]] = []
    images = 0
    for index, note in enumerate(notes):
        title = str(note.get("title") or note.get("name") or "").strip()
        author = str(note.get("author") or note.get("author_name") or "").strip()
        url = str(note.get("url") or note.get("link") or "").strip()

        text, raw_images, engagement = "", [], ""
        if url:
            detail = _read_note(url, timeout)
            if detail["ok"] and detail.get("data") is not None:
                text, raw_images, engagement = _detail_fields(detail["data"])

        source = url or "小红书"
        localized = _localize_images(raw_images, index + 1, workdir, source)
        images += len(localized)

        note_source: dict[str, Any] = {"title": title, "author": author}
        if url:
            note_source["url"] = url

        text = text.strip()
        names = [title] if title else []
        for match in _QUOTE_RE.findall(text):
            name = match.strip()
            if name and name not in names:
                names.append(name)

        description = (text.splitlines() or [""])[0][:120] if text else title
        if engagement:
            description = "{}（{}）".format(description, engagement)
        category = _category(title + text)
        for name in names:
            candidate: dict[str, Any] = {
                "name": name,
                "category": category,
                "description": description,
                "note_source": dict(note_source),
            }
            if localized:
                candidate["image"] = dict(localized[0])
            candidates.append(candidate)

    if not candidates:
        failed["message"] = "no candidates extracted from {} notes".format(len(notes))
        return failed

    return {
        "ok": True,
        "candidates": candidates,
        "notes": len(notes),
        "images": images,
        "tool": TOOL,
    }
