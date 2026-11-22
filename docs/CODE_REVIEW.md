# EvoReview 代码审查报告

| 项目 | 内容 |
|---|---|
| 审查对象 | EvoReview 0.4.0（`pyproject.toml` / `evoagent/__init__.py`） |
| 审查范围 | `evoagent/` 全部核心模块、`pyproject.toml`、`web/app.js`、`docs/`、`.env.example` |
| 审查方式 | **静态阅读**（未能执行命令，见下方「审查限制」） |
| 结论 | 无「立即导致数据泄露的配置无关型致命漏洞」，但存在 **1 个高危越权面** 与 **6 个中危问题**，其余为低危/一致性/可维护性问题 |

---

## 审查限制

本次审查**没有运行任何命令**：

- `bash` 工具在本环境被固定到 `wsl.exe`，而系统未安装 WSL 发行版，`ls` / `git` / `python` / `pytest` 全部直接失败；
- 因此以下结论全部来自源码静态阅读，**未执行**测试套件、类型检查、`bandit` / `semgrep` 扫描、依赖漏洞扫描；
- 行号未标注（无 grep 能力），改用「文件 + 符号名 + 代码片段」定位，可直接搜索到。

后续若能提供可用的 shell / Python 路径，建议补跑：

```powershell
python -m unittest discover -s tests -v
python -m pip install bandit semgrep
bandit -r evoagent -f json
```

---

## 总览

代码整体质量**高于同类项目平均水平**，主要优点：

- 分层清晰：`api`（HTTP）/ `app`（用例编排）/ `agent`（Agent 运行时）/ `domain`（模型与门禁）/ `adapters`（基础设施）/ `ports`（协议）。
- 持久化层全部使用参数化 SQL，无字符串拼接注入面。
- 路径穿越防护（`commonpath` 校验、Skill 资源拒绝符号链接）实现正确。
- 前端 `escapeHtml` / `escapeAttr` 覆盖了全部插值点，未发现 XSS。
- JWT 全签名 `hmac.compare_digest`、PBKDF2 310k 轮、dummy-hash 时间均衡、token 每次回查用户状态（支持即时吊销）。
- Webhook **先验签再解析**，`X-GitHub-Delivery` 幂等 + 重放时间窗 + claim/lease，设计规范。
- `Settings.validate_evolution()` 在 `build_container()` 中被调用，能拦截「关闭鉴权 + 非 loopback 绑定」的危险组合。

问题按严重程度汇总：

| 级别 | 数量 | 编号 |
|---|---|---|
| 🔴 高危 | 1 | H-1 |
| 🟠 中危 | 6 | M-1 ~ M-6 |
| 🟡 低危 / 一致性 | 13 | L-1 ~ L-13 |
| ✅ 已验证无问题 | — | 见末节 |

---

## 🔴 高危问题

### H-1 `/github/install` 未鉴权，泄露 setup state，导致跨租户绑定

**位置**：`evoagent/api.py` → `ApiHandler._handle_get()`

**现象**

`/github/install` 与 `/github/setup` 都在这一行**之前**处理：

```python
principal = self._authenticate_or_send("read")
```

`/github/install` 直接把配置中的 state 拼进重定向 URL：

```python
if path == "/github/install":
    if not self.settings.github_app_slug:
        self._send_json(503, {...}); return
    location = "https://github.com/apps/%s/installations/new" % self.settings.github_app_slug
    if self.settings.github_setup_state:
        location += "?" + urllib.parse.urlencode({"state": self.settings.github_setup_state})
    self.send_response(302)
    self.send_header("Location", location)
```

而 `/github/setup` 的**唯一授权依据**就是这个 state：

```python
expected_state = self.settings.github_setup_state
if not hmac.compare_digest(expected_state, query.get("state", [""])[0]):
    self._send_json(403, {"error": "invalid setup state"}); return
...
tenant_id = self.settings.default_tenant_id
authorization = self.headers.get("Authorization", "")
if self.settings.auth_required and authorization:   # 注意：没有 Authorization 时会直接使用 default tenant
    ...
self.service.store.save_installation(installation_id, account, tenant_id)
```

**攻击链**

