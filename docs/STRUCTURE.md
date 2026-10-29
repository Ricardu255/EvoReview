# 结构拆分与最终目录

本文记录 EvoReview 从单体服务向“应用层 + 领域层 + 端口/适配器”迁移后的最终边界。0.4.0 是 breaking-change 版本：旧顶层兼容层已删除，根目录只保留稳定入口模块。

## 依赖方向

```text
api → app → domain
          ↘ ports ← adapters
bootstrap → 所有具体实现
```

- `domain` 只依赖标准库和领域模型。
- `app` 只依赖 `ports` 和 `domain`；不依赖 `adapters` 或 `agent`。
- `adapters` 实现 `ports`，可以依赖具体数据库/队列/模型 SDK。
- 只有 `bootstrap.py` 负责构造具体适配器。

## 最终根目录

```text
evoagent/
├── __init__.py
├── __main__.py
├── api.py
├── bootstrap.py
├── config.py
├── errors.py
├── prompts.py
├── service.py
├── time_utils.py
├── agent/
├── app/
├── adapters/
├── domain/
├── evaluation/
└── ports/
```

根目录只保留 9 个模块和 6 个子包：38 个旧顶层兼容模块已在 0.4.0 删除，另有 5 个存储相关模块迁入 `adapters/stores/`，不再提供兼容导出或弃用告警。

## 稳定导入路径

- SQLite 存储：`from evoagent.adapters.stores.sqlite.store import TaskStore`
- PostgreSQL 存储：`from evoagent.adapters.stores.postgres.store import PostgresTaskStore, create_store`
- SQL 方言助手：`from evoagent.adapters.stores.shared.sql import SqlStoreMixin`
- Prompt 持久化：`from evoagent.adapters.stores.shared.prompt import PromptStoreMixin`
- Webhook 持久化：`from evoagent.adapters.stores.shared.webhook import WebhookStoreMixin`
- 时间工具：`from evoagent.time_utils import utc_now`
- 持久化协议：`from evoagent.ports.stores import StoreProtocol`

## Agent Core

- `AgenticReviewer` 位于 `evoagent/agent/reviewer.py`，只继承 `evoagent/agent/reviewers.py` 的 `Reviewer`，不再继承任何 mixin。
- `reviewer.py` 只保留 facade、一次 review 的上下文和兼容包装；`orchestration.py`、`findings.py`、`assignments.py`、`scanning.py`、`lead.py`、`memory_hooks.py`、`tools.py`、`session.py`、`collaborators.py` 分别承载协作者。
- 协作者只依赖 `ReviewRuntime`，不再持有整个 `AgenticReviewer`；`RuntimeBackref` 通过显式只读 property 暴露依赖。
- `review_rules.py` 位于 `agent/`，因为它依赖 `Reviewer`，不属于纯 domain。
- 其余 Agent Core 模块也都在 `agent/`：`bounded_role.py`、`harness.py`、`lead_session.py`、`context_manager.py`、`memory.py`、`runtime.py`、`telemetry.py`、`repository_tools.py`。
- 顶层 Agent Core 兼容模块已删除，外部代码直接使用上述包路径。

## Domain

- `models.py`、`modes.py`、`gates.py`、`diff_parser.py`、`finding_identity.py`、`report.py` 位于 `evoagent/domain/`。
- domain 内不允许 `from ..`，保持纯领域依赖。
- 旧顶层 domain 兼容模块已删除。

## Evaluation / Evolution

- `arms.py`、`benchmark.py`、`experiments.py`、`harness.py`、`v2.py` 位于 `evoagent/evaluation/`。
- `evolution.py`、`evolution_v2.py`、`evolution_proof.py`、`skill_evolution.py`、`skills.py` 也位于 `evaluation/`。
- `DEFAULT_CONTROLLED_DATASET` / `DEFAULT_PROMPT_DATASET` 的路径已按新层级修正。
- 旧顶层 evaluation 兼容模块已删除。

## Infrastructure adapters

- `task_queue.py`、`github.py`、`llm.py`、`auth.py`、`observability.py`、`metrics.py`、`rollout.py`、`fixer.py`、`patching.py`、`verifier.py` 位于 `evoagent/adapters/`。
- 旧顶层 adapter 兼容模块已删除。

