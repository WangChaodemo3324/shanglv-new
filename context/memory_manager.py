"""
记忆管理器 (Memory Manager)
统一管理两层记忆，提供简单的API
- 短期记忆：Redis 缓存（滑动窗口 + TTL，分布式会话共享），降级为进程内存
- 长期记忆：PostgreSQL 持久化（偏好/行程/聊天记录），降级为 JSON 文件
- Redis 缓存层：偏好热数据 + LLM 总结结果（写时失效 / TTL 过期）
"""
from typing import Any, Dict
from .short_term_memory import ShortTermMemory
from .long_term_memory import LongTermMemory
import hashlib
import json
import logging

logger = logging.getLogger(__name__)

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


class _CachedLongTermProxy:
    """
    LongTermMemory 的 Redis 缓存代理：
    - get_preference() 读 Redis（热数据，TTL 由 REDIS_CONFIG 控制）
    - save_preference / add_hotel_brand / add_airline / delete_all 写时失效缓存
    - 其余属性与方法直接透传
    """

    def __init__(self, long_term: LongTermMemory, redis_client=None, user_id: str = ""):
        self._long_term = long_term
        self._redis = redis_client
        self._pref_key = f"shanglv:prefs:{user_id}"
        self._pref_ttl = 86400
        if redis_client is not None:
            try:
                from config import REDIS_CONFIG
                self._pref_ttl = REDIS_CONFIG.get("preference_ttl_sec", 86400)
            except Exception:
                pass

    def get_preference(self, pref_type: str = None):
        """从权威存储读取偏好，禁用存在并发回填窗口的共享读缓存。"""
        # Preferences are small and correctness-sensitive. Read the authoritative
        # store until a versioned, atomic cache protocol exists. Delete-before/
        # after-write alone cannot prevent a late reader from refilling old data.
        return self._long_term.get_preference(pref_type)

    def _invalidate(self):
        if self._redis is not None:
            try:
                self._redis.delete(self._pref_key)
            except Exception as e:
                logger.debug(f"Preference cache invalidation failed: {e}")

    def save_preference(self, *args, **kwargs):
        self._invalidate()
        return self._long_term.save_preference(*args, **kwargs)

    def add_hotel_brand(self, *args, **kwargs):
        self._invalidate()
        return self._long_term.add_hotel_brand(*args, **kwargs)

    def add_airline(self, *args, **kwargs):
        self._invalidate()
        return self._long_term.add_airline(*args, **kwargs)

    def delete_all(self, *args, **kwargs):
        self._invalidate()
        return self._long_term.delete_all(*args, **kwargs)

    def close(self):
        return self._long_term.close()

    def __getattr__(self, name):
        return getattr(self._long_term, name)