1. 攻击者 `curl -i https://<public-host>/github/install`，从 `Location` 响应头读出 `EVOAGENT_GITHUB_SETUP_STATE`；
2. 该 state 是**静态环境变量**，不是一次性 nonce，无法作废，可反复使用；
3. 攻击者在自己账号下安装 GitHub App，拿到自己的 `installation_id`；
4. 请求 `/github/setup?state=<偷到的 state>&installation_id=<自己的 id>`，且**不带** `Authorization`；
5. 由于未带 `Authorization`，`tenant_id` 直接落到 `default_tenant_id`，installation 被绑定到受害租户；
6. 此后攻击者的 webhook 会在受害租户下创建任务、写入任务/报告/记忆数据，并消耗受害方的 LLM 额度（`allow_unbound_installations=false` 本意是防这个，但被 state 泄露绕过）。

**修复建议**

- `/github/install` 加 `manage` 权限校验（至少不能让匿名用户读到 `Location`）；
- state 改为**一次性 + 带 TTL**：写库（如复用 `webhook_deliveries` 的模式），校验后立即消费；或改用 GitHub App 官方 setup flow，把 installation 绑定与登录态强关联；
- `/github/setup` 在 `auth_required=true` 时**强制要求** `Authorization`，只有 `auth_required=false`（本地模式）才允许回退到 `default_tenant_id`；
- 加审计告警：同一 state 多次成功使用即视为异常。

---

## 🟠 中危问题

### M-1 仓库工具会把 `.env` 等敏感文件喂给模型

**位置**：`evoagent/agent/repository_tools.py` → `RepositoryToolSuite._safe_path()` / `read_file()`

```python
def _safe_path(self, relative: str) -> str:
    ...
    relative = str(relative).replace("\\", "/").lstrip("/")
    target = os.path.realpath(os.path.join(self.root, relative))
    if os.path.commonpath([self.root, target]) != self.root:
        raise ValueError("path escapes repository root")
    return target          # 只防穿越，不防敏感文件

def read_file(self, path: str, start_line: int = 1, end_line: int = 240) -> dict:
    target = self._safe_path(path)
    with open(target, "r", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
```

`_files()` 列表时跳过 `SKIP_DIRS`（含 `.git`），但 `read_file` / `ast_analyze` / `git_context` 是**直接按路径打开**的，没有 denylist。

**影响**

`.env`、`.git/config`、`*.pem`、`id_rsa`、`credentials*` 都可能被 Agent 读取，内容进入模型上下文，随后进入报告，可通过 `GET /v1/tasks/{id}/report`、`GET /v1/tasks/{id}` 或 GitHub PR 评论取回。这是**确定存在的数据外泄通道**（不需要额外漏洞）。

**修复建议**

在 `_safe_path()` 中增加敏感文件 denylist，命中即 `raise PermissionError`：

```python
_SENSITIVE = re.compile(
    r"(^|/)(\.env(\..*)?|\.git/|\.ssh/|id_(rsa|dsa|ecdsa|ed25519)|"
    r".*\.(pem|key|p12|pfx|jks)|credentials(\..*)?|\.netrc|\.npmrc|\.pypirc)$",
    re.IGNORECASE,
)
```

并考虑加一个 `EVOAGENT_REPOSITORY_TOOL_DENY_GLOBS` 环境变量让运维补充规则。

---

### M-2 登录限流在隧道场景退化为「单桶」，可用来锁死管理员

**位置**：`evoagent/api.py::_client_key`，`evoagent/adapters/auth.py::AuthManager.login`

```python
def _client_key(self) -> str:
    try:
        return str(self.client_address[0])
    except (IndexError, TypeError, AttributeError):
        return "unknown"
```

```python
def _login_keys(self, username, client_key):
    return (
        ((client_key, "user", username), self.login_max_failures),
        ((client_key, "client"), self.login_max_failures * CLIENT_FAILURE_MULTIPLIER),
    )
```

**问题**

README 推荐的部署方式是 Cloudflare Quick Tunnel / ngrok（`cloudflared tunnel --url http://127.0.0.1:8080`）。此时**所有请求的 `client_address` 都是 `127.0.0.1`**，于是：

- 所有人共享同一个 `(127.0.0.1, "client")` 桶，限额 `10 × 5 = 50` 次 / 300 秒；
- 任何人连发 50 次错误登录，即可让**包括 admin 在内的所有人**在窗口期内无法登录（可用性攻击）；
- 服务端不读取 `X-Forwarded-For`，无法区分真实来源。

**修复建议**

- 新增 `EVOAGENT_TRUSTED_PROXIES`（CIDR 列表），仅当 `client_address` 属于可信代理时才采用 `X-Forwarded-For` 的最后一跳；
- 或对 loopback 来源单独放宽策略 / 使用独立的管理端口；
- 增加全局（跨 IP）失败率监控与告警，而不只做拒绝。

