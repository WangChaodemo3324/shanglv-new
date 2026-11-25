"""
行程评估器（执行后评估）

对 plan-trip 输出的行程做确定性规则检查（纯代码，不调 LLM，毫秒级）：
  1. 天数覆盖：用户要求的 duration_days 与实际 daily_plans 数一致
  2. 必去项覆盖：用户点名要去的地方出现在行程里
  3. 预算合规：门票/活动花费合计不超过预算
  4. 时间无重叠：同一天活动两两间隔 ≥ 30 分钟（求解器保证过，复核兜底）
  5. 偏好体现：用户酒店/航司偏好至少被提及（软性，缺失只提示不判失败）

评估结论三档：
  passed   —— 全部硬性检查通过
  warning  —— 硬性通过但软性缺失（偏好未体现等），放行但标注
  failed   —— 任一硬性检查不通过，附具体缺陷列表（供打回重生成或用户提示）
"""
from typing import Dict, Any, List, Optional

# 活动间最小间隔（分钟），与求解器 travel_time_min 默认一致
MIN_GAP_MIN = 30


def evaluate_itinerary(itinerary: Dict[str, Any], key_entities: Dict[str, Any],
                       user_preferences: Optional[Dict] = None,
                       budget_cny: Optional[float] = None,
                       must_visit: Optional[List[str]] = None) -> Dict[str, Any]:
    """评估行程质量。

    Args:
        itinerary: plan-trip 输出的 itinerary 对象（daily_plans[].activities[].time/cost 等）
        key_entities: 意图识别的行程要素（duration_days 等）
        user_preferences: 用户偏好（hotel_brands/airlines 等）
        budget_cny: 活动预算上限（None = 未约束）
        must_visit: 用户点名必去的地方（None 时从 key_entities 推断不出则跳过该项）

    Returns:
        {verdict: "passed"|"warning"|"failed", checks: [{name, passed, detail}], issues: [...]}
    """
    checks: List[Dict[str, Any]] = []
    issues: List[str] = []
    soft_warnings: List[str] = []

    daily_plans = (itinerary or {}).get("daily_plans", []) or []

    # ---------- 1. 天数覆盖 ----------
    expected_days = key_entities.get("duration_days")
    if expected_days:
        actual_days = len(daily_plans)
        ok = actual_days == int(expected_days)
        checks.append({"name": "天数覆盖", "passed": ok,
                       "detail": f"要求 {expected_days} 天，实际 {actual_days} 天"})
        if not ok:
            issues.append(f"行程天数不符：要求 {expected_days} 天，实际 {actual_days} 天")
    else:
        checks.append({"name": "天数覆盖", "passed": True, "detail": "用户未指定天数，跳过"})

    # ---------- 2. 必去项覆盖 ----------
    # 必去清单来源：显式传入 > key_entities.other 里逗号分隔的「必去」描述（弱推断，跳过）
    targets = [m for m in (must_visit or []) if m and str(m).strip()]
    if targets:
        all_text = _itinerary_text(itinerary)
        missing = [t for t in targets if str(t) not in all_text]
        ok = not missing
        checks.append({"name": "必去项覆盖", "passed": ok,
                       "detail": f"必去 {len(targets)} 项" + (f"，缺失：{'、'.join(map(str, missing))}" if missing else "，全部覆盖")})
        if missing:
            issues.append(f"必去项未纳入行程：{'、'.join(map(str, missing))}")
    else:
        checks.append({"name": "必去项覆盖", "passed": True, "detail": "无显式必去项，跳过"})

    # ---------- 3. 预算合规 ----------
    total_cost = _total_activity_cost(itinerary)
    if budget_cny is not None:
        ok = total_cost <= float(budget_cny)
        checks.append({"name": "预算合规", "passed": ok,
                       "detail": f"活动花费 ¥{total_cost}（上限 ¥{budget_cny}）"})
        if not ok:
            issues.append(f"活动花费 ¥{total_cost} 超出预算 ¥{budget_cny}")
    else:
        checks.append({"name": "预算合规", "passed": True,
                       "detail": f"活动花费 ¥{total_cost}（未设上限）"})

    # ---------- 4. 时间无重叠 ----------
    overlap_detail = _check_overlaps(daily_plans)
    checks.append({"name": "时间无重叠", "passed": not overlap_detail,
                   "detail": "逐日核对通过" if not overlap_detail else "；".join(overlap_detail[:3])})
    if overlap_detail:
        issues.extend(overlap_detail)

    # ---------- 5. 偏好体现（软性） ----------
    if user_preferences:
        all_text = _itinerary_text(itinerary)
        brands = _as_list(user_preferences.get("hotel_brands"))
        airlines = _as_list(user_preferences.get("airlines"))
        missing_prefs = [b for b in brands if str(b) not in all_text]
        if brands and missing_prefs:
            soft_warnings.append(f"酒店偏好 {('、'.join(map(str, missing_prefs)))} 未在行程中体现")
        if airlines and not any(str(a) in all_text for a in airlines):
            soft_warnings.append("航空偏好未在交通建议中体现")
        checks.append({"name": "偏好体现（软性）", "passed": not soft_warnings,
                       "detail": "已体现" if not soft_warnings else "；".join(soft_warnings)})

    # ---------- 结论 ----------
    if issues:
        verdict = "failed"
    elif soft_warnings:
        verdict = "warning"
    else:
        verdict = "passed"

    return {
        "verdict": verdict,
        "checks": checks,
        "issues": issues,
        "warnings": soft_warnings,
        "total_cost": total_cost,
    }


