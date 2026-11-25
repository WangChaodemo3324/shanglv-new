"""
短期记忆 (Short-term Memory)
基于 Redis 的会话记忆缓存（滑动窗口），支持分布式部署时的会话共享；
Redis 不可用时自动降级为进程内存实现，对外 API 不变。
"""
from typing import List, Dict, Any
from datetime import datetime
from uuid import uuid4
import json
import logging

logger = logging.getLogger(__name__)

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


class ShortTermMemory:
    """
    短期记忆：存储最近的对话历史
    - Redis 模式：按 session_id 存 List，TTL 1 小时，跨进程/跨实例共享
    - 内存模式（降级）：进程内滑动窗口（原实现）
    - 存储最近 5-10 轮对话，自动淘汰旧消息
    """

    def __init__(self, max_turns: int = 10, session_id: str = None, redis_config: Dict = None,
                 user_id: str = "", pg_fallback=None):
        """
        初始化短期记忆

        Args:
            max_turns: 最大保存轮数（一轮 = 一对用户-助手消息）
            session_id: 会话ID
            redis_config: Redis 连接配置（None 时用 config.REDIS_CONFIG 默认值）
            user_id: 用户ID（Redis key 必须含用户维度，否则不同用户用同名会话ID会互相串）
        """
        self.max_turns = max_turns
        self.session_id = session_id or f"stm_{uuid4().hex[:8]}"
        self.user_id = user_id
        self._pg_fallback = pg_fallback  # MemoryManager 自身（读 PG chat_history 用）
        self.messages: List[Dict[str, Any]] = []  # 内存降级模式 / 本地读缓存

        self.redis_client = None
        self.session_ttl = 3600
        self._redis_key = f"shanglv:stm:{user_id}:{self.session_id}"
        self._redis_available = False

        if REDIS_AVAILABLE and redis_config is not False:
            try:
                if redis_config is None:
                    from config import REDIS_CONFIG
                    redis_config = REDIS_CONFIG
                self.session_ttl = redis_config.get("session_ttl_sec", 3600)
                self.redis_client = redis.Redis(
                    retry=redis.retry.Retry(redis.backoff.NoBackoff(), 0),
                    host=redis_config.get("host", "127.0.0.1"),
                    port=redis_config.get("port", 6379),
                    db=redis_config.get("db", 0),
                    password=redis_config.get("password"),
                    socket_connect_timeout=redis_config.get("socket_connect_timeout", 2),
                    socket_timeout=redis_config.get("socket_timeout", 2),
                    decode_responses=True,
                )
                self.redis_client.ping()
                self._redis_available = True
                logger.info(f"Short-term memory using Redis (session={self.session_id}, ttl={self.session_ttl}s)")
            except Exception as e:
                logger.warning(f"Redis unavailable, falling back to in-memory short-term memory: {e}")
                self.redis_client = None
                self._redis_available = False

    def add_message(self, role: str, content: str, metadata: Dict = None):
        """
        添加消息到短期记忆

        Args:
            role: 角色 (user/assistant)
            content: 消息内容
            metadata: 额外的元数据
        """
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {}
        }

        if self._redis_available:
            try:
                key = self._redis_key
                pipe = self.redis_client.pipeline()
                pipe.rpush(key, json.dumps(message, ensure_ascii=False))
                # 滑动窗口：每轮 = 2 条消息（用户 + 助手）
                pipe.ltrim(key, -(self.max_turns * 2), -1)
                pipe.expire(key, self.session_ttl)
                pipe.execute()
                self.messages.append(message)  # 本地镜像，读路径免网络往返
                logger.debug(f"Added message to short-term memory (Redis): {role}")
                return
            except Exception as e:
                logger.warning(f"Redis write failed, falling back to memory: {e}")
                self._redis_available = False
                self.redis_client = None

        # 内存模式（降级）
        self.messages.append(message)
        max_messages = self.max_turns * 2
        if len(self.messages) > max_messages:
            self.messages = self.messages[-max_messages:]
        logger.debug(f"Added message to short-term memory: {role}")

    def get_recent_context(self, n_turns: int = None) -> List[Dict[str, Any]]:
        """
        获取最近 n 轮对话。
        Redis 未命中（TTL 过期/服务重启）时从长期记忆的 PG chat_history 降级读取（按 session_id），
        保证跨小时/跨进程的对话上下文不丢失。

        Args:
            n_turns: 获取轮数，默认为全部

        Returns:
            最近的消息列表
        """
        if self._redis_available:
            try:
                # n轮 = 2n条消息，负索引从尾部取最近 n 轮
                raw = (self.redis_client.lrange(self._redis_key, -(n_turns * 2), -1)
                       if n_turns else self.redis_client.lrange(self._redis_key, 0, -1))
                if raw:
                    return [json.loads(r) for r in raw]
                # Redis 空（TTL 过期）：尝试从 PG 降级读取本会话最近对话
                fallback = self._fallback_from_pg(n_turns)
                if fallback:
                    return fallback
                return []
            except Exception as e:
                logger.warning(f"Redis read failed, using local mirror: {e}")
                self._redis_available = False
                self.redis_client = None

        # 内存模式
        if n_turns is None:
            return self.messages.copy()
        n_messages = n_turns * 2
        return self.messages[-n_messages:] if len(self.messages) > n_messages else self.messages.copy()

    def _fallback_from_pg(self, n_turns: int = None) -> List[Dict[str, Any]]:
        """Redis 未命中时，从 PG chat_history 读本会话最近对话（长期记忆表，永不过期）"""
        try:
            if not self._pg_fallback:
                return []
            rows = self._pg_fallback.long_term.get_chat_history(
                limit=(n_turns * 2) if n_turns else 100,
                session_id=self.session_id)
            return [{"role": r.get("role"), "content": r.get("content", ""),
                     "timestamp": r.get("timestamp") or "",
                     "metadata": {}} for r in rows if r.get("role") in ("user", "assistant")]
        except Exception as e:
            logger.debug(f"PG fallback read failed: {e}")
            return []

    def get_context_string(self, n_turns: int = 5) -> str:
        """
        获取最近对话的字符串表示

        Args:
            n_turns: 获取轮数

        Returns:
            格式化的对话字符串
        """
        messages = self.get_recent_context(n_turns)
        if not messages:
            return "无历史对话"

        lines = []
        for msg in messages:
            role_name = "用户" if msg["role"] == "user" else "助手"
            lines.append(f"{role_name}: {msg['content']}")

        return "\n".join(lines)

    def clear(self):
        """清空短期记忆"""
        if self._redis_available:
            try:
                self.redis_client.delete(self._redis_key)
            except Exception as e:
                logger.warning(f"Redis clear failed: {e}")
        self.messages = []
        logger.info("Short-term memory cleared")

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        msgs = self.get_recent_context()
        return {
            "total_messages": len(msgs),
            "max_turns": self.max_turns,
            "backend": "redis" if self._redis_available else "memory",
            "session_id": self.session_id,
            "oldest_message_time": msgs[0]["timestamp"] if msgs else None,
            "newest_message_time": msgs[-1]["timestamp"] if msgs else None
        }