---

### M-3 打包缺陷：`pip install .` 后控制台静态资源与 skills 目录缺失

**位置**：`pyproject.toml`、`evoagent/api.py::WEB_ROOT`、`evoagent/config.py::skills_dir`

```toml
[tool.setuptools.packages.find]
include = ["evoagent*"]
```

`web/` 与 `skills/` 位于包外，且未声明 `package-data`。而：

```python
WEB_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web"))
```

安装到 `site-packages` 后指向不存在的 `site-packages/web` → `GET /` 与 `/assets/*` 全部 404，Web 控制台不可用。

同时 `skills_dir` 默认值是相对路径 `"skills"`，取决于启动时的 CWD，从其它目录启动会扫不到 Skill。

另外 **`evoagent.egg-info/SOURCES.txt` 已过期**：缺失 `evoagent/app/comments.py`、`evoagent/adapters/stores/shared/migrations.py`（两者均被实际 import），说明构建元数据未随代码更新。

**修复建议**

- 把 `web/` 与 `skills/` 移入包内（如 `evoagent/web`、`evoagent/skills`），或在 `pyproject.toml` 增加：

```toml
[tool.setuptools]
include-package-data = true

[tool.setuptools.package-data]
evoagent = ["web/*", "skills/**/*"]
```

- `WEB_ROOT` / `skills_dir` 优先使用包内相对路径，环境变量仅作覆盖；
- 重新执行 `pip install -e .` 或构建以刷新 `egg-info`，并把 `*.egg-info/` 从版本控制中移除。

---

### M-4 Redis 队列重试无退避，Stream 无限增长，取消任务触发 critical 告警

**位置**：`evoagent/adapters/task_queue.py`

**（a）Redis 路径没有指数退避**

```python
except Exception as exc:
    if envelope["attempt"] >= self.max_attempts:
        self._dead_letter(envelope, str(exc))
    elif self._redis:
        self._redis.xadd(self.STREAM, {"envelope": json.dumps(envelope)})
    else:
        delay = min(2 ** (envelope["attempt"] - 1), 10)   # 仅内存模式有退避
        timer = threading.Timer(delay, self._submit_memory, args=(envelope,))
```

README 宣称的是「Redis Streams ACK、worker leases、exponential-backoff retries 和死信队列」，但 Redis 路径实际是**瞬间重投**。对 LLM 429 / 502 这类故障，等于 3 次连打，放大下游压力。

**（b）Stream 与 DLQ 没有长度上限**

`submit()`、重投 `xadd`、`_dead_letter()` 的 `xadd` 都没有 `maxlen=` 或 `XTRIM`，长期运行 Redis 内存无限增长。

**（c）取消任务被当成可重试错误**

`RuntimeCancelled`（`evoagent/agent/runtime.py`）继承 `RuntimeError`，**不是** `PermanentTaskError`，因此：

1. `harness.run()` 抛 `RuntimeCancelled`；
2. `_deliver_owned()` 走通用 `except Exception` → 重试；
3. 每次重试 `cancel_check` 立即返回 `True`，再次抛 `RuntimeCancelled`；
4. 重试耗尽 → `_dead_letter()`；
5. `mark_dead_letter()`（`evoagent/app/review_execution.py`）发出 **critical 级别告警**：`"Task %s entered the dead-letter queue"`。

即：用户主动取消一次审查，会换来 3 次无效重试 + 一条 critical 告警 + 一条死信看板记录。

**修复建议**

- Redis 路径复用与内存模式相同的退避：重投时写入 `envelope["retry_after"]`，worker 在 `XREADGROUP` 时跳过未到期的项；或改用 `XADD` 到延迟 stream + 定时迁移；
- 所有 `xadd` 加 `maxlen=..., approximate=True`（例如 10 万条），DLQ 单独设更小上限；
- 为 `RuntimeCancelled` 增加专门分支：直接 ACK、不重试、不告警、不写 DLQ。

---

### M-5 `EVOAGENT_DB_PATH=:memory:` 完全不可用

**位置**：`evoagent/adapters/stores/sqlite/store.py::_connect`、`evoagent/adapters/stores/sqlite/schema.py::_init`

```python
@contextmanager
def _connect(self) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(self.path, timeout=10)
    ...
    finally:
        conn.close()
```

