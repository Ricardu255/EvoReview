# EvoReview 100 条受控集实验结果

> 执行模式：`controlled-offline-no-llm`。本报告没有调用大模型，
> 只验证确定性规则、Agent 编排计数和 Skill 选择门禁。

## 数据

- 总样本：100
- Train / Validation / Holdout：60 / 20 / 20（按仓库隔离）

## 实验一：规则基线

| Precision | Recall | F1 | 高风险召回 | Clean accuracy | Safe fix | E2E fix |
|---:|---:|---:|---:|---:|---:|---:|
| 82.50% | 82.50% | 82.50% | 94.74% | 91.67% | 78.79% | 65.00% |

## 实验二：受控离线五臂消融

| 实验臂 | Precision | Recall | F1 | 高风险召回 | Clean accuracy | 无效评论/PR | 模拟角色调用/PR |
|---|---:|---:|---:|---:|---:|---:|---:|
| `single-llm` | 83.33% | 62.50% | 71.43% | 84.21% | 91.67% | 0.0500 | 1.00 |
| `single-llm-scanner` | 82.50% | 82.50% | 82.50% | 94.74% | 91.67% | 0.0700 | 1.00 |
| `multi-llm-no-critic` | 82.50% | 82.50% | 82.50% | 94.74% | 91.67% | 0.0700 | 4.00 |
| `full-agentic` | 100.00% | 82.50% | 90.41% | 94.74% | 100.00% | 0.0000 | 5.00 |
| `full-agentic-evolved-skill` | 100.00% | 82.50% | 90.41% | 94.74% | 100.00% | 0.0000 | 5.00 |

隐藏 Holdout 上，Critic、多 Agent 和 Evolved Skill 相对对应基线的 F1 差值均为 0；
Scanner 相对单规则基线的 Holdout F1 变化为 +22.37 个百分点。

## 实验三：Skill 自进化

| 实验臂 | Precision | Recall | F1 | 高风险召回 | Clean accuracy |
|---|---:|---:|---:|---:|---:|
| `static-skill` | 100.00% | 37.50% | 54.55% | 66.67% | 100.00% |
| `evolved-skill` | 100.00% | 37.50% | 54.55% | 66.67% | 100.00% |
| `random-feedback-skill` | 100.00% | 37.50% | 54.55% | 66.67% | 100.00% |

- 第 1 轮学习规则：`REL-FLOAT-MONEY`, `REL-NAIVE-DATETIME`, `REL-UNBOUNDED-RETRY`, `SEC-ASSERT-AUTH`, `SEC-INSECURE-COOKIE`, `SEC-WEAK-RANDOM`
- Validation 候选：`rejected`（没有达到 F1 最小提升）
- Holdout 激活门禁：`blocked`
- 结论：在该仓库隔离切分上，Skill 自进化没有得到准确率提升证据。