# ---------- 工具函数 ----------

def _itinerary_text(itinerary: Dict[str, Any]) -> str:
    """把行程压成一段纯文本用于包含性检查"""
    try:
        return json_dumps_cn(itinerary)
    except Exception:
        return str(itinerary)


def json_dumps_cn(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def _as_list(v) -> List:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _parse_hhmm(s: str) -> Optional[int]:
    """'09:30' → 570；解析失败返回 None"""
    try:
        h, m = str(s).strip().split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, IndexError):
        return None


def _total_activity_cost(itinerary: Dict[str, Any]) -> float:
    """合计行程里的活动费用（cost 字段；缺失按 0）"""
    total = 0.0
    for plan in (itinerary or {}).get("daily_plans", []) or []:
        for act in plan.get("activities", []) or []:
            c = act.get("cost")
            if isinstance(c, (int, float)):
                total += float(c)
    return round(total, 2)


def _check_overlaps(daily_plans: List[Dict]) -> List[str]:
    """逐日检查活动两两间隔 ≥ MIN_GAP_MIN；返回违规描述列表"""
    problems = []
    for plan in daily_plans:
        day = plan.get("day", "?")
        items = []
        for act in plan.get("activities", []) or []:
            t = str(act.get("time", ""))
            if "-" not in t:
                continue
            s, _, e = t.partition("-")
            s_min, e_min = _parse_hhmm(s), _parse_hhmm(e)
            if s_min is None or e_min is None:
                continue
            route = act.get("travel_from_previous") or {}
            min_gap = route.get("duration_min", MIN_GAP_MIN)
            if not isinstance(min_gap, (int, float)) or min_gap < 0:
                min_gap = MIN_GAP_MIN
            items.append((s_min, e_min, act.get("location", act.get("name", "?")), min_gap))
        items.sort()
        for i in range(len(items) - 1):
            gap = items[i + 1][0] - items[i][1]
            min_gap = items[i + 1][3]
            if gap < min_gap:
                problems.append(
                    f"Day{day}「{items[i][2]}」与「{items[i+1][2]}」间隔 {gap} 分钟（要求 ≥{min_gap}）")
    return problems


# ================================================================
# 通用校验器（确定性规则 + 有界重试）
# ================================================================

