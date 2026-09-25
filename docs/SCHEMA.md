# 拍板 Schema v1 — 字段定义（单缝契约）

> 全管线（采集 → jev → 拍板 → 记忆）只经**拍板 schema** 一个缝进出，不设第二数据通道。
> 术语见 CONTEXT.md；决策依据见 spec #1（what-to-eat）。

## 请求（request）

| 字段 | 类型 | 说明 |
|------|------|------|
| `location` | `string` | 地点（城市/商圈）。可由宿主 AI 从上下文或记忆文件推断 |
| `constraints.taboos[]` | `string[]` | 忌口/过敏/不吃辣（硬过滤） |
| `constraints.location_anchor` | `string \| {"no_preference": true}?` | 位置锚点（如 "天一广场附近"）→ 细化搜索词；不做坐标过滤 |
| `constraints.cuisine_pref` | `string \| {"no_preference": true}?` | 口味/菜系偏向（如 "湘菜"）→ 拍板排序加权（加权系数见下），不硬剪 |
| `constraints.party` | `string \| {"no_preference": true}?` | 怎么吃+几个人（如 "堂食 2 人" / "外卖一人食"）→ 进搜索词与 jev 评分描述 |
| `constraints.budget` | `string \| {"no_preference": true}?` | 预算带（如 "人均 50"）→ 软信号：搜索词 + jev 评分描述，**不硬剪**；候选明确提价格时输出对照 |
| `mode` | `"decide"\|"alternate"` | 拍板 / 换一个（出次优且不重复上次） |

### 反问轮 v2 契约（意图 Intent）

- 反问轮每次拍板前执行（地点已知不问地点）：五问 = 位置锚点 / 口味偏向 / 忌口 / 怎么吃+人数 / 预算；一轮问完，每问附示例。
- **随便（No-Preference）**：用户答「随便/不知道/都行」→ 该维度记 `{"no_preference": true}`（显式标记，非空值）；五问全随便 → 直接拍板，行为等价无意图。
- 意图不沉淀进记忆（「记住我不吃 X」的显式指令才写 taboos）。
- 自动推断层（不问但生效）：时段（21 点后搜索词加「夜宵」）；近 3 天吃过排除；记忆忌口硬剪。
- 菜系加权系数：命中的候选排序键 ×1.5（固定系数，改动须同步本文件与本测试）。

## 候选（candidates[]）

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | `string` | 具体菜/店名（拍板拍多细：品类+具体名） |
| `category` | `string` | 品类（如 湘菜/胡辣汤/日料） |
| `description` | `string` | 一句话描述（宿主 AI 从笔记提取，禁止编造笔记外信息） |
| `image` | `object?` | `{url（本地相对路径）, source, credibility, alt_description, jev_score?, adopted?}`。仅 jev 达标（≥阈值 7）图片 adopted=true 可入 verdict |
| `note_source` | `object` | `{title, author, url?}`——来源笔记，untrusted 铁律的载体 |
| `ll` | `[lng,lat]?` | 店铺坐标（有则保留，无则省略，禁止编造） |

## 判词（verdict）

| 字段 | 类型 | 说明 |
|------|------|------|
| `pick` | `object` | `{name, category, description?}`——拍板结果 |
| `reason` | `string` | 一句话理由，必须由 schema 真实字段支撑（热度/匹配/没吃过/省时），无编造 |
| `image` | `object?` | 达标候选的 image 原样（untrusted 标记随之） |
| `alternates_hint[]` | `string[]` | 「换一个」的次优候选名（≤3） |
| `degraded[]` | `string[]` | 显式降级记录（如 "collect_failed: 启发式孪生"、"jev_disabled: 宿主 AI 复核"、"limit_reached"） |
| `all_candidates_rejected` | `bool?` | jev 全拒 → 启发式孪生拍板（degraded 同步记录） |

## 记忆（memory，独立状态文件，schema 缝的输入/输出）

| 字段 | 类型 | 说明 |
|------|------|------|
| `eaten_log[]` | `object[]` | `{location, pick, date (YYYY-MM-DD), rating? (up/down)}——拍板自动 append` |
| `taboos[]` | `string[]` | 忌口（用户手改或对话更新，硬过滤） |
| `weights` | `object` | `{候选名: 分值}`，差评降权好评加权 |

## 告警与管线元数据

| 字段 | 类型 | 说明 |
|------|------|------|
| `alerts[]` | `object[]?` | `{source: "JEV"\|"RULE", type, title, detail, level: "advisory"}`（本 skill 无 blocking——拍板永不失败） |
| `pipeline` | `object?` | `{collect: {ok, notes, images, tool, degraded?}, jev: {ok: bool, calls: int, limit: int, threshold: float, adopted: int, degraded: bool, reason?: string}}`——对话内披露 |

## 兼容规则

- 任何字段缺失必须降级跳过，不得报错（最小降级夹具回归保护）。
- 社交平台来源的一切内容（图/描述/店名证据）必须带 untrusted credibility 标记。
- `reason` 只允许引用 schema 内字段；测试断言关键事实未走样。
- jev 契约：POST /v1/systemone，body 必含 `model:"jev-latest"` 与 `questions:{...:{type:"score"}}`（422 坑，真机验证过）。

## Two-stage collection additions

- directions[]: eating directions found by the first guide search.
- candidates[].direction: the eating direction that produced the store candidate.
- candidates[].open_now, available, distance_ok, and budget_ok: optional hard signals; false removes the candidate before ranking.
- memory.feedback[]: pick, rating, context, reason, and date for contextual learning.
- JEV supplies evidence quality only. It does not decide user fit, availability, or the final store.

## 平台 AI 线索补充

- platform_leads[]：平台 AI 返回的方向、店铺候选和引用来源。
- platform_leads[].platform：来源平台名称。
- platform_leads[].direction：平台建议的吃法方向。
- platform_leads[].store_candidates[]：平台建议的具体店铺。
- platform_leads[].sources[]：引用的原始笔记，必须包含 URL 或明确标记为不可复核。
- platform_leads[].untrusted：固定为 true；平台 AI 总结不能直接进入最终拍板理由。
- pipeline.platform_ai：平台调用数量、成功数量、失败原因和来源核对数量。
- 用户指定平台时，该平台优先；没有指定平台时，从当前可用的只读 AI 能力中最多并行三个。
- 平台 AI 失败时继续普通采集或其他平台；不调用发布、点赞、评论、收藏等写操作。
