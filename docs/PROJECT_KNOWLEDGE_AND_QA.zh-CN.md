# EvoReview 项目知识清单与答辩问答

> 本文依据当前本地代码整理，适合项目介绍、技术面试和答辩复习。实现机制、测试覆盖与真实生产效果是不同层面的证据，介绍项目时应分别说明。

## 1. 一分钟项目介绍

EvoReview 是面向 GitHub PR（Pull Request，拉取请求）的代码审查系统。它接收 diff（代码差异补丁），通过规则扫描以及 Lead、Security、Correctness/Reliability、Critic 等 Agent（智能体角色）协作生成问题报告，再用确定性的证据门禁过滤缺乏依据的结论。系统保存模型与工具的执行账本，支持任务续跑，并通过 Finding 级评测、Prompt/Skill 进化门禁持续评估审查质量。

**定位边界：**底层 LLM（Large Language Model，大语言模型）由外部服务提供；EvoReview 主要实现编排、工具、审查与进化控制，没有训练新的基础模型权重。

```text
API / GitHub Webhook
  → ReviewService 创建任务
  → ReviewHarness：Planning / Executing / Reviewing
  → AgenticReviewer：扫描 → Lead 分派 → Worker → Critic → Lead 汇总
  → FindingGate 过滤
  → ReviewReport + ExecutionLedger 持久化
```

## 2. 英文术语速查

| 英文术语 | 中文解释 | 在 EvoReview 中的含义 |
|---|---|---|
| PR / Pull Request | 拉取请求 | GitHub 上待合并的代码改动 |
| diff | 差异补丁 | 记录修改文件和代码行的文本 |
| LLM / Large Language Model | 大语言模型 | 提供审查判断能力的外部模型 |
| Agent | 智能体角色 | 具有任务、提示词、工具权限和预算的模型调用单元 |
| Lead | 主协调角色 | 分派任务、判断风险、最终取舍 |
| Worker | 执行角色 | 按领域调查代码问题 |
| Critic | 质疑角色 | 挑战候选问题及其证据 |
| Finding | 审查发现 | 一条带位置、证据、严重程度和建议的问题 |
| Gate | 门禁 | 按确定性条件决定是否接受或阻断 |
| Checkpoint | 检查点 | 用于中断后续跑的持久化中间状态 |
| Ledger | 执行账本 | 原始模型和工具调用及使用量记录 |
| Baseline / Candidate | 基线版 / 候选版 | Prompt/Skill 版本对照中的旧版与候选版 |
| Regression | 回归、退化 | 旧版通过而候选版退化的指标或任务 |
| JSONL / JSON Lines | 每行一个 JSON 对象的文件 | 存储标注评测任务集 |
| Webhook | 网络回调 | GitHub 在 PR 事件发生时通知 EvoReview |
| HMAC | 基于密钥的消息认证码 | 验证 Webhook 请求签名 |
| RBAC | 基于角色的访问控制 | admin、maintainer、auditor 权限区分 |
| TTL / Time to Live | 存活时间 | 工作记忆自动过期的时间 |
| ACK / Acknowledgement | 消息确认 | 队列处理后确认消息 |
| DLQ / Dead Letter Queue | 死信队列 | 存放多次处理失败的任务 |
| AST / Abstract Syntax Tree | 抽象语法树 | 按程序语法结构分析代码 |
| CST / Concrete Syntax Tree | 具体语法树 | 保留更多源码细节的语法结构 |
| Token | 模型文本计量单位 | 限制上下文并统计调用量 |
| Draft PR | 草稿拉取请求 | 自动修复验证后创建的待人工审查 PR |
| CI / Continuous Integration | 持续集成 | 推送或 PR 时自动运行测试与检查 |

## 3. 必须掌握的知识清单

| 优先级 | 知识点 | 应能讲清的问题 |
|---|---|---|
| 必会 | 项目定位 | EvoReview 与底层 LLM 各负责什么；是否训练了模型 |
| 必会 | 请求主链路 | API/Webhook 如何创建任务，怎样得到报告 |
| 必会 | 多 Agent 协作 | 四种角色、普通任务和高风险任务有何区别 |
| 必会 | 审查发现门禁 | 位置、证据、置信度和高风险附加要求 |
| 必会 | Finding 级评测 | Precision、Recall、F1 和受控对照实验如何定义 |
| 必会 | 进化门禁 | Prompt/Skill 候选如何被接受、拒绝或回滚 |
| 重点 | 检查点与队列 | 续跑、重试、ACK、租约、死信与幂等性 |
| 重点 | 工具与 Skill | 注册表、参数校验、角色授权、Skill 权限交集 |
| 重点 | 记忆和上下文 | 租户/仓库隔离、工作记忆、历史记忆和 diff 压缩 |
| 重点 | 进化评估 | Prompt 进化和 Agent Skill 进化的不同回放路径 |
| 重点 | 自动修复 | 补丁解析、结构检查、前后测试、Draft PR |
| 重点 | 安全与部署 | Webhook 签名、Bearer Token、RBAC、数据库和 Redis |
| 加分 | 效果证明 | Precision、Recall、F1、成本、延迟和对照实验 |
| 加分 | 局限与规划 | 标注数据、真实回归 CI 接线、断言强度和生产验证 |