class MemoryManager:
    """
    记忆管理器：统一管理两层记忆
    - 短期记忆：最近对话（会话级，Redis + 降级内存）
    - 长期记忆：用户偏好和历史（跨会话，PostgreSQL + 降级 JSON）
    - 缓存层：偏好热数据、LLM 总结结果
    """

    def __init__(self, user_id: str, session_id: str, storage_path: str = "data/memory",
                 llm_model=None, postgres_config=None, redis_config=None):
        """
        初始化记忆管理器

        Args:
            user_id: 用户ID
            session_id: 会话ID
            storage_path: 长期记忆 JSON 降级存储路径
            llm_model: LLM模型实例（用于总结长期记忆）
            postgres_config: PostgreSQL 配置（None 用 config.POSTGRES_CONFIG；False 强制 JSON）
            redis_config: Redis 配置（None 用 config.REDIS_CONFIG；False 强制禁用）
        """
        self.user_id = user_id
        self.session_id = session_id
        self.llm_model = llm_model

        # 共享的 Redis 客户端（短期记忆 + 偏好/总结缓存），不可用时整体禁用
        redis_client = self._init_redis_client(redis_config)

        # 初始化两层记忆
        self.short_term = ShortTermMemory(
            max_turns=10, session_id=session_id, redis_config=redis_config, user_id=user_id,
            pg_fallback=self,  # Redis TTL 过期后从 PG chat_history 降级读取
        )
        if not self.short_term._redis_available and redis_client is not None:
            redis_client = None  # 短期记忆连不上则缓存层也不必尝试
        self._redis_client_cache = redis_client  # 供 LLM 总结缓存使用

        long_term_impl = LongTermMemory(user_id, storage_path, postgres_config=postgres_config)
        self.long_term = _CachedLongTermProxy(
            long_term_impl, redis_client=redis_client, user_id=user_id)
        # 直接引用（部分测试代码直接访问 .data / ._pg_available 等内部属性）
        self.long_term_impl = long_term_impl

        logger.info(f"Memory manager initialized for user {user_id}, session {session_id} "
                    f"(stm={'redis' if self.short_term._redis_available else 'memory'}, "
                    f"ltm={'postgres' if long_term_impl._pg_available else 'json'})")

    def _init_redis_client(self, redis_config):
        if not REDIS_AVAILABLE or redis_config is False:
            return None
        try:
            if redis_config is None:
                from config import REDIS_CONFIG
                redis_config = REDIS_CONFIG
            client = redis.Redis(
                retry=redis.retry.Retry(redis.backoff.NoBackoff(), 0),
                host=redis_config.get("host", "127.0.0.1"),
                port=redis_config.get("port", 6379),
                db=redis_config.get("db", 0),
                password=redis_config.get("password"),
                socket_connect_timeout=redis_config.get("socket_connect_timeout", 2),
                socket_timeout=redis_config.get("socket_timeout", 2),
                decode_responses=True,
            )
            client.ping()
            return client
        except Exception as e:
            logger.warning(f"Redis cache layer disabled: {e}")
            return None

    # ========== 短期记忆操作 ==========

    def add_message(self, role: str, content: str, metadata: Dict = None):
        """
        添加消息到短期记忆和长期记忆

        Args:
            role: 角色 (user/assistant)
            content: 消息内容
            metadata: 元数据
        """
        # 添加到短期记忆（当前会话）
        self.short_term.add_message(role, content, metadata)

        # 同时添加到长期记忆（跨会话持久化）
        self.long_term.add_chat_message(role, content, self.session_id)

    # ========== 长期记忆操作 ==========
    # 注意：大部分方法直接使用 self.short_term 和 self.long_term 即可，无需封装

    # ========== 综合查询 ==========

    def get_full_context(self) -> Dict[str, Any]:
        """
        获取完整上下文（两层记忆）

        Returns:
            完整上下文字典
        """
        return {
            "short_term": {
                "recent_dialogue": self.short_term.get_recent_context(5),
                "context_string": self.short_term.get_context_string(5),
                "statistics": self.short_term.get_statistics()
            },
            "long_term": {
                "preferences": self.long_term.get_preference(),
                "chat_history": self.long_term.get_chat_history(10),
                "trip_history": self.long_term.get_trip_history(5),
                "frequent_destinations": self.long_term.get_frequent_destinations(3),
                "statistics": self.long_term.get_statistics()
            }
        }

    # ========== 会话管理 ==========

    def end_session(self):
        """结束会话"""
        self.short_term.clear()
        logger.info(f"Session ended: {self.session_id}")

    async def get_long_term_summary_async(self, max_messages: int = 50) -> str:
        """
        使用LLM总结长期聊天历史（异步版本），结果经 Redis 缓存。
        缓存 key 含历史指纹（消息数 + 末条内容哈希），历史不变才命中，保证不吐陈旧总结。

        Args:
            max_messages: 最多总结的消息数量

        Returns:
            总结后的文本
        """
        if not self.llm_model:
            return ""

        # 获取长期聊天历史（排除当前会话）
        all_history = self.long_term.get_chat_history(limit=max_messages)
        history_from_other_sessions = [
            msg for msg in all_history
            if msg.get("session_id") != self.session_id
        ]

        # 获取行程历史
        trip_history = self.long_term.get_trip_history(limit=20)

        # 如果既没有聊天记录也没有行程记录，直接返回
        if not history_from_other_sessions and not trip_history:
            return ""

        # --- 缓存命中检查 ---
        fingerprint = ""
        if history_from_other_sessions:
            last = history_from_other_sessions[-1]
            raw = f"{len(history_from_other_sessions)}|{last.get('content', '')[:80]}"
            fingerprint = hashlib.md5(raw.encode('utf-8')).hexdigest()[:12]
        cache_key = f"shanglv:summary:{self.user_id}:{fingerprint}"
        if hasattr(self, '_redis_client_cache') and self._redis_client_cache is not None:
            try:
                cached = self._redis_client_cache.get(cache_key)
                if cached:
                    logger.info("Long-term summary cache hit")
                    return cached
            except Exception as e:
                logger.debug(f"Summary cache read failed: {e}")

        # 构建聊天记录文本
        history_text = []
        for msg in history_from_other_sessions[-max_messages:]:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp", "")
            history_text.append(f"[{timestamp}] {role}: {content}")

        history_str = "\n".join(history_text) if history_text else "（无聊天记录）"

        # 构建行程历史文本
        trip_text = []
        for trip in trip_history:
            origin = trip.get("origin", "未知")
            destination = trip.get("destination", "未知")
            start_date = trip.get("start_date", "")
            end_date = trip.get("end_date", "")
            purpose = trip.get("purpose", "旅游")
            timestamp = trip.get("timestamp", "")

            if start_date and end_date:
                trip_text.append(f"[{timestamp}] {origin} → {destination} ({start_date} 至 {end_date}) - {purpose}")
            elif start_date:
                trip_text.append(f"[{timestamp}] {origin} → {destination} ({start_date}) - {purpose}")
            else:
                trip_text.append(f"[{timestamp}] {origin} → {destination} - {purpose}")

        trip_str = "\n".join(trip_text) if trip_text else "（无行程记录）"

        # 使用LLM总结
        summarization_prompt = f"""请总结以下历史信息中的关键内容，包括：
1. 用户的旅行偏好和习惯
2. 用户询问过的重要问题
3. 用户的出行历史和目的地
4. 其他重要的上下文信息

【历史聊天记录】
{history_str}

【历史行程记录】
{trip_str}

请用简洁的语言总结（不超过200字）："""

        try:
            # 调用模型（异步调用）
            response = await self.llm_model([{"role": "user", "content": summarization_prompt}])

            # 处理异步生成器响应
            summary = ""
            if hasattr(response, '__aiter__'):
                # 异步生成器，需要迭代获取内容
                async for chunk in response:
                    if isinstance(chunk, str):
                        summary = chunk
                    elif hasattr(chunk, 'content'):
                        if isinstance(chunk.content, str):
                            summary = chunk.content
                        elif isinstance(chunk.content, list):
                            for item in chunk.content:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    summary = item.get('text', '')
            elif hasattr(response, 'content'):
                summary = str(response.content)
            else:
                summary = str(response)

            logger.info(f"Generated long-term memory summary ({len(summary)} chars)")

            # --- 回填缓存 ---
            if hasattr(self, '_redis_client_cache') and self._redis_client_cache is not None:
                try:
                    ttl = 3600
                    from config import REDIS_CONFIG
                    ttl = REDIS_CONFIG.get("summary_ttl_sec", 3600)
                    self._redis_client_cache.setex(cache_key, ttl, summary)
                except Exception as e:
                    logger.debug(f"Summary cache write failed: {e}")

            return summary.strip()

        except Exception as e:
            logger.error(f"Failed to generate long-term summary: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ""