## SQLite / PostgreSQL stores

- 两个后端按能力拆到 `evoagent/adapters/stores/{sqlite,postgres}/`：`tasks.py`、`memory.py`、`failures.py`、`skills.py`、`installations.py`、`auth.py`、`observability.py`、`schema.py`。
- 每个后端新增 `store.py`，只保留连接、建表和兼容类名；两个文件都 ≤50 行。
- `evoagent/adapters/stores/sqlite/store.py` 组合 SQLite mixin、共享 Prompt/Webhook mixin 和 `StoreProtocol`。
- `evoagent/adapters/stores/postgres/store.py` 组合 PostgreSQL mixin、共享 Prompt/Webhook mixin 和 `StoreProtocol`；`create_store()` 按 URL 选择后端。
- 持久化协议只位于 `evoagent/ports/stores/`，按任务、记忆、失败案例、进化、Skill、Webhook、认证、可观测性拆分。
- dialect-neutral 的 auth、failure case、installation、memory、skills 收敛到 `shared/`；`shared/sql.py`、`shared/prompt.py`、`shared/webhook.py` 承载跨后端 SQL 规则，`shared/migrations.py` 承载一次性租户数据迁移。
- `SqlStoreMixin` 提供 `_write()`、`_in_clause()`、`_greatest()`、`_acquire_key_lock()`，统一 SQLite 进程锁、PostgreSQL advisory lock 和方言差异。

## Application / Ports

- `ReviewPolicy`、`ReviewsApplicationService`、`GitHubApplicationService`、`FixesApplicationService`、`SkillApplicationService`、`FeedbackApplicationService`、`EvolutionApplicationService` 位于 `evoagent/app/`。
- `ReviewsApplicationService` 拆成兼容 facade + `review_lifecycle.py` + `review_execution.py`；两者通过 `ReviewExecutionService` 组合，不形成循环依赖。
- GitHub 评论投递位于 `app/comments.py`（`ReviewCommentService`）：有界重试、失败审计和手动重放，独立于审查事务。
- `ReviewService` 只保留兼容 facade，构造委托给 composition root。
- 当前明确保留的跨层例外：`agent -> adapters.llm`、`adapters -> agent.telemetry`、`evaluation -> adapters.{llm,verifier}`，以及 `evaluation/evolution_proof.py -> adapters.stores.sqlite.store`；后续可以引入 LLM/telemetry/store port 消除。

## 已完成的清理

- 0.4.0 删除 38 个旧顶层兼容模块；`tests/test_architecture.py` 守卫根目录只包含 9 个稳定入口模块。
- `evoagent/` 内部、`scripts/`、`tests/` 均已改用新包路径。
- `bootstrap.py` 直接导入 `evoagent.adapters.stores.postgres.store.create_store`。
- `evaluation/evolution_proof.py` 直接导入 SQLite `TaskStore` 和 `evoagent.time_utils.utc_now`。

## 尚未完成

- `sqlite/tasks.py`（200 行）/`postgres/tasks.py`（203 行）和 `sqlite/observability.py`（202 行）/`postgres/observability.py`（188 行）仍各自维护两份 SQL；按当前风险偏好暂缓。
- Agent 协作者已只依赖 `ReviewRuntime`；未知属性会直接抛 `AttributeError`，不会静默代理到 reviewer 或 runtime 的任意字段。

## 后续可选

1. 引入 LLM/telemetry port，消除 `agent -> adapters.llm` 和 `adapters -> agent.telemetry` 两个跨层例外。
2. 继续细化 `ReviewLifecycleService` 的投递/恢复边界。
3. 在 PostgreSQL 回归覆盖充分时，用 `_returning()`、`_limit()` 等 dialect hook 收敛 tasks 和 observability 的重复 SQL。

## 约束

- `python -m pytest tests -q` 全绿。
- API、数据库 schema、报告格式不变。
- 不新增跨层 import。
- 旧路径已移除，不再提供兼容导出；新的 breaking-change 版本从 0.4.0 开始。