### 3.1 请求到报告的主链路

1. **输入：**API（应用程序接口）接收仓库、diff 等参数；GitHub Webhook 则接收 PR 事件并异步获取 diff。
2. **服务层：**`ReviewService` 校验输入大小、模型配置和仓库权限，保存任务与原始输入。默认最大 diff 为 1 MiB。
3. **Planning（规划）：**`ReviewHarness` 解析 unified diff（统一差异格式），提取修改文件与新增行。
4. **Executing（执行）：**本地规则扫描与多角色审查生成候选 Finding。
5. **门控：**`FindingGate` 按位置、证据、置信度及高风险附加要求过滤候选问题。
6. **Reviewing（复核与报告）：**生成 `ReviewReport`，保存协作摘要、使用量和原始调用记录。

任务状态包括 `PENDING`（待处理）、`PLANNING`（规划中）、`EXECUTING`（执行中）、`REVIEWING`（报告处理中）、`SUCCESS`（成功）、`FAILED`（失败）、`CANCELLED`（取消）。默认运行预算为最多 8 步、120 秒，可由配置调整。

代码：[service.py](../evoagent/service.py)、[harness.py](../evoagent/agent/harness.py)、[runtime.py](../evoagent/agent/runtime.py)。

### 3.2 多角色协作

- **Lead（主协调角色）：**结合 diff、扫描结果、记忆和 Skill 目录分派任务，判断风险级别，最终决定采纳项。
- **Security Worker（安全执行角色）：**调查输入、权限、敏感数据和危险调用链。
- **Correctness/Reliability Worker（正确性/可靠性执行角色）：**调查状态、异常、并发、资源和兼容性。
- **Critic（质疑角色）：**对候选问题做证据与反例挑战。

默认普通任务通常是两次 Lead、两个 Worker 各一次、Critic 一次，共 5 次 LLM 调用。高风险任务可以让 Worker 使用授权工具，Lead 最多请求一轮 Worker 返工，因此可能增加调用、延迟与 Token 消耗。多 Agent 带来可分工、可审计的流程；质量收益仍要靠单 Agent 对照实验验证。

代码：[reviewer.py](../evoagent/agent/reviewer.py)、[lead_session.py](../evoagent/agent/lead_session.py)。

### 3.3 工具、Skill 与证据

`BoundedRole`（受预算约束的角色循环）要求模型给出 `tool`（调用工具）或 `final`（最终输出）动作。Tool Registry（工具注册表）验证工具名、必填参数、类型和额外字段；各角色还有自己的工具权限。工具结果写入 Observation（观察结果）与 ExecutionLedger（执行账本）。

Agent Skill 是包含 `SKILL.md` 的领域指令包：Lead 先看到名称和描述，选中后才把正文注入 Worker。`allowed-tools`（允许工具集合）只能缩小角色已有权限，Skill 本身不会安装工具或创建连接。仓库搜索、读取文件、符号定位和测试等工具由运行时提供；仅有远端 diff、没有本地仓库检出时，部分取证工具不可用。

代码：[bounded_role.py](../evoagent/agent/bounded_role.py)、[skills.py](../evoagent/evaluation/skills.py)、[repository_tools.py](../evoagent/agent/repository_tools.py)。

### 3.4 Finding Gate：审查发现门禁

一条 Finding（审查发现）包含规则 ID、严重程度、标题、解释、文件与行号、证据、修复建议、测试建议、置信度、证据引用和调用链等字段。门禁检查：

1. 问题位置应在 diff 的新增行，必要字段不能为空；
2. 应有匹配代码、工具引用或调用链证据；
3. 置信度达到默认下限 0.55；
4. `HIGH`（高）或 `CRITICAL`（严重）问题须有更强证据，并附修复与测试建议。

这是确定性过滤，不能证明每条通过的问题一定是真实缺陷。当前 diff 解析主要提取新增行，因此删除代码、跨文件效应或未修改行上的根因可能难以精确定位。

代码：[models.py](../evoagent/domain/models.py)、[gates.py](../evoagent/domain/gates.py)、[diff_parser.py](../evoagent/domain/diff_parser.py)。

## 4. 轨迹回归评估已移出 EvoReview

