# DevBot — 研发能效 Agent 平台 / GitHub 智能 PR 评审

> PR 来了用 LangGraph 编排多个 Critic 并行评审 + 沙箱实跑验证，加权定级回写；代码生成、单测生成、需求拆解做成确定性技能流水线，统一注册成一张技能表；经 MCP 接 [CodeDoc](../codedoc) 图谱获取跨仓变更影响。

## 🌐 在线体验 (Live Demo)

- **综合门户**:http://36.213.150.205:8501/platform/
- **DevBot**:http://36.213.150.205:8502
- 测试账号:`fctest` / `fctest123`(或自行注册)

> 演示为 http 单实例,可能不定时维护。


## ✨ 特性

- **多 Critic 并行 PR 评审**：LangGraph 扇出四个视角 Critic(正确性/设计/安全/可读性，领域纪律 prompt 保正交)，加权聚合 + 一票否决；跨 Critic 去重 + severity 校准压误报。
- **沙箱实跑验证(exec_check)**：对 PR 新增自包含代码用 rlimit 子进程实跑——崩溃即客观 error finding，并复用单测技能测真实覆盖率(不靠 LLM 判对错)。
- **代码生成**：检索接地(经 CodeDoc 取相似实现 + 真实函数体当 API 参考) → 生成 → AST 语法检查 → 沙箱实跑 → 失败修复回路(≤2) → 终审。
- **单测生成**：AST 确定性抽真实分支/异常场景 → 生成 → 沙箱 + coverage.py 测覆盖率 → 低于阈值针对未覆盖行补测重跑(≤2)。
- **统一技能表**：review / codegen / testgen / requirement 经同一 `run_skill` 入口分发(Web API 与 webhook 命令同源)。
- **评测 Harness**：4 套件 / 7 指标(finding F1、risk 准确率、ECE 校准、注入抵抗、长度退化等) + PSI 漂移 + CI 回归门禁；支持 mock(可复现)与 `--real`(真 Critic + 真 judge)。
- **GitHub 集成**：Webhook 接 PR opened/synchronize(HMAC-SHA256)，结果回写 PR Review 评论 + Commit Status(给信号，不调 merge)。

## 🏗 架构

```
PR/Webhook → 取 diff + 经 MCP 调 CodeDoc 拿变更影响子图
          → LangGraph 扇出 四 Critic(并行) + 沙箱 exec_check(并行)
          → 加权聚合 + 一票否决 → 跨 Critic 去重 + severity 校准 → 回写 PR

技能(确定性流水线): codegen / testgen / requirement  ── 统一注册 run_skill ──> API / webhook
```

## 🧰 技术栈

LangGraph · FastAPI · PostgreSQL · rlimit 沙箱 + coverage.py · MCP(接 CodeDoc) · Platt 校准

## 🚀 快速开始

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                      # 填入 SiliconFlow Key、PG、CodeDoc 内部 key 等
export $(grep -v '^#' .env | xargs)

uvicorn devbot.api.app:app --host 0.0.0.0 --port 8502
```

## 📡 主要接口

- `GET /api/v1/skills` — 列出技能
- `POST /api/v1/skill/{name}` — 调用 review / codegen / testgen / requirement，body `{"payload": {...}}`
- `POST /api/v1/review` · `POST /webhook/github` — PR 评审 / GitHub 回调
- 评测：`devbot-eval run [--real]`

## ⚙️ 配置

见 `.env.example`。需配合 CodeDoc 服务(`CODEDOC_*`)。**仓库内不含任何真实密钥。**

## 📄 License

MIT
