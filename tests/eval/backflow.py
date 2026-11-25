"""
日志回流管线：把评测过程的结构化结果转成可复用数据资产，并做分级标记。

流程（规则先行，语义复用 Judge 结果——不重复调 LLM）：
  1. 落日志：每 case 全字段转一行 JSONL（query/路由/回答/耗时/各层判定/rubric 结果）
  2. 规则筛选（确定性）：
     - 执行异常 / 错误占位（请求失败/执行失败）/ 路由不符 / 内容验收未过 → REWRITE
  3. 语义分级（复用 rubric 结果）：
     - rubric 有 fail → REWRITE（reason 带 fail 准则）
     - rubric 有 unverifiable 且无 fail → REVIEW（证据不足，需人工复核）
     - 全部通过 → KEEP

三级标签：KEEP（可用）/ REVIEW（待人工复核）/ REWRITE（需修正后重跑）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

LABELS = ["KEEP", "REVIEW", "REWRITE"]

_ERROR_KEYWORDS = ("请求失败", "执行失败")


def label_case(rec: Dict) -> Dict:
    """单条回流记录的分级（rec 为评测侧收集的 case 全字段 dict）。

    返回 {label, label_reason}——label ∈ KEEP / REVIEW / REWRITE。"""
    answer = rec.get("answer", "")
    error = rec.get("error")

    # ---- 规则层（确定性，命中即定标签）----
    if error or any(k in answer for k in _ERROR_KEYWORDS):
        return {"label": "REWRITE", "label_reason": f"执行异常或错误占位（{str(error or answer)[:80]}）"}

    if not rec.get("route_ok", False):
        return {"label": "REWRITE",
                "label_reason": f"路由不符：期望 {rec.get('expect')} 实际 {rec.get('routed')}"}

    if not rec.get("content_ok", False):
        return {"label": "REWRITE", "label_reason": "内容验收未过（关键词缺失）"}

    # ---- 语义层（复用 rubric，不再调 LLM）----
    rubric = rec.get("rubric") or {}
    criteria = rubric.get("criteria") or []
    fails = [c for c in criteria if c.get("status") == "fail"]
    unverif = [c for c in criteria if c.get("status") == "unverifiable"]

    if fails:
        names = ", ".join(c["id"] for c in fails)
        first_reason = fails[0].get("reason", "")
        return {"label": "REWRITE", "label_reason": f"rubric fail：{names}（{first_reason[:80]}）"}

    if unverif:
        return {"label": "REVIEW",
                "label_reason": f"rubric 不可判定 {len(unverif)} 条（证据不足需人工复核）"}

    return {"label": "KEEP", "label_reason": "路由/内容/全部 rubric 通过"}


def build_backflow_records(results: List[Dict]) -> List[Dict]:
    """把评测的 case 结果列表转成回流记录（含分级标签）。"""
    records = []
    for r in results:
        rec = {
            "case_id": r.get("id"),
            "turn_type": "single",
            "query": r.get("query"),
            "answer": (r.get("answer") or "")[:4000],
            "intent": r.get("intent"),
            "expect": r.get("expect"),
            "routed": r.get("routed"),
            "route_ok": r.get("route_ok"),
            "content_ok": r.get("content_ok"),
            "passed": r.get("passed"),
            "difficulty": r.get("difficulty"),
            "duration_s": r.get("duration_s"),
            "error": r.get("error"),
            "rubric": r.get("rubric"),
        }
        rec.update(label_case(rec))
        records.append(rec)
    return records


def write_backflow(records: List[Dict], out_path: Path) -> Dict[str, int]:
    """落盘 JSONL + 返回标签统计。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    stats = {label: 0 for label in LABELS}
    for rec in records:
        stats[rec["label"]] = stats.get(rec["label"], 0) + 1
    logger.info(f"backflow written: {out_path} ({len(records)} rows)")
    return stats
