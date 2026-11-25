"""
输出契约归一化

收敛关键流转数据的归一化：LLM 输出的字段名漂移、形态错误（嵌套列表/标量变数组/
字符串 null）在这里统一兜底为合法形态。参照 wenshu harness 的 from_dict 模式。

背景：此类 bug 已发生三次（偏好嵌套列表、replace 标量被包成数组、行程原始 JSON 外漏），
散落的 or data.get(...) 兜底修不过来——契约层一次性管住。
"""
from typing import Any, Dict, List, Optional

# 通常为标量的偏好类型（出现数组形态时降为首个元素）
_SCALAR_PREF_TYPES = {"home_location", "seat_preference", "budget_level",
                      "meal_preference", "room_preference"}
# 通常为列表的偏好类型（出现标量时升为单元素列表）
_LIST_PREF_TYPES = {"hotel_brands", "airlines"}


def _flatten(v: Any) -> List[str]:
    """展平嵌套列表为字符串列表（LLM 偶发输出 [['a','b']] 形态）"""
    out: List[str] = []
    for i in (v if isinstance(v, list) else [v]):
        if isinstance(i, list):
            out.extend(str(j) for j in i)
        elif i is not None:
            out.append(str(i))
    return out


def normalize_preferences(raw: Any) -> List[Dict[str, Any]]:
    """归一化 preference skill 的输出（编排器落库前调用）。

    输入形态宽容度：
    - {"preferences": [...]} / 直接 [...]
    - 每项 {type, value, action}，action 缺省 replace
    - value 形态修正：标量型偏好收到数组→降首个；列表型收到标量→升列表
    """
    if isinstance(raw, dict):
        raw = raw.get("preferences", [])
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ptype = str(item.get("type") or "").strip()
        if not ptype:
            continue
        value = item.get("value")
        action = item.get("action") or "replace"
        # 空值跳过
        if value is None or (isinstance(value, str) and (not value.strip() or value.strip().lower() == "null")):
            continue
        if isinstance(value, list):
            if not any(v for v in _flatten(value)):
                continue
        # 形态修正
        if ptype in _SCALAR_PREF_TYPES and isinstance(value, list):
            flat = _flatten(value)
            value = flat[0] if flat else None
        elif ptype in _LIST_PREF_TYPES:
            value = _flatten(value) if isinstance(value, list) else [str(value)]
        if value is None:
            continue
        out.append({"type": ptype, "value": value,
                    "action": action if action in ("append", "replace") else "replace"})
    return out


def normalize_itinerary_result(raw: Any) -> Optional[Dict[str, Any]]:
    """归一化 plan-trip 的输出：剥掉 LLM 偶发的 stage 等外层标记，
    返回含合法 itinerary 的 dict（或 None 表示无行程）。"""
    if not isinstance(raw, dict):
        return None
    d = dict(raw)
    d.pop("stage", None)  # 三段式流程标记，不进下游
    it = d.get("itinerary")
    if not isinstance(it, dict):
        return d
    # daily_plans 每日活动的 time 字段合法性兜底
    plans = it.get("daily_plans")
    if isinstance(plans, list):
        for p in plans:
            if isinstance(p, dict) and not isinstance(p.get("activities"), list):
                p["activities"] = []
    return d


def normalize_budget_result(raw: Any) -> Optional[Dict[str, Any]]:
    """归一化 budget_check 输出：数字字段转 int/None（LLM 偶发输出字符串数字）。"""
    if not isinstance(raw, dict):
        return None
    d = dict(raw)
    for k in ("accommodation_cap_per_night", "nights", "accommodation_total_cap",
              "meal_cap_per_day", "days", "meal_total_cap", "estimated_total_cap"):
        v = d.get(k)
        if isinstance(v, str):
            vs = v.strip().replace("元", "").replace(",", "")
            try:
                d[k] = int(float(vs)) if vs else None
            except ValueError:
                d[k] = None
    return d


def normalize_intention(raw: Any) -> Dict[str, Any]:
    """归一化意图识别输出（编排器入口调用）：必备字段兜底 + 布尔字段强转。"""
    if not isinstance(raw, dict):
        raw = {}
    d = dict(raw)
    d.setdefault("reasoning", "")
    if not isinstance(d.get("intents"), list):
        d["intents"] = []
    if not isinstance(d.get("key_entities"), dict):
        d["key_entities"] = {}
    # 字符串 null 统一为 None（key_entities 值域清洗）
    ke = {}
    for k, v in d["key_entities"].items():
        if isinstance(v, str) and v.strip().lower() == "null":
            ke[k] = None
        else:
            ke[k] = v
    d["key_entities"] = ke
    if not isinstance(d.get("rewritten_query"), str):
        d["rewritten_query"] = ""
    if not isinstance(d.get("missing_info"), list):
        d["missing_info"] = []
    d["complexity"] = d.get("complexity") if d.get("complexity") in ("simple", "standard") else "standard"
    d["replan"] = bool(d.get("replan", False))
    if not isinstance(d.get("agent_schedule"), list):
        d["agent_schedule"] = []
    return d
