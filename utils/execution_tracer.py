"""
执行追踪（Execution Trace）

多用户场景下的运行记录系统：每次 skill 执行落一条结构化记录，
供事后排查（哪一步慢了/失败了/重试了）与指标统计（延迟分布、失败率、调用频次）。

存储：
- PostgreSQL 模式：execution_trace 表（运维数据，与用户记忆表分离）
- 内存降级模式：环形缓冲（最近 500 条），PG 不可用时保底

设计约束：
- 追踪写入失败绝不影响主流程（全部静默捕获）
- input/output 只存摘要（各截 500 字）——追踪要的是"谁跑了多久成没成"，不是复读机
"""
from typing import Dict, Any, List, Optional
from datetime import datetime
import json
import logging
import threading

logger = logging.getLogger(__name__)

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG_AVAILABLE = True
except ImportError:
    PSYCOPG_AVAILABLE = False

# PG 不可用时的内存环形缓冲大小
_MEMORY_BUFFER_SIZE = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_trace (
    id            SERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL DEFAULT '',
    session_id    TEXT,
    query_digest  TEXT,
    agent_name    TEXT NOT NULL,
    priority      INTEGER,
    status        TEXT NOT NULL,
    duration_ms   INTEGER,
    fast_path     BOOLEAN NOT NULL DEFAULT FALSE,
    replan_reused BOOLEAN NOT NULL DEFAULT FALSE,
    input_digest  TEXT,
    output_digest TEXT,
    turn_id       TEXT,
    payload       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trace_user_session ON execution_trace (user_id, session_id, id);
CREATE INDEX IF NOT EXISTS idx_trace_agent_time ON execution_trace (agent_name, created_at);
"""

# 旧库平滑升级：先补列（幂等），再建引用新列的索引——顺序不能反，
# 否则旧表上索引先于列存在，整个初始化失败，tracer 静默降级内存模式
_MIGRATION = """
ALTER TABLE execution_trace ADD COLUMN IF NOT EXISTS turn_id TEXT;
ALTER TABLE execution_trace ADD COLUMN IF NOT EXISTS payload TEXT;
CREATE INDEX IF NOT EXISTS idx_trace_turn ON execution_trace (session_id, turn_id);
"""

_DIGEST_MAX = 500


def _digest(obj: Any) -> str:
    """输入/输出摘要：JSON 化后截断（保留可读性，不存全文）"""
    try:
        s = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    except (TypeError, ValueError):
        s = str(obj)
    return s[:_DIGEST_MAX] + ("…" if len(s) > _DIGEST_MAX else "")


class ExecutionTracer:
    """执行追踪记录器（编排器持有，_execute_agent 前后调用）"""

    def __init__(self, postgres_config: Optional[Dict] = None):
        self._conn = None
        self._pg_available = False
        self._connect_kwargs: Dict = {}
        self._memory_buffer: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

        if PSYCOPG_AVAILABLE and postgres_config is not False:
            try:
                if postgres_config is None:
                    from config import POSTGRES_CONFIG
                    postgres_config = POSTGRES_CONFIG
                self._connect_kwargs = dict(
                    host=postgres_config.get("host", "127.0.0.1"),
                    port=postgres_config.get("port", 5432),
                    dbname=postgres_config.get("dbname", "shanglv"),
                    user=postgres_config.get("user", "shanglv"),
                    password=postgres_config.get("password", "shanglv"),
                    connect_timeout=postgres_config.get("connect_timeout", 5),
                )
                self._conn = psycopg2.connect(**self._connect_kwargs)
                self._conn.autocommit = True
                with self._conn.cursor() as cur:
                    cur.execute(_SCHEMA)
                    cur.execute(_MIGRATION)
                self._pg_available = True
                logger.info("Execution tracer using PostgreSQL")
            except Exception as e:
                logger.warning(f"Trace PG unavailable, using memory buffer: {e}")
                self._pg_available = False

    # ---------- 写入 ----------

    def record(self, user_id: str, session_id: str, agent_name: str,
               status: str, duration_ms: int, priority: int = 1,
               fast_path: bool = False, replan_reused: bool = False,
               input_data: Any = None, output_data: Any = None,
               query_digest: str = "", turn_id: str = None,
               payload: Any = None) -> None:
        """记录一次 skill 执行。任何异常静默——追踪不能影响主流程。

        turn_id：本轮对话的标识（server 每次请求生成一个），同轮各 skill 共享。
        payload：完整结果 JSON（区别于 output_digest 摘要），供跨重启恢复 _last_results。
        """
        try:
            payload_str = json.dumps(payload, ensure_ascii=False) if payload is not None else None
        except (TypeError, ValueError):
            payload_str = None
        row = {
            "user_id": user_id or "",
            "session_id": session_id,
            "query_digest": (query_digest or "")[:200],
            "agent_name": agent_name,
            "priority": int(priority or 1),
            "status": status,
            "duration_ms": int(duration_ms or 0),
            "fast_path": bool(fast_path),
            "replan_reused": bool(replan_reused),
            "input_digest": _digest(input_data),
            "output_digest": _digest(output_data),
            "turn_id": turn_id,
            "payload": payload_str,
            "created_at": datetime.now().isoformat(),
        }
        try:
            self._write_pg(row)
        except Exception as e:
            logger.debug(f"Trace write failed: {e}")
        try:
            with self._lock:
                self._memory_buffer.append(row)
                if len(self._memory_buffer) > _MEMORY_BUFFER_SIZE:
                    self._memory_buffer = self._memory_buffer[-_MEMORY_BUFFER_SIZE:]
        except Exception:
            pass

    def _write_pg(self, row: Dict) -> None:
        if not self._pg_available:
            return
        # 连接断开时重连一次
        for attempt in (1, 2):
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO execution_trace
                           (user_id, session_id, query_digest, agent_name, priority, status,
                            duration_ms, fast_path, replan_reused, input_digest, output_digest,
                            turn_id, payload)
                           VALUES (%(user_id)s, %(session_id)s, %(query_digest)s, %(agent_name)s,
                                   %(priority)s, %(status)s, %(duration_ms)s, %(fast_path)s,
                                   %(replan_reused)s, %(input_digest)s, %(output_digest)s,
                                   %(turn_id)s, %(payload)s)""",
                        row)
                return
            except psycopg2.Error:
                if attempt == 1:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    try:
                        self._conn = psycopg2.connect(**self._connect_kwargs)
                        self._conn.autocommit = True
                        continue
                    except Exception:
                        self._pg_available = False
                        return
                return

    # ---------- 查询 ----------

    def query(self, user_id: str = None, session_id: str = None,
              agent_name: str = None, limit: int = 50) -> List[Dict[str, Any]]:
        """查询执行记录（时间倒序）"""
        if self._pg_available:
            try:
                sql, params = "SELECT * FROM execution_trace WHERE 1=1", []
                if user_id:
                    sql += " AND user_id = %s"; params.append(user_id)
                if session_id:
                    sql += " AND session_id = %s"; params.append(session_id)
                if agent_name:
                    sql += " AND agent_name = %s"; params.append(agent_name)
                sql += " ORDER BY id DESC LIMIT %s"; params.append(limit)
                with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, tuple(params))
                    rows = cur.fetchall()
                for r in rows:
                    if r.get("created_at"):
                        r["created_at"] = r["created_at"].isoformat()
                return rows
            except Exception as e:
                logger.warning(f"Trace query failed: {e}")
        # 内存降级查询
        with self._lock:
            rows = list(self._memory_buffer)
        out = []
        for r in reversed(rows):
            if user_id and r.get("user_id") != user_id:
                continue
            if session_id and r.get("session_id") != session_id:
                continue
            if agent_name and r.get("agent_name") != agent_name:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def latest_turn_results(self, session_id: str) -> Dict[str, Dict]:
        """恢复指定会话最近一轮的各 skill 完整结果（跨进程/重启后重建 _last_results 用）。

        取该会话最后一条带 payload 的记录所属的 turn_id，再取该轮全部 success 记录。
        意图识别等无 payload 的记录天然排除。失败静默返回空 dict——恢复是优化不是依赖。
        """
        try:
            if self._pg_available:
                with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT turn_id FROM execution_trace "
                        "WHERE session_id = %s AND payload IS NOT NULL AND turn_id IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1", (session_id,))
                    row = cur.fetchone()
                    if not row:
                        return {}
                    cur.execute(
                        "SELECT agent_name, payload FROM execution_trace "
                        "WHERE session_id = %s AND turn_id = %s AND status = 'success' "
                        "AND payload IS NOT NULL ORDER BY id", (session_id, row["turn_id"]))
                    out = {}
                    for r in cur.fetchall():
                        try:
                            out[r["agent_name"]] = json.loads(r["payload"])
                        except (json.JSONDecodeError, TypeError):
                            continue
                    return out
            # 内存降级：倒序扫环形缓冲，取最后一个「带 payload 的轮」
            with self._lock:
                rows = list(self._memory_buffer)
            last_turn = None
            for r in reversed(rows):
                if r.get("session_id") == session_id and r.get("payload"):
                    last_turn = r.get("turn_id")
                    break
            if last_turn is None:
                return {}
            out = {}
            for r in rows:
                if r.get("session_id") == session_id and r.get("turn_id") == last_turn \
                        and r.get("status") == "success" and r.get("payload"):
                    try:
                        out[r["agent_name"]] = json.loads(r["payload"])
                    except (json.JSONDecodeError, TypeError):
                        continue
            return out
        except Exception as e:
            logger.debug(f"latest_turn_results failed: {e}")
            return {}

    def stats(self, user_id: str = None, hours: int = 24):
        """聚合统计：各 skill 的调用次数 / 平均耗时 / 失败率 / P95；快速路径命中率"""
        if self._pg_available:
            try:
                sql = """SELECT agent_name,
                                count(*)                          AS calls,
                                round(avg(duration_ms))           AS avg_ms,
                                percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms,
                                sum(CASE WHEN status != 'success' THEN 1 ELSE 0 END)::float
                                    / count(*)                    AS failure_rate
                         FROM execution_trace
                         WHERE created_at > now() - %s * interval '1 hour'"""
                params: list = [hours]
                if user_id:
                    sql += " AND user_id = %s"; params.append(user_id)
                sql += " GROUP BY agent_name ORDER BY calls DESC"
                with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, tuple(params))
                    rows = cur.fetchall()
                fp_sql = """SELECT count(*) FILTER (WHERE fast_path) AS fast_path_hits,
                                   count(*) AS total
                            FROM execution_trace
                            WHERE created_at > now() - %s * interval '1 hour'"""
                fp_params = [hours]
                if user_id:
                    fp_sql += " AND user_id = %s"
                    fp_params.append(user_id)
                fp_rows = self._pg_execute_fetch(fp_sql, tuple(fp_params))
                fp = fp_rows[0] if fp_rows else {"fast_path_hits": 0, "total": 0}
                return {"by_agent": rows, "fast_path": fp}
            except Exception as e:
                logger.warning(f"Trace stats failed: {e}")
        # 内存降级：简单聚合
        with self._lock:
            rows = list(self._memory_buffer)
        agg: Dict[str, Dict] = {}
        for r in rows:
            a = agg.setdefault(r["agent_name"], {"agent_name": r["agent_name"], "calls": 0,
                                                 "avg_ms": 0, "failure_rate": 0.0})
            a["calls"] += 1
            a["avg_ms"] += r.get("duration_ms", 0)
            if r.get("status") != "success":
                a["failure_rate"] += 1
        for a in agg.values():
            if a["calls"]:
                a["avg_ms"] = round(a["avg_ms"] / a["calls"])
                a["failure_rate"] = round(a["failure_rate"] / a["calls"], 3)
        return {"by_agent": list(agg.values()),
                "fast_path": {"fast_path_hits": 0, "total": 0},
                "note": "内存降级模式（PG 不可用），仅含最近记录"}

    def _pg_execute_fetch(self, sql: str, params: tuple) -> Optional[List[Dict]]:
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
