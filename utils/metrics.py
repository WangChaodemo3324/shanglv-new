"""
Prometheus 指标埋点（/metrics 端点暴露）

指标设计：
- QUERY_TOTAL      按 intent 分桶的问答计数
- QUERY_LATENCY    端到端问答时延直方图（秒）
- LLM_LATENCY      LLM 单次调用时延（秒，按 agent 名分桶）
- SKILL_TOTAL      skill 执行结果计数（按 agent/status 分桶）
- FASTPATH_TOTAL   快速路径命中计数
- IN_FLIGHT        当前并发处理中的请求数（Gauge）
"""
from prometheus_client import Counter, Gauge, Histogram

QUERY_TOTAL = Counter("shanglv_query_total", "问答总次数", ["intent"])
QUERY_LATENCY = Histogram("shanglv_query_latency_seconds", "端到端问答时延", buckets=(1, 5, 10, 30, 60, 120, 300, 600))
LLM_LATENCY = Histogram("shanglv_llm_latency_seconds", "LLM 单次调用时延", ["agent"], buckets=(1, 5, 15, 30, 60, 120, 300))
SKILL_TOTAL = Counter("shanglv_skill_total", "skill 执行结果", ["agent", "status"])
FASTPATH_TOTAL = Counter("shanglv_fastpath_total", "快速路径命中次数")
IN_FLIGHT = Gauge("shanglv_requests_in_flight", "处理中的请求数")
