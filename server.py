#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
商旅助手 - FastAPI Web 服务

把 CLI 的「意图识别 → 编排执行」流程暴露为 HTTP API：

  POST /chat                自然语言对话（与 CLI process_query 同一套流程）
  GET  /health              LLM 健康检查（含熔断器状态）
  GET  /users/{uid}/status   记忆全景（偏好/行程/统计）
  GET  /users/{uid}/history  历史行程
  GET  /users/{uid}/prefs    用户偏好

启动：
  venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8000
"""
import json
import logging
import os
import sys
import uuid
import copy
import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from config import LLM_CONFIG, SYSTEM_CONFIG, RESILIENCE_CONFIG
from agentscope.model import OpenAIChatModel
from agentscope.message import Msg
from context.memory_manager import MemoryManager
from context.short_term_memory import ShortTermMemory
from agents.intention_agent import IntentionAgent
from agents.skill_dispatcher import SkillDispatcher
from agents.lazy_agent_registry import LazyAgentRegistry
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.llm_resilience import retry_with_backoff, run_health_check as check_llm_health

logger = logging.getLogger(__name__)


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


class _UserPipeline:
    """单个用户的编排链（memory_manager 会注入 skill runner（need_memory 声明），故按用户隔离）。
    同一用户开启新会话时调用 switch_session()，短期记忆与聊天记录按新 session_id 隔离。"""

    def __init__(self, model, memory_manager):
        self.memory_manager = memory_manager
        self.registry = LazyAgentRegistry(model, {}, memory_manager)
        self.orchestrator = SkillDispatcher(
            name="SkillDispatcher",
            agent_registry=self.registry,
            memory_manager=memory_manager,
        )

    def switch_session(self, session_id: str):
        """切换会话：更新 memory_manager 的 session_id；短期记忆重新绑定新 Redis key"""
        if session_id and session_id != self.memory_manager.session_id:
            self.memory_manager.session_id = session_id
            # ShortTermMemory 的 Redis key 在构造时生成，需重建绑定
            self.memory_manager.short_term = ShortTermMemory(
                max_turns=10, session_id=session_id,
                redis_config=None,  # 读 config.REDIS_CONFIG 默认值
                user_id=self.memory_manager.user_id,
            )
            # 重规划结果缓存按新会话从执行日志恢复（M2）
            self.orchestrator.switch_session(session_id)


class AppState:
    """全局组件（在 lifespan 中初始化，替代 CLI 的 initialize_system）"""

    model = None
    intention_agent = None
    circuit_breaker = None
    idempotency = None
    pi_client = None  # pi-durable 客户端（M6；None = 功能关闭）
    pipelines = {}  # user_id -> _UserPipeline

    def __init__(self):
        self.pipelines = {}
        self.session_pipelines = {}
        self.session_locks = {}


state = AppState()


def build_model() -> OpenAIChatModel:
    if not LLM_CONFIG.get("api_key"):
        logger.warning("Model credentials not configured; history APIs remain available")
        return None
    timeout_sec = SYSTEM_CONFIG.get("timeout", 60)
    return OpenAIChatModel(
        model_name=LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        client_kwargs={
            "base_url": LLM_CONFIG["base_url"],
            "timeout": float(timeout_sec),
        },
        # agentscope 1.0.16 要求 temperature/max_tokens 放在 generate_kwargs
        generate_kwargs={
            "temperature": LLM_CONFIG.get("temperature", 0.7),
            "max_tokens": LLM_CONFIG.get("max_tokens", 16384),
        },
    )


def get_pipeline(user_id: str, session_id: Optional[str] = None) -> _UserPipeline:
    """按 user_id 复用编排链（长期记忆 PG / 短期 Redis 均跨会话持久）"""
    p = state.pipelines.get(user_id)
    if p is None:
        mm = MemoryManager(user_id=user_id, session_id=session_id or uuid.uuid4().hex[:8],
                           llm_model=state.model)
        p = _UserPipeline(state.model, mm)
        # 新建编排链时从执行日志恢复该会话最近一轮结果（M2：重启后续命局部重规划）
        p.orchestrator.restore_last_results(mm.session_id)
        state.pipelines[user_id] = p
    if not session_id:
        return p
    key = (user_id, session_id)
    if key not in state.session_pipelines:
        # Share the user's authoritative long-term store (including JSON data),
        # but never mutate another session's short-term binding or dispatcher.
        mm = copy.copy(p.memory_manager)
        mm.session_id = session_id
        mm.short_term = ShortTermMemory(max_turns=10, session_id=session_id,
                                       user_id=user_id)
        session_pipeline = _UserPipeline(state.model, mm)
        state.session_pipelines[key] = session_pipeline
    return state.session_pipelines[key]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 启动：与 cli.initialize_system 相同的组件装配 ----
    init_agentscope()
    state.model = build_model()
    state.intention_agent = IntentionAgent(name="IntentionAgent", model=state.model)
    rc = RESILIENCE_CONFIG
    state.circuit_breaker = CircuitBreaker(
        failure_threshold=rc.get("circuit_failure_threshold", 5),
        recovery_timeout_sec=rc.get("circuit_recovery_timeout_sec", 60.0),
        half_open_successes=rc.get("circuit_half_open_successes", 2),
    )
    # M1 幂等守卫：Redis 优先（跨重启），降级进程内存
    from utils.idempotency import IdempotencyGuard
    state.idempotency = IdempotencyGuard()
    await state.idempotency.start()
    # 启用时装配 pi-durable 客户端，技能批次始终走该服务。
    from config import PI_DURABLE_CONFIG
    if PI_DURABLE_CONFIG.get("enabled"):
        from utils.pi_durable_client import PiDurableClient
        state.pi_client = PiDurableClient(PI_DURABLE_CONFIG)
        print(f"✓ pi-durable client ready ({PI_DURABLE_CONFIG.get('base_url')})")
    print(f"✓ 商旅助手 API ready (model: {LLM_CONFIG['model_name']})")
    try:
        yield
    finally:
        if state.pi_client is not None:
            try:
                await state.pi_client.close()
            except Exception:
                logger.exception("Failed to close pi-durable client")
        for p in state.pipelines.values():
            try:
                p.memory_manager.long_term.close()
            except Exception:
                logger.exception("Failed to close long-term memory")
        await state.idempotency.close()


app = FastAPI(title="商旅助手 API", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def catch_all_errors(request, call_next):
    """全局异常兜底：真实堆栈落文件（/tmp/shanglv_errors.log），便于定位间歇性 500"""
    try:
        return await call_next(request)
    except Exception as e:
        import traceback, time as _t
        with open("/tmp/shanglv_errors.log", "a") as f:
            f.write(f"\n[{_t.strftime('%m-%d %H:%M:%S')}] {request.method} {request.url.path}\n")
            f.write(traceback.format_exc())
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500, content={"detail": f"服务器内部错误: {str(e)[:200]}"})
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=20000,
                         description="用户自然语言输入")
    user_id: str = Field("default_user", description="用户ID（决定长期记忆隔离）")
    session_id: Optional[str] = Field(None, description="会话ID（不传则新建）")
    request_id: Optional[str] = Field(None, max_length=128,
                                      description="幂等键（前端生成；重复提交返回同一结果）")

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value):
        if not value.strip():
            raise ValueError("message must contain non-whitespace characters")
        return value


class ChatResponse(BaseModel):
    user_id: str
    session_id: str
    intention: Optional[dict] = None
    agents_called: list = []
    display_text: str = ""
    result: Optional[dict] = None
    error: Optional[str] = None


def render_display_text(result_data: dict) -> str:
    """用 CLI 同一套 Rich 渲染逻辑生成纯文本（与 tests/test_cli_qa.py 的捕获方式一致）"""
    from io import StringIO
    from rich.console import Console
    from cli import ShanglvCLI

    capture = StringIO()
    console = Console(file=capture, force_terminal=False, no_color=True, width=100)
    cli = ShanglvCLI()
    cli.console = console
    try:
        cli._display_results(result_data)
    except Exception as e:
        return f"（结果渲染失败：{e}）"
    return capture.getvalue().strip()


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """自然语言对话：意图识别 → 编排执行（与 CLI process_query 同流程）。

    M1 幂等：request_id（缺省用 会话+消息 哈希）相同的重发请求不重跑流水线——
    done 直接回放缓存响应；inflight 等在途请求完成后回放同一结果；失败释放键允许重试。
    """
    from utils.idempotency import IdempotencyUnavailable, make_key
    guard = state.idempotency
    idem_key = make_key(req.user_id, req.session_id or "", req.message, req.request_id)
    try:
        status, value = await guard.begin(idem_key)
        if status == "done":
            return ChatResponse(**json.loads(value))
        if status == "inflight":
            cached = await guard.wait_done(idem_key, timeout_sec=300)
            if cached is not None:
                return ChatResponse(**json.loads(cached))
            raise HTTPException(
                status_code=503, detail="尚未取得原请求的结果，请用原请求 ID 稍后重试",
                headers={"Retry-After": "2"},
            )
    except IdempotencyUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="暂时无法确认幂等状态，请用原请求 ID 稍后重试",
            headers={"Retry-After": "2"},
        ) from exc

    token = value
    heartbeat = asyncio.create_task(guard.keep_alive(idem_key, token))
    try:
        try:
            response = await _chat_execute(req)
        except BaseException:
            try:
                await guard.release(idem_key, token)
            except IdempotencyUnavailable:
                logger.warning("Could not confirm idempotency release; lease will expire")
            raise
        try:
            completed = await guard.complete(idem_key, token, response.model_dump_json())
        except IdempotencyUnavailable as exc:
            raise HTTPException(
                status_code=503, detail="暂时无法确认结果是否已缓存，请用原请求 ID 稍后重试",
                headers={"Retry-After": "2"},
            ) from exc
        if not completed:
            raise HTTPException(
                status_code=503, detail="请求已失去幂等键归属，请用原请求 ID 稍后重试",
                headers={"Retry-After": "2"},
            )
        return response
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


def idem_request_key(req: ChatRequest) -> str:
    """durable request_id：与幂等守卫的 request_id 语义同源——
    显式 request_id 直接用；缺省用 会话+内容哈希（双击/重发自然去重）。"""
    if req.request_id:
        return req.request_id
    import hashlib
    h = hashlib.md5(f"{req.user_id}|{req.session_id or ''}|{req.message}".encode("utf-8")).hexdigest()
    return f"hash:{h}"


async def _chat_execute(req: ChatRequest) -> ChatResponse:
    from utils.metrics import IN_FLIGHT
    # Requests in one session share dialogue/cache state and must be ordered.
    # Different sessions remain concurrent and use separate pipelines.
    key = (req.user_id, req.session_id or "")
    lock = state.session_locks.setdefault(key, asyncio.Lock())
    async with lock:
        IN_FLIGHT.inc()
        try:
            return await _chat_execute_inner(req)
        finally:
            IN_FLIGHT.dec()


async def _chat_execute_inner(req: ChatRequest) -> ChatResponse:
    import time as _mt
    from utils.metrics import QUERY_TOTAL, QUERY_LATENCY
    _mt0 = _mt.monotonic()

    # Safety and complaint routes are deterministic and must remain available
    # when the model is missing or its circuit breaker is open.
    from utils.sensitive_router import check_sensitive_request
    sensitive = check_sensitive_request(req.message)
    if sensitive:
        pipeline = get_pipeline(req.user_id, req.session_id)
        mm = pipeline.memory_manager
        mm.add_message("user", req.message)
        mm.add_message("assistant", sensitive["response"])
        QUERY_TOTAL.labels(intent="escalation").inc()
        QUERY_LATENCY.observe(_mt.monotonic() - _mt0)
        return ChatResponse(
            user_id=req.user_id, session_id=mm.session_id,
            agents_called=[{"agent_name": "escalation", "status": "success", "priority": 0}],
            display_text=sensitive["response"],
            result={"status": "escalated", "category": sensitive["category"]},
        )

    if state.model is None:
        raise HTTPException(status_code=503, detail="模型 API 未配置；历史对话可正常查看，请先配置模型凭据并重启后端")
    try:
        state.circuit_breaker.raise_if_open()
    except CircuitOpenError:
        raise HTTPException(status_code=503, detail="服务暂时不可用（熔断中），请稍后再试")

    pipeline = get_pipeline(req.user_id, req.session_id)
    if req.session_id:
        pipeline.switch_session(req.session_id)
    mm = pipeline.memory_manager

    pipeline.orchestrator.set_trace_context(req.user_id, mm.session_id, req.message,
                                            turn_id=uuid.uuid4().hex)
    import time as _t
    _intent_t0 = _t.monotonic()

    rc = RESILIENCE_CONFIG
    max_retries = rc.get("max_retries", 3)

    # 1. 拼上下文（长期记忆摘要 + 最近对话 + 当前输入），与 CLI 一致
    long_term_summary = await _long_term_summary(mm, req.message)
    recent = mm.short_term.get_recent_context(n_turns=5)
    context_messages = []
    if long_term_summary:
        context_messages.append(Msg(name="system", content=long_term_summary, role="system"))
    for msg in recent:
        context_messages.append(Msg(name=msg["role"], content=msg["content"], role=msg["role"]))
    context_messages.append(Msg(name="user", content=req.message, role="user"))

    # 2. 意图识别（带重试）：M7 起优先走 durable GenerationTask（pi-durable），失败降级本地 IntentionAgent
    intention_result = None  # 先置空：失败路径 finally 里也要能引用（记 exception 追踪）
    try:
        intention_result = await _recognize_intent(state, pipeline, context_messages, req, mm)
        state.circuit_breaker.record_success()
    except CircuitOpenError:
        raise HTTPException(status_code=503, detail="服务暂时不可用（熔断中）")
    except Exception as e:
        state.circuit_breaker.record_failure()
        raise HTTPException(status_code=502, detail=f"意图识别失败: {e}")
    finally:
        try:
            pipeline.orchestrator.tracer.record(
                user_id=req.user_id, session_id=mm.session_id, query_digest=req.message,
                agent_name="intention_recognition",
                status="success" if intention_result else "exception",
                duration_ms=int((_t.monotonic() - _intent_t0) * 1000),
                priority=0, output_data={"intents": "（意图识别）"},
                turn_id=pipeline.orchestrator._trace_ctx.get("turn_id"))
        except Exception:
            pass

    try:
        intention_data = json.loads(intention_result.content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="无法理解您的需求，请重新描述")

    mm.add_message("user", req.message)

    # 3. 编排执行：按服务配置选择 durable 或本地执行。
    result_data = None
    if state.pi_client is not None:
        from utils.durable_turn import run_durable_turn, DurableGateResult
        from utils.pi_durable_client import DurableSubmitRejected, DurableTurnTimeout, PiDurableUnavailable
        try:
            durable = await run_durable_turn(
                state.pi_client, pipeline.orchestrator, intention_data,
                user_id=req.user_id, session_id=mm.session_id,
                request_id=req.request_id or idem_request_key(req),
                turn_id=pipeline.orchestrator._trace_ctx.get("turn_id", ""),
            )
        except DurableGateResult as gate:
            # 门控命中：与本地 needs_input 同语义（不发 LLM 的追问应答）
            result_data = gate.gate
            durable = None
        except DurableSubmitRejected as e:
            status_code = e.status_code if e.status_code in (400, 413, 422) else 502
            raise HTTPException(status_code=status_code, detail=str(e)) from e
        except DurableTurnTimeout as e:
            raise HTTPException(status_code=503, detail=f"{e}")
        except PiDurableUnavailable as e:
            raise HTTPException(status_code=503,
                                detail=f"编排任务处理中断（{e}），稍后可用相同 request_id 重发获取结果")
        if durable is not None:
            result_data = pipeline.orchestrator.finalize_turn(
                intention_data, durable["results"], fast_path=durable["fast_path"])
            state.circuit_breaker.record_success()
        elif result_data is None:
            result_data = {"status": "no_agents", "message": "没有需要调度的智能体"}
    else:
        try:
            orchestration_result = await retry_with_backoff(
                lambda: pipeline.orchestrator.reply(intention_result),
                max_retries=max_retries,
                base_delay_sec=rc.get("retry_base_delay_sec", 1.0),
                max_delay_sec=rc.get("retry_max_delay_sec", 30.0),
            )
            state.circuit_breaker.record_success()
        except CircuitOpenError:
            raise HTTPException(status_code=503, detail="服务暂时不可用（熔断中）")
        except Exception as e:
            state.circuit_breaker.record_failure()
            raise HTTPException(status_code=502, detail=f"编排执行失败: {e}")

        try:
            result_data = json.loads(orchestration_result.content)
        except json.JSONDecodeError:
            result_data = {"error": "解析结果失败"}

    mm.add_message("assistant", json.dumps(result_data, ensure_ascii=False))

    agents_called = [
        {"agent_name": r.get("agent_name"), "status": r.get("status"), "priority": r.get("priority")}
        for r in result_data.get("results", [])
    ]

    # ---- Prometheus 出口埋点 ----
    from utils.metrics import SKILL_TOTAL, FASTPATH_TOTAL
    QUERY_LATENCY.observe(_mt.monotonic() - _mt0)
    for r in result_data.get("results", []):
        SKILL_TOTAL.labels(agent=r.get("agent_name", "unknown"), status=r.get("status", "unknown")).inc()
    if result_data.get("fast_path"):
        FASTPATH_TOTAL.inc()
    intent_type = (intention_data.get("intents") or [{}])[0].get("type", "unknown")
    QUERY_TOTAL.labels(intent=intent_type).inc()

    return ChatResponse(
        user_id=req.user_id,
        session_id=mm.session_id,
        intention={
            "intents": intention_data.get("intents", []),
            "rewritten_query": intention_data.get("rewritten_query", ""),
        },
        agents_called=agents_called,
        display_text=render_display_text(result_data),
        result=result_data,
    )


async def _recognize_intent(state, pipeline, context_messages, req: ChatRequest, mm):
    """M7：意图识别双路——durable GenerationTask 优先，pi-durable 不可用/超时降级本地 IntentionAgent。
    两路共用 build_prompt（prompt 单一事实源）与下游解析（调用方拿到的都是 Msg，协议不变）。
    意图识别幂等（同 prompt 同输出，无副作用），降级安全。"""
    if state.pi_client is not None:
        try:
            if await state.pi_client.available():
                prompt = state.intention_agent.build_prompt(context_messages)
                rid = f"intent:{idem_request_key(req)}"
                submission_id = await state.pi_client.submit_intent(prompt, rid, req.user_id)
                if submission_id:
                    text = await state.pi_client.wait_intent(submission_id)
                    if text is not None:
                        return Msg(name="IntentionAgent", content=text, role="assistant")
                    logger.warning("pi-durable intent 未完成 → 降级本地意图识别")
        except Exception as e:
            logger.warning("pi-durable intent 路径异常 → 降级本地意图识别: %s", e)
    # 本地路径（与改造前完全一致）
    rc = RESILIENCE_CONFIG
    return await retry_with_backoff(
        lambda: state.intention_agent.reply(context_messages),
        max_retries=rc.get("max_retries", 3),
        base_delay_sec=rc.get("retry_base_delay_sec", 1.0),
        max_delay_sec=rc.get("retry_max_delay_sec", 30.0),
    )


async def _long_term_summary(mm: MemoryManager, user_input: str) -> str:
    """与 cli._get_long_term_summary 等价（偏好 + LLM 会话摘要 + 相关行程）"""
    parts = []
    prefs = mm.long_term.get_preference()
    if prefs:
        lines = ["【用户背景信息】（来自长期记忆，可用于推断缺失信息）"]
        for k, v in prefs.items():
            if v:
                lines.append(f"• {k}: {', '.join(v) if isinstance(v, list) else v}")
        if len(lines) > 1:
            parts.extend(lines)
    chat_summary = await mm.get_long_term_summary_async(max_messages=50)
    if chat_summary:
        parts.append("\n【历史会话总结】")
        parts.append(chat_summary)
    all_trips = mm.long_term.get_trip_history(limit=None)
    if all_trips:
        relevant = [t for t in all_trips
                    if (t.get("origin") or "") in user_input or (t.get("destination") or "") in user_input]
        show = relevant[:2] + [t for t in all_trips if t not in relevant][:1]
        if show:
            parts.append("\n【历史行程】")
            for i, t in enumerate(show[:3], 1):
                parts.append(f"{i}. {t.get('origin','未知')} → {t.get('destination','未知')} "
                             f"({t.get('start_date','')}) - {t.get('purpose','')}")
    return "\n".join(parts)


class SkillExecuteRequest(BaseModel):
    """pi-durable → Python 的 skill 执行回调载荷（durable SkillTask 的 input 镜像）。
    agent_name 以路径参数为准，body 内可选（对拍用）。"""
    request_id: str = Field("", max_length=128)
    user_id: str = Field("default_user", max_length=64)
    session_id: str = Field("", max_length=64)
    turn_id: str = Field("", max_length=64)
    agent_name: str = Field("", max_length=64)
    priority: int = 1
    reason: str = Field("", max_length=8000)
    expected_output: str = Field("", max_length=8000)
    context: dict = Field(default_factory=dict)
    previous_results: list = Field(default_factory=list)
    fast_path: bool = False


def _require_skill_execute_token(request: Request):
    """pi-durable→Python 回调鉴权：X-Internal-Token 与 config.skill_execute_token 一致。
    未配置令牌时拒绝（回调端点不裸奔）——与 pi-durable 侧「未配 token 返回 503」同一策略。"""
    import hmac
    from config import PI_DURABLE_CONFIG
    expected = (PI_DURABLE_CONFIG.get("skill_execute_token") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="回调令牌未配置（SKILL_EXECUTE_TOKEN）")
    supplied = request.headers.get("x-internal-token", "")
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="内部服务令牌无效")


class ParseRequest(BaseModel):
    """pi-durable → Python 的 parse 回调载荷（SkillGenTask 的 parse 阶段）"""
    request_id: str = Field("", max_length=128)
    user_id: str = Field("default_user", max_length=64)
    session_id: str = Field("", max_length=64)
    turn_id: str = Field("", max_length=64)
    agent_name: str = Field("", max_length=64)
    text: str = Field(..., max_length=200000)


@app.post("/internal/skills/{agent_name}/execute")
async def internal_skill_execute(agent_name: str, body: SkillExecuteRequest,
                                  request: Request):
    """pi-durable 的 skill 执行回调：复用 _execute_agent（consumes 过滤 /
    执行追踪 / 契约归一化 / 确定性校验带缺陷重试 全部原样生效）。
    返回统一信封 {code, message, data}；skill 层失败也走 code:0（result.status=error），
    传输层错误才走 code:1——pi-durable 按此区分「skill 失败继续流程」与「回调失败重试」。"""
    _require_skill_execute_token(request)
    try:
        pipeline = get_pipeline(body.user_id, body.session_id or None)
        if body.session_id:
            pipeline.switch_session(body.session_id)
        pipeline.orchestrator.set_trace_context(
            body.user_id, body.session_id, body.reason[:200], turn_id=body.turn_id or None)
        # 快速路径标记：trace 的 fast_path 字段与主轮一致
        pipeline.orchestrator._fast_path_used = body.fast_path
        result = await pipeline.orchestrator._execute_agent(
            agent_name=agent_name,
            context=body.context,
            reason=body.reason,
            expected_output=body.expected_output,
            previous_results=body.previous_results,
            task_priority=body.priority,
        )
        return {"code": 0, "message": "ok", "data": {"result": result}}
    except HTTPException:
        raise
    except Exception as e:
        return {"code": 1, "message": f"skill 执行回调失败: {e}"}


@app.post("/internal/skills/{agent_name}/prepare")
async def internal_skill_prepare(agent_name: str, body: SkillExecuteRequest, request: Request):
    """M8 prepare 阶段回调：Python 拼好 prompt（确定性逻辑：占位符替换/记忆取数/consumes 过滤/边界检查）。
    返回 {action:"generate", prompt} 或 {action:"result", result}（短路终态）。
    P0 覆盖 9 个声明式 llm_skill；agent.py 型 skill（ask-question/plan-trip/query-info）
    暂走整段 execute 降级形态（返回带 error 的 result，旧 SkillTask 语义）。"""
    _require_skill_execute_token(request)
    try:
        pipeline = get_pipeline(body.user_id, body.session_id or None)
        if body.session_id:
            pipeline.switch_session(body.session_id)
        pipeline.orchestrator.set_trace_context(
            body.user_id, body.session_id, body.reason[:200], turn_id=body.turn_id or None)
        pipeline.orchestrator._fast_path_used = body.fast_path

        agent = pipeline.orchestrator.agent_registry.get(agent_name)
        if agent is None:
            return {"code": 0, "message": "ok", "data": {"action": "result", "result": {
                "status": "error", "message": f"智能体未注册: {agent_name}"}}}
        # P0：仅声明式 llm_skill（有 prepare_prompt 方法）走 stage 协议
        if not hasattr(agent, "prepare_prompt"):
            return {"code": 0, "message": "ok", "data": {"action": "result", "result": {
                "status": "error",
                "agent_name": agent_name,
                "data": {"error": "agent.py 型 skill 暂走整段 execute 回调"},
                "message": "skill 不支持 prepare 协议（P0 范围外），请回退 execute 端点",
            }}}

        # consumes 过滤（与 _execute_agent 同口径）
        filtered_prev = pipeline.orchestrator._filter_previous_results(agent_name, body.previous_results)
        data = {
            "context": body.context,
            "reason": body.reason,
            "expected_output": body.expected_output,
            "previous_results": filtered_prev,
        }
        prepared = await agent.prepare_prompt(data)
        if isinstance(prepared, dict) and prepared.get("_error"):
            return {"code": 0, "message": "ok", "data": {"action": "result", "result": prepared["result"]}}
        return {"code": 0, "message": "ok", "data": {"action": "generate", "prompt": prepared}}
    except HTTPException:
        raise
    except Exception as e:
        return {"code": 1, "message": f"prepare 回调失败: {e}"}


@app.post("/internal/skills/{agent_name}/parse")
async def internal_skill_parse(agent_name: str, body: ParseRequest, request: Request):
    """M8 parse 阶段回调：Python 解析 LLM 文本（四层 JSON 解析/契约归一化/确定性校验打回）。
    返回 {action:"result", result} 或 {action:"retry", prompt}（校验打回：拼缺陷修正 prompt）。"""
    _require_skill_execute_token(request)
    try:
        pipeline = get_pipeline(body.user_id, body.session_id or None)
        agent = pipeline.orchestrator.agent_registry.get(agent_name)
        if agent is None or not hasattr(agent, "parse_output"):
            return {"code": 0, "message": "ok", "data": {"action": "result", "result": {
                "status": "error", "message": f"智能体未注册或不支持 parse 协议: {agent_name}"}}}

        result = agent.parse_output(body.text)

        # error 字段检查（与 _execute_agent 同口径）
        if isinstance(result, dict) and "error" in result:
            return {"code": 0, "message": "ok", "data": {"action": "result", "result": {
                "status": "error", "agent_name": agent_name, "data": result,
                "message": result.get("error", "未知错误")}}}

        # 契约归一化（budget 数字字段）
        if isinstance(result, dict) and agent_name == "budget_check":
            from utils.contracts import normalize_budget_result
            result = normalize_budget_result(result) or result

        # 确定性校验（expense_estimate/conflict_check）→ 失败返回 retry（拼缺陷修正 prompt）
        if isinstance(result, dict) and agent_name in ("expense_estimate", "conflict_check"):
            from utils.evaluator import (verify_expense_estimate, verify_conflict_check)
            verify_fn = (verify_expense_estimate if agent_name == "expense_estimate"
                         else verify_conflict_check)
            verdict = verify_fn(result)
            if verdict["verdict"] == "failed" and body.request_id:
                # 打回：缺陷拼进原 prompt 重试（pi-durable 会用新 stage 重生成）
                # 原始 prompt 无法从 parse 侧拿到——retry prompt 用「修正指令 + 原始输出」让 LLM 自纠
                retry_prompt = (
                    f"你上次的输出未通过确定性校验，有以下问题必须修正：\n"
                    + "\n".join(f"- {i}" for i in verdict["issues"])
                    + "\n\n请修正以上问题后重新输出完整 JSON（保持原输出格式）。上次的输出：\n"
                    + body.text[:30000]
                )
                return {"code": 0, "message": "ok", "data": {"action": "retry", "prompt": retry_prompt}}

        return {"code": 0, "message": "ok", "data": {"action": "result", "result": {
            "status": "success", "agent_name": agent_name, "data": result}}}
    except HTTPException:
        raise
    except Exception as e:
        return {"code": 1, "message": f"parse 回调失败: {e}"}


@app.get("/metrics")
async def metrics():
    """Prometheus 抓取端点（文本格式指标）"""
    from prometheus_client import generate_latest
    from fastapi.responses import Response
    return Response(content=generate_latest(), media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/health")
async def health():
    """LLM 健康检查 + 熔断器状态（对应 CLI 的 health 命令）"""
    if state.model is None:
        ok, msg = False, "model credentials not configured"
    else:
        ok, msg = await check_llm_health(
            LLM_CONFIG["base_url"], LLM_CONFIG["api_key"], LLM_CONFIG["model_name"],
            timeout_sec=RESILIENCE_CONFIG.get("health_check_timeout_sec", 10.0),
        )
    return {
        "llm": "ok" if ok else f"fail: {msg}",
        "circuit": state.circuit_breaker.state if state.circuit_breaker else "unknown",
        "memory": {
            "short_term": ("redis" if state.pipelines and
                           next(iter(state.pipelines.values())).memory_manager.short_term._redis_available
                           else "unknown"),
        },
    }


@app.get("/users/{user_id}/status")
async def user_status(user_id: str):
    """记忆全景：偏好 / 行程 / 统计（对应 CLI 的 status 命令）"""
    return get_pipeline(user_id).memory_manager.get_full_context()


@app.get("/users/{user_id}/history")
async def user_history(user_id: str, limit: int = 10):
    return {"history": get_pipeline(user_id).memory_manager.long_term.get_trip_history(limit=limit)}


@app.get("/users/{user_id}/trace")
async def user_trace(user_id: str, session_id: str = None, agent_name: str = None, limit: int = 50):
    """执行记录查询：该用户（可按会话/agent 过滤）的 skill 执行明细，时间倒序"""
    tracer = get_pipeline(user_id).orchestrator.tracer
    if tracer is None:
        raise HTTPException(status_code=503, detail="追踪器不可用")
    return tracer.query(user_id=user_id, session_id=session_id,
                        agent_name=agent_name, limit=min(limit, 500))


@app.get("/users/{user_id}/trace/stats")
async def user_trace_stats(user_id: str, hours: int = 24):
    """执行统计：各 skill 调用次数 / 平均耗时 / P95 / 失败率，快速路径命中率"""
    p = get_pipeline(user_id)
    return p.orchestrator.tracer.stats(user_id=user_id, hours=hours)


@app.get("/users/{user_id}/chats")
async def user_chats(user_id: str, session_id: str = None, limit: int = 100):
    """按会话回放聊天记录（长期记忆，PG）。
    assistant 侧存的是编排器聚合 JSON，此处用与实时回复同一套 Rich 渲染为展示文本。"""
    mm = get_pipeline(user_id).memory_manager
    rows = mm.long_term.get_chat_history(limit=limit, session_id=session_id)
    out = []
    for r in rows:
        if r.get("role") not in ("user", "assistant"):
            continue
        content = r.get("content") or ""
        itinerary = None
        if r.get("role") == "assistant" and content.startswith("{"):
            try:
                data = json.loads(content)
                content = render_display_text(data)
                # 附带结构化行程（前端回放时恢复手册卡片）
                for res in data.get("results", []):
                    if res.get("agent_name") == "itinerary_planning":
                        itinerary = (res.get("data") or {}).get("itinerary")
                        break
            except json.JSONDecodeError:
                pass
        item = {"role": r.get("role"), "content": content}
        if itinerary:
            item["itinerary"] = itinerary
            # 确定性 key（会话+行号）：前端回放时用它把数据存进 localStorage，历史卡片链接不失效
            item["dataKey"] = f"{session_id or 'default'}:{len(out)}"
        out.append(item)
    return out


@app.get("/users/{user_id}/sessions")
async def list_sessions(user_id: str):
    """会话列表：从 PG 聚合每个 session 的首条用户消息（做标题）和时间"""
    mm = get_pipeline(user_id).memory_manager
    if not mm.long_term_impl._pg_available:
        grouped = {}
        for row in mm.long_term.get_chat_history():
            sid = row.get("session_id")
            if not sid:
                continue
            item = grouped.setdefault(sid, {"session_id": sid, "title": "新对话",
                "message_count": 0, "started_at": row.get("timestamp"), "last_at": None})
            if row.get("role") == "user" and item["title"] == "新对话":
                item["title"] = str(row.get("content") or "新对话")[:30]
            item["message_count"] += 1
            item["last_at"] = row.get("timestamp")
        return sorted(grouped.values(), key=lambda x: x["last_at"] or "", reverse=True)
    try:
        rows = mm.long_term_impl._pg_execute(
            "SELECT session_id, "
            "  min(created_at) AS started_at, "
            "  max(created_at) AS last_at, "
            "  count(*) AS n_msgs "
            "FROM chat_history WHERE user_id = %s AND session_id IS NOT NULL "
            "GROUP BY session_id ORDER BY max(id) DESC",
            (user_id,), fetch=True)
        sessions = []
        for r in rows or []:
            sid = r["session_id"]
            title_row = mm.long_term_impl._pg_execute(
                "SELECT content FROM chat_history "
                "WHERE user_id = %s AND session_id = %s AND role = 'user' ORDER BY id ASC LIMIT 1",
                (user_id, sid), fetch=True)
            title = title_row[0]["content"][:30] if title_row else "新对话"
            sessions.append({
                "session_id": sid,
                "title": title,
                "message_count": r["n_msgs"],
                "started_at": r["started_at"].isoformat() if r.get("started_at") else None,
                "last_at": r["last_at"].isoformat() if r.get("last_at") else None,
            })
        return sessions
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"会话列表查询失败: {e}")


@app.delete("/users/{user_id}/sessions/{session_id}")
async def delete_session(user_id: str, session_id: str):
    """删除单个会话的聊天记录（PG + Redis 短期记忆）；不动偏好和行程"""
    mm = get_pipeline(user_id).memory_manager
    if not mm.long_term_impl._pg_available:
        store = mm.long_term_impl
        store.data["chat_history"] = [r for r in store.data["chat_history"]
                                      if r.get("session_id") != session_id]
        store._save()
    else:
        r = mm.long_term_impl._pg_execute(
            "DELETE FROM chat_history WHERE user_id = %s AND session_id = %s",
            (user_id, session_id))
        if r is None:
            raise HTTPException(status_code=500, detail="删除失败")
    bound = state.session_pipelines.pop((user_id, session_id), None)
    if bound:
        bound.memory_manager.short_term.clear()
    if mm.session_id == session_id:
        mm.short_term.clear()
    # 同步清理该会话的 Redis 短期记忆（否则 TTL 内残留的上下文可能串回）
    try:
        if mm._redis_client_cache is not None:
            mm._redis_client_cache.delete(f"shanglv:stm:{user_id}:{session_id}")
    except Exception:
        pass  # Redis 不可用时不阻断删除
    return {"deleted": session_id}


@app.get("/users/{user_id}/prefs")
async def user_prefs(user_id: str):
    return {"preferences": get_pipeline(user_id).memory_manager.long_term.get_preference()}


# 根路径说明（正式前端在 web-next，经 3000 端口访问）
@app.get("/", include_in_schema=False)
async def root():
    return {"service": "商旅助手 API", "docs": "/docs", "前端": "web-next（npm run dev，http://localhost:3000）"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