```python
def _init(self) -> None:
    with self._connect() as conn:
        if self.path != ":memory:":        # 说明本意是支持内存库
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        ... CREATE TABLE ...
```

`sqlite3.connect(":memory:")` 每次调用都会创建一个**全新的私有空库**。`_init()` 建完表后连接被关闭，schema 随之消失；之后任何 `_connect()` 都是空库，全部查询报 `no such table`。

**修复建议**

```python
def __init__(self, path: str):
    self.path = path
    self._lock = threading.Lock()
    if path == ":memory:":
        self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._memory_conn.row_factory = sqlite3.Row
    self._init()
```

或使用共享缓存 URI：`file:evoagentmem?mode=memory&cache=shared`。

---

### M-6 `POST` 鉴权失败返回 403，前端不会重新登录

**位置**：`evoagent/api.py::do_POST`，`web/app.js::api()`

```python
# _principal() 在 token 缺失 / 过期 / 非法时抛的都是 PermissionError
def _principal(self, permission: str = "read") -> Principal:
    ...
    principal = self.service.auth.authenticate(self.headers.get("Authorization", ""))
    self.service.auth.require(principal, (permission,))
    return principal
```

```python
# do_POST
except PermissionError as exc:
    self._send_json(403, {"error": str(exc)})

# do_GET 走的 _authenticate_or_send
except PermissionError as exc:
    self._send_json(401, {"error": str(exc)})
```

前端只在 401 时清理 token 并弹出登录层：

```js
if (response.status === 401) {
  if (accessToken && !path.startsWith("/v1/auth/login")) {
    accessToken = "";
    localStorage.removeItem("evoagent_token");
  }
  $("#login-overlay").classList.remove("hidden");
  $("#logout").classList.add("hidden");
}
```

**影响**

token 过期后：`GET /api/dashboard` 会正常弹登录框；但只要用户先触发任意 `POST`（提交审查、记录反馈、`/v1/skills/reload`、`/v1/tasks/{id}/fix`），就只会收到一个「permission denied」toast，页面停留在过期状态，用户无法自行恢复。

**修复建议**

新增 `errors.py::AuthenticationRequired`（或复用 `Unauthorized`），在 `AuthManager.authenticate` 中抛出，`do_GET` / `do_POST` / `do_DELETE` 统一映射为 401；`PermissionError` 保留给真正的权限不足 → 403。

---

## 🟡 低危 / 一致性问题

