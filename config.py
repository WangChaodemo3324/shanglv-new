"""
商旅助手多智能体系统配置
"""
import os

# 高德官方 Streamable HTTP MCP。配置密钥后启用，密钥只留在服务端。
AMAP_MCP_CONFIG = {
    "enabled": os.environ.get("AMAP_MCP_ENABLED", "1") == "1",
    "api_key": os.environ.get("AMAP_MAPS_API_KEY", ""),
    "timeout_sec": 12,
    "planning_timeout_sec": 90,
    "max_activities": 10,
    "transport": os.environ.get("AMAP_TRANSPORT", "driving"),
    "buffer_min": 10,
}

# LLM Configuration
LLM_CONFIG = {
    "base_url": os.environ.get("AIGC_BASE_URL", ""),  # 你的 OpenAI 兼容网关地址
    "api_key": os.environ.get("AIGC_APP_ID", ""),  # 从环境变量或此处填写
    "model_name": os.environ.get("FILL_THOUGHT_MODEL", "deepseek-v4-flash"),
}

# System Configuration
SYSTEM_CONFIG = {
    "enable_llm": True,  # Set to True to use LLM (recommended), False for rule-based
    "log_level": "INFO",
    "max_retries": 3,
    "timeout": 60,  # Increased timeout for better stability
}

# RAG 知识库：嵌入模型（本地路径，无需连 HuggingFace）
RAG_CONFIG = {
    "embedding_model": "data/models/bge-m3",  
}

# 连接与可用性：重试、熔断、健康检查
RESILIENCE_CONFIG = {
    "max_retries": 3,              # 单次请求最大重试次数
    "retry_base_delay_sec": 1.0,   # 重试退避基数（秒）
    "retry_max_delay_sec": 30.0,   # 重试退避上限（秒）
    "circuit_failure_threshold": 5, # 连续失败多少次后熔断
    "circuit_recovery_timeout_sec": 60.0,  # 熔断后多少秒进入半开
    "circuit_half_open_successes": 2,      # 半开状态下连续成功多少次后关闭
    "health_check_timeout_sec": 10.0,      # 健康检查请求超时（秒）
}

# 长期记忆持久化：PostgreSQL（描述文档中的目标态，当前落地实现）
POSTGRES_CONFIG = {
    "host": os.environ.get("PG_HOST", "127.0.0.1"),
    "port": int(os.environ.get("PG_PORT", 5432)),
    "dbname": os.environ.get("PG_DBNAME", "shanglv"),
    "user": os.environ.get("PG_USER", "shanglv"),
    "password": os.environ.get("PG_PASSWORD", "shanglv"),
    "connect_timeout": 5,  # 连不上时快速降级到 JSON 文件，避免启动卡死
}

# pi-durable 服务（Node，M6/M8）启动时的模型环境变量取值也来自这里：
#   AIGC_API_KEY = LLM_CONFIG 的 api_key；INTENT_MODEL/SKILL_MODEL = LLM_CONFIG 的 model_name
# （见 文档和视频/如何运行.md 第 6 步：AIGC_APP_ID=<config.py 里的值>）
# pi-durable 服务（Node，M6）：skill 调度图/checkpoint/恢复由 durable 承载
# enabled=0 时由 Python 本地执行；启用时由 pi-durable 执行技能批次。
PI_DURABLE_CONFIG = {
    "enabled": os.environ.get("PI_DURABLE_ENABLED", "1") == "1",
    "base_url": os.environ.get("PI_DURABLE_URL", "http://127.0.0.1:8788"),
    # Python→pi-durable 的 X-Internal-Token（与 services/pi-durable 的 INTERNAL_TOKEN 一致）
    "token": os.environ.get("PI_DURABLE_TOKEN", "shanglv-local"),
    # pi-durable→Python 回调 /internal/skills/{name}/execute 的令牌（两边一致）
    "skill_execute_token": os.environ.get("SKILL_EXECUTE_TOKEN", "shanglv-local"),
    "submit_timeout_sec": 10.0,   # 提交一轮编排的上限（超时视为不可用 → 降级本地）
    "wait_timeout_sec": float(os.environ.get("PI_DURABLE_WAIT_TIMEOUT", "480")),  # 等结果总预算
    "poll_interval_sec": 30.0,    # 长轮询单次等待
    "health_ttl_sec": 15.0,       # 健康检查缓存
}
REDIS_CONFIG = {
    "host": os.environ.get("REDIS_HOST", "127.0.0.1"),
    "port": int(os.environ.get("REDIS_PORT", 6380)),
    "db": int(os.environ.get("REDIS_DB", 0)),
    "password": os.environ.get("REDIS_PASSWORD", None),
    "socket_connect_timeout": 2,   # 连不上时快速降级到进程内存
    "socket_timeout": 2,
    "session_ttl_sec": 3600,       # 短期记忆 TTL（1小时）
    "preference_ttl_sec": 86400,   # 偏好热数据缓存 24h
    "summary_ttl_sec": 3600,       # LLM 总结缓存 1h
}
