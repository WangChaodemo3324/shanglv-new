"""
行程规划智能体（三段式：LLM 提取 → 求解器排程 → LLM 叙述）

与纯 llm_skill 不同点：中间插入了确定性求解步骤，故保留独立 agent.py。
- 第①段：把 SKILL.md 正文（extract 段指令）作为 prompt 调 LLM，得到 constraints + activities
- 第②段：solver.solve_itinerary() 做时间窗/预算/必去项求解（无 LLM，毫秒级）
- 第③段：把求解结果拼回 prompt 再调 LLM，叙述成最终行程 JSON

求解器不可用时自动退化为单段式（SKILL.md 的 narrate 规则作为纯 LLM 提示），行为不劣于重构前。
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from typing import Optional, Union, List, Dict
import json
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from utils.skill_runners import _parse_input, _call_llm_text, _parse_llm_json, _reply
from utils.skill_loader import SkillLoader
from utils.evaluator import evaluate_itinerary
from utils.amap_mcp import load_map_config
from utils.map_planning import enrich_activities, bind_schedule

logger = logging.getLogger(__name__)

# 同目录的求解器
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from solver import solve_itinerary, SOLVER_AVAILABLE


class ItineraryPlanningAgent(AgentBase):
    """行程规划智能体（三段式）"""

    def __init__(self, name: str = "ItineraryPlanningAgent", model=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model
        self.skill_loader = SkillLoader()

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        data = _parse_input(x)
        context = data.get("context", {})
        user_query = context.get("rewritten_query", "") or str(data.get("rewritten_query", ""))
        ke = context.get("key_entities", {}) or {}
        prefs = context.get("user_preferences", {}) or {}
        prev = data.get("previous_results", []) or []

        instruction = self.skill_loader.get_skill_content("plan-trip") or "请根据用户需求生成行程规划。"

        from datetime import datetime
        current_date = datetime.now().strftime("%Y年%m月%d日")

        # SKILL.md 正文占位符在此替换（与 llm_runner 同一套语义）——
        # 截取段落前先填充，避免 {{current_date}} 等字面量混进 prompt
        filled = (instruction
                  .replace("{{current_date}}", current_date)
                  .replace("{{user_query}}", user_query)
                  .replace("{{key_entities}}", json.dumps(ke, ensure_ascii=False))
                  .replace("{{user_preferences}}", json.dumps(prefs, ensure_ascii=False))
                  .replace("{{previous_results}}", json.dumps(prev, ensure_ascii=False)[:3000]))
        # 取各段指令（用章节标题「## 第①段」定位，避免正文开头提到段名时误截）
        extract_part = self._section(filled, "## 第①段", "## 第③段")
        narrate_part = self._section(filled, "## 第③段", None)

        # ---------- 求解器不可用：退化为单段式 ----------
        if not SOLVER_AVAILABLE:
            logger.warning("Solver unavailable, falling back to single-stage LLM planning")
            prompt = filled
            text = await _call_llm_text(self.model, [{"role": "user", "content": prompt}])
            result = _parse_llm_json(text)
            return _reply(self.name, result if result else {"status": "success", "answer": text})

        # ---------- 第①段：提取约束与活动池 ----------
        extract_prompt = extract_part + "\n\n请直接输出第①段要求的 JSON（extract）。"
        try:
            text1 = await _call_llm_text(self.model, [{"role": "user", "content": extract_prompt}])
            extract = _parse_llm_json(text1)
        except Exception as e:
            logger.error(f"Extract stage failed: {e}")
            return _reply(self.name, {"status": "error", "message": f"约束提取失败: {e}"})

        if not extract or "activities" not in extract:
            # 提取失败：退化为单段式
            prompt = filled
            text = await _call_llm_text(self.model, [{"role": "user", "content": prompt}])
            result = _parse_llm_json(text)
            return _reply(self.name, result if result else {"status": "success", "answer": text})

        constraints = extract.get("constraints", {}) or {}
        activities = extract.get("activities", []) or []
        # 行程要素兜底：LLM 忘了 days 时用 key_entities 的
        if not constraints.get("days") and ke.get("duration_days"):
            constraints["days"] = int(ke["duration_days"])

        if not isinstance(activities, list) or not all(isinstance(a, dict) and a.get("name") for a in activities):
            return _reply(self.name, {"status": "error", "message": "候选活动格式不正确，请重试"})
        # 路线矩阵只接受地图查询结果，模型不能自行声明某条路线已被验证。
        constraints.pop("travel_times_min", None)
        activities, matrix, map_evidence = await enrich_activities(
            ke.get("destination"), activities, load_map_config())
        constraints["travel_times_min"] = matrix

        # ---------- 第②段：确定性求解（毫秒级，无 LLM） ----------
        solution = solve_itinerary(constraints, activities)
        logger.info(f"Solver: {solution.get('status')} | {solution.get('message')}")

        if solution.get("status") == "infeasible":
            return _reply(self.name, {
                "status": "success",
                "query": user_query,
                "missing_info": ["duration_days"] if not ke.get("duration_days") else [],
                "itinerary": None,
                "solver_summary": solution.get("message", "约束无解"),
                "note": "行程无法在给定约束下排出，请补充天数或减少必去项后重试",
                "map_evidence": map_evidence,
            })

        # ---------- 第③段：叙述 ----------
        narrate_prompt = (narrate_part +
                          f"\n【地图参考】\n{json.dumps(map_evidence, ensure_ascii=False)}" +
                          f"\n\n【求解结果】（排程已经过时间冲突与预算校验，安排不可改动）\n"
                          f"{json.dumps(solution, ensure_ascii=False, indent=1)}\n\n"
                          f"请按第③段要求输出最终行程 JSON。")
        try:
            text3 = await _call_llm_text(self.model, [{"role": "user", "content": narrate_prompt}])
            result = _parse_llm_json(text3)
        except Exception as e:
            logger.error(f"Narrate stage failed: {e}")
            return _reply(self.name, {"status": "error", "message": f"行程叙述失败: {e}"})

        # 已求解的结构由代码回填，避免模型叙述改变路线约束和地图坐标。
        result = bind_schedule(result, solution, map_evidence)

        # ---------- 执行后评估（确定性规则，毫秒级）----------
        # 必去项从 extract 阶段的活动池里取 user_marked 项
        must_visit = [a.get("name") for a in activities if a.get("user_marked")]
        eval_result = self._evaluate(result, ke, prefs, constraints.get("budget_cny"), must_visit)

        # 失败 → 带着具体缺陷原因重生成一次（只重试一次，避免循环）
        if eval_result.get("verdict") == "failed":
            issues_text = "\n".join(f"- {i}" for i in eval_result.get("issues", []))
            logger.info(f"Evaluation failed, regenerating once: {issues_text}")
            retry_prompt = (narrate_part +
                            f"\n\n【求解结果】（排程已经过时间冲突与预算校验，安排不可改动）\n"
                            f"{json.dumps(solution, ensure_ascii=False, indent=1)}\n\n"
                            f"【上次输出未通过质检，以下问题必须修正】\n{issues_text}\n\n"
                            f"请按第③段要求重新输出修正后的行程 JSON。")
            try:
                text3b = await _call_llm_text(self.model, [{"role": "user", "content": retry_prompt}])
                retry_result = _parse_llm_json(text3b)
            except Exception as e:
                logger.warning(f"Regeneration failed: {e}")
                retry_result = None
            if retry_result and "itinerary" in retry_result:
                retry_result = bind_schedule(retry_result, solution, map_evidence)
                eval_result = self._evaluate(retry_result, ke, prefs,
                                             constraints.get("budget_cny"), must_visit)
                if eval_result.get("verdict") != "failed":
                    result = retry_result   # 重生成通过，采用
                else:
                    # 仍失败：采用重试版但给用户标注未满足项
                    result = retry_result
                    result["evaluation_warnings"] = [
                        f"⚠️ 未满足：{i}" for i in eval_result.get("issues", [])]

        # 输出契约归一化（剥 stage 标记 / activities 形态兜底）
        from utils.contracts import normalize_itinerary_result
        result = normalize_itinerary_result(result) or {"status": "success", "answer": text3}

        # 附上评估结论（展示与下游使用）
        if isinstance(result, dict):
            result.setdefault("evaluation", {
                "verdict": eval_result.get("verdict"),
                "checks": eval_result.get("checks"),
            })
            if solution.get("status") == "solved" and "solver_summary" not in result:
                result.setdefault("solver_summary",
                                  f"求解器：{solution.get('message', '')}，门票合计 ¥{solution.get('total_cost', 0)}")
        return _reply(self.name, result)

    def _evaluate(self, result: Dict, ke: Dict, prefs: Dict,
                  budget, must_visit) -> Dict:
        """对叙述结果的 itinerary 跑确定性评估；无行程时视为 failed"""
        itinerary = (result or {}).get("itinerary")
        if not itinerary:
            return {"verdict": "failed", "checks": [],
                    "issues": ["输出中没有 itinerary 结构"], "warnings": [], "total_cost": 0}
        try:
            return evaluate_itinerary(itinerary, ke or {}, prefs or {}, budget, must_visit)
        except Exception as e:
            logger.warning(f"Evaluation error: {e}")
            return {"verdict": "warning", "checks": [], "issues": [],
                    "warnings": [f"评估器异常：{e}"], "total_cost": 0}

    @staticmethod
    def _section(text: str, start_marker: str, end_marker: Optional[str]) -> str:
        """截取 SKILL.md 中两个标记之间的段落"""
        try:
            i = text.index(start_marker)
        except ValueError:
            return text
        if end_marker is None:
            return text[i:]
        try:
            j = text.index(end_marker, i + len(start_marker))
            return text[i:j]
        except ValueError:
            return text[i:]
