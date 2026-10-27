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

尚未完成：

- SQLite/PostgreSQL 实现仍由 `TaskStore` / `PostgresTaskStore` 承载，尚未按端口拆成独立适配器。
- `ReviewService` 仍有 499 行，业务方法尚未迁入 `app/` 应用服务。
- `AgenticReviewer` 仍有 992 行，编排/工具/上下文/记忆/结果处理尚未迁入 `agent/`。

## 下一阶段

1. 把 `ReviewService` 按业务拆成 `reviews`、`github`、`feedback`、`evolution`、`skills`、`fixes` 应用服务，`ReviewService` 只保留 facade。
2. 把 SQLite/PostgreSQL 的共享 SQL 提到 `adapters/stores/sql_common.py`，再按端口拆适配器。
3. 把 `AgenticReviewer` 按 `scan`、`orchestrator`、`lead`、`session`、`memory_hooks`、`tools`、`findings` 拆分。

每个阶段都必须满足：

- `python -m pytest tests -q` 全绿；
- API、数据库 schema、报告格式不变；
- 不新增跨层 import；
- 旧模块路径至少保留一个版本的兼容导出。