早期版本中的轨迹回归评测、首错归因与 YAML 发布门禁已移除；EvoReview 不再在仓库内维护第二套通用评测引擎。审查报告仍保留 `ExecutionLedger` 的原始模型/工具调用日志，可用于审计或导出到外部评测框架。

如需版本级评测，请使用独立的评测框架；EvoReview 的 Finding 级质量评估仍保留在第 5 节。

## 5. 质量指标：公式与解释

对于标注缺陷评估：TP（True Positive，真正例）是报告并匹配的真实问题；FP（False Positive，假正例）是报告了不真实或不匹配的问题；FN（False Negative，假负例）是真实问题未被报告。

```text
Precision（精确率）= TP / (TP + FP)
Recall（召回率）   = TP / (TP + FN)
F1                = 2 × Precision × Recall / (Precision + Recall)
```

若 TP=8、FP=2、FN=4，则精确率为 80%，召回率约为 66.7%，F1 约为 72.7%。高精确率表示误报较少，高召回率表示漏报较少。**Finding 级 F1 衡量缺陷匹配质量，不等同于端到端任务成功率。**

证明多 Agent 的质量收益应固定任务集、标注规则和底层模型条件，并比较 Precision、Recall、F1、关键风险召回率、延迟、Token、成本和失败案例。不能只展示几个成功演示或只报一个总体百分比。代码：[evolution.py](../evoagent/evaluation/evolution.py)、[evaluation_harness.py](../evoagent/evaluation/harness.py)。

## 6. 高频答辩问题与参考回答

### Q1：这个项目是否训练了新模型？

没有。EvoReview 使用外部 LLM（大语言模型），实现审查编排、工具、记忆、评估、门禁和版本管理。需要把“模型能力”与“系统工程能力”区分开，不能称它为自研基础模型。

### Q2：为什么使用多 Agent，而不是一次模型调用？

安全、可靠性、质疑和最终决策是不同职责。拆分后可以明确任务归属、保存中间结果、审计证据来源，并在高风险任务中允许额外调查。代价是延迟和成本增加，所以普通任务限制为固定单轮。多 Agent 是否优于单 Agent 仍需对照实验，不能仅由架构推断。

**追问：Critic 若使用同一底层模型，是否独立？**它在流程和输入视角上相对独立，但并非统计意义上的完全独立，同源模型仍可能共享偏差。

### Q3：模型返回合法 JSON 后，为什么还需要门禁？

JSON 只说明格式可解析，不能说明问题真实、位置正确或证据充分。EvoReview 还检查工具协议、代码位置、证据引用、置信度和高风险发现的修复/测试要求。模型判断与确定性校验负责不同问题。

### Q4：LLM 输出不规范或服务端限流怎么办？

`JsonChatClient` 尝试严格 JSON 解析，也兼容代码围栏、对象前后多余文本和尾逗号等常见偏差。对部分限流、网络和服务端错误进行有限重试。兼容解析提高可用性，不能取代后续业务校验。代码：[llm.py](../evoagent/adapters/llm.py)。

### Q5：模型能无限调用工具吗？

不能。`BoundedRole` 限制步数、时间和 Token；Tool Registry 只允许注册工具，按 Schema（参数结构规范）校验调用。角色权限以及所选 Skill 的权限声明进一步限制可用工具。

### Q6：任务中断后如何续跑？是否保证恰好执行一次？

三阶段运行时和 Lead 会话都保存 Checkpoint（检查点），恢复时跳过已完成步骤，并恢复分派、Worker 结果、Critic 决策及执行账本。但外部模型调用与数据库写入不构成跨系统原子事务，因此不能声称严格端到端 exactly-once（恰好一次）语义。代码：[lead_session.py](../evoagent/agent/lead_session.py)。

### Q7：异步任务失败如何处理？

无 Redis 时使用进程内执行；配置 Redis 后使用 Streams（流式消息）、消费者组、ACK（消息确认）、租约恢复、退避重试与 DLQ（死信队列）。达到最大重试次数的任务进入死信，可查询和重放。进程内队列不是持久队列，生产部署需要明确队列与数据库的可靠性要求。代码：[task_queue.py](../evoagent/adapters/task_queue.py)。

### Q8：GitHub Webhook 如何防伪造和重复？

使用共享密钥计算 HMAC-SHA256 签名，校验请求体；还检查重放时间窗口，并通过 GitHub Delivery ID 记录和去重。普通业务 API 走 Bearer Token（持有者令牌）与 RBAC（基于角色的访问控制），Webhook 则走签名信任边界。代码：[api.py](../evoagent/api.py)、[github.py](../evoagent/adapters/github.py)。

### Q9：如何降低误报和幻觉？

结合规则扫描、仓库工具证据、多角色质疑和 Finding Gate。报告问题须定位到新增行并满足证据与置信度条件，高风险问题有额外约束。但这些手段只能降低风险，最终准确率应由真实标注集、人工复核和对照评估证明。

