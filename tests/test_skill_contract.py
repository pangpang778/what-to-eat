"""T6（#7）SKILL.md 交互契约断言。

- frontmatter 合法：name 在场、description 含触发场景枚举
- 硬规则关键词在场：问完必拍板 / 最多一轮 / 换一个 / untrusted
- 触发词清单 + 不触发边界在场
- 拍板输出格式示例在场（含 untrusted 标记样例）
- 管线步骤与脚本引用在场（schema 单缝：collect/jev/memory/拍板/记录）

契约来源：spec #1 Implementation Decisions 3/4/6/7、docs/SCHEMA.md、CONTEXT.md。
"""

import unittest
from pathlib import Path

SKILL = Path("SKILL.md")
RAW = SKILL.read_text(encoding="utf-8")


def _frontmatter() -> dict[str, str]:
    text = RAW.strip()
    assert text.startswith("---"), "SKILL.md 必须以 frontmatter 开头"
    end = text.index("\n---", 3)
    fields: dict[str, str] = {}
    for line in text[3:end].strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


FRONTMATTER = _frontmatter()
BODY = RAW[RAW.index("\n---", 3) + 4:]  # frontmatter 之后的正文


class FrontmatterTests(unittest.TestCase):
    def test_name_present(self):
        self.assertEqual(FRONTMATTER.get("name"), "what-to-eat")

    def test_description_enumerates_trigger_scenarios(self):
        desc = FRONTMATTER.get("description", "")
        self.assertTrue(desc, "description 必须在场")
        # 触发场景枚举（至少覆盖核心触发词与边界声明）
        for keyword in ("今晚吃什么", "有什么好吃的", "随便吃点", "不触发"):
            self.assertIn(keyword, desc)


class HardRuleKeywordTests(unittest.TestCase):
    """硬规则关键词：SKILL.md 文案必须写明，缺失即契约走样。"""

    def test_must_verdict_keyword(self):
        self.assertIn("问完必拍板", RAW)

    def test_single_round_keyword(self):
        self.assertIn("最多一轮", RAW)

    def test_alternate_keyword(self):
        self.assertIn("换一个", RAW)

    def test_untrusted_keyword(self):
        self.assertIn("untrusted", RAW)

    def test_no_option_list_rule_stated(self):
        # 硬规则：拍板不把决策推回用户（不给选项列表）
        self.assertIn("不给选项列表", RAW)


class TriggerBoundaryTests(unittest.TestCase):
    def test_trigger_list_present(self):
        for trigger in ("今晚吃什么", "有什么好吃的", "来", "随便吃点"):
            self.assertIn(trigger, BODY)

    def test_non_trigger_boundary_present(self):
        for keyword in ("不触发", "纯聊天"):
            self.assertIn(keyword, BODY)


class OutputFormatTests(unittest.TestCase):
    def test_verdict_output_example_present(self):
        # 输出格式示例：菜名行 / 理由 / 图 / 来源 / 换一个提示
        for keyword in ("今晚吃这个", "理由", "![", "图源", "不吃这个"):
            self.assertIn(keyword, BODY)

    def test_example_carries_untrusted_marker(self):
        self.assertIn("untrusted：社交平台内容", BODY)

    def test_degraded_annotation_stated(self):
        for keyword in ("非实时采集", "宿主 AI 复核"):
            self.assertIn(keyword, BODY)


class PipelineContractTests(unittest.TestCase):
    def test_pipeline_steps_and_script_refs(self):
        # 管线五环节及对应 scripts/ 模块引用（schema 单缝）
        for step, ref in (
            ("collect", "scripts/collect_eats.py"),
            ("jev", "jev"),
            ("memory", "scripts/memory.py"),
        ):
            self.assertIn(step, BODY)
            self.assertIn(ref, BODY)

    def test_memory_hooks_referenced(self):
        for fn in ("filter_candidates", "record_verdict", "apply_rating"):
            self.assertIn(fn, BODY)

    def test_alternate_contract(self):
        for keyword in ("mode=alternate", "不重走反问轮", "不重复"):
            self.assertIn(keyword, BODY)

    def test_schema_doc_referenced(self):
        self.assertIn("docs/SCHEMA.md", RAW)
        self.assertIn("CONTEXT.md", RAW)


class ClarifyRoundV2ContractTests(unittest.TestCase):
    """反问轮 v2（spec #9）：五问 + 随便语义 + 自动推断层。"""

    def test_five_questions_present(self):
        # 五问维度在场（SKILL.md 文案与 docs/SCHEMA.md 反问轮 v2 契约互为镜像）
        for keyword in ("location_anchor", "cuisine_pref", "taboos", "party", "budget"):
            self.assertIn(keyword, RAW)
        self.assertIn("五问", RAW)

    def test_single_round_le_5(self):
        self.assertIn("≤5 问", RAW)

    def test_no_preference_semantics_stated(self):
        self.assertIn("no_preference", RAW)
        # 五问全随便 → 直接拍板
        self.assertIn("直接拍板", RAW)

    def test_auto_inference_layer_stated(self):
        # 自动推断层（不问但生效）：时段→夜宵、近 3 天吃过、记忆忌口
        self.assertIn("夜宵", RAW)

    def test_intent_not_persisted_rule_stated(self):
        self.assertIn("意图不沉淀进记忆", RAW)

    def test_schema_doc_carries_v2_contract(self):
        schema = Path("docs/SCHEMA.md").read_text(encoding="utf-8")
        for keyword in ("反问轮 v2", "location_anchor", "cuisine_pref", "no_preference"):
            self.assertIn(keyword, schema)
        # 加权系数契约：文档与代码互锁
        self.assertIn("×1.5", schema)
        from scripts.decide import CUISINE_BOOST
        self.assertEqual(CUISINE_BOOST, 1.5)


if __name__ == "__main__":
    unittest.main()