| 编号 | 位置 | 问题 | 建议 |
|---|---|---|---|
| **L-1** | `evoagent/adapters/observability.py::AlertManager.evaluate` | 用**全量历史** `dashboard_stats` 计算失败率 `tasks_failed / tasks_total`，一旦越过阈值就永久告警且永不恢复；早期故障会长期污染告警 | 改用滑动窗口（按 `created_at` 过滤最近 N 分钟） |
| **L-2** | `evoagent/config.py` vs `evoagent/bootstrap.py` | `alert_window_seconds`、`alert_webhook_url`、`alert_smtp_host`、`alert_email_to`、`continuous_eval_seconds` 被解析但**从未接线**；`AlertManager(store, failure_rate, min_samples)` 只接收三个参数 | 要么接线，要么从 `config.py`、README（「持久化告警」）、`.env.example` 中删除 |
| **L-3** | `evoagent/app/review_lifecycle.py::create_review` / `enqueue_review` | `RunMode.parse(mode) if mode else None` 的结果被丢弃，`create_task()` 硬编码 `"mode": RunMode.AGENTIC.value`；API 接受 `mode` 参数但完全无效 | 要么把 `mode` 存进 task input 并在 `resolve_mode()` 使用，要么从 API 移除该参数并返回 400 |
| **L-4** | `evoagent/adapters/rollout.py::ReleaseManager.assignment` | shadow 桶区间写成 `bucket >= canary_percent and bucket < shadow_percent`；当 `shadow_percent <= canary_percent` 时影子流量恒为 0；`configure()` 不校验两者关系 | 校验 `0 <= canary <= shadow <= 100`，或把 shadow 定义为「剩余流量的百分比」并在文档中明确 |
| **L-5** | `evoagent/agent/harness.py::_reviewing` | `reviewer_summary` 为空时 `run_mode` / `components` / `execution` 直接是 `{}`，报告字段缺失；`_ctx.step` 取自 `trace` 的 `max(step)`，与 `trace_events` 自增主键语义不一致 | 为缺省分支补 `execution` 骨架；步数改为显式计数而非 `max()` |
| **L-6** | `evoagent/adapters/verifier.py::verify_worktree` | 对**不可信仓库**（GitHub `zipball`）执行 `EVOAGENT_REPAIR_TEST_COMMAND`，并在结果中标注 `network_disabled: False`，无容器/沙箱隔离。自动修复外部 PR = 在审查机上执行对方代码 | 文档显式声明该风险；默认要求显式开启；条件允许时用容器/无网络沙箱执行 |
| **L-7** | `evoagent/agent/repository_tools.py::run_repository_checks` | `shutil.copytree(self.root, checkout, ignore=...)` 默认 `symlinks=False`，会**跟随复制**仓库内的符号链接，可能把仓库外的大文件/敏感文件拷进临时副本再被扫描 | 传 `symlinks=True`，并在复制前校验链接目标仍在 `root` 内 |
| **L-8** | `evoagent/agent/repository_tools.py::_restricted_env` | 只保留 `PATH/SYSTEMROOT/WINDIR/LANG/...`，Windows 下丢失 `APPDATA` / `USERPROFILE` / `HOME`，`pip`、`npm`、`mypy` 缓存等工具链可能异常；保留 `PATH` 也意味着仓库内配置仍可间接执行 | 补齐 Windows 必需变量，并考虑 `PATH` 白名单化 |
| **L-9** | `evoagent/api.py::_handle_get`（`/health`） | `/health` 未鉴权，却返回 `llm_provider`、`llm_model`、`queue`、`runtime` 等部署细节 | 拆分为 `/health`（仅状态）与 `/health/detail`（需 `read` 权限） |
| **L-10** | `evoagent/api.py::_headers` | 无 `Content-Security-Policy`；token 存 `localStorage`（README 已说明），配合隧道公网暴露时缺 CSP 是明显短板 | 增加 `default-src 'self'; object-src 'none'; frame-ancestors 'none'` |
| **L-11** | `evoagent/config.py::Settings.validate_evolution` | 只校验了少数字段；`report_max_findings`、`max_steps`、`timeout_seconds`、`queue_*`、`agent_context_*`、`agent_token_budget` 等无范围校验，`agent_context_input_tokens + 输出预算` 可能超过 `agent_context_window_tokens` | 补充范围与交叉校验（如 `input + output < window`） |
| **L-12** | `evoagent/adapters/auth.py::login` | 登录成功后只清 `keys[0][0]`（user 桶），不清 client 桶，成功的登录不会重置 client 级计数 | 成功时同时清理 client 桶（或按比例衰减） |
| **L-13** | `evoagent/adapters/github.py::_request` | `float(reset)` / `float(retry_after)` 未捕获 `ValueError`，脏响应头会抛异常并绕过重试分支 | 用 `try/except (TypeError, ValueError)` 回退到默认退避 |
| **L-14** | `evoagent/domain/diff_parser.py::parse_unified_diff` | hunk 内不校验行数是否真的耗尽，`old_remaining` / `new_remaining` 可被打成负数；畸形 diff 会把后续行全部吞进当前 hunk | 在 `finish_hunk_if_complete()` 中校验 `== 0`，异常时终止解析 |
| **L-15** | `evoagent/agent/repository_tools.py::_files` | `os.walk` 对每个文件调用 `_safe_path()`（每文件一次 `realpath`），大仓库下开销显著；`search_repository` 对每个文件全量读取 | 缓存 `realpath` 结果；搜索改为流式读取并设单文件上限 |

---

## ✅ 已检查且确认无问题的部分

以下为审查中**主动验证通过**的点，记录以避免后续重复排查：

