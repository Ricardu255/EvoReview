# 结构拆分路线

本文记录 EvoReview 从单体服务向“应用层 + 领域层 + 端口/适配器”迁移的边界与顺序。目标不是一次性重写，而是每个阶段都保持 API、数据库和行为不变。

## 依赖方向

```text
api → app → domain
          ↘ ports ← adapters
bootstrap → 所有具体实现
```

- `domain` 只依赖标准库和领域模型。
- `app` 只依赖 `ports`、`domain` 和 `agent` 的 facade。
- `adapters` 实现 `ports`，可以依赖具体数据库/队列/模型 SDK。
- 只有 `bootstrap.py` 负责构造具体适配器。

## 当前阶段

已完成第一阶段：

- `evoagent/bootstrap.py`：`ServiceContainer` 与 `build_container()`，集中装配。
- `ReviewService(settings)` 保持兼容，但构造委托给 composition root。
- `evoagent/ports/stores/`：按任务、记忆、失败案例、进化、Skill、Webhook、认证、可观测性拆分的持久化协议。
- `evoagent/store_contract.py` 只做兼容导出，保持旧 import 路径。
- `tests/test_architecture.py` 守卫依赖方向。
- `tests/test_bootstrap.py`、`tests/test_store_ports.py` 覆盖新的装配和端口契约。
- 已提取 `ReviewPolicy`、`ReviewsApplicationService`、`GitHubApplicationService`、`FixesApplicationService`、`SkillApplicationService`、`FeedbackApplicationService`、`EvolutionApplicationService`。
- `ReviewService` 从 653 行降到 243 行，只保留兼容 facade。
- `AgenticReviewer` 已拆到 `evoagent/agent/`：
  - `reviewer.py` 217 行，只保留 facade 和一次 review 的上下文
  - `orchestration.py` 244 行
  - `findings.py` 156 行
  - `assignments.py` 109 行
  - `scanning.py` 104 行
  - `lead.py` 86 行
  - `memory_hooks.py` 63 行
  - `tools.py` 36 行
  - `session.py` 17 行
  - `agentic_core.py` 只做兼容 re-export

- SQLite/PostgreSQL 已按能力拆到 `evoagent/adapters/stores/{sqlite,postgres}/`：
  - `tasks.py`、`memory.py`、`failures.py`、`skills.py`、`installations.py`、`auth.py`、`observability.py`、`schema.py`
  - `store.py` 37 行、`postgres_store.py` 37 行，只保留连接/建表和兼容类名
  - `shared/rows.py` 共享 SQLite/PostgreSQL 的 JSON、时间戳行解码
- `PromptStoreMixin` / `WebhookStoreMixin` 继续作为跨后端共享 SQL 规则。

尚未完成：

- 多数 capability 方法仍分别维护 SQLite 和 PostgreSQL 两份 SQL；`shared/rows.py` 只共享行解码，尚未共享查询构造。
- `ReviewsApplicationService` 仍有 342 行，后续可再按“任务生命周期 / 执行 / 投递”细分。
- Agent 模块当前通过 mixin 共享 reviewer 状态；文件边界已建立，后续可进一步改为显式协作者对象。

后续可选：

- 把 `ReviewsApplicationService` 拆成 `ReviewExecutionService` + `ReviewDeliveryService`。
- 把 `OrchestrationMixin` 改成 `ReviewOrchestrator` 组合对象，去掉 mixin 状态共享。
- 用 dialect hook（`_sql`、`_json`、`_bool`、`_returning`）把 capability 查询收敛成共享基类，进一步减少两份 SQL 的重复。

## 下一阶段

1. 把 `ReviewService` 按业务拆成 `reviews`、`github`、`feedback`、`evolution`、`skills`、`fixes` 应用服务，`ReviewService` 只保留 facade。
2. 把 SQLite/PostgreSQL 的共享 SQL 提到 `adapters/stores/sql_common.py`，再按端口拆适配器。
3. 把 `AgenticReviewer` 按 `scan`、`orchestrator`、`lead`、`session`、`memory_hooks`、`tools`、`findings` 拆分。

每个阶段都必须满足：

- `python -m pytest tests -q` 全绿；
- API、数据库 schema、报告格式不变；
- 不新增跨层 import；
- 旧模块路径至少保留一个版本的兼容导出。
