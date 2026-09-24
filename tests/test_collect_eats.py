"""T3（#4）采集管线测试：mock subprocess / mock 下载，全程不联网。

- 成功路径：mock opencli 输出 → 候选结构断言（name/note_source/credibility=untrusted）
- 失败路径（命令不存在/超时/零笔记/零候选）→ ok=False + degraded="collect_failed" 且不抛异常
- 图片本地化：mock 下载 → 文件落盘、url 指向本地相对路径
- 双关键词搜索策略断言
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import collect_eats  # noqa: E402


SEARCH_NOTE = {
    "title": "郑州必吃！胡辣汤天花板",
    "author": "郑州吃货王",
    "url": "https://www.xiaohongshu.com/explore/abc123",
    "likes": 1200,
}

NOTE_DETAIL = {
    "desc": "来郑州必喝「方中山胡辣汤」，配「葛记焖饼」绝了。\n人均 20 元，早上排队。",
    "images": [
        {"url": "https://sns-img.xhscdn.com/abc123.webp", "alt_description": "胡辣汤成品图"}
    ],
}


def _run_response(payload):
    stdout = json.dumps(payload, ensure_ascii=False)
    return mock.Mock(returncode=0, stdout=stdout, stderr="")


def _fake_opencli(run_mock, search_payload=None, note_payload=None):
    """按命令形态分流：search → 搜索结果，note → 笔记详情。"""
    search_payload = search_payload if search_payload is not None else {"notes": [SEARCH_NOTE]}
    note_payload = note_payload if note_payload is not None else NOTE_DETAIL

    def side_effect(command, **kwargs):
        joined = " ".join(command)
        if " search " in joined:
            return _run_response(search_payload)
        if " note " in joined:
            return _run_response(note_payload)
        return _run_response({})

    run_mock.side_effect = side_effect


class SuccessPathTests(unittest.TestCase):
    def setUp(self):
        patcher_resolve = mock.patch.object(collect_eats, "_resolve_command", return_value="opencli")
        patcher_run = mock.patch.object(collect_eats.subprocess, "run")
        patcher_dl = mock.patch.object(
            collect_eats, "_download", side_effect=lambda url, dest, timeout=30: True
        )
        patcher_resolve.start()
        self.run_mock = patcher_run.start()
        patcher_dl.start()
        self.addCleanup(patcher_resolve.stop)
        self.addCleanup(patcher_run.stop)
        self.addCleanup(patcher_dl.stop)
        _fake_opencli(self.run_mock)

    def test_candidate_structure(self):
        result = collect_eats.collect("郑州", "build/test-workdir")
        self.assertTrue(result["ok"])
        self.assertEqual(result["tool"], "opencli")
        self.assertGreaterEqual(result["notes"], 1)
        self.assertGreaterEqual(result["images"], 1)
        self.assertGreater(len(result["candidates"]), 0)
        names = {c["name"] for c in result["candidates"]}
        # 规则解析：标题出候选 + 正文「」引号出店名菜名
        self.assertIn("郑州必吃！胡辣汤天花板", names)
        self.assertIn("方中山胡辣汤", names)
        self.assertIn("葛记焖饼", names)
        for candidate in result["candidates"]:
            self.assertTrue(candidate["name"])
            self.assertTrue(candidate["category"])
            self.assertTrue(candidate["description"])
            src = candidate["note_source"]
            self.assertEqual(src["title"], SEARCH_NOTE["title"])
            self.assertEqual(src["author"], SEARCH_NOTE["author"])
            self.assertEqual(src["url"], SEARCH_NOTE["url"])
            image = candidate["image"]
            self.assertIn("untrusted", image["credibility"])
            self.assertIn("小红书", image["credibility"])
            self.assertEqual(image["alt_description"], "胡辣汤成品图")

    def test_dual_keyword_search_strategy(self):
        collect_eats.collect("郑州", "build/test-workdir")
        commands = [" ".join(call.args[0]) for call in self.run_mock.call_args_list]
        self.assertTrue(any("郑州 美食攻略" in c for c in commands), commands)
        self.assertTrue(any("郑州 必吃" in c for c in commands), commands)

    def test_duplicate_notes_deduped_across_keywords(self):
        # 两个关键词返回同一篇笔记 → 按 url 去重，notes 计 1
        result = collect_eats.collect("郑州", "build/test-workdir")
        self.assertEqual(result["notes"], 1)


class DegradedTests(unittest.TestCase):
    def _assert_degraded(self, result):
        self.assertFalse(result["ok"])
        self.assertEqual(result["degraded"], "collect_failed")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["tool"], "opencli")

    def test_command_not_found(self):
        with mock.patch.object(collect_eats, "_resolve_command", return_value=None):
            self._assert_degraded(collect_eats.collect("郑州", "build/test-workdir"))

    def test_timeout_does_not_raise(self):
        with mock.patch.object(collect_eats, "_resolve_command", return_value="opencli"):
            with mock.patch.object(
                collect_eats.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="opencli", timeout=120),
            ):
                self._assert_degraded(collect_eats.collect("郑州", "build/test-workdir"))

    def test_zero_notes(self):
        with mock.patch.object(collect_eats, "_resolve_command", return_value="opencli"):
            with mock.patch.object(collect_eats.subprocess, "run") as run_mock:
                _fake_opencli(run_mock, search_payload={"notes": []})
                self._assert_degraded(collect_eats.collect("郑州", "build/test-workdir"))

    def test_zero_candidates(self):
        # 笔记存在但标题为空、正文无引号可解析 → 零候选也显式降级
        with mock.patch.object(collect_eats, "_resolve_command", return_value="opencli"):
            with mock.patch.object(collect_eats.subprocess, "run") as run_mock:
                _fake_opencli(
                    run_mock,
                    search_payload={"notes": [{"title": "", "author": "", "url": "https://x.com/n1"}]},
                    note_payload={"desc": "这篇没有可解析的店名。", "images": []},
                )
                self._assert_degraded(collect_eats.collect("郑州", "build/test-workdir"))

    def test_empty_location(self):
        self._assert_degraded(collect_eats.collect("  ", "build/test-workdir"))


class ImageLocalizationTests(unittest.TestCase):
    def _collect_with_download(self, tmpdir, download):
        with mock.patch.object(collect_eats, "_resolve_command", return_value="opencli"):
            with mock.patch.object(collect_eats.subprocess, "run") as run_mock:
                with mock.patch.object(collect_eats, "_download", side_effect=download):
                    _fake_opencli(run_mock)
                    return collect_eats.collect("郑州", tmpdir)

    def test_image_lands_on_disk_with_relative_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)

            def fake_download(url, dest, timeout=30):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"fake-image-bytes")
                return True

            result = self._collect_with_download(workdir, fake_download)
            self.assertTrue(result["ok"])
            self.assertGreaterEqual(result["images"], 1)
            image = result["candidates"][0]["image"]
            self.assertFalse(image["url"].startswith("http"))
            self.assertTrue(image["url"].startswith("images/"))
            local = workdir / Path(image["url"])
            self.assertTrue(local.is_file())
            self.assertEqual(local.read_bytes(), b"fake-image-bytes")
            self.assertEqual(image["source"], SEARCH_NOTE["url"])
            self.assertIn("untrusted", image["credibility"])

    def test_failed_download_keeps_collection_alive(self):
        # 图片下载失败 → 候选不带 image，但采集不失败（素材不失败铁律）
        result = self._collect_with_download("build/test-dl-fail", lambda url, dest, timeout=30: False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["images"], 0)
        for candidate in result["candidates"]:
            self.assertNotIn("image", candidate)


if __name__ == "__main__":
    unittest.main()


class SearchKeywordsV2Tests(unittest.TestCase):
    """反问轮 v2：意图合成搜索词（spec #9）。"""

    def test_no_constraints_falls_back_to_location(self):
        keywords = collect_eats.search_keywords("宁波")
        self.assertEqual(
            keywords,
            ["{} {}".format("宁波", s) for s in collect_eats.SEARCH_SUFFIXES],
        )

    def test_string_extras_appended(self):
        keywords = collect_eats.search_keywords(
            "宁波",
            {"location_anchor": "天一广场附近", "party": "外卖一人食", "budget": "人均 50"},
        )
        self.assertEqual(
            keywords,
            ["宁波 天一广场附近 外卖一人食 人均 50 {}".format(s) for s in collect_eats.SEARCH_SUFFIXES],
        )

    def test_no_preference_and_invalid_dimensions_skipped(self):
        # no_preference（显式标记）与非字符串值都不进搜索词
        keywords = collect_eats.search_keywords(
            "宁波",
            {"location_anchor": {"no_preference": True}, "cuisine_pref": 123, "budget": "  "},
        )
        self.assertEqual(
            keywords,
            ["{} {}".format("宁波", s) for s in collect_eats.SEARCH_SUFFIXES],
        )

    def test_collect_passes_constraints_through(self):
        # collect(constraints=…) → 搜索词带意图
        with mock.patch.object(collect_eats, "search_keywords", wraps=collect_eats.search_keywords) as spy:
            collect_eats.collect("宁波", Path("build/test-constraints"), constraints={"party": "堂食 2 人"})
            called = spy.call_args
        self.assertEqual(called.args[1], {"party": "堂食 2 人"})


