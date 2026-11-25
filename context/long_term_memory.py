"""
长期记忆 (Long-term Memory)
PostgreSQL 持久化存储（偏好 / 行程 / 聊天记录），支持跨会话与并发安全；
PostgreSQL 不可用时自动降级为 JSON 文件实现（data/memory/{user_id}.json），对外 API 不变。

表结构（库 shanglv）：
  preferences(user_id, type, value JSONB, updated_at)          -- PK(user_id, type)
  chat_history(id serial, user_id, role, content, session_id, created_at)
  trip_history(id serial, user_id, trip_id, origin, destination,
               start_date, end_date, purpose, data JSONB, created_at)
  user_stats(user_id, total_trips, total_messages,
             frequent_destinations JSONB, total_queries, updated_at)  -- PK(user_id)
"""
from typing import Dict, Any, List
import json
import os
from datetime import datetime
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG_AVAILABLE = True
except ImportError:
    PSYCOPG_AVAILABLE = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    user_id   TEXT NOT NULL,
    type      TEXT NOT NULL,
    value     JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, type)
);
CREATE TABLE IF NOT EXISTS chat_history (
    id         SERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    session_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chat_user_session ON chat_history (user_id, session_id, id);
CREATE TABLE IF NOT EXISTS trip_history (
    id           SERIAL PRIMARY KEY,
    user_id      TEXT NOT NULL,
    trip_id      TEXT,
    origin       TEXT,
    destination  TEXT,
    start_date   TEXT,
    end_date     TEXT,
    purpose      TEXT,
    data         JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trip_user ON trip_history (user_id, id);
CREATE TABLE IF NOT EXISTS user_stats (
    user_id               TEXT PRIMARY KEY,
    total_trips           INTEGER NOT NULL DEFAULT 0,
    total_messages        INTEGER NOT NULL DEFAULT 0,
    total_queries         INTEGER NOT NULL DEFAULT 0,
    frequent_destinations JSONB NOT NULL DEFAULT '{}',
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class LongTermMemory:
    """
    长期记忆：持久化用户信息
    - PostgreSQL 模式：事务性写入，多进程并发安全，跨会话/跨实例共享
    - JSON 模式（降级）：单文件 data/memory/{user_id}.json（原实现）
    - 用户偏好（家庭地址、酒店品牌、航空公司等）、历史行程记录、统计信息
    """

    def __init__(self, user_id: str, storage_path: str = "data/memory",
                 postgres_config: Dict = None):
        """
        初始化长期记忆

        Args:
            user_id: 用户ID
            storage_path: JSON 降级模式的存储路径
            postgres_config: PostgreSQL 连接配置（None 用 config.POSTGRES_CONFIG；False 强制 JSON 模式）
        """
        self.user_id = user_id
        self.storage_path = storage_path
        self.db_path = os.path.join(storage_path, f"{user_id}.json")

        # 确保存储目录存在
        Path(storage_path).mkdir(parents=True, exist_ok=True)

        self._pg_conn = None
        self._pg_available = False

        if PSYCOPG_AVAILABLE and postgres_config is not False:
            try:
                if postgres_config is None:
                    from config import POSTGRES_CONFIG
                    postgres_config = POSTGRES_CONFIG
                self._pg_connect_kwargs = dict(
                    host=postgres_config.get("host", "127.0.0.1"),
                    port=postgres_config.get("port", 5432),
                    dbname=postgres_config.get("dbname", "shanglv"),
                    user=postgres_config.get("user", "shanglv"),
                    password=postgres_config.get("password", "shanglv"),
                    connect_timeout=postgres_config.get("connect_timeout", 5),
                )
                self._pg_conn = psycopg2.connect(**self._pg_connect_kwargs)
                self._pg_conn.autocommit = False
                with self._pg_conn.cursor() as cur:
                    cur.execute(_SCHEMA)
                self._pg_conn.commit()
                self._pg_available = True
                # 首次接入：把旧 JSON 数据迁移进 PG（幂等）
                self._migrate_json_to_pg()
                logger.info(f"Long-term memory using PostgreSQL (user: {user_id})")
            except Exception as e:
                logger.warning(f"PostgreSQL unavailable, falling back to JSON file: {e}")
                self._pg_conn = None
                self._pg_available = False

        # JSON 降级模式的数据载体
        self.data = self._load() if not self._pg_available else None
        if not self._pg_available:
            logger.info(f"Long-term memory initialized for user: {user_id} (JSON mode)")

    # ========== PG 基础设施 ==========

    def _pg_execute(self, sql: str, params: tuple = None, fetch: bool = False):
        """执行 SQL（自动重连一次）。fetch=True 返回行列表；写操作成功返回 True；失败返回 None"""
        for attempt in (1, 2):
            try:
                with self._pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, params)
                    result = cur.fetchall() if fetch else True
                self._pg_conn.commit()
                return result
            except psycopg2.Error as e:
                self._pg_conn.rollback()
                conn_lost = "connection" in str(e).lower() or e.pgcode in ("57P01", "57P03", "08003", "08006")
                if attempt == 1 and conn_lost:
                    try:
                        self._pg_conn.close()
                    except Exception:
                        pass
                    try:
                        self._pg_conn = psycopg2.connect(**self._pg_connect_kwargs)
                        continue
                    except Exception:
                        logger.warning(f"PG reconnect failed, degrading to JSON mode: {e}")
                        self._pg_available = False
                        self.data = self._load()
                        return None
                logger.error(f"PG execute failed: {e}")
                return None
        return None

    def _migrate_json_to_pg(self):
        """把已存在的 JSON 文件数据一次性导入 PG（仅当 PG 中该用户无任何数据时）"""
        if not os.path.exists(self.db_path):
            return
        try:
            with open(self.db_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Skip JSON->PG migration (unreadable file): {e}")
            return

        existing = self._pg_execute(
            "SELECT 1 FROM preferences WHERE user_id = %s LIMIT 1", (self.user_id,), fetch=True)
        chat_count = self._pg_execute(
            "SELECT count(*) AS c FROM chat_history WHERE user_id = %s", (self.user_id,), fetch=True)
        if (existing and len(existing) > 0) or (chat_count and chat_count[0]["c"] > 0):
            return  # PG 已有数据，不迁移

        prefs = data.get("preferences", [])
        if isinstance(prefs, list):
            for p in prefs:
                if isinstance(p, dict) and p.get("type"):
                    self._pg_execute(
                        "INSERT INTO preferences (user_id, type, value) VALUES (%s, %s, %s) "
                        "ON CONFLICT (user_id, type) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                        (self.user_id, p["type"], json.dumps(p.get("value"), ensure_ascii=False)))
        elif isinstance(prefs, dict):
            for t, v in prefs.items():
                if v is not None:
                    self._pg_execute(
                        "INSERT INTO preferences (user_id, type, value) VALUES (%s, %s, %s) "
                        "ON CONFLICT (user_id, type) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                        (self.user_id, t, json.dumps(v, ensure_ascii=False)))

        for m in data.get("chat_history", []):
            self._pg_execute(
                "INSERT INTO chat_history (user_id, role, content, session_id) VALUES (%s, %s, %s, %s)",
                (self.user_id, m.get("role", "user"), m.get("content", ""), m.get("session_id")))

        for t in data.get("trip_history", []):
            self._pg_execute(
                "INSERT INTO trip_history (user_id, trip_id, origin, destination, start_date, end_date, purpose, data) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (self.user_id, t.get("trip_id"), t.get("origin"), t.get("destination"),
                 t.get("start_date"), t.get("end_date"), t.get("purpose"),
                 json.dumps(t, ensure_ascii=False)))

        stats = data.get("statistics", {})
        self._pg_execute(
            "INSERT INTO user_stats (user_id, total_trips, total_messages, total_queries, frequent_destinations) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET total_trips = EXCLUDED.total_trips, "
            "total_messages = EXCLUDED.total_messages, total_queries = EXCLUDED.total_queries, "
            "frequent_destinations = EXCLUDED.frequent_destinations",
            (self.user_id, stats.get("total_trips", 0), stats.get("total_messages", 0),
             stats.get("total_queries", 0),
             json.dumps(stats.get("frequent_destinations", {}), ensure_ascii=False)))
        logger.info(f"Migrated JSON memory of user {self.user_id} into PostgreSQL")

    # ========== JSON 降级模式（原实现） ==========

    def _load(self) -> Dict[str, Any]:
        """（JSON 模式）从文件加载数据"""
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    logger.debug(f"Loaded long-term memory from {self.db_path}")
                    return self._migrate_data(data)
            except Exception as e:
                logger.error(f"Failed to load long-term memory: {e}")
                return self._init_data()
        else:
            logger.info("No existing long-term memory, creating new")
            return self._init_data()

    def _migrate_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """（JSON 模式）迁移旧数据格式"""
        if "chat_history" not in data:
            data["chat_history"] = []
        if "trip_history" not in data:
            data["trip_history"] = []
        if "statistics" not in data:
            data["statistics"] = {}
        if "total_messages" not in data.get("statistics", {}):
            data["statistics"]["total_messages"] = 0
        if "preferences" not in data:
            data["preferences"] = []

        if isinstance(data.get("preferences"), dict):
            old_prefs = data["preferences"]
            new_prefs = []
            for pref_type, pref_value in old_prefs.items():
                if pref_value is not None:
                    new_prefs.append({"type": pref_type, "value": pref_value})
            data["preferences"] = new_prefs
            logger.info(f"Migrated: Converted preferences from dict to list ({len(new_prefs)} items)")

        if isinstance(data.get("preferences"), list):
            fixed_prefs = []
            for pref in data["preferences"]:
                if isinstance(pref, dict):
                    if pref.get("type") == "preferences" and isinstance(pref.get("value"), list):
                        for nested_pref in pref["value"]:
                            if isinstance(nested_pref, dict) and "type" in nested_pref:
                                fixed_prefs.append({"type": nested_pref["type"], "value": nested_pref["value"]})
                        logger.info("Migrated: Fixed nested preferences bug")
                    else:
                        fixed_prefs.append(pref)
            if fixed_prefs != data["preferences"]:
                data["preferences"] = fixed_prefs

        self.data = data
        self._save()
        return data

    def _init_data(self) -> Dict[str, Any]:
        """（JSON 模式）初始化数据结构"""
        return {
            "user_id": self.user_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "preferences": [],
            "chat_history": [],
            "trip_history": [],
            "statistics": {
                "total_trips": 0,
                "total_messages": 0,
                "frequent_destinations": {}
            }
        }

    def _save(self):
        """（JSON 模式）保存数据到文件"""
        if self._pg_available:
            return  # PG 模式不需要整文件保存
        try:
            self.data["updated_at"] = datetime.now().isoformat()
            with open(self.db_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            logger.debug(f"Saved long-term memory to {self.db_path}")
        except Exception as e:
            logger.error(f"Failed to save long-term memory: {e}")

    # ========== 偏好 ==========

    def save_preference(self, pref_type: str, value: Any):
        """保存用户偏好"""
        if self._pg_available:
            r = self._pg_execute(
                "INSERT INTO preferences (user_id, type, value) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id, type) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (self.user_id, pref_type, json.dumps(value, ensure_ascii=False)))
            if r is not None:
                return
            logger.info(f"Saved preference: {pref_type} = {value}")
            return

        preferences = self.data["preferences"]
        for pref in preferences:
            if pref.get("type") == pref_type:
                pref["value"] = value
                self._save()
                logger.info(f"Saved preference: {pref_type} = {value}")
                return
        preferences.append({"type": pref_type, "value": value})
        self._save()
        logger.info(f"Saved preference: {pref_type} = {value}")

    def get_preference(self, pref_type: str = None) -> Any:
        """获取用户偏好；pref_type=None 返回 {type: value} 字典"""
        if self._pg_available:
            rows = self._pg_execute(
                "SELECT type, value FROM preferences WHERE user_id = %s", (self.user_id,), fetch=True)
            if rows is not None:
                result = {r["type"]: r["value"] for r in rows}
                if pref_type is None:
                    return result
                return result.get(pref_type)
            return {} if pref_type is None else None

        preferences = self.data["preferences"]
        if pref_type is None:
            result = {}
            for pref in preferences:
                result[pref.get("type")] = pref.get("value")
            return result
        for pref in preferences:
            if pref.get("type") == pref_type:
                return pref.get("value")
        return None

    def _append_to_list_preference(self, pref_type: str, item: str):
        """列表型偏好追加（hotel_brands / airlines）"""
        if self._pg_available:
            r = self._pg_execute(
                "SELECT value FROM preferences WHERE user_id = %s AND type = %s",
                (self.user_id, pref_type), fetch=True)
            if r is not None:
                current = r[0]["value"] if r else []
                if not isinstance(current, list):
                    current = [current] if current else []
                if item not in current:
                    current.append(item)
                self._pg_execute(
                    "INSERT INTO preferences (user_id, type, value) VALUES (%s, %s, %s) "
                    "ON CONFLICT (user_id, type) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                    (self.user_id, pref_type, json.dumps(current, ensure_ascii=False)))
                logger.info(f"Appended {item} to {pref_type}")
                return
            return

        preferences = self.data["preferences"]
        for pref in preferences:
            if pref.get("type") == pref_type:
                if not isinstance(pref["value"], list):
                    pref["value"] = [pref["value"]] if pref["value"] else []
                if item not in pref["value"]:
                    pref["value"].append(item)
                self._save()
                return
        preferences.append({"type": pref_type, "value": [item]})
        self._save()

    def add_hotel_brand(self, brand: str):
        """添加酒店品牌偏好（追加到列表）"""
        self._append_to_list_preference("hotel_brands", brand)
        logger.info(f"Added hotel brand preference: {brand}")

    def add_airline(self, airline: str):
        """添加航空公司偏好（追加到列表）"""
        self._append_to_list_preference("airlines", airline)
        logger.info(f"Added airline preference: {airline}")

    # ========== 聊天记录 ==========

    def add_chat_message(self, role: str, content: str, session_id: str = None):
        """添加聊天消息到长期记忆"""
        if self._pg_available:
            r = self._pg_execute(
                "INSERT INTO chat_history (user_id, role, content, session_id) VALUES (%s, %s, %s, %s)",
                (self.user_id, role, content, session_id))
            if r is not None:
                self._pg_execute(
                    "UPDATE user_stats SET total_messages = total_messages + 1, updated_at = now() "
                    "WHERE user_id = %s", (self.user_id,))
                # 首条消息时 user_stats 行尚不存在，补建后重试一次
                self._pg_execute(
                    "INSERT INTO user_stats (user_id, total_messages) VALUES (%s, 1) "
                    "ON CONFLICT (user_id) DO NOTHING", (self.user_id,))
                return

        self.data["chat_history"].append({
            "role": role, "content": content,
            "timestamp": datetime.now().isoformat(), "session_id": session_id
        })
        self.data["statistics"]["total_messages"] += 1
        self._save()
        logger.debug(f"Added chat message to long-term memory: {role}")

    def get_chat_history(self, limit: int = None, session_id: str = None) -> List[Dict[str, Any]]:
        """获取聊天历史（可按会话过滤）"""
        if self._pg_available:
            sql = "SELECT role, content, session_id, created_at AS timestamp FROM chat_history WHERE user_id = %s"
            params: list = [self.user_id]
            if session_id:
                sql += " AND session_id = %s"
                params.append(session_id)
            sql += " ORDER BY id DESC LIMIT %s"
            params.append(limit or 10000)
            rows = self._pg_execute(sql, tuple(params), fetch=True)
            if rows is not None:
                rows = list(reversed(rows))
                for r in rows:
                    r["timestamp"] = r["timestamp"].isoformat() if r.get("timestamp") else ""
                return rows
            return []

        messages = self.data["chat_history"]
        if session_id:
            messages = [m for m in messages if m.get("session_id") == session_id]
        if limit:
            return messages[-limit:]
        return messages

    # ========== 行程 ==========

    def save_trip_history(self, trip_info: Dict[str, Any]):
        """保存行程历史"""
        destination = trip_info.get("destination")
        if self._pg_available:
            r = self._pg_execute(
                "INSERT INTO trip_history (user_id, trip_id, origin, destination, start_date, end_date, purpose, data) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (self.user_id, None, trip_info.get("origin"), destination,
                 trip_info.get("start_date"), trip_info.get("end_date"),
                 trip_info.get("purpose"), json.dumps(trip_info, ensure_ascii=False)))
            if r is not None:
                self._pg_execute(
                    "UPDATE user_stats SET total_trips = total_trips + 1, updated_at = now() "
                    "WHERE user_id = %s", (self.user_id,))
                # 首条行程时 user_stats 行尚不存在，补建后重试一次
                self._pg_execute(
                    "INSERT INTO user_stats (user_id, total_trips) VALUES (%s, 1) "
                    "ON CONFLICT (user_id) DO NOTHING", (self.user_id,))
                if destination:
                    self._pg_execute(
                        "UPDATE user_stats SET frequent_destinations = jsonb_set(frequent_destinations, "
                        "ARRAY[%s], (COALESCE(frequent_destinations->>%s, '0')::int + 1)::text::jsonb) "
                        "WHERE user_id = %s", (destination, destination, self.user_id))
                logger.info("Saved trip history to PostgreSQL")
                return

        trip_record = {
            "trip_id": f"trip_{len(self.data['trip_history']) + 1}",
            "timestamp": datetime.now().isoformat(),
            **trip_info
        }
        self.data["trip_history"].append(trip_record)
        self.data["statistics"]["total_trips"] += 1
        if destination:
            freq = self.data["statistics"]["frequent_destinations"]
            freq[destination] = freq.get(destination, 0) + 1
        self._save()
        logger.info(f"Saved trip history: {trip_record['trip_id']}")

    def get_trip_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """获取历史行程"""
        if self._pg_available:
            sql = "SELECT data FROM trip_history WHERE user_id = %s"
            params: list = [self.user_id]
            sql += " ORDER BY id DESC LIMIT %s"
            params.append(limit if limit else 10000)
            rows = self._pg_execute(sql, tuple(params), fetch=True)
            if rows is not None:
                trips = [dict(r["data"]) for r in rows]
                return list(reversed(trips))
            return []

        return self.data["trip_history"][-limit:] if limit else self.data["trip_history"]

    def get_frequent_destinations(self, top_n: int = 5) -> List[tuple]:
        """获取常去目的地 [(destination, count), ...]"""
        if self._pg_available:
            rows = self._pg_execute(
                "SELECT frequent_destinations FROM user_stats WHERE user_id = %s",
                (self.user_id,), fetch=True)
            if rows is not None:
                freq = rows[0]["frequent_destinations"] if rows else {}
                sorted_dest = sorted(freq.items(), key=lambda x: x[1], reverse=True)
                return sorted_dest[:top_n]
            return []

        freq = self.data["statistics"]["frequent_destinations"]
        sorted_dest = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        return sorted_dest[:top_n]

    # ========== 统计与维护 ==========

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        if self._pg_available:
            rows = self._pg_execute(
                "SELECT total_trips, total_messages, total_queries, frequent_destinations "
                "FROM user_stats WHERE user_id = %s", (self.user_id,), fetch=True)
            if rows is not None:
                if rows:
                    r = rows[0]
                    return {
                        "total_trips": r["total_trips"],
                        "total_messages": r["total_messages"],
                        "total_queries": r["total_queries"],
                        "frequent_destinations": r["frequent_destinations"],
                    }
                return {"total_trips": 0, "total_messages": 0, "total_queries": 0, "frequent_destinations": {}}
            return {}

        return self.data["statistics"].copy()

    def delete_all(self):
        """删除该用户所有数据（包括 JSON 文件）"""
        if self._pg_available:
            self._pg_execute("DELETE FROM preferences WHERE user_id = %s", (self.user_id,))
            self._pg_execute("DELETE FROM chat_history WHERE user_id = %s", (self.user_id,))
            self._pg_execute("DELETE FROM trip_history WHERE user_id = %s", (self.user_id,))
            self._pg_execute("DELETE FROM user_stats WHERE user_id = %s", (self.user_id,))
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
            logger.warning(f"Deleted long-term memory file: {self.db_path}")

    def close(self):
        """关闭数据库连接"""
        if self._pg_conn:
            try:
                self._pg_conn.close()
            except Exception:
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
