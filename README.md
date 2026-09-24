# what-to-eat

让 AI 帮你决定今晚吃什么：先用小红书找吃法方向，再比较具体店铺，最后只拍板一家。

## 给 AI 的一句话

把下面这句话交给 Claude Code、Codex 或其他能操作本机文件的 AI：

~~~
请安装并配置这个 GitHub skill：https://github.com/pangpang778/what-to-eat；自动识别当前 AI 宿主并安装到正确的 skills 目录，检查并安装 OpenCLI，检查小红书登录状态，JEV_API_KEY 有就启用、没有就降级，然后用 /what-to-eat 杭州滨江区附近找个吃的 跑一次真实验收，只有需要我登录或提供 key 时才停下来告诉我具体动作。
~~~

安装完成后，使用下面的触发方式：

~~~
/what-to-eat 杭州滨江区附近找个吃的
~~~

也可以直接说：

~~~
/what-to-eat 今天晚上想吃点热乎的，不要烧烤
~~~

## AI 安装时会做什么

1. 识别当前宿主是 Claude Code 还是 Codex。
2. 从 GitHub 获取本 skill，并安装到当前宿主的 skills 目录。
3. 检查 Python、Node.js 和 OpenCLI；能自动安装的依赖自动安装。
4. 检查小红书登录状态。没有登录时暂停，提示你完成登录。
5. 检查可选的 JEV key：

   ~~~powershell
   $env:JEV_API_KEY = "你的 key"
   ~~~

6. 先跑离线测试，再用你给的地点做一次真实验收。

AI 不会把 key 写入仓库，也不会替你偷偷登录外部平台。

## 它会怎么决定

~~~
需求状态
  → 小红书第一轮：确定吃法方向
  → 小红书第二轮：搜索具体店铺
  → 最多保留 3 家店铺候选
  → 过滤忌口和明显不可执行的店
  → 比较当前匹配度、证据质量、方便程度
  → 最终拍板一家
~~~

JEV 只判断素材证据质量，不决定你的口味，也不决定店铺是否营业。JEV 不可用时，流程会明确降级，不会假装有评分。

## 需要什么

- Python 3.10+
- Node.js 和 npm
- OpenCLI：npm install -g @jackwener/opencli
- 已登录的小红书 OpenCLI 会话
- JEV API key（可选）

OpenCLI 登录由 AI 检查。需要手动登录时，运行：

~~~
opencli xiaohongshu login
~~~

## 手动安装备用方式

如果你的 AI 不能自动安装：

~~~
git clone https://github.com/pangpang778/what-to-eat.git
npm install -g @jackwener/opencli
python -m pytest tests/ -q
~~~

然后把仓库中的 SKILL.md 和 scripts/ 安装到当前 AI 宿主的 skill 目录，并使用 /what-to-eat 触发。

## 开发者验证

~~~
python -m pytest -q
python -m compileall -q scripts tests
~~~

核心术语和字段见 CONTEXT.md、docs/SCHEMA.md。当前实现保留 OpenCLI、小红书和 JEV 的降级路径；真实搜索结果依赖当前浏览器会话和平台适配器状态。
