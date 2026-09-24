# 拍板 Schema v1 — 字段定义（单缝契约）

> 全管线（采集 → jev → 拍板 → 记忆）只经**拍板 schema** 一个缝进出，不设第二数据通道。
> 术语见 CONTEXT.md；决策依据见 spec #1（what-to-eat）。

## 请求（request）

| 字段 | 类型 | 说明 |
|------|------|------|
| `location` | `string` | 地点（城市/商圈）。可由宿主 AI 从上下文或记忆文件推断 |
| `constraints.taboos[]` | `string[]` | 忌口/过敏（硬过滤） |
| `constraints.budget` | `string?` | 人均预算带（如 "100 以内"），建议层 |
| `constraints.party` | `string?` | 几个人/场景（独食/朋友聚/家庭） |
| `mode` | `"decide"\|"alternate"` | 拍板 / 换一个（出次优且不重复上次） |

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
