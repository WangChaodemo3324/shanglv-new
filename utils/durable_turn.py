"""durable 分支执行体（server / cli 共用）。

流程：plan_batches（复用 dispatcher 的归一化/排序/replan/门控/分批）
  → 构造 pi-durable payload（batches + context + replan 复用结果 + skill_meta）
  → submit（明确拒绝或无法确认时抛异常）
  → wait（超时/不可用抛异常，上层 503 提示重发接续）
  → 返回 results 列表（供 dispatcher.finalize_turn 聚合/记忆更新）。

门控命中（needs_input）在 Python 侧完成——不进 durable（durable 只承载已确认要素的调度执行）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from utils.pi_durable_client import PiDurableClient

logger = logging.getLogger(__name__)


class DurableGateResult(Exception):
    """门控命中：携带 needs_input 结构，由调用方按本地 gate 语义返回追问。"""

    def __init__(self, gate: Dict[str, Any]):
        super().__init__("gate")
        self.gate = gate


async def run_durable_turn(
    client: PiDurableClient,
    orchestrator,
    intention_data: Dict[str, Any],
    user_id: str,
    session_id: str,
    request_id: str,
    turn_id: str,
) -> Optional[Dict[str, Any]]:
    """执行一轮 durable 编排。

    返回：
      {"results": [...], "fast_path": bool}   成功
      None                                    空调度
    抛出：
      DurableGateResult                       门控命中（needs_input）
      DurableSubmitRejected                   提交被明确拒绝
      DurableTurnTimeout / PiDurableUnavailable   上层 503 提示重发
    """
    # 1. 复用 dispatcher 的编排前置规划（归一化/排序/replan/门控/分批——单一事实源，不复制逻辑）
    plan = orchestrator.plan_batches(intention_data)
    if plan["kind"] == "gate":
        raise DurableGateResult(plan["gate"])
    if plan["kind"] == "empty":
        # 空调度本地路径都没有编排可做，直接返回空结果（与本地 no_agents 语义一致）
        return None
    if plan["kind"] != "batches":
        return None

    batches = plan["batches"]
    reused = plan["reused"]
    context = plan["context"]
    fast_path = plan["fast_path"]

    # 组装 payload（skill_meta 从 dispatcher 的 _skill_meta 提取 replay 声明）
    skill_meta = {}
    for batch in batches:
        for entry in batch:
            name = entry.get("agent_name", "")
            replay = orchestrator._get_replay(name)
            if replay:
                skill_meta[name] = {"replay": replay}

    payload = {
        "request_id": request_id,
        "user_id": user_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "batches": [[{
            "agent_name": t.get("agent_name", ""),
            "priority": t.get("priority", 1),
            "reason": t.get("reason", "") or "",
            "expected_output": t.get("expected_output", "") or "",
        } for t in batch] for batch in batches],
        "context": context,
        "previous_results": reused,
        "fast_path": fast_path,
        "skill_meta": skill_meta,
    }

    # 提交失败会抛异常，等待和确认均使用同一个任务。
    submitted = await client.submit_turn(payload)
    task_id = submitted["task_id"]
    if submitted.get("duplicate"):
        logger.info("pi-durable duplicate submission (task %s) — 接续在途/已完成任务", task_id)

    result = await client.wait_turn(task_id)
    return {
        "results": result.get("results", []),
        "fast_path": fast_path,
    }