### Q10：为什么主要审查新增行？

新增行便于在 PR 上精确评论，也能避免把旧问题泛泛归因于当前变更。局限是删除行、跨文件效应或未修改行上的根因可能无法准确表达；仓库级工具可提供额外上下文，但当前行级定位仍以新增行为中心。

### Q11：为什么不在 EvoReview 内做轨迹回归评测？

避免在 EvoReview 内维护第二套通用评测引擎。轨迹比较、首错归因和发布门禁由独立评测框架承担；EvoReview 保留 `ExecutionLedger` 原始日志、Finding 级评测、多 Agent 消融和进化门禁。

### Q12：EvoReview 现在如何评估审查质量？

两条线：一是 Finding 级离线评测，使用受控数据集和真实 PR 数据集，按路径、行号、规则和严重程度做一对一匹配，计算 Precision、Recall、F1；二是 Prompt/Skill 进化门禁，把候选版本与基线在固定数据集上回放比较，要求候选改善且受保护指标不回退。

### Q13：Finding 级 Precision/Recall 怎么算？

TP 是报告并匹配的真实问题，FP 是报告了不真实或不匹配的问题，FN 是真实问题未被报告。匹配时限制路径、行号容差和规则身份，避免把同一行上的不同问题重复计数。公式见第 5 节。

### Q14：Prompt 进化会不会过拟合？

会有风险，因此使用 validation set（验证集）和 holdout set（留出集），要求候选改善且受保护指标不回退。当前最少样本配置较小，更适合证明机制；严肃的效果结论需要更大且冻结的数据集。服务层 Prompt 进化回放使用单模型 Reviewer，不等同完整多角色生产链路。代码：[evolution.py](../evoagent/evaluation/evolution.py)。

### Q15：Agent Skill 与普通 Prompt 有何区别？

全局 Prompt 适合所有任务共享的基本约束；Agent Skill 有清晰领域边界，包含可按需注入的调查流程、证据标准和工具权限声明。Skill 正文由 Lead 选中后才传给 Worker，减少无关规则占用上下文。Skill 进化回放走多角色 `AgenticReviewer`。代码：[skill_evolution.py](../evoagent/evaluation/skill_evolution.py)。

### Q16：记忆系统是否用了向量数据库？

当前实现按租户和仓库隔离记录，以关键词重叠、重要度等进行召回；工作记忆有 TTL，任务完成后整理摘要并清理临时观察。这不是基于 Embedding（文本向量表示）的向量检索，不能称为向量数据库方案。代码：[memory.py](../evoagent/agent/memory.py)。

### Q17：自动修复为何不直接修改原 PR 分支？

修复模块对通过门控的问题生成限定路径的 patch，经过补丁解析、AST/CST 或编译检查，以及配置的修复前后测试。验证通过后只创建新分支和 Draft PR，让人继续审阅。缺少测试命令时只给建议，不宣称自动修复成功。代码：[patching.py](../evoagent/adapters/patching.py)。

### Q18：项目如何做权限控制与部署？

普通 API 可启用登录、短期 Bearer Token 和 admin/maintainer/auditor 权限；任务与记忆按租户、仓库访问。本地可用 SQLite 和进程内队列；部署配置提供 PostgreSQL 与 Redis。`auth_required` 的代码默认值为 `False`，公开部署必须显式启用鉴权，并替换示例密钥与密码。代码：[auth.py](../evoagent/adapters/auth.py)、[config.py](../evoagent/config.py)、[docker-compose.yml](../docker-compose.yml)。

### Q19：如何证明项目真正提升了审查质量？

应区分单元/集成测试和真实标注 PR 对照评估。真实效果需要说明样本来源、人工标注规则、基线系统、样本量、Precision、Recall、F1、关键风险召回率、延迟、成本及失败案例。不能仅凭少数成功案例宣称在生产 PR 上提升了某个百分比。

### Q20：目前最值得改进的是什么？

优先扩大并冻结真实 PR 标注集，把 Finding 级评测接入 CI；加强结构化断言，减少对答案子串的依赖；改进删除行及跨文件问题的定位；在真实 PR 上测量多 Agent 相对单 Agent 的质量收益与额外成本。

## 7. 建议准备的三个演示案例

1. **成功审查：**选一段 PR diff，逐步说明 Lead 分派、Worker 取证、Critic 质疑、Finding Gate 采纳或拒绝，以及报告字段。
2. **Finding 级评测：**在受控数据集上展示 Precision、Recall、F1，并说明多 Agent 与单 Agent 的对照结果。
3. **进化门禁：**展示一个 Prompt 或 Skill 候选在固定数据集上的回放、门禁判定和回滚原因。

准备这三个可复述的具体案例，比单独背架构图更容易应对深入追问。
