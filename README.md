# what-to-eat

今晚吃什么 / 某地吃什么 —— 社交平台攻略现采 + jev 筛选 + 直接拍板的 Claude Code skill。

- Spec: #1 · 票：#2-#8 · 术语：CONTEXT.md · 契约：docs/SCHEMA.md
- 测试：`python -m pytest tests/ -q`（87 passed）
- 真机验收证据：`generated/zhengzhou/pipeline_result.json`（2026-09-24，真采集+真 jev 评分拍板）

## 管线

采集（OpenCLI×小红书）→ jev 素材筛选（默认开启，限次 20，阈值 7）→ 记忆过滤（忌口/近3天/权重）→ 拍板（具体菜/店+理由+真图）→ 自动记录。

## 已知问题 / 边界（真机验收发现）

1. **候选提取分两层**：脚本只做规则级提取（笔记标题）；菜品级深度提取是宿主 AI 运行时职责（SKILL.md 管线第 2 步）——真机 demo 用宿主 AI vision 提取 4 个真候选验收通过。`opencli xiaohongshu note` 回包不含图片字段，图片用 `opencli xiaohongshu download <url> --output <dir>` 单独落地。
2. **OpenCLI flags 教训**：`--window background --site-session persistent` 实测返回空结果；最小 flag 集（`-f json`）有效。会话态问题，若复现需在 adapter 层修。
3. **jev 评分归一化**：jev 对 4 档 criteria 返回 0-3 档插值原始分，展示分 = raw/3×10（阈值 7 = raw 2.1）。请求体必须带 instructions + criteria，缺了 422（真机踩坑）。
4. **拍板池只收 jev 达标候选**：全拒时走启发式孪生兜底并显式标注；alternates_hint 在达标候选 ≤1 时为空。
5. 状态文件 `state/memory.json` 不入 git；`generated/` 下为真机验收证据。