def verify_expense_estimate(data: Dict[str, Any]) -> Dict[str, Any]:
    """费用预估的算术复核（纯代码）：
    1. budget_cap.total == accommodation + meals（有值时）
    2. estimate 各分项的 total 区间 ≈ 各分项区间之和（允许 ±10% 容差）
    3. over_budget 与数字一致（estimate.total 下限 > budget_cap.total 时应为 true）"""
    issues = []
    cap = data.get("budget_cap") or {}
    if isinstance(cap, dict):
        a, m, t = cap.get("accommodation"), cap.get("meals"), cap.get("total")
        if all(isinstance(x, (int, float)) for x in (a, m, t)):
            if abs(a + m - t) > 1:
                issues.append(f"预算上限算术不符：住宿 {a} + 餐饮 {m} ≠ 合计 {t}")
    est = data.get("estimate") or {}
    if isinstance(est, dict) and isinstance(est.get("total"), dict):
        lo = hi = 0
        ok_parts = True
        for part in ("accommodation", "meals", "intercity_transport", "local_transport"):
            p = est.get(part)
            if isinstance(p, dict) and isinstance(p.get("range"), list) and len(p["range"]) == 2 \
                    and all(isinstance(x, (int, float)) for x in p["range"]):
                lo += p["range"][0]
                hi += p["range"][1]
            else:
                ok_parts = False
        t_range = est["total"].get("range")
        if ok_parts and isinstance(t_range, list) and len(t_range) == 2 \
                and all(isinstance(x, (int, float)) for x in t_range):
            tol = 0.1
            if abs(lo - t_range[0]) > lo * tol or abs(hi - t_range[1]) > hi * tol:
                issues.append(f"预估合计与分项之和不符：分项和 {lo}-{hi}，合计 {t_range[0]}-{t_range[1]}")
        # over_budget 一致性
        ob = data.get("over_budget")
        cap_total = cap.get("total")
        if isinstance(cap_total, (int, float)) and isinstance(t_range, list) and len(t_range) == 2:
            should = t_range[0] > cap_total
            if ob is True and not should:
                issues.append("标记超支但预估下限未超过预算上限，请复核")
    return {"verdict": "passed" if not issues else "failed", "issues": issues}


def verify_conflict_check(data: Dict[str, Any]) -> Dict[str, Any]:
    """冲突检测的区间复核（纯代码）：
    result=='no_conflict' 时，checked_trip 的时间区间与 trips 里的任一行程都不重叠
    （用行程数据自证，不依赖 LLM 的判断）。"""
    issues = []
    if data.get("result") != "no_conflict":
        return {"verdict": "passed", "issues": issues}
    checked = data.get("checked_trip") or {}
    cs, ce = _to_date(checked.get("start_date")), _to_date(checked.get("end_date"))
    if not cs or not ce:
        return {"verdict": "passed", "issues": issues}  # 无法核对时放行（不误伤）
    # 冲突源：trips（itinerary_lookup 提供的结构化历史）——result=='no_conflict' 时
    # conflicts 必为空，能走到这里说明 LLM 的判断需要用行程数据自证
    for t in (data.get("trips") or []):
        ts, te = _to_date(t.get("start_date")), _to_date(t.get("end_date"))
        if ts and te and cs <= te and ts <= ce:  # 区间相交
            # 未来行程才算冲突（过去的记录不算——与 conflict-check 规则一致）
            from datetime import date
            if te >= date.today():
                issues.append(f"标记无冲突但与行程 {t.get('destination', '?')}（{ts}~{te}）日期重叠")
    return {"verdict": "passed" if not issues else "failed", "issues": issues}


def _to_date(v) -> Optional["object"]:
    """'2027-03-25' → date；解析失败返回 None"""
    from datetime import date
    if not isinstance(v, str):
        return None
    try:
        y, m, d = v.strip()[:10].split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, IndexError):
        return None


async def verified_execute(execute_fn, verify_fn, max_retry: int = 1):
    """通用「执行 → 校验 → 有界打回重试」。

    Args:
        execute_fn: 协程工厂，签名 execute_fn(issues)——issues 为 None 表示首次执行
                    （复用已有输出即可），为缺陷列表表示带原因重跑（调用方据此修 prompt）
        verify_fn:  dict → {"verdict": "passed"/"failed", "issues": [...]}
        max_retry:  校验失败后最多重跑次数（0 = 只校验不重试）

    Returns:
        (最终输出, 校验结论)  # 仍失败时输出带 evaluation_warnings 标注
    """
    result = await execute_fn(None)
    verdict = verify_fn(result or {})
    retries = 0
    while verdict.get("verdict") == "failed" and retries < max_retry:
        retries += 1
        result = await execute_fn(verdict.get("issues", []))
        verdict = verify_fn(result or {})
    if verdict.get("verdict") == "failed":
        if isinstance(result, dict):
            result.setdefault("evaluation_warnings",
                              [f"⚠️ 校验未通过：{i}" for i in verdict.get("issues", [])])
    return result, verdict
