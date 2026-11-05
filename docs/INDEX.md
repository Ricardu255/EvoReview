# EvoReview 文档索引

本目录集中存放项目说明、架构设计与评测说明；仓库根目录只保留英文 `README.md`。

## 项目说明

- [README（English）](../README.md)
- [README（简体中文）](README.zh-CN.md)

## 架构与运行

- [结构拆分与最终目录](STRUCTURE.md)
- [运行逻辑（简体中文）](MODEL_RUNTIME_FLOW.md)
- [Runtime flow（English）](MODEL_RUNTIME_FLOW.en.md)

## 审查与评测

- [变异模拟评测语料](experiments/mutation-corpus.zh-CN.md)

## 维护约定

- 新增项目文档优先放入 `docs/`，并按“架构 / 评测”归入对应子目录。
- 图片等二进制资源放入 `docs/assets/`，正文使用相对路径引用。
- 移动或重命名文档后，请同步更新本索引以及文档内的相对链接。
