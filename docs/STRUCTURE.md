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
- `AgenticReviewer` 已拆到 `evoagent/agent/`，并从 mixin 继承改为显式协作者组合：
  - `reviewer.py` 269 行，只保留 facade、一次 review 的上下文和兼容包装
  - `orchestration.py` 245 行，`ReviewOrchestrator`
  - `findings.py` 156 行，`FindingPolicy`
  - `assignments.py` 110 行，`AssignmentRunner`
  - `scanning.py` 105 行，`ScanRunner`
  - `lead.py` 87 行，`LeadRunner`
  - `memory_hooks.py` 64 行，`MemoryHooks`
  - `tools.py` 37 行，`ToolPolicy`
  - `session.py` 18 行，`SessionManager`
  - `collaborators.py` 7 行，`ReviewerBackref`
  - `agentic_core.py` 只做兼容 re-export
  - `AgenticReviewer` 现在只继承 `Reviewer`，不再继承任何 mixin

- SQLite/PostgreSQL 已按能力拆到 `evoagent/adapters/stores/{sqlite,postgres}/`：
  - `tasks.py`、`memory.py`、`failures.py`、`skills.py`、`installations.py`、`auth.py`、`observability.py`、`schema.py`
  - `store.py` 37 行、`postgres_store.py` 37 行，只保留连接/建表和兼容类名
  - `shared/rows.py` 共享 JSON、时间戳行解码
- 已把 dialect-neutral 的 auth、failure case、installation、memory、skills 收敛到 `shared/`：
  - `shared/auth.py`
  - `shared/failures.py`
  - `shared/installations.py`
  - `shared/memory.py`
  - `shared/skills.py`
  - 两个 backend 的对应模块只剩 4 行 subclass
- `SqlStoreMixin` 新增 `_write()`、`_in_clause()`、`_greatest()`、`_acquire_key_lock()`，统一 SQLite 进程锁、PostgreSQL advisory lock 和方言差异。
- `PromptStoreMixin` / `WebhookStoreMixin` 继续作为跨后端共享 SQL 规则。
- `ReviewsApplicationService` 已拆成兼容 facade（101 行）+ `review_lifecycle.py`（171 行）+ `review_execution.py`（197 行）；两者通过 `ReviewExecutionService` 组合，不形成循环依赖。

尚未完成：

- `tasks.py`（184/186 行）和 `observability.py`（195/180 行）仍各自维护两份 SQL；按当前风险偏好暂缓。
- Agent 协作者目前通过 `ReviewerBackref` 访问 reviewer 的共享运行时；下一步可注入更窄的 runtime 对象，彻底去掉 backref。
- `ReviewerBackref.__getattr__` 是过渡设计，不应继续扩大使用范围。

后续可选：

- 为 Agent 协作者引入 `ReviewRuntime`（store、context manager、budgets、memory manager 等），去掉 reviewer backref。
- 继续细化 `ReviewLifecycleService` 的投递部分（queue 提交/恢复）与任务创建部分。
- 用 `_returning()`、`_limit()` 等 dialect hook 在风险可控时继续收敛 tasks 和 observability。

## 下一阶段

1. 用 `ReviewRuntime` 替换 Agent 协作者的 `ReviewerBackref`。
2. 继续细化 `ReviewLifecycleService` 的投递/恢复边界。
3. 在 PostgreSQL 回归覆盖充分时再处理 tasks 和 observability 的重复 SQL。

每个阶段都必须满足：

- `python -m pytest tests -q` 全绿；
- API、数据库 schema、报告格式不变；
- 不新增跨层 import；
- 旧模块路径至少保留一个版本的兼容导出。