- **SQL 注入**：`evoagent/adapters/stores/**` 全部参数化；`SqlStoreMixin._sql()` 方言转换（`?` ↔ `%s`、剥离 `::jsonb`）正确，无字符串拼接的 WHERE/ORDER BY。
- **路径穿越**：`RepositoryToolSuite._safe_path()` 的 `os.path.commonpath` 校验能挡住 `../`、绝对路径（含跨盘符 `D:/...`）、以及 `C:/Windows/...` 这类同盘绝对路径。
- **Skill 资源**：`evaluation/skills.py` 拒绝符号链接、`_safe_relative_path` 拒绝空段/`.`/`..`、`read_resource` 二次 `startswith(directory + os.sep)` 校验、大小与数量上限齐全。
- **补丁应用**：`adapters/patching.py` 的 `parse_unified_patch` / `apply_file_patch` 校验 hunk 行数、路径白名单、上下文逐行匹配，且改动后必须通过 `compile()` + `tokenize`，再走隔离副本前后测试对比。
- **XSS**：`web/app.js` 所有插值点均经 `escapeHtml()` 或 `escapeAttr()`；`textContent` 用于报告正文。
- **认证**：JWT 用 `hmac.compare_digest` 做全签名比较；登录对不存在的用户也执行一次 PBKDF2（dummy hash）以均衡耗时；`authenticate()` 每次回查 `users.active` 与 `memberships`，支持无需等 TTL 的即时吊销。
- **Webhook**：`verify_signature()` 在 `_read_json()` **之前**调用；`claim_webhook()` 做 `delivery_id` 幂等 + `payload_sha256` / `tenant_id` 冲突检测 + lease；`X-GitHub-Event` 与重启时间窗校验齐全。
- **启动校验**：`build_container()` 首行调用 `settings.validate_evolution()`，因此「`AUTH_REQUIRED=false` + 非 loopback 绑定」会在启动时被拒绝，`--check` 与正常启动行为一致。
- **压缩包解压**：`RepairVerifier.verify_archive` 有压缩前后大小上限、成员数量上限、`../` 与绝对路径拒绝、以及 `commonpath` 式的二次校验。

---

## 修复优先级建议

| 优先级 | 编号 | 理由 | 预估工作量 |
|---|---|---|---|
| P0 | **H-1** | 唯一的越权路径，且可被匿名触发 | 0.5 天（state 改一次性 + `/github/install` 加鉴权） |
| P0 | **M-1** | 确定存在的数据外泄通道，无需额外漏洞 | 0.5 天（denylist + 单测） |
| P1 | **M-6** | 一行异常类型改动，直接影响可用性 | 1 小时 |
| P1 | **M-4** | 生产稳定性：重试风暴 + Redis OOM + 取消即告警 | 1 天 |
| P1 | **M-2** | 公网部署下的可用性攻击面 | 0.5 天 |
| P2 | **M-3** | 影响可发布性（`pip install` 后不可用） | 0.5 天 |
| P2 | **M-5** | `:memory:` 完全不可用（`schema.py` 里已埋了判断，属明显疏漏） | 2 小时 |
| P3 | **L-2 / L-3** | 死配置与无效参数，误导运维与调用方 | 2 小时 |
| P3 | 其余 L-* | 逐项按需处理 | — |

---

## 附录：本次审查覆盖的文件

```text
pyproject.toml
.env.example
README.md
docs/README.zh-CN.md
web/app.js
evoagent/__main__.py
evoagent/__init__.py
evoagent/api.py
evoagent/bootstrap.py
evoagent/config.py
evoagent/service.py
evoagent/adapters/auth.py
evoagent/adapters/github.py
evoagent/adapters/llm.py
evoagent/adapters/metrics.py
evoagent/adapters/observability.py
evoagent/adapters/patching.py
evoagent/adapters/rollout.py
evoagent/adapters/task_queue.py
evoagent/adapters/verifier.py
evoagent/adapters/stores/shared/sql.py
evoagent/adapters/stores/shared/rows.py
evoagent/adapters/stores/shared/auth.py
evoagent/adapters/stores/shared/webhook.py
evoagent/adapters/stores/sqlite/store.py
evoagent/adapters/stores/sqlite/schema.py
evoagent/adapters/stores/sqlite/tasks.py
evoagent/adapters/stores/sqlite/auth.py
evoagent/agent/harness.py
evoagent/agent/runtime.py
evoagent/agent/reviewer.py
evoagent/agent/bounded_role.py
evoagent/agent/context_manager.py
evoagent/agent/repository_tools.py
evoagent/app/policies.py
evoagent/app/github.py
evoagent/app/reviews.py
evoagent/app/review_execution.py
evoagent/app/review_lifecycle.py
evoagent/domain/diff_parser.py
evoagent/evaluation/skills.py
```

**未覆盖**（建议后续补充审查）：`evoagent/agent/{lead,collaborators,orchestration,session,scanning,findings,memory,memory_hooks,tools,telemetry,assignments,lead_session,reviewers,review_rules}.py`、`evoagent/domain/{gates,models,report,modes,finding_identity}.py`、`evoagent/evaluation/*`（除 `skills.py`）、`evoagent/adapters/stores/postgres/*`、`evoagent/ports/**`、`tests/**`、`skills/**`。
