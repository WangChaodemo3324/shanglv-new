#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
商旅助手 - CLI 交互界面
使用 Rich 库实现美观的终端交互
"""
import asyncio
import sys
import os
# 添加项目根目录到路径
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
import json

# 导入系统组件
from agentscope.model import OpenAIChatModel
from config import LLM_CONFIG, SYSTEM_CONFIG, RESILIENCE_CONFIG
from context.memory_manager import MemoryManager
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.llm_resilience import retry_with_backoff, run_health_check as check_llm_health
from agents.intention_agent import IntentionAgent
from agents.skill_dispatcher import SkillDispatcher
# 移除其他智能体的导入，改用懒加载


def init_agentscope():
    """初始化 AgentScope 框架（1.0.16+ 的 API 变化：init() 不再接受 model_configs，
    模型配置直接在 Agent 初始化时指定）"""
    import agentscope
    agentscope.init(
        project="Shanglv-Travel-Planning",
        name="multi_agent_system",
        logging_level="INFO"
    )
    print(f"✓ AgentScope initialized (version: {agentscope.__version__})")


class ShanglvCLI:
    """商旅助手 CLI"""

    def __init__(self):
        """初始化 CLI"""
        self.console = Console()
        self.user_id = None
        self.session_id = None
        self.memory_manager = None
        self.orchestrator = None
        self.intention_agent = None
        self.model = None
        self._agent_cache = {}  # 智能体缓存
        self.circuit_breaker = None  # 在 initialize_system 中从 RESILIENCE_CONFIG 初始化

    def print_banner(self):
        """打印欢迎横幅"""
        self.console.print("\n[bold cyan]🌏 商旅助手[/bold cyan] - 让差旅更简单\n", style="bold")

    def print_help(self):
        """打印帮助信息"""
        table = Table(title="命令列表", show_header=True, header_style="bold magenta")
        table.add_column("命令", style="cyan", width=20)
        table.add_column("说明", style="white")

        table.add_row("help", "显示此帮助信息")
        table.add_row("status", "查看当前状态和记忆")
        table.add_row("health", "检查 LLM 服务是否可用")
        table.add_row("clear", "清空当前任务（保留长期记忆）")
        table.add_row("history", "查看历史行程")
        table.add_row("preferences", "查看用户偏好")
        table.add_row("exit", "退出程序")
        table.add_row("", "")
        table.add_row("[自然语言]", "直接输入您的需求，如：")
        table.add_row("", "  - 我要从上海去北京出差")
        table.add_row("", "  - 北京的住宿标准是多少")
        table.add_row("", "  - 查询明天的天气")

        self.console.print(table)

    async def initialize_system(self):
        """初始化系统 - 使用懒加载优化启动速度"""
        # 获取用户信息
        self.user_id = Prompt.ask(
            "用户ID",
            default="default_user"
        )

        # 生成会话ID
        import uuid
        self.session_id = str(uuid.uuid4())[:8]

        with self.console.status("初始化中...", spinner="dots"):
            # 初始化AgentScope
            init_agentscope()

            # 初始化模型
            timeout_sec = SYSTEM_CONFIG.get("timeout", 60)
            self.model = OpenAIChatModel(
                model_name=LLM_CONFIG["model_name"],
                api_key=LLM_CONFIG["api_key"],
                client_kwargs={
                    "base_url": LLM_CONFIG["base_url"],
                    "timeout": float(timeout_sec),
                },
                # agentscope 1.0.16 要求 temperature/max_tokens 放在 generate_kwargs，
                # 直接传顶层会被忽略（走默认 token 上限，推理模型的思考会挤截正文）
                generate_kwargs={
                    "temperature": LLM_CONFIG.get("temperature", 0.7),
                    "max_tokens": LLM_CONFIG.get("max_tokens", 16384),
                },
            )

            # 初始化记忆管理器（传入LLM模型用于总结）
            self.memory_manager = MemoryManager(
                user_id=self.user_id,
                session_id=self.session_id,
                llm_model=self.model
            )

            # 初始化意图识别智能体（必须预加载）
            self.intention_agent = IntentionAgent(
                name="IntentionAgent",
                model=self.model
            )

            # 使用懒加载注册器（智能体在首次使用时才加载）
            from agents.lazy_agent_registry import LazyAgentRegistry
            self._agent_cache = {}
            lazy_registry = LazyAgentRegistry(
                model=self.model, 
                cache=self._agent_cache,
                memory_manager=self.memory_manager
            )

            # 预先加载关键智能体（可选，利用 preload）
            # lazy_registry.preload("memory_query", "preference")

            # 初始化协调器（M2：新会话从执行日志恢复最近一轮结果，续命局部重规划）
            self.orchestrator = SkillDispatcher(
                name="SkillDispatcher",
                agent_registry=lazy_registry,
                memory_manager=self.memory_manager
            )
            self.orchestrator.restore_last_results(self.session_id)

            # 熔断器（连接与可用性）
            rc = RESILIENCE_CONFIG
            self.circuit_breaker = CircuitBreaker(
                failure_threshold=rc.get("circuit_failure_threshold", 5),
                recovery_timeout_sec=rc.get("circuit_recovery_timeout_sec", 60.0),
                half_open_successes=rc.get("circuit_half_open_successes", 2),
            )

            # 启用时装配 pi-durable 客户端，技能批次始终走该服务。
            self.pi_client = None
            from config import PI_DURABLE_CONFIG
            if PI_DURABLE_CONFIG.get("enabled"):
                from utils.pi_durable_client import PiDurableClient
                self.pi_client = PiDurableClient(PI_DURABLE_CONFIG)

        self.console.print(f"✓ 就绪 (用户: {self.user_id}) - 输入 help 查看帮助\n", style="green")

    async def _recognize_intent(self, context_messages, user_input: str):
        """M7：意图识别双路——durable GenerationTask 优先，降级本地（与 server._recognize_intent 同构）。
        意图识别幂等，降级安全。"""
        if self.pi_client is not None:
            try:
                if await self.pi_client.available():
                    prompt = self.intention_agent.build_prompt(context_messages)
                    import hashlib
                    rid = (f"intent:hash:{hashlib.md5(f'{self.user_id}|{self.session_id}|{user_input}'.encode('utf-8')).hexdigest()}")
                    submission_id = await self.pi_client.submit_intent(prompt, rid, self.user_id)
                    if submission_id:
                        text = await self.pi_client.wait_intent(submission_id)
                        if text is not None:
                            from agentscope.message import Msg as _Msg
                            return _Msg(name="IntentionAgent", content=text, role="assistant")
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    "pi-durable intent 路径异常 → 降级本地意图识别: %s", e)
        # 本地路径（与改造前完全一致）
        rc = RESILIENCE_CONFIG
        return await retry_with_backoff(
            lambda: self.intention_agent.reply(context_messages),
            max_retries=rc.get("max_retries", 3),
            base_delay_sec=rc.get("retry_base_delay_sec", 1.0),
            max_delay_sec=rc.get("retry_max_delay_sec", 30.0),
        )

    async def process_query(self, user_input: str):
        """
        处理用户查询（原逻辑保留；仅在入口加熔断检查、对 LLM 调用加重试）
        """
        import time
        start_time = time.time()

        # ---------- 仅新增：熔断检查 ----------
        if self.circuit_breaker:
            try:
                self.circuit_breaker.raise_if_open()
            except CircuitOpenError:
                self.console.print(
                    "\n[bold yellow]⚠ 服务暂时不可用，请稍后再试。[/bold yellow]\n",
                    style="dim"
                )
                return

        # 敏感路由：敏感请求确定性前置路由（不走 LLM 编排）
        from utils.sensitive_router import check_sensitive_request
        sensitive = check_sensitive_request(user_input)
        if sensitive:
            self.console.print(sensitive["response"])
            self.memory_manager.add_message("user", user_input)
            self.memory_manager.add_message("assistant", sensitive["response"])
            return

        rc = RESILIENCE_CONFIG
        max_retries = rc.get("max_retries", 3)

        with self.console.status("思考中...", spinner="dots"):
            from agentscope.message import Msg

            # 本轮追踪上下文（skill 执行 + 意图识别都会记录；turn_id 自动生成，同轮共享）
            self.orchestrator.set_trace_context(self.user_id, self.session_id, user_input)
            import time as _t
            _intent_t0 = _t.monotonic()

            # 1. 获取长期记忆摘要与上下文（原逻辑不变）
            long_term_summary = await self._get_long_term_summary(user_input)
            recent_context = self.memory_manager.short_term.get_recent_context(n_turns=5)
            context_messages = []
            if long_term_summary:
                context_messages.append(Msg(name="system", content=long_term_summary, role="system"))
            for msg in recent_context:
                context_messages.append(Msg(name=msg["role"], content=msg["content"], role=msg["role"]))
            context_messages.append(Msg(name="user", content=user_input, role="user"))

            # 2. 意图识别（M7：durable GenerationTask 优先，降级本地；两路共用 build_prompt）
            intention_result = None
            try:
                intention_result = await self._recognize_intent(context_messages, user_input)
                if self.circuit_breaker:
                    self.circuit_breaker.record_success()
            except CircuitOpenError:
                raise
            except Exception as e:
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure()
                raise
            finally:
                try:
                    self.orchestrator.tracer.record(
                        user_id=self.user_id, session_id=self.session_id,
                        query_digest=user_input, agent_name="intention_recognition",
                        status="success" if intention_result else "exception",
                        duration_ms=int((_t.monotonic() - _intent_t0) * 1000),
                        priority=0, output_data={"intents": "（意图识别）"},
                        turn_id=self.orchestrator._trace_ctx.get("turn_id"))
                except Exception:
                    pass

            # 3. 解析意图识别结果（原逻辑不变：解析失败则友好提示并 return）
            try:
                intention_data = json.loads(intention_result.content)
            except json.JSONDecodeError:
                self.console.print("❌ 无法理解您的需求，请重新描述", style="bold red")
                return

        # 4. 添加用户输入到短期记忆（原逻辑不变）
        self.memory_manager.add_message("user", user_input)

        # 5. 按服务配置选择 durable 或本地执行
        result_data = None
        if self.pi_client is not None:
            from utils.durable_turn import run_durable_turn, DurableGateResult
            from utils.pi_durable_client import DurableSubmitRejected, DurableTurnTimeout, PiDurableUnavailable
            import hashlib
            _rid = f"hash:{hashlib.md5(f'{self.user_id}|{self.session_id}|{user_input}'.encode('utf-8')).hexdigest()}"
            try:
                durable = await run_durable_turn(
                    self.pi_client, self.orchestrator, intention_data,
                    user_id=self.user_id, session_id=self.session_id,
                    request_id=_rid,
                    turn_id=self.orchestrator._trace_ctx.get("turn_id", ""),
                )
            except DurableGateResult as gate:
                result_data = gate.gate
                durable = None
            except (DurableSubmitRejected, DurableTurnTimeout, PiDurableUnavailable) as e:
                self.console.print(f"\n[bold yellow]⚠ {e}[/bold yellow]\n")
                return
            if durable is not None:
                result_data = self.orchestrator.finalize_turn(
                    intention_data, durable["results"], fast_path=durable["fast_path"])
                if self.circuit_breaker:
                    self.circuit_breaker.record_success()

            elif result_data is None:
                result_data = {"status": "no_agents", "message": "没有需要调度的智能体"}
        else:
            orchestration_result = None
            try:
                orchestration_result = await retry_with_backoff(
                    lambda: self.orchestrator.reply(intention_result),
                    max_retries=max_retries,
                    base_delay_sec=rc.get("retry_base_delay_sec", 1.0),
                    max_delay_sec=rc.get("retry_max_delay_sec", 30.0),
                )
                if self.circuit_breaker:
                    self.circuit_breaker.record_success()
            except CircuitOpenError:
                raise
            except Exception as e:
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure()
                raise

            # 6. 解析执行结果（原逻辑不变）
            try:
                result_data = json.loads(orchestration_result.content)
            except json.JSONDecodeError:
                result_data = {"error": "解析结果失败"}

        # 7. 显示调用的智能体与最终结果（原逻辑不变）
        self._display_agents_called(result_data)
        self.console.print()
        self._display_results(result_data)
        self.memory_manager.add_message("assistant", json.dumps(result_data, ensure_ascii=False))

    def _display_agents_called(self, result_data: dict):
        """显示调用的智能体列表"""
        results = result_data.get("results", [])
        if not results:
            return

        # 收集所有调用的智能体
        agents_called = []
        for result in results:
            agent_name = result.get("agent_name", "")
            status = result.get("status", "")

            display_name = self._get_agent_display_name(agent_name)

            # 根据状态添加标记
            if status == "success":
                agents_called.append(f"{display_name} ✓")
            elif status == "error":
                agents_called.append(f"{display_name} ✗")
            else:
                agents_called.append(f"{display_name} ?")

        if agents_called:
            self.console.print()
            self.console.print(f"🤖 调用智能体: {', '.join(agents_called)}", style="dim")

    def _display_results(self, result_data: dict):
        """显示执行结果 - 确保永远有回复"""
        self.console.print()

        # 获取结果列表
        results = result_data.get("results", [])

        if not results:
            # 情况0: 前置条件门控——缺关键要素，追问用户
            if result_data.get("status") == "needs_input":
                self.console.print("❓ [bold yellow]需要补充信息[/bold yellow]")
                self.console.print(result_data.get("message", ""))
                on_hold = result_data.get("agents_on_hold", [])
                if on_hold:
                    names = [self._get_agent_display_name(a) for a in on_hold]
                    self.console.print(f"\n[dim]已准备好的规划：{'、'.join(names)}（等你的信息）[/dim]")
                self.console.print()
                return
            # 情况1: 没有任何智能体被调用
            status = result_data.get("status", "unknown")
            if status == "no_agents":
                self.console.print("✓ 好的，我已记录下来。", style="green")
                self.console.print("\n💡 您可以继续补充信息，或者尝试：", style="dim")
                self.console.print("  • 规划行程：「帮我规划去北京的行程」", style="dim")
                self.console.print("  • 查询信息：「北京的天气怎么样」", style="dim")
                self.console.print("  • 问问题：「差旅标准是多少」", style="dim")
            else:
                self.console.print("未能获取有效结果，请重新描述您的需求。", style="yellow")
        else:
            # 情况2: 有智能体被调用，生成人性化回复
            has_output = self._generate_human_response(results)

            # 情况3: 智能体执行了但没有显示内容（兜底）
            if not has_output:
                self.console.print("✓ 已处理您的请求。", style="green")

        self.console.print()

    async def _get_long_term_summary(self, user_input: str = "") -> str:
        """
        生成长期记忆摘要，用于传递给IntentionAgent
        使用LLM总结历史聊天记录 + 结构化偏好

        Args:
            user_input: 用户输入，用于筛选相关历史行程

        Returns:
            格式化的长期记忆摘要
        """
        summary_parts = []

        # 1. 用户偏好信息（始终加载）
        prefs = self.memory_manager.long_term.get_preference()
        if prefs:
            pref_lines = ["【用户背景信息】（来自长期记忆，可用于推断缺失信息）"]

            # 遍历所有偏好，全部加载
            for pref_key, pref_value in prefs.items():
                if pref_value:  # 只添加有值的偏好
                    # 如果是列表，用逗号连接
                    if isinstance(pref_value, list):
                        pref_lines.append(f"• {pref_key}: {', '.join(pref_value)}")
                    else:
                        pref_lines.append(f"• {pref_key}: {pref_value}")

            # 只有在有具体偏好内容时才添加
            if len(pref_lines) > 1:
                summary_parts.extend(pref_lines)

        # 2. 使用LLM总结历史聊天记录
        chat_summary = await self.memory_manager.get_long_term_summary_async(max_messages=50)
        if chat_summary:
            summary_parts.append("\n【历史会话总结】")
            summary_parts.append(chat_summary)

        # 3. 智能筛选相关历史行程
        all_trips = self.memory_manager.long_term.get_trip_history(limit=None)
        if all_trips:
            # 筛选相关的行程（地点匹配）
            relevant_trips = []
            other_trips = []

            for trip in all_trips:
                origin = trip.get("origin", "") or ""
                destination = trip.get("destination", "") or ""

                # 如果用户输入提到了这个行程的地点，标记为相关
                if (origin and origin in user_input) or (destination and destination in user_input):
                    relevant_trips.append(trip)
                else:
                    other_trips.append(trip)

            # 优先显示相关的，再补充最近的
            trips_to_show = relevant_trips[:2] + other_trips[:1]  # 2条相关 + 1条最近

            if trips_to_show:
                summary_parts.append("\n【历史行程】")
                for i, trip in enumerate(trips_to_show[:3], 1):
                    origin = trip.get("origin", "未知")
                    destination = trip.get("destination", "未知")
                    start_date = trip.get("start_date", "")
                    purpose = trip.get("purpose", "")

                    # 标记相关性
                    relevance_mark = "✦ " if trip in relevant_trips else ""
                    summary_parts.append(
                        f"{i}. {relevance_mark}{origin} → {destination} ({start_date}) - {purpose}"
                    )

        return "\n".join(summary_parts) if summary_parts else ""

    def _generate_human_response(self, results: list) -> bool:
        """
        根据结果生成人性化的回复
        """
        has_output = False

        for result in results:
            agent_name = result.get("agent_name", "")
            status = result.get("status", "")
            data = result.get("data", {})
            current_agent_shown = False  # 标记当前Agent是否有内容展示

            # 处理失败的智能体
            if status == "error":
                error_msg = data.get("error", "未知错误")
                agent_display_name = self._get_agent_display_name(agent_name)
                self.console.print(f"❌ {agent_display_name}执行失败: {error_msg}", style="red")
                has_output = True
                continue

            # 只处理成功的智能体 (RAG 的 no_knowledge 视为一种特殊的成功/提示)
            if status != "success" and not (agent_name == "rag_knowledge" and status == "no_knowledge"):
                continue

            # --- 特定 Agent 处理 ---

            # 行程规划
            if agent_name == "itinerary_planning":
                itinerary = data.get("itinerary")
                # 增强：支持从 data.data.itinerary 获取
                if not itinerary and "data" in data and isinstance(data["data"], dict):
                    itinerary = data["data"].get("itinerary")
                # 兜底：agent 走了 answer 路径（原始 JSON 文本），二次解析渲染
                if not itinerary and isinstance(data.get("answer"), str) and data["answer"].lstrip().startswith("{"):
                    try:
                        re_parsed = json.loads(data["answer"].lstrip().strip('`').removeprefix("json"))
                        itinerary = re_parsed.get("itinerary")
                        if itinerary:
                            data = re_parsed
                    except json.JSONDecodeError:
                        pass
                
                if itinerary:
                    title = itinerary.get('title', '行程规划')
                    self.console.print(f"\n✈️  [bold cyan]{title}[/bold cyan]")
                    self.console.print(f"时长: {itinerary.get('duration', '未知')}\n")

                    # 每日行程
                    for day_plan in itinerary.get("daily_plans", []):
                        day_num = day_plan.get("day", 1)
                        self.console.print(f"[bold yellow]第 {day_num} 天[/bold yellow]")

                        # 兼容 activities 和 time_slots
                        activities = day_plan.get("activities") or day_plan.get("time_slots") or []
                        for slot in activities:
                            time = slot.get("time", "")
                            # 兼容 activity 和 location
                            activity = slot.get("activity") or slot.get("location") or ""
                            description = slot.get("description", "")
                            transport = slot.get("transport", "")

                            self.console.print(f"  {time} - {activity}")
                            if description:
                                self.console.print(f"    {description}", style="dim")
                            if transport:
                                self.console.print(f"    🚇 {transport}", style="dim")

                        # 餐食建议
                        meals = day_plan.get("meals", {})
                        if meals:
                            self.console.print()
                            if meals.get("lunch"):
                                self.console.print(f"  🍜 {meals['lunch']}", style="dim")
                            if meals.get("dinner"):
                                self.console.print(f"  🍽️  {meals['dinner']}", style="dim")
                        self.console.print()

                    # 注意事项
                    notes = list(itinerary.get("notes") or []) + list(itinerary.get("tips") or [])
                    if notes:
                        self.console.print("[bold]📌 注意事项[/bold]")
                        for note in notes:
                            self.console.print(f"  • {note}")
                    # 质检结论
                    evaluation = data.get("evaluation")
                    if evaluation:
                        icons = {"passed": "✅", "warning": "⚠️", "failed": "❌"}
                        labels = {"passed": "质检通过", "warning": "质检通过（有提示）", "failed": "质检未通过"}
                        v = evaluation.get("verdict", "warning")
                        self.console.print(f"  {icons.get(v, '•')} [bold]{labels.get(v, v)}[/bold] [dim]（天数/必去/预算/时间/偏好 五项检查）[/dim]")
                        for c in evaluation.get("checks", []):
                            if not c.get("passed"):
                                self.console.print(f"      ✗ {c.get('name')}: {c.get('detail')}")
                    for w in data.get("evaluation_warnings", []):
                        self.console.print(f"  {w}", style="bold yellow")
                    current_agent_shown = True

            # 预算标准
            elif agent_name == "budget_check":
                if data.get("destination"):
                    self.console.print(f"💰 [bold green]{data['destination']}（{data.get('city_tier', '')}）差旅标准[/bold green]")
                    if data.get("accommodation_cap_per_night"):
                        total = data.get("accommodation_total_cap")
                        nights = data.get("nights")
                        self.console.print(f"  • 住宿: [cyan]≤{data['accommodation_cap_per_night']}元/晚[/cyan]"
                                           + (f"（{nights}晚 ≤{total}元）" if total else "（未给天数）"))
                    if data.get("meal_cap_per_day"):
                        self.console.print(f"  • 餐饮: [cyan]≤{data['meal_cap_per_day']}元/天[/cyan]"
                                           + (f"（{data.get('days')}天 ≤{data.get('meal_total_cap')}元）" if data.get('meal_total_cap') else ""))
                    if data.get("estimated_total_cap"):
                        self.console.print(f"  • 住宿+餐饮合计上限: [bold cyan]{data['estimated_total_cap']}元[/bold cyan]（交通另计，实报实销）")
                    for note in data.get("notes", []):
                        self.console.print(f"  • [dim]{note}[/dim]")
                    current_agent_shown = True

            # 费用预估
            elif agent_name == "expense_estimate":
                est = data.get("estimate", {})
                cap = data.get("budget_cap", {})
                if est:
                    self.console.print("📊 [bold green]费用预估[/bold green]")
                    def _rng(r):
                        v = r.get("range") if isinstance(r, dict) else None
                        return f"{v[0]}-{v[1]}元" if v and len(v) == 2 else "无法估算"
                    if est.get("accommodation"): self.console.print(f"  • 住宿: {_rng(est['accommodation'])}（标准 ≤{cap.get('accommodation')}）")
                    if est.get("meals"): self.console.print(f"  • 餐饮: {_rng(est['meals'])}（标准 ≤{cap.get('meals')}）")
                    if est.get("intercity_transport"): self.console.print(f"  • 城际交通: {_rng(est['intercity_transport'])}")
                    if est.get("local_transport"): self.console.print(f"  • 市内交通: {_rng(est['local_transport'])}")
                    if est.get("total"):
                        self.console.print(f"  • [bold]预估合计: {_rng(est['total'])}[/bold]"
                                           + (f"（公司住宿+餐饮上限 {cap.get('total')}元）" if cap.get("total") else ""))
                    if data.get("over_budget"):
                        self.console.print("  ⚠️ [bold yellow]预估超出公司住宿+餐饮标准（交通实报实销不在此列）[/bold yellow]")
                    for w in data.get("warnings", [])[:3]:
                        self.console.print(f"  ⚠️ {w}")
                    for s in data.get("suggestions", [])[:3]:
                        self.console.print(f"  💡 {s}")
                    current_agent_shown = True

            # 价格对比 / 方案对比
            elif agent_name in ("compare_fares", "trip_compare"):
                opts = data.get("options") or data.get("plans") or []
                if opts:
                    title = "🔀 价格对比" if agent_name == "compare_fares" else "⚖️ 行程方案对比"
                    self.console.print(f"{title}")
                    def _as_list(v):
                        if isinstance(v, list):
                            return v
                        if isinstance(v, str):
                            # 逗号/顿号分隔的字符串拆成要点
                            import re as _re
                            return [x for x in _re.split(r"[，、；;]", v) if x.strip()]
                        return []

                    for o in opts:
                        price = o.get("price_range") or o.get("estimated_cost")
                        price_str = f"{price[0]}-{price[1]}元" if price and isinstance(price, list) else (f"{price}元" if price else "")
                        self.console.print(f"  • [cyan]{o.get('name', '?')}[/cyan] {price_str}")
                        for p in _as_list(o.get("pros"))[:2]:
                            self.console.print(f"      ✓ {p}")
                        for c in _as_list(o.get("cons"))[:2]:
                            self.console.print(f"      ✗ {c}")
                    if data.get("recommendation"):
                        self.console.print(f"  👉 [bold green]推荐: {data['recommendation']}[/bold green] [dim]{data.get('recommendation_reason', '')}[/dim]")
                    current_agent_shown = True

            # 冲突检测
            elif agent_name == "conflict_check":
                result = data.get("result")
                if result:
                    icon = {"no_conflict": "✅", "warning": "⚠️", "conflict": "❌"}.get(result, "•")
                    label = {"no_conflict": "无冲突", "warning": "有衔接风险", "conflict": "存在冲突"}.get(result, result)
                    checked = data.get("checked_trip") or {}
                    dest = checked.get("destination", "")
                    dates = f"{checked.get('start_date', '')} ~ {checked.get('end_date', '')}" if checked.get("start_date") else ""
                    self.console.print(f"{icon} 行程冲突检测: {dest} {dates} → [bold]{label}[/bold]")
                    for c in data.get("conflicts", []):
                        self.console.print(f"  ❌ [{c.get('severity', '')}]{c.get('description', '')}")
                    for w in data.get("warnings", []):
                        self.console.print(f"  ⚠️ {w.get('description', w if isinstance(w, str) else '')}")
                    current_agent_shown = True

            # 出行天气建议
            elif agent_name == "weather_alert":
                if data.get("destination"):
                    self.console.print(f"🌦️ [bold green]{data['destination']}出行建议[/bold green] [dim]（风险等级: {data.get('overall_risk', '未知')}）[/dim]")
                    if data.get("weather_summary"):
                        self.console.print(f"  天气: {data['weather_summary']}")
                    cat_icon = {"packing": "🎒", "transport": "🚄", "schedule": "📅", "health": "💊"}
                    cat_name = {"packing": "行装", "transport": "交通", "schedule": "行程", "health": "健康"}
                    for a in data.get("alerts", []):
                        c = a.get("category", "other")
                        self.console.print(f"  {cat_icon.get(c, '•')} [bold]{cat_name.get(c, c)}[/bold]: {a.get('advice', '')}")
                    current_agent_shown = True

            # 偏好管理
            elif agent_name == "preference":
                raw_prefs = data.get("preferences")
                # 增强：支持从 data.data.preferences 获取
                if not raw_prefs and "data" in data and isinstance(data["data"], dict):
                    raw_prefs = data["data"].get("preferences")

                if isinstance(raw_prefs, dict):
                    prefs_list = raw_prefs.get("preferences", [])
                else:
                    prefs_list = raw_prefs if isinstance(raw_prefs, list) else []

                if prefs_list:
                    self.console.print("✓ [bold green]已更新您的偏好设置[/bold green]")
                    type_names = {
                        "home_location": "常驻地",
                        "transportation_preference": "交通偏好",
                        "hotel_brands": "酒店偏好",
                        "airlines": "航空公司偏好",
                        "seat_preference": "座位偏好",
                        "meal_preference": "餐食偏好",
                        "budget_level": "预算等级"
                    }
                    for pref in prefs_list:
                        pref_type = pref.get("type", "")
                        pref_value = pref.get("value", "")
                        action = pref.get("action", "replace")
                        display_type = type_names.get(pref_type, pref_type)
                        action_text = "追加" if action == "append" else "设置为"
                        self.console.print(f"  • {display_type} {action_text} [cyan]{pref_value}[/cyan]")
                    current_agent_shown = True
                    has_itinerary = any(r.get("agent_name") == "itinerary_planning" for r in results)
                    if not has_itinerary:
                        self.console.print("\n💡 下次规划行程时会参考这些偏好。", style="dim")
                else:
                    # 检查是否有错误信息
                    err = data.get("error", "")
                    if err:
                        self.console.print(f"偏好未保存: {err}", style="yellow")
                        current_agent_shown = True
                    # 如果只是没提取到，可能就是没偏好，不强求显示，交给兜底逻辑

            # 事项收集已并入意图识别（key_entities），无独立 agent；此处保留 itinerary_planning 的缺信息提示逻辑

            # 信息查询
            elif agent_name == "information_query":
                query_results = data.get("results")
                if not query_results and "data" in data and isinstance(data["data"], dict):
                    query_results = data["data"].get("results")
                if not query_results:
                    query_results = data # 兜底：data 本身就是 results

                if not isinstance(query_results, dict):
                    query_results = {}

                summary = query_results.get("summary", "")
                sources = query_results.get("sources", []) or []
                message = query_results.get("message", "")
                error = query_results.get("error", "")

                if summary:
                    self.console.print(f"\n{summary}")
                    current_agent_shown = True
                elif message:
                    self.console.print(f"\n{message}", style="dim")
                    current_agent_shown = True
                elif error:
                    self.console.print(f"\n{error}", style="yellow")
                    current_agent_shown = True

                if sources:
                    self.console.print("\n[bold]参考来源[/bold]")
                    for i, source in enumerate(sources[:3], 1):
                        url = source.get("url", "") if isinstance(source, dict) else str(source)
                        self.console.print(f"  {i}. {url}", style="dim")
                    current_agent_shown = True

            # RAG知识库查询
            elif agent_name == "rag_knowledge":
                answer = data.get("answer")
                if not answer and "data" in data and isinstance(data["data"], dict):
                    answer = data["data"].get("answer")
                
                # 增强：也查找 content
                if not answer:
                    answer = data.get("content") or data.get("data", {}).get("content")

                # 深度清洗
                if isinstance(answer, dict):
                    answer = answer.get("answer", str(answer))
                
                if isinstance(answer, str) and answer.strip().startswith("{") and answer.strip().endswith("}"):
                    try:
                        import json
                        json_obj = json.loads(answer)
                        if isinstance(json_obj, dict) and "answer" in json_obj:
                            answer = json_obj["answer"]
                    except:
                        pass

                if answer:
                    self.console.print(f"\n{answer}")
                    current_agent_shown = True

            # 记忆查询
            elif agent_name == "itinerary_lookup":
                trips = data.get("trips", [])
                if data.get("total_trips") is not None:
                    self.console.print(f"📍 [bold green]差旅历史（共 {data.get('total_trips', 0)} 次）[/bold green]")
                    for t in trips[:8]:
                        dates = f"{t.get('start_date', '')}" + (f" ~ {t.get('end_date', '')}" if t.get('end_date') else "")
                        self.console.print(f"  • {t.get('origin', '?')} → [cyan]{t.get('destination', '?')}[/cyan] {dates} {t.get('purpose', '')}")
                    freq = data.get("visit_frequency", {})
                    if freq:
                        freq_str = "、".join(f"{k}×{v}" for k, v in list(freq.items())[:5])
                        self.console.print(f"  📊 到访频次: {freq_str}")
                    top = data.get("top_destinations", [])
                    if top:
                        self.console.print(f"  🏆 最常去: {'、'.join(top[:3])}")
                    current_agent_shown = True

            elif agent_name == "memory_query":
                query_result = data.get("answer") or data.get("result") or data.get("content")
                if not query_result and "data" in data and isinstance(data["data"], dict):
                    inner = data["data"]
                    query_result = inner.get("answer") or inner.get("result") or inner.get("content")

                if query_result:
                    self.console.print(f"\n{query_result}")
                    current_agent_shown = True

            # --- 通用兜底 (如果特定逻辑未生效) ---
            if not current_agent_shown:
                # 尝试查找通用字段
                common_keys = ["answer", "content", "result", "message", "summary", "text", "description"]
                fallback_content = ""
                
                # 扁平查找
                for k in common_keys:
                    if k in data and isinstance(data[k], str) and data[k].strip():
                        fallback_content = data[k]
                        break
                
                # 嵌套查找 data.data
                if not fallback_content and "data" in data and isinstance(data["data"], dict):
                    for k in common_keys:
                        if k in data["data"] and isinstance(data["data"][k], str) and data["data"][k].strip():
                            fallback_content = data["data"][k]
                            break

                if fallback_content:
                    self.console.print(f"\n{fallback_content}")
                    current_agent_shown = True
                else:
                    # 实在啥也没有，打印个成功标记，避免完全静默
                    agent_display_name = self._get_agent_display_name(agent_name)
                    self.console.print(f"✓ {agent_display_name}已完成", style="green")
                    current_agent_shown = True

            if current_agent_shown:
                has_output = True

        return has_output

    def _get_agent_display_name(self, agent_name: str) -> str:
        """获取智能体的显示名称"""
        agent_display_names = {
            "budget_check": "预算标准",
            "compare_fares": "价格对比",
            "itinerary_lookup": "行程结构化查询",
            "weather_alert": "出行天气建议",
            "expense_estimate": "费用预估",
            "trip_compare": "方案对比",
            "conflict_check": "行程冲突检测",
            "preference": "偏好管理",
            "itinerary_planning": "行程规划",
            "information_query": "信息查询",
            "rag_knowledge": "知识库查询",
            "memory_query": "记忆查询",
        }
        return agent_display_names.get(agent_name, agent_name)

    def show_status(self):
        """显示当前状态"""
        # 记忆统计
        full_context = self.memory_manager.get_full_context()
        short_term_stats = full_context["short_term"]["statistics"]
        long_term_stats = full_context["long_term"]["statistics"]

        memory_table = Table(title="记忆状态", show_header=True, header_style="bold magenta")
        memory_table.add_column("类型", style="cyan")
        memory_table.add_column("状态", style="white")

        memory_table.add_row(
            "短期记忆",
            f"{short_term_stats['total_messages']} 条消息"
        )
        memory_table.add_row(
            "长期记忆",
            f"{long_term_stats['total_trips']} 次行程"
        )
        memory_table.add_row(
            "已加载智能体",
            f"{len(self._agent_cache)} 个"
        )

        self.console.print(memory_table)
        self.console.print()

        # 历史对话
        recent_messages = self.memory_manager.short_term.get_recent_context(n_turns=5)
        if recent_messages:
            dialogue_table = Table(title="最近对话 (最多5轮)", show_header=True, header_style="bold cyan")
            dialogue_table.add_column("角色", style="cyan", width=8)
            dialogue_table.add_column("内容", style="white", width=60)
            dialogue_table.add_column("时间", style="dim", width=12)

            for msg in recent_messages:
                role_name = "👤 用户" if msg["role"] == "user" else "🤖 助手"
                content = msg["content"]

                # 截断过长的内容
                if len(content) > 100:
                    content = content[:100] + "..."

                # 格式化时间
                timestamp = msg.get("timestamp", "")
                if timestamp:
                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(timestamp)
                        time_str = dt.strftime("%H:%M:%S")
                    except:
                        time_str = ""
                else:
                    time_str = ""

                dialogue_table.add_row(role_name, content, time_str)

            self.console.print(dialogue_table)
            self.console.print()

    async def run_health_check(self):
        """在会话内执行健康检查并显示熔断器状态"""
        if self.circuit_breaker:
            status = self.circuit_breaker.get_status()
            self.console.print(f"[bold]熔断器[/bold]: {status['state']}", style="cyan")
        ok, msg = await check_llm_health(
            base_url=LLM_CONFIG["base_url"],
            api_key=LLM_CONFIG["api_key"],
            model_name=LLM_CONFIG["model_name"],
            timeout_sec=RESILIENCE_CONFIG.get("health_check_timeout_sec", 10.0),
        )
        if ok:
            self.console.print("LLM 服务: [green]正常[/green]", style="bold")
        else:
            self.console.print(f"LLM 服务: [red]不可用[/red] - {msg}", style="bold")
        self.console.print()

    def show_history(self):
        """显示历史行程"""
        history = self.memory_manager.long_term.get_trip_history(10)

        if not history:
            self.console.print("暂无历史行程", style="yellow")
            return

        table = Table(title="历史行程", show_header=True, header_style="bold magenta")
        table.add_column("ID", style="cyan")
        table.add_column("出发地", style="white")
        table.add_column("目的地", style="white")
        table.add_column("日期", style="white")
        table.add_column("目的", style="white")

        for trip in history:
            table.add_row(
                trip.get("trip_id", ""),
                trip.get("origin", ""),
                trip.get("destination", ""),
                trip.get("start_date", ""),
                trip.get("purpose", "")
            )

        self.console.print(table)

    def show_preferences(self):
        """显示用户偏好"""
        prefs = self.memory_manager.long_term.get_preference()

        table = Table(title="用户偏好", show_header=True, header_style="bold magenta")
        table.add_column("类型", style="cyan")
        table.add_column("值", style="white")

        for key, value in prefs.items():
            if value:
                table.add_row(key, str(value))

        self.console.print(table)

    async def run(self):
        """运行 CLI"""
        # 打印横幅
        self.print_banner()

        # 初始化系统
        await self.initialize_system()

        # 主循环
        while True:
            try:
                # 获取用户输入
                user_input = Prompt.ask("\n[cyan]>[/cyan]")

                if not user_input.strip():
                    continue

                # 处理命令
                command = user_input.strip().lower()

                if command == "exit":
                    self.memory_manager.end_session()
                    self.console.print("再见！", style="cyan")
                    break
                elif command == "help":
                    self.print_help()
                elif command == "status":
                    self.show_status()
                elif command == "health":
                    await self.run_health_check()
                elif command == "clear":
                    self.memory_manager.short_term.clear()
                    self.console.print("✓ 已清空短期记忆", style="green")
                elif command == "history":
                    self.show_history()
                elif command == "preferences":
                    self.show_preferences()
                else:
                    # 处理自然语言查询
                    await self.process_query(user_input)

            except KeyboardInterrupt:
                self.console.print("\n使用 'exit' 退出", style="dim")
            except CircuitOpenError:
                self.console.print("\n[bold yellow]⚠ 服务暂时不可用，请稍后再试。[/bold yellow]", style="dim")
            except Exception as e:
                self.console.print(f"\n错误: {e}", style="red")


def run_health_check_standalone() -> int:
    """
    独立执行健康检查（用于 `python cli.py health`）。
    不进入交互式 CLI，只检测 LLM 是否可达。
    Returns:
        0 成功，1 失败（便于脚本/监控）
    """
    import asyncio
    init_agentscope()
    ok, msg = asyncio.run(check_llm_health(
        base_url=LLM_CONFIG["base_url"],
        api_key=LLM_CONFIG["api_key"],
        model_name=LLM_CONFIG["model_name"],
        timeout_sec=RESILIENCE_CONFIG.get("health_check_timeout_sec", 10.0),
    ))
    if ok:
        print("OK")
        return 0
    print(f"FAIL: {msg}")
    return 1


def main():
    """主函数"""
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() == "health":
        exit(run_health_check_standalone())
    cli = ShanglvCLI()
    asyncio.run(cli.run())


if __name__ == "__main__":
    main()
