# ChinaTravel 测试数据

来源：https://huggingface.co/datasets/LAMDA-NeSy/ChinaTravel

作者及引用信息见 `raw/README.md`。查询数据许可为 CC BY 4.0：
https://creativecommons.org/licenses/by/4.0/
使用、发布或改编时须保留署名、来源、许可链接并说明修改。

## 本次导入

下载日期：2026-11-04。固定版本见 `raw/revision.txt`，原始 CSV 的 SHA256 和实际条数见 `manifest.json`。

| 划分 | 条数 | 建议用途 |
| --- | ---: | --- |
| easy | 300 | 简单规划开发与回归 |
| medium | 150 | 复杂约束开发与回归 |
| human | 154 | 人工需求开发与回归 |
| human1000 | 1000 | 留出评测，避免用于提示词调优 |

共 1604 条。未下载偏好变体、英文 Phase 2 或配套沙盒。

`raw/` 保留原始 CSV 和上游说明。各划分的 JSON 提取了 `id`、中文 `query`、意图和参考实体，同时完整保留 `source_record`。原文没有被改写，没有补造出发日期，也没有增加参考答案。

其中 Human1000 原始记录只有 uid 和自然语言需求，缺少结构化实体，其 `reference_entities` 保持空对象，不填充 0 或推测值。

## 当前接入边界

这些文件是独立测试数据资产，尚未接入 `tests/eval/run_eval.py` 的自动评分；原有 `cases.json` 不变。每条记录标记 `evaluation_status=pending_adapter`，不能把导入成功解释为评测通过。

后续适配需要区分两种评测：

1. 当前产品流程：原题缺少日期时，允许追问并验收缺失字段与后续续跑；不能一律要求直接生成行程。
2. 官方规划基准：使用配套沙盒、原生计划结构和官方校验器，检查具体约束；当前关键词验收不能替代它。

原始 `hard_logic_py` 是上游 DSL/约束文本，仅作为数据保留，不直接 eval/exec。参考实体和原生约束供验收使用，不应作为隐藏答案注入被测系统。改编、补全日期或生成多轮案例时应另存派生集并记录改动；派生集结果不称为官方基准成绩。

这些数据不加入 RAG 知识库。本次未调用模型，也未生成新的评测成绩。
