# 变异模拟评测语料

`evoagent/evaluation/mutation_corpus.py` 通过确定性变异生成带标签的 review 样本：

- 每个算子提供一组“安全实现 / 风险实现”源码对
- 生成 unified diff、`before_files`、`after_files`、期望 Finding 和修复验证字段
- 按 10 个虚拟仓库做 train/validation/holdout 隔离
- 内置 hard negative，避免只测“看起来危险”的字符串
- 同一 `seed` 和 `--per-rule` 输出完全一致

## 覆盖规则

当前覆盖 14 条确定性规则：

`SEC-EVAL`、`SEC-SUBPROCESS-SHELL`、`SEC-HARDCODED-SECRET`、`SEC-SQL-CONCAT`、`REL-EMPTY-EXCEPT`、`REL-DEBUG-PRINT`、`SEC-PATH-TRAVERSAL`、`SEC-YAML-LOAD`、`SEC-WEAK-HASH`、`SEC-INSECURE-TEMPFILE`、`SEC-WEAK-RANDOM`、`REL-UNBOUNDED-RETRY`、`SEC-ASSERT-AUTH`、`SEC-INSECURE-COOKIE`。

## 使用

```bash
# 生成 4 组/规则，写入 output/
python scripts/generate_mutation_corpus.py --per-rule 4 --seed 1

# 直接回放确定性 Reviewer 并输出指标
python scripts/run_mutation_evaluation.py --per-rule 4 --seed 1
```

生成文件默认位于 `output/mutation-corpus/`，`output/` 已被忽略，不会入库。

## 局限

这是 `mutation-synthetic` 数据，不是真实 PR 人工标注：

- 可以验证解析、规则、门禁、编排、修复和评测链路
- 不能用来宣称真实生产准确率
- 模板和变异算子可能让模型“见过类似样本”
- 真实效果仍需要独立的人工标注 PR holdout
