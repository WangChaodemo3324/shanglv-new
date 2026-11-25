# ChinaTravel 中文静态知识补充

来源：[LAMDA-NeSy/ChinaTravel-Sandbox](https://huggingface.co/datasets/LAMDA-NeSy/ChinaTravel-Sandbox)，作者与论文引用见上游数据卡。许可 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)，使用和再分发须保留署名、来源、许可证链接，并说明转换。

仅下载三个中文表：景点 3,413 条、餐馆 4,655 条、住宿 3,866 条，共 11,934 条。涵盖北京、成都、重庆、广州、杭州、南京、上海、深圳、苏州、武汉。

这些是静态测评沙盒记录，不是实时价格、营业时间、可订房态或企业差旅制度。不得将其参考价格用于承诺真实订票或预订。记录中的坐标未额外确认坐标系，未自动接入地图渲染。

文件：

- `*.parquet`：上游原始中文表。
- `manifest.json`：固定上游提交版本、下载时间、记录数、原始文件 SHA-256 与转换文件 SHA-256。
- `documents.jsonl`：每个地点一条知识记录，添加中文字段名、静态说明和出处，保留稳定的地点 ID；未合成额外事实。
- `knowledge.sqlite`：独立的 SQLite FTS5 关键词索引，采用中文单字/双字及英文词分词、BM25 排序，支持显式城市和类别筛选、精确名称优先。索引正文不包含重复的静态免责声明，但返回内容包含它。

重建方式（在项目根目录运行）：

```powershell
# 导入工具需要 requests 和 duckdb；运行时检索只需要 Python 标准库。
.\.venv-offline\Scripts\python.exe skills/ask-question/script/import_chinatravel.py
# 已下载文件校验后本地重建，不联网：
.\.venv-offline\Scripts\python.exe skills/ask-question/script/import_chinatravel.py --local
```

导入固定上游 revision；使用类别、语言、城市和原始 ID 组成稳定 ID。先构建临时索引再替换，重复运行不追加重复记录。校验失败不替换已有可检索索引。

RAG Agent 自动读取本目录索引。原有向量检索可用时，将补充关键词排名作为一路加入 RRF；缺少向量依赖时，允许仅使用该关键词索引。`get_stats()` 报告当前模式。`init_knowledge_base.py` 也读取 JSONL，因此未来在支持 Milvus Lite 且安装本地嵌入模型的环境中重建向量库时会包含本批数据；当前 Windows 环境没有完成本批向量化。

没有改动 ChinaTravel 规划需求测试集。本次索引没有自动接入行程求解器的候选生成，主要供 `ask-question` 知识问答检索使用。生成最终自然语言回答仍需要配置模型；原文检索不调用付费 API。

验证分两部分：隔离测试仅使用临时合成记录，不读取本目录真实数据、不联网；真实数据检查只读索引，验证条数、元数据、城市范围和示例检索。二者均不等于大模型回答质量评估。
