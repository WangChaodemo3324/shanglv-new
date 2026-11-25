"""
行程排程求解器（plan-trip 内部模块，kind 从 llm_skill 升级为 solver_skill）

三段式流程的中间段：
  ①LLM 提取约束 + 候选活动池 → ②本模块确定性求解 → ③LLM 把解叙述成行程

求解目标（全部为硬约束，不满足即报告无解，不静默放松）：
  - 每天活动时间落在该活动的开放时段内（默认 09:00-21:00）
  - 活动之间不重叠（含往返交通时间）
  - 每天活动数不超过上限（默认 4，控制疲劳度）
  - 必去活动（user_marked=true）必须被安排，否则报告缺项
  - 总门票花费不超过预算（住宿/交通不计入，另按标准）
优化目标：优先安排评分高的活动（rating）。
"""
from typing import Dict, Any, List
import logging

logger = logging.getLogger(__name__)

# 尝试导入求解库；缺失时 plan-trip 自动退回纯 LLM 模式
try:
    from ortools.sat.python import cp_model as _cp
    SOLVER_AVAILABLE = True
except ImportError:
    SOLVER_AVAILABLE = False


def solve_itinerary(constraints: Dict[str, Any], activities: List[Dict[str, Any]]) -> Dict[str, Any]:
    """求解行程排程。

    Args:
        constraints: {
            days: 天数,
            daily_window: [开, 闭]（分钟数，默认 [540, 1260] 即 09:00-21:00）,
            max_activities_per_day: 每天上限（默认 4）,
            budget_cny: 活动预算上限（默认无限制）,
            travel_time_min: 缺少路线时采用的平均交通时间（分钟，默认 30）,
            travel_times_min: 由地图查询产生的有向耗时矩阵，键为 "活动索引:活动索引"
        }
        activities: 每项 {
            name, duration_min（时长）, rating（可选，0-10）,
            open_from/open_to（可选，分钟数）,
            cost（可选，门票花费）,
            user_marked（可选，必去）
        }

    Returns:
        {
            status: "solved" | "infeasible" | "solver_unavailable",
            days: [ {date_index, items: [{name, start, end, cost}]} ],
            scheduled: [活动名], unscheduled: [未安排的活动名]（非必去项装不下时）,
            total_cost: 门票合计,
            message: 说明
        }
    """
    if not SOLVER_AVAILABLE:
        return {"status": "solver_unavailable",
                "message": "求解库未安装（pip install ortools）", "days": [],
                "scheduled": [], "unscheduled": [a.get("name", "?") for a in activities], "total_cost": 0}

    days = int(constraints.get("days") or 0)
    if days <= 0 or not activities:
        return {"status": "infeasible", "message": "缺少天数或活动池为空", "days": [],
                "scheduled": [], "unscheduled": [a.get("name", "?") for a in activities], "total_cost": 0}

    window = constraints.get("daily_window") or [540, 1260]
    w_open, w_close = int(window[0]), int(window[1])
    max_per_day = int(constraints.get("max_activities_per_day") or 4)
    budget = constraints.get("budget_cny")
    travel = int(constraints.get("travel_time_min") or 30)

    n = len(activities)
    model = _cp.CpModel()

    # ---- 变量 ----
    # day[i] ∈ [-1, days)：-1 表示不安排（仅非必去项允许）
    day = []
    for i, a in enumerate(activities):
        if a.get("user_marked"):
            day.append(model.NewIntVar(0, days - 1, f"d{i}"))          # 必去：必须安排
        else:
            day.append(model.NewIntVar(-1, days - 1, f"d{i}"))

    # start[i]：活动开始时刻（分钟）；未安排时取 0（无意义但不参与约束）
    start = []
    for i, a in enumerate(activities):
        dur = max(int(a.get("duration_min") or 120), 15)
        a_open = int(a.get("open_from") or w_open)
        a_close = int(a.get("open_to") or w_close)
        lo = max(a_open, w_open)
        hi = min(a_close, w_close) - dur   # 开放时段与每日窗口的交集，减去时长
        if hi < lo:
            # 时长超过开放窗口：硬约束无法满足，不静默放松——
            # 强制不安排（非必去项进 unscheduled；必去项因 day 域不含 -1 而整体无解，如实报告）
            model.Add(day[i] == -1)
            hi = lo  # 仅保持变量域非空，值不参与任何约束
        start.append(model.NewIntVar(lo, hi, f"s{i}"))
        # 存时长供后续用
        a["_dur"] = dur

    # ---- 约束 ----
    # b[i][d] = 1 表示活动 i 被安排在第 d 天（day[i]==d）；day[i]==-1 时全部为 0
    b = [[model.NewBoolVar(f"b{i}_{d}") for d in range(days)] for i in range(n)]
    for i in range(n):
        for d in range(days):
            model.Add(day[i] == d).OnlyEnforceIf(b[i][d])
            model.Add(day[i] != d).OnlyEnforceIf(b[i][d].Not())

    # 1. 每天活动数上限
    for d in range(days):
        model.Add(sum(b[i][d] for i in range(n)) <= max_per_day)

    # 每天一个经过虚拟起终点 0 的回路。未选活动走自环，
    # 只有真正相邻的活动边承担交通间隔，支持非对称路线耗时。
    matrix = constraints.get("travel_times_min") or {}
    for d in range(days):
        empty = model.NewBoolVar(f"empty{d}")
        arcs = [(0, 0, empty)]
        model.Add(sum(b[i][d] for i in range(n)) == 0).OnlyEnforceIf(empty)
        model.Add(sum(b[i][d] for i in range(n)) >= 1).OnlyEnforceIf(empty.Not())
        for i in range(n):
            arcs.append((i + 1, i + 1, b[i][d].Not()))
            for source, target in ((0, i + 1), (i + 1, 0)):
                edge = model.NewBoolVar(f"edge{d}_{source}_{target}")
                model.AddImplication(edge, b[i][d])
                arcs.append((source, target, edge))
            for j in range(n):
                if i == j:
                    continue
                edge = model.NewBoolVar(f"edge{d}_{i + 1}_{j + 1}")
                model.AddImplication(edge, b[i][d])
                model.AddImplication(edge, b[j][d])
                gap = max(0, int(matrix.get(f"{i}:{j}", travel)))
                model.Add(start[i] + activities[i]["_dur"] + gap <= start[j]).OnlyEnforceIf(edge)
                arcs.append((i + 1, j + 1, edge))
        model.AddCircuit(arcs)

    # 3. 预算（门票合计 ≤ budget）
    if budget is not None:
        cost_expr = []
        for i, a in enumerate(activities):
            c = int(a.get("cost") or 0)
            if c > 0:
                for d in range(days):
                    cost_expr.append((b[i][d], c))
        if cost_expr:
            model.Add(_cp.LinearExpr.weighted_sum(
                [v for v, _ in cost_expr], [w for _, w in cost_expr]) <= int(budget))

    # ---- 目标：最大化总评分（必去项权重×3），让好活动尽量都进来 ----
    obj_terms = []
    for i, a in enumerate(activities):
        r = int(a.get("rating") or 5)
        w = r * (3 if a.get("user_marked") else 1)
        obj_terms.extend((b[i][d], w) for d in range(days))
    model.Maximize(_cp.LinearExpr.weighted_sum(
        [v for v, _ in obj_terms], [w for _, w in obj_terms]))

    solver = _cp.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0
    status = solver.Solve(model)
    if status not in (_cp.OPTIMAL, _cp.FEASIBLE):
        return {"status": "infeasible", "message": "约束无解：请放宽天数/时段/预算或减少必去项",
                "days": [], "scheduled": [], "unscheduled": [a.get("name", "?") for a in activities],
                "total_cost": 0}

    # ---- 提取解 ----
    def _fmt(minute):
        return f"{minute // 60:02d}:{minute % 60:02d}"

    out_days = [{"date_index": d, "items": []} for d in range(days)]
    scheduled, unscheduled = [], []
    total_cost = 0
    for i, a in enumerate(activities):
        d = solver.Value(day[i])
        if d < 0:
            unscheduled.append(a.get("name", "?"))
            continue
        s = solver.Value(start[i])
        c = int(a.get("cost") or 0)
        total_cost += c
        item = {"name": a.get("name", "?"), "start": _fmt(s),
                "end": _fmt(s + a["_dur"]), "cost": c, "activity_index": i}
        # 坐标透传（前端地图路线渲染用；LLM 未提供时为 null）
        for key in ("coordinate_system", "poi_id"):
            if key in a:
                item[key] = a[key]
        if a.get("lat") is not None and a.get("lng") is not None:
            item["lat"] = a.get("lat")
            item["lng"] = a.get("lng")
        out_days[d]["items"].append(item)
        scheduled.append(a.get("name", "?"))
    for dd in out_days:
        dd["items"].sort(key=lambda x: x["start"])
        for previous, current in zip(dd["items"], dd["items"][1:]):
            key = f"{previous['activity_index']}:{current['activity_index']}"
            current["travel_from_previous"] = {
                "duration_min": int(matrix.get(key, travel)),
                "source": "amap_mcp" if key in matrix else "estimated",
            }

    return {"status": "solved", "days": out_days, "scheduled": scheduled,
            "unscheduled": unscheduled, "total_cost": total_cost,
            "message": f"已安排 {len(scheduled)}/{n} 项活动（未安排 {len(unscheduled)} 项）"}
