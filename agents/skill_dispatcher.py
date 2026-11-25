"""
调度器 SkillDispatcher
职责：消费意图识别产出的调度计划，确定性执行全部 skill（自身不调用 LLM）

核心机制（按 reply() 执行顺序）：
1. 前置条件门控：规划类请求缺关键行程要素时拦截追问，不发 LLM
2. 局部重规划：修改类请求复用上轮信息层结果，只重跑分析层
3. 简单问题快速路径：单 skill 无依赖直接执行，跳过批次聚合
4. 两级并行调度：同优先级 asyncio.gather 并行，分析层消费信息层产出
5. consumes 按需投喂：按 SKILL.md 声明过滤前序结果
6. 输出契约归一化 + 确定性校验（失败带原因打回一次）
7. 结果聚合 / 记忆更新 / 执行追踪
"""
from agentscope.message import Msg
from typing import Optional, Union, List, Dict, Any
import json
import logging
import asyncio

from utils.skill_loader import SkillLoader
from utils.execution_tracer import ExecutionTracer
from utils.contracts import normalize_intention, normalize_preferences

logger = logging.getLogger(__name__)


class SkillDispatcher:
    """调度器：消费意图识别的调度计划，确定性执行 skill 并聚合结果"""

    def __init__(
        self,
        name: str = "SkillDispatcher",
        agent_registry: Dict[str, Any] = None,
        memory_manager = None,
        **kwargs
    ):
        """
        初始化协调器

        Args:
            name: 智能体名称
            agent_registry: skill 注册表 {调度名: agent 实例（通用 runner 包装）}
            memory_manager: 记忆管理器
        """
        self.name = name
        self.agent_registry = agent_registry or {}
        self.memory_manager = memory_manager
        self._fast_path_used = False
        # skill 的 consumes 声明缓存（调度名 → consumes 列表）
        self._skill_meta = self._load_skill_meta()
        # 上一轮各 agent 的结果缓存 {agent_name: result_dict}，局部重规划时复用 P1
        self._last_results: Dict[str, Dict] = {}
        self._last_reuse_key = None
        # 执行追踪（PG 优先，内存降级；写入失败静默不影响主流程）
        self.tracer = ExecutionTracer()
        # 当前请求的追踪上下文（user_id/session_id/query），由 CLI/server 在每轮设置
        self._trace_ctx: Dict[str, str] = {}

    # 需要完整行程要素才值得发 LLM 的规划类 skill（其余 skill 缺要素也能干）
    _GATED_AGENTS = {"itinerary_planning", "expense_estimate", "conflict_check", "trip_compare"}
    # 各 skill 的硬前置字段
    _REQUIRED_FIELDS = {
        "itinerary_planning": ["destination", "duration_days", "start_date"],
        "expense_estimate": ["destination", "duration_days"],
        "conflict_check": ["destination", "start_date", "end_date"],
        "trip_compare": ["destination"],
    }
    _FIELD_HINTS = {
        "destination": "要去哪个城市（如：杭州）",
        "origin": "从哪里出发（如：北京；不说也可以用你的家庭住址推断）",
        "duration_days": "去几天（如：3天）",
        "start_date": "哪天出发（如：10月20日 / 下周三）",
        "end_date": "哪天返回（不说可按天数推算）",
    }

    def _check_preconditions(self, intention_data: Dict[str, Any],
                             sorted_schedule: List[Dict]) -> Optional[Dict[str, Any]]:
        """返回「待确认」结构化应答；无缺失返回 None 放行。
        规则：调度里有规划类 skill 且其硬前置字段缺失时拦截。
        宽容性：以 key_entities 实际值为准（LLM 忘标 missing_info 也拦、误标但有值也放行）。"""
        gated = [t.get("agent_name", "") for t in sorted_schedule
                 if t.get("agent_name", "") in self._GATED_AGENTS]
        if not gated:
            return None
        ke = intention_data.get("key_entities", {}) or {}
        missing = []
        for name in gated:
            for f in self._REQUIRED_FIELDS.get(name, []):
                v = ke.get(f)
                if (v is None or (isinstance(v, str) and (not v.strip() or v.strip().lower() == "null"))) \
                        and f not in missing:
                    missing.append(f)
        # end_date 可由 start_date + duration_days 推算，不单独拦
        if "end_date" in missing and ke.get("start_date") and ke.get("duration_days"):
            missing.remove("end_date")
        # origin 缺失可由家庭住址推断，不拦（单城行程也合法）
        missing = [f for f in missing if f != "origin"]
        if not missing:
            return None
        hints = [f"  • {self._FIELD_HINTS.get(f, f)}" for f in missing]
        logger.info(f"gate: missing {missing}, asking user instead of running {gated}")
        return {
            "status": "needs_input",
            "missing_info": missing,
            "agents_on_hold": gated,
            "message": "规划前需要确认以下信息，补充后我马上继续：\n" + "\n".join(hints),
        }

    def set_trace_context(self, user_id: str, session_id: str, query: str,
                          turn_id: str = None):
        """CLI/server 每轮请求前调用：设置本轮追踪上下文。
        turn_id 标识本轮对话（同轮各 skill 共享），未传则自动生成。"""
        import uuid as _uuid
        self._trace_ctx = {"user_id": user_id or "", "session_id": session_id or "",
                           "query": (query or "")[:200],
                           "turn_id": turn_id or _uuid.uuid4().hex}

    def restore_last_results(self, session_id: str):
        """从执行日志恢复该会话最近一轮的 skill 结果到 _last_results（跨重启续命局部重规划）。
        只在内存缓存为空时恢复（进程内已有更新的轮次时不覆盖）。失败静默。"""
        if self._last_results or not session_id:
            return
        try:
            restored = self.tracer.latest_turn_results(session_id)
            if restored:
                self._last_results = restored
                logger.info(f"Restored {len(restored)} agent results from trace log "
                            f"for session {session_id}")
        except Exception as e:
            logger.debug(f"restore_last_results failed: {e}")

    def switch_session(self, session_id: str):
        """会话切换（server/CLI 复用）：清空旧会话的结果缓存，按新会话从日志恢复"""
        self._last_results = {}
        self._last_reuse_key = None
        self.restore_last_results(session_id)

    def _load_skill_meta(self) -> Dict[str, List[str]]:
        """读取所有 SKILL.md frontmatter 的 consumes/need_memory，建立调度名→元信息映射"""
        from utils.skill_loader import DISPATCH_ALIASES
        meta: Dict[str, List[str]] = {}
        try:
            loader = SkillLoader()
            infos = loader.load_skills() or {}
            for dir_name, info in infos.items():
                # 目录名下划线化（与 LazyAgentRegistry 的 legacy mapping 一致）
                meta[dir_name.replace("-", "_")] = info if isinstance(info, dict) else {}
            # 调度别名（itinerary_planning/rag_knowledge 等历史名）指向同一份声明——
            # 否则 plan-trip 的 consumes 对调度名失效（收全量前序结果 + fast-path 保护虚设）
            for dispatch, dir_name in DISPATCH_ALIASES.items():
                if dir_name in infos and dispatch not in meta:
                    meta[dispatch] = infos[dir_name] if isinstance(infos[dir_name], dict) else {}
        except Exception as e:
            logger.warning(f"Skill meta load failed, consumes pruning disabled: {e}")
        return meta

    def _get_consumes(self, agent_name: str) -> List[str]:
        """返回该 agent 声明消费的前序 agent 名；['*'] 或空 = 全量"""
        info = self._skill_meta.get(agent_name) or self._skill_meta.get(agent_name.replace("_", "-")) or {}
        return info.get("consumes") or []

    def _get_replay(self, agent_name: str) -> Optional[str]:
        """返回 SKILL.md 声明的重放安全性：safe = 只读可重跑，unsafe = 崩溃后不重放。
        当前作为恢复策略的依据记录在 trace 日志里（M2 恢复 + M3 声明）。"""
        info = self._skill_meta.get(agent_name) or self._skill_meta.get(agent_name.replace("_", "-")) or {}
        return info.get("replay")

    def _filter_previous_results(self, agent_name: str, previous_results: List[Dict]) -> List[Dict]:
        """按 consumes 过滤前序结果（控制上下文膨胀）"""
        consumes = self._get_consumes(agent_name)
        if not consumes or consumes == ["*"]:
            return previous_results
        return [r for r in previous_results if r.get("agent_name") in consumes]

    def _apply_replan(self, sorted_schedule: List[Dict]) -> tuple:
        """局部重规划：从调度中剔除「上一轮已成功、且本轮无必要重跑」的 P1 节点，
        返回 (复用的 agent 结果列表, 剩余调度)。
        策略（保守）：
          - 剔除条件：priority == 1（信息类）且上一轮同名 agent 结果为 success
            且它不是用户本轮直接要的答案（本轮调度里存在 P2 消费它）
          - 剔除的 P1 结果直接进入 results（作为 P2 的输入与最终聚合的一部分）
        误判代价分析：若本轮 P1 其实变了（如用户换了目的地导致预算标准变化），
        意图识别不给 replan 标记即可全量重跑——replan 判定权在意图识别。"""
        has_p2 = any(t.get("priority", 1) > 1 for t in sorted_schedule)
        if not has_p2:
            return [], sorted_schedule  # 没有 P2 时不存在"复用给谁"的问题，全量跑

        reused_results = []
        remaining = []
        for task in sorted_schedule:
            name = task.get("agent_name", "")
            if task.get("priority", 1) == 1 and name in self._last_results:
                cached = self._last_results[name]
                if cached.get("status") == "success":
                    reused_results.append({
                        "agent_name": name,
                        "priority": task.get("priority", 1),
                        "result": cached,
                    })
                    continue
            remaining.append(task)
        if not remaining:
            # 极端：全是可复用 P1（不该发生，意图识别 replan 时必调度了 P2）
            return [], sorted_schedule
        return reused_results, remaining

    def plan_batches(self, intention_data: Dict[str, Any]) -> Dict[str, Any]:
        """编排前置规划（本地路径与 pi-durable 路径共用）：
        归一化 → 排序 → 局部重规划 → 前置门控 → 分批。

        返回 {
            "kind": "gate", "gate": {needs_input 结构}        # 拦截追问，不发 LLM
          | "kind": "empty"                                     # 无调度计划
          | "kind": "batches", "batches": [[task, ...], ...],  # 按相邻 priority 分组
                         "reused": [result, ...],               # replan 复用的 P1 结果（排最前）
                         "fast_path": bool, "context": {...}
        }"""
        agent_schedule = intention_data.get("agent_schedule", [])
        if not agent_schedule:
            return {"kind": "empty"}

        self._fast_path_used = False
        sorted_schedule = sorted(agent_schedule, key=lambda x: x.get("priority", 999))

        # ---------- 局部重规划 ----------
        replan = (bool(intention_data.get("replan")) and bool(self._last_results)
                  and self._last_reuse_key is not None
                  and self._last_reuse_key == self._reuse_key(intention_data))
        reused: List[Dict] = []
        if replan:
            reused, sorted_schedule = self._apply_replan(sorted_schedule)
            if reused:
                logger.info(f"Replan: reused {len(reused)} P1 results, "
                            f"rerunning {[t.get('agent_name') for t in sorted_schedule]}")

        # ---------- 前置条件门控 ----------
        gate = self._check_preconditions(intention_data, sorted_schedule)
        if gate:
            return {"kind": "gate", "gate": gate}

        logger.info(f"Orchestrating {len(sorted_schedule)} agents")

        context = self._prepare_context(intention_data)

        # ---------- 快速路径判定 ----------
        complexity = intention_data.get("complexity", "standard")
        fast_path = (complexity == "simple" and len(sorted_schedule) == 1
                     and not self._get_consumes(sorted_schedule[0].get("agent_name", "")))

        # ---------- 按相邻 priority 分批（standard 路径的执行单元） ----------
        batches: List[List[Dict]] = []
        current_priority = None
        parallel_tasks: List[Dict] = []
        for task in sorted_schedule:
            priority = task.get("priority", 0)
            if current_priority is not None and priority != current_priority:
                if parallel_tasks:
                    batches.append(parallel_tasks)
                    parallel_tasks = []
            current_priority = priority
            parallel_tasks.append(task)
        if parallel_tasks:
            batches.append(parallel_tasks)

        return {
            "kind": "batches",
            "batches": batches,
            "reused": reused,
            "fast_path": fast_path,
            "context": context,
        }

    def finalize_turn(self, intention_data: Dict[str, Any],
                      results: List[Dict], fast_path: bool = False) -> Dict[str, Any]:
        """编排收尾（本地路径与 pi-durable 路径共用）：
        刷新 _last_results → 聚合 → 记忆更新。返回聚合结果（不包 Msg 信封）。
        fast_path 供 durable 路径透传（本地路径用 self._fast_path_used）。"""
        self._fast_path_used = fast_path

        # 刷新结果缓存（重规划复用的 P1 结果已在 results 里，一并续命到下一轮）
        self._last_results = {}
        for r in results:
            self._last_results[r.get("agent_name", "")] = r.get("result", {})

        # 聚合结果
        final_result = self._aggregate_results(results, intention_data)

        # 更新记忆
        reuse_key = self._reuse_key(intention_data)
        if self.memory_manager:
            self._update_memory(intention_data, results)

        self._last_reuse_key = reuse_key

        return final_result

    def _reuse_key(self, intention_data):
        # Conservative whole-input invalidation: no provenance after restoration
        # means no reuse. Fine-grained per-skill dependency keys can follow later.
        prefs = self.memory_manager.long_term.get_preference() if self.memory_manager else {}
        return json.dumps({"entities": intention_data.get("key_entities") or {},
                           "preferences": prefs}, sort_keys=True, ensure_ascii=False, default=str)

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        """
        协调执行流程

        Args:
            x: 输入消息，应包含 IntentionAgent 的输出

        Returns:
            Msg: 执行结果
        """
        if x is None:
            return Msg(
                name=self.name,
                content=json.dumps({"error": "No input provided"}),
                role="assistant"
            )

        # 解析输入
        if isinstance(x, list):
            intention_output = x[-1].content if x else "{}"
        else:
            intention_output = x.content

        # 解析意图识别结果
        try:
            intention_data = json.loads(intention_output) if isinstance(intention_output, str) else intention_output
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse intention output: {e}")
            return Msg(
                name=self.name,
                content=json.dumps({"error": "Invalid intention format"}),
                role="assistant"
            )

        # 意图输出契约归一化（null 字符串/非法枚举/形态兜底）
        intention_data = normalize_intention(intention_data)

        plan = self.plan_batches(intention_data)
        if plan["kind"] == "empty":
            return Msg(
                name=self.name,
                content=json.dumps({
                    "status": "no_agents",
                    "message": "没有需要调度的智能体"
                }),
                role="assistant"
            )
        if plan["kind"] == "gate":
            return Msg(
                name=self.name,
                content=json.dumps(plan["gate"], ensure_ascii=False),
                role="assistant"
            )

        batches = plan["batches"]
        reused = plan["reused"]
        context = plan["context"]
        fast_path = plan["fast_path"]

        if fast_path:
            # ---------- 简单问题快速路径 ----------
            # 单 skill、无 consumes 依赖、且 LLM 判定 simple → 直接执行，跳过分组/聚合的中间结构
            task = batches[0][0]
            logger.info(f"Fast path (simple): {task.get('agent_name')}")
            # 先置标记再执行：追踪记录在 _execute_agent 内部落库，晚了就记成 False
            self._fast_path_used = True
            result = await self._execute_agent(
                agent_name=task.get("agent_name"),
                context=context,
                reason=task.get("reason", ""),
                expected_output=task.get("expected_output", ""),
                previous_results=[],
                task_priority=task.get("priority", 1)
            )
            results = reused + [{
                "agent_name": task.get("agent_name"),
                "priority": task.get("priority", 0),
                "result": result
            }]
        else:
            # ---------- standard 路径：两级并行 + consumes 剪枝 ----------
            # 剪枝：若调度里存在 P2，P1 层只保留「被任一 P2 声明消费」或「本身就是答案组成部分」的节点。
            # 简化策略：P2 声明了 consumes 时，未出现在任何 consumes 里且优先级同为 1 的节点照跑
            #（它们通常是用户直接要的答案，如同时问政策+预算），不做激进删除——避免误伤复合意图。
            # 复用的 P1 结果先进入 results：P2 批次执行时它们已在 previous_results 里，
            # 分析层只重跑也能消费上轮信息层产出（同时进入最终聚合）
            results = list(reused)
            for batch_tasks in batches:
                batch_results = await self._execute_parallel_agents(batch_tasks, context, results)
                results.extend(batch_results)

        final_result = self.finalize_turn(intention_data, results, fast_path=self._fast_path_used)

        return Msg(
            name=self.name,
            content=json.dumps(final_result, ensure_ascii=False),
            role="assistant"
        )

    def _prepare_context(self, intention_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        准备上下文信息，供 skill 使用

        Args:
            intention_data: 意图识别结果

        Returns:
            上下文字典
        """
        context = {
            "reasoning": intention_data.get("reasoning", ""),
            "intents": intention_data.get("intents", []),
            "key_entities": intention_data.get("key_entities", {}),
            "rewritten_query": intention_data.get("rewritten_query", "")
        }

        # 从记忆系统获取上下文
        if self.memory_manager:
            # 短期记忆：最近对话（与意图识别统一 5 轮，保持全系统上下文口径一致）
            recent_context = self.memory_manager.short_term.get_recent_context(5)
            context["recent_dialogue"] = recent_context

            # 长期记忆：用户偏好
            preferences = self.memory_manager.long_term.get_preference()
            context["user_preferences"] = preferences

        return context

    async def _execute_parallel_agents(
        self,
        tasks: List[Dict],
        context: Dict[str, Any],
        previous_results: List[Dict]
    ) -> List[Dict]:
        """
        并行执行多个智能体

        Args:
            tasks: 任务列表，每个任务包含 agent_name, priority, reason, expected_output
            context: 上下文信息
            previous_results: 前序智能体的结果

        Returns:
            执行结果列表
        """
        if not tasks:
            return []

        # 如果只有一个任务，直接执行
        if len(tasks) == 1:
            task = tasks[0]
            result = await self._execute_agent(
                agent_name=task.get("agent_name"),
                context=context,
                reason=task.get("reason", ""),
                expected_output=task.get("expected_output", ""),
                previous_results=previous_results,
                task_priority=task.get("priority", 1)
            )
            return [{
                "agent_name": task.get("agent_name"),
                "priority": task.get("priority", 0),
                "result": result
            }]

        # 多个任务并行执行
        logger.info(f"Executing {len(tasks)} agents in parallel")

        # 创建并行任务
        parallel_coroutines = []
        for task in tasks:
            agent_name = task.get("agent_name")
            priority = task.get("priority", 0)
            reason = task.get("reason", "")
            expected_output = task.get("expected_output", "")

            logger.info(f"Parallel executing agent: {agent_name} (priority={priority})")

            # 创建协程
            coroutine = self._execute_agent(
                agent_name=agent_name,
                context=context,
                reason=reason,
                expected_output=expected_output,
                previous_results=previous_results,
                task_priority=priority
            )
            parallel_coroutines.append((agent_name, priority, coroutine))

        # 使用 asyncio.gather 并行执行
        execution_results = await asyncio.gather(
            *[coro for _, _, coro in parallel_coroutines],
            return_exceptions=True
        )

        # 整理结果
        results = []
        for (agent_name, priority, _), exec_result in zip(parallel_coroutines, execution_results):
            if isinstance(exec_result, Exception):
                logger.error(f"Parallel agent execution failed: {agent_name}, error: {exec_result}")
                result = {
                    "status": "error",
                    "agent_name": agent_name,
                    "data": {"error": str(exec_result)},
                    "message": f"并行执行失败: {str(exec_result)}"
                }
            else:
                result = exec_result

            results.append({
                "agent_name": agent_name,
                "priority": priority,
                "result": result
            })

        return results

    async def _execute_agent(
        self,
        agent_name: str,
        context: Dict[str, Any],
        reason: str,
        expected_output: str,
        previous_results: List[Dict],
        task_priority: int = 1
    ) -> Dict[str, Any]:
        """
        执行单个智能体

        Args:
            agent_name: 智能体名称
            context: 上下文信息
            reason: 调用原因
            expected_output: 期望输出
            previous_results: 前序智能体的结果

        Returns:
            执行结果
        """
        # 检查智能体是否注册
        if agent_name not in self.agent_registry:
            logger.warning(f"Agent not registered: {agent_name}")
            return {
                "status": "error",
                "message": f"智能体未注册: {agent_name}"
            }

        agent = self.agent_registry[agent_name]

        # 构建输入消息（previous_results 按 SKILL.md 的 consumes 声明过滤——控制上下文膨胀）
        filtered_prev = self._filter_previous_results(agent_name, previous_results)

        # 构建输入消息
        input_msg = Msg(
            name="Orchestrator",
            content=json.dumps({
                "context": context,
                "reason": reason,
                "expected_output": expected_output,
                "previous_results": filtered_prev
            }, ensure_ascii=False),
            role="user"
        )

        # 执行追踪——计时包裹（记录失败静默）
        import time as _time
        _t0 = _time.monotonic()

        def _trace(status: str, result_obj: Any):
            try:
                self.tracer.record(
                    user_id=self._trace_ctx.get("user_id", ""),
                    session_id=self._trace_ctx.get("session_id", ""),
                    query_digest=self._trace_ctx.get("query", ""),
                    agent_name=agent_name, status=status,
                    duration_ms=int((_time.monotonic() - _t0) * 1000),
                    priority=task_priority,
                    fast_path=self._fast_path_used,
                    input_data={"reason": reason, "expected": expected_output,
                                "replay": self._get_replay(agent_name) or ""},
                    output_data=result_obj,
                    turn_id=self._trace_ctx.get("turn_id"),
                    payload=result_obj if isinstance(result_obj, dict) else None,
                )
            except Exception:
                pass

        try:
            # 调用智能体
            response = await agent.reply(input_msg)

            # 解析响应
            if isinstance(response.content, str):
                try:
                    result = json.loads(response.content)
                except json.JSONDecodeError:
                    result = {"output": response.content}
            else:
                result = response.content

            # 检查 result 中是否有 error 字段
            # 如果有，说明智能体内部执行失败了
            if isinstance(result, dict) and "error" in result:
                error_msg = result.get("error", "未知错误")
                _trace("error", result)
                return {
                    "status": "error",
                    "agent_name": agent_name,
                    "data": result,
                    "message": error_msg
                }

            # 输出契约归一化（budget 数字字段 / itinerary 形态兜底）
            if isinstance(result, dict):
                if agent_name == "budget_check":
                    from utils.contracts import normalize_budget_result
                    result = normalize_budget_result(result) or result

            # 确定性校验（算术/区间复核，毫秒级）→ 失败带缺陷原因有界重试一次，仍失败标 ⚠️ 放行
            if isinstance(result, dict) and agent_name in ("expense_estimate", "conflict_check"):
                from utils.evaluator import (verify_expense_estimate,
                                             verify_conflict_check, verified_execute)
                verify_fn = (verify_expense_estimate if agent_name == "expense_estimate"
                             else verify_conflict_check)
                _first = {"used": False}

                async def _exec_with_fix(issues):
                    """首次复用已执行的输出；重试时把校验缺陷拼进 reason 让 LLM 修正重算"""
                    if not _first["used"]:
                        _first["used"] = True
                        return result
                    retry_payload = json.loads(input_msg.content)
                    retry_payload["reason"] = (
                        (reason or "") + "\n【上次输出未通过确定性校验，以下问题必须修正】\n"
                        + "\n".join(f"- {i}" for i in issues)).strip()
                    retry_msg = Msg(name="Orchestrator",
                                    content=json.dumps(retry_payload, ensure_ascii=False),
                                    role="user")
                    try:
                        resp = await agent.reply(retry_msg)
                        if isinstance(resp.content, str):
                            return json.loads(resp.content)
                        return resp.content if isinstance(resp.content, dict) else result
                    except Exception as retry_e:
                        logger.warning(f"retry failed for {agent_name}: {retry_e}")
                        return result

                result, _v = await verified_execute(_exec_with_fix, verify_fn)
            _trace("success", result if isinstance(result, dict) else str(result)[:200])
            return {
                "status": "success",
                "agent_name": agent_name,
                "data": result
            }

        except Exception as e:
            logger.error(f"Agent execution failed: {agent_name}, error: {e}")
            _trace("exception", {"error": str(e)})
            # 返回友好的错误信息，但不中断流程
            return {
                "status": "error",
                "agent_name": agent_name,
                "data": {"error": str(e)},
                "message": f"智能体执行失败: {str(e)}"
            }

    def _aggregate_results(
        self,
        results: List[Dict],
        intention_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        聚合多个智能体的结果

        Args:
            results: 所有智能体的执行结果
            intention_data: 原始意图识别结果

        Returns:
            聚合后的最终结果
        """
        aggregated = {
            "status": "completed",
            "fast_path": getattr(self, "_fast_path_used", False),
            "intention": {
                "intents": intention_data.get("intents", []),
                "key_entities": intention_data.get("key_entities", {})
            },
            "agents_executed": len(results),
            "results": []
        }

        # 收集每个智能体的结果
        for result in results:
            aggregated["results"].append({
                "agent_name": result["agent_name"],
                "priority": result["priority"],
                "status": result["result"].get("status", "unknown"),
                "data": result["result"].get("data", {})
            })

        # 检查是否有错误
        errors = [r for r in results if r["result"].get("status") == "error"]
        if errors:
            aggregated["status"] = "partial_failure"
            aggregated["errors"] = len(errors)

        return aggregated

    def _update_memory(self, intention_data: Dict[str, Any], results: List[Dict]):
        """
        更新记忆系统

        Args:
            intention_data: 意图识别结果
            results: 智能体执行结果
        """
        if not self.memory_manager:
            return

        # 提取并保存信息到长期记忆
        for result in results:
            agent_name = result["agent_name"]
            data = result["result"].get("data", {})

            # 如果是偏好智能体，保存偏好信息到长期记忆
            if agent_name == "preference" and isinstance(data, dict):
                # 输出契约归一化（嵌套列表/形态错误/空值统一兜底），再做服务端确定性合并
                for pref in normalize_preferences(data):
                    pref_type = pref["type"]
                    pref_value = pref["value"]
                    pref_action = pref["action"]

                    from utils.contracts import _flatten
                    # append → value 为增量，合并进现有列表去重；replace → value 即最终值
                    if pref_action == "append" and pref_type in ("hotel_brands", "airlines"):
                        current = self.memory_manager.long_term.get_preference(pref_type) or []
                        cur_flat = _flatten(current)
                        merged = cur_flat + [v for v in _flatten(pref_value) if v not in cur_flat]
                        self.memory_manager.long_term.save_preference(pref_type, merged)
                        logger.info(f"Appended to {pref_type}: {merged}")
                    elif pref_action == "append":
                        # 非列表型追加（如 meal_preference）：拼接字符串
                        current = self.memory_manager.long_term.get_preference(pref_type)
                        final_val = f"{current}、{pref_value}" if current else str(pref_value)
                        self.memory_manager.long_term.save_preference(pref_type, final_val)
                    else:
                        self.memory_manager.long_term.save_preference(pref_type, pref_value)
                        logger.info(f"Replaced {pref_type}: {pref_value}")


            # 如果是行程规划智能体，保存行程到长期记忆
            if agent_name == "itinerary_planning" and isinstance(data, dict):
                itinerary = data.get("itinerary", {})

                # 只要有行程信息就保存（不管是否完全规划好）
                if itinerary:
                    # 行程要素直接取意图识别的 key_entities（原 event-collection skill 的职责）
                    ke = intention_data.get("key_entities", {}) or {}

                    origin = ke.get("origin")
                    destination = ke.get("destination")
                    start_date = ke.get("start_date")
                    end_date = ke.get("end_date")
                    purpose = ke.get("trip_purpose") or "旅游"

                    # 保存到长期记忆（只要有目的地就保存；同目的地+同日期的重复请求不重复落库）
                    if destination:
                        existing = self.memory_manager.long_term.get_trip_history(limit=100)
                        duplicate = any(
                            e.get("destination") == destination
                            and e.get("start_date") == start_date
                            and (not start_date or e.get("end_date") == end_date)
                            for e in existing
                        )
                        if not duplicate:
                            self.memory_manager.long_term.save_trip_history({
                                "origin": origin,
                                "destination": destination,
                                "start_date": start_date,
                                "end_date": end_date,
                                "purpose": purpose
                            })
                            logger.info(f"Saved trip to long-term memory: {origin} -> {destination}")
                        else:
                            logger.info(f"Duplicate trip skipped: {destination} {start_date}")

        logger.info("Memory updated after orchestration")
