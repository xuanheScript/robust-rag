# 阶段 3：Cleaning Pipeline

阶段 3 在 Parser-neutral Canonical Document 上执行确定性、结构感知清洗。它不调用外部模型，不修改阶段 2 的 Parse Artifact 或原始 Canonical 文件。

## 数据边界

每次运行都从阶段 2 的 Canonical 文件重新创建工作副本，并将每个 Block 的 `normalized_text` 重置为 `original_text` 后依次执行算子。输出写入独立目录：

```text
data/canonical/{document_id}/{document_version_id}/cleaned/{cleaning_run_id}/
├── canonical-document.json
└── cleaning-report.json
```

数据库中的 `CleaningRun` 保存输入/输出哈希、流水线与配置版本、算子摘要、问题数量、产物 URI、耗时状态和错误。完整问题证据保存在 `cleaning-report/1.0` 文件中。

## 默认算子顺序

1. Unicode 与换行归一化。
2. 异常控制字符移除。
3. 结构感知空白规范化；代码缩进与表格 Tab 分隔不会被正文规则破坏。
4. 基于同父节点物理坐标的阅读顺序修正。
5. 跨页重复页眉、页脚和短导航清除。
6. 无子节点的空内容 Block 清除。
7. 精确重复 Block 标记并保留。
8. 近重复 Block 标记并保留。
9. Block 和文档语言识别。
10. 表格首行表头候选准备。
11. 来源定位完整性检查。

算子完成后统一修复 `semantic_order`、同级前后链接、标题路径、Token 估算和失效父节点引用。

## 幂等、重跑与比较

幂等键由 Canonical 记录、输入内容哈希、流水线版本和配置版本组成。相同组合的成功结果会直接复用；使用新的 `CLEANING_CONFIG_VERSION` 或算法版本可以生成独立运行。

相关接口：

```text
GET /api/v1/documents/{document_id}/versions/{version_id}/cleaning-runs
GET /api/v1/documents/{document_id}/versions/{version_id}/cleaning-runs/{run_id}/document
GET /api/v1/documents/{document_id}/versions/{version_id}/cleaning-runs/{run_id}/report
GET /api/v1/documents/{document_id}/versions/{version_id}/cleaning-runs/{run_id}/compare?against_run_id={other_run_id}
```

比较结果包含输出哈希、增加/移除的 Block、规范文本发生变化的 Block 以及两次运行的问题数量。

## 配置

```text
CLEANING_CONFIG_VERSION=stage3-cleaning-v1
CLEANING_BOILERPLATE_MIN_OCCURRENCES=3
CLEANING_BOILERPLATE_MIN_PAGE_RATIO=0.6
CLEANING_NEAR_DUPLICATE_THRESHOLD=0.92
CLEANING_NEAR_DUPLICATE_MIN_CHARS=80
```

阈值属于版本化算法配置。调整阈值时必须同步修改配置版本，才能保留可复现、可比较的历史运行。

## 任务推进

Worker 在解析成功后立即执行清洗：

```text
PARSING → CLEANING → DOCUMENT_EVALUATING
```

阶段 4 尚未实现，因此清洗成功的 Job 会以 `PENDING` 停留在 `DOCUMENT_EVALUATING`。清洗失败会保存 CleaningRun、StageRun 和 Job 错误，并将文档版本置为 `FAILED`，可由后续重试流程安全恢复。