class TwoStageCollectionTests(unittest.TestCase):
    def test_store_names_come_from_note_content_not_guide_title(self):
        note = {"title": "宁波美食攻略：四家店", "author": "本地人", "url": "https://example.com/n"}
        detail = {"content": "🦀蟹爸爸肉蟹煲：鸡爪入味 🍜融铁牛螺蛳粉：可以续粉"}
        with mock.patch.object(collect_eats, "_search_notes", return_value=([note], None)), mock.patch.object(
            collect_eats, "_read_note", return_value={"ok": True, "data": detail}
        ):
            result = collect_eats.collect("宁波", "build/store-extraction", direction="夜宵", stores_only=True)
        self.assertEqual([item["name"] for item in result["candidates"]], ["蟹爸爸肉蟹煲", "融铁牛螺蛳粉"])
        self.assertNotIn(note["title"], [item["name"] for item in result["candidates"]])

    def test_collect_two_stage_discovers_directions_then_caps_stores(self):
        store_a = {"name": "店 A", "category": "湘菜", "description": "酸辣"}
        store_b = {"name": "店 B", "category": "烧烤", "description": "炭火"}
        store_c = {"name": "店 C", "category": "面食", "description": "热汤"}
        with mock.patch.object(
            collect_eats,
            "discover_directions",
            return_value={"ok": True, "directions": ["湘菜", "烧烤"]},
        ), mock.patch.object(
            collect_eats,
            "collect",
            side_effect=[
                {"ok": True, "candidates": [store_a, store_b], "notes": 2, "images": 0, "tool": "opencli"},
                {"ok": True, "candidates": [store_b, store_c], "notes": 2, "images": 0, "tool": "opencli"},
            ],
        ) as collect_mock:
            result = collect_eats.collect_two_stage("郑州", "build/two-stage")

        self.assertTrue(result["ok"])
        self.assertEqual(result["directions"], ["湘菜", "烧烤"])
        self.assertEqual([c["name"] for c in result["candidates"]], ["店 A", "店 B", "店 C"])
        self.assertLessEqual(len(result["candidates"]), 3)
        self.assertEqual(collect_mock.call_count, 2)
        self.assertEqual(collect_mock.call_args_list[0].kwargs["direction"], "湘菜")
