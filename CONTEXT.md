# CONTEXT — 领域术语表

本文件只是术语表，不含实现细节。

## 拍板（Verdict）

本 skill 的核心动作：不给选项列表，直接给一个具体答案（品类+具体菜/店名）+ 一句话理由 + 真图来源 + 「换一个」兜底。决策疲劳场景的产物：用户要的是被决定，不是又一个选项。

## 候选（Candidate）

从社交平台攻略（小红书等）提取的一道可推荐项：品类/店/菜+描述+素材图+来源笔记。是拍板的原料池。

## 采集（Collect）

OpenCLI × 小红书搜索「{location} 美食攻略/必吃」并抓取笔记与图的过程。每次拍板实时采集（用户定案），不建静态菜池。失败或零结果时显式降级为启发式孪生。

## 判断点（Judgment Point）

流程中可插入 jev 外部判断的决策位。沿用 travel-guide 的模式：独立开关（env）、限次、超时降级。本 skill 与 travel-guide 相反——默认开启，因为核心价值依赖筛选。

## 启发式孪生（Heuristic Twin）

jev 或采集不可用时的退路：退回宿主 AI 自行评估/建议，降级必须显式记录（verdict.degraded[]），产物永不失败。

## 记忆（Memory）

本地 JSON 状态文件：吃过日志（拍板自动记录）、忌口（硬过滤）、偏好权重（差评降权/好评加权）。拍板前过滤，拍板后自动记录。用户可随时查看/手改。

## untrusted 铁律

社交平台来源的一切内容（图/描述/店名证据）必须带 untrusted 来源标记。jev 低分素材不得进入判词。

## 换一个（Alternate）

不满意时的零成本兜底：出次优候选且不重复上一次拍板，不重走反问轮、不退回选项列表。

## 反问轮（Clarify Round）

grill-me 式的需求澄清：零输入时反问缺失的关键项（地点/忌口/预算/人数，≤4 问），用户答完必须拍板。反问是为了拍得更准，不是把决策推回给用户。

## Two-stage decision glossary

- Decision state: tonight's craving, mood, energy, location, meal mode, party, budget, and taboos.
- Eating direction: a category found in the first Xiaohongshu guide search.
- Store candidate: a concrete store found by the second search under an eating direction.
- Evidence: source title, author, URL, date, and note content; social content remains untrusted.
- Final decision: one executable store selected from the remaining candidates.
- Feedback: meal result and reason tags attached to the matching decision state.
