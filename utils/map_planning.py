"""将地图查询结果送入排程，并将最终时间和坐标绑定到求解结果。"""
import asyncio
import copy
from datetime import datetime, timezone

from utils.amap_mcp import AmapMCP, MapToolError


async def enrich_activities(city, activities, config):
    enriched = copy.deepcopy(activities)
    matrix = {}
    evidence = {"provider": "amap_mcp", "status": "estimated", "routes": [], "places": [],
                "nearby": [], "warnings": [], "queried_at": datetime.now(timezone.utc).isoformat()}
    if not config.get("enabled") or not config.get("api_key"):
        evidence["warnings"].append("高德 MCP 未启用或缺少密钥，交通时间使用估算。")
        return enriched, matrix, evidence
    if not city or len(activities) > int(config.get("max_activities", 10)):
        evidence["warnings"].append("缺少单城市目的地或候选活动超过地图查询上限，交通时间使用估算。")
        return enriched, matrix, evidence

    try:
        async with asyncio.timeout(float(config.get("planning_timeout_sec", 90))):
            async with AmapMCP.connect(config) as maps:
                resolved = {}
                for i, activity in enumerate(enriched):
                    for key in ("lat", "lng", "coordinate_system", "poi_id"):
                        activity.pop(key, None)
                    try:
                        place = await maps.resolve_place(city, activity.get("place_name") or activity["name"])
                        resolved[i] = place
                        activity.update(lat=place["lat"], lng=place["lng"],
                                        coordinate_system="GCJ-02", poi_id=place["id"])
                        evidence["places"].append({"activity_index": i, **place})
                    except MapToolError:
                        evidence["warnings"].append(f"{activity.get('name', '?')}地点未确认，相关交通时间使用估算。")

                # 有向矩阵：驾车往返可能不同。单轮最多 90 次路线查询。
                mode = config.get("transport", "driving")
                for i, origin in resolved.items():
                    for j, destination in resolved.items():
                        if i == j:
                            continue
                        try:
                            route = await maps.route(origin["location"], destination["location"], mode)
                            route["buffer_min"] = int(config.get("buffer_min", 10))
                            matrix[f"{i}:{j}"] = route["duration_min"] + route["buffer_min"]
                            evidence["routes"].append({"from_index": i, "to_index": j, **route})
                        except MapToolError:
                            evidence["warnings"].append(f"{origin['name']}至{destination['name']}路线未确认，使用估算。")
                # 提供就近用餐参考，不把餐厅查询结果当作已验证的营业/预订信息。
                if resolved:
                    first = next(iter(resolved.values()))
                    try:
                        evidence["nearby"] = await maps.nearby(first["location"])
                    except MapToolError:
                        evidence["warnings"].append("周边餐厅查询不可用。")
    except (MapToolError, TimeoutError):
        evidence["warnings"].append("地图连接失败或超出查询时间，缺失路线使用估算。")
    expected = len(activities) * (len(activities) - 1)
    if matrix:
        evidence["status"] = "queried" if len(matrix) == expected else "partial"
    return enriched, matrix, evidence


def bind_schedule(result, solution, evidence):
    """模型负责文案，已求解的活动、时间、坐标和交通信息由代码回填。"""
    result = copy.deepcopy(result) if isinstance(result, dict) else {}
    result["status"] = "success"
    itinerary = result.get("itinerary")
    if not isinstance(itinerary, dict):
        itinerary = {}
        result["itinerary"] = itinerary
    old_days = itinerary.get("daily_plans") or []
    plans = []
    for d, solved_day in enumerate(solution["days"]):
        old_day = old_days[d] if d < len(old_days) and isinstance(old_days[d], dict) else {}
        old_activities = old_day.get("activities") or []
        if not isinstance(old_activities, list):
            old_activities = []
        descriptions = {a.get("location"): a for a in old_activities if isinstance(a, dict)}
        day = {key: value for key, value in old_day.items() if key != "activities"}
        day["day"] = d + 1
        day["activities"] = []
        for item in solved_day["items"]:
            activity = copy.deepcopy(descriptions.get(item["name"], {}))
            for key in ("lat", "lng", "coordinate_system", "poi_id"):
                activity.pop(key, None)
                if key in item:
                    activity[key] = item[key]
            activity.update(location=item["name"], time=f"{item['start']}-{item['end']}", cost=item["cost"])
            if "travel_from_previous" in item:
                route = item["travel_from_previous"]
                prefix = "地图查询" if route["source"] == "amap_mcp" else "估算"
                activity["transport"] = f"{prefix}，含缓冲预留 {route['duration_min']} 分钟"
                activity["travel_from_previous"] = route
            day["activities"].append(activity)
        plans.append(day)
    itinerary["daily_plans"] = plans
    itinerary.setdefault("tips", [])
    if not isinstance(itinerary["tips"], list):
        itinerary["tips"] = [str(itinerary["tips"])]
    itinerary["tips"].extend(evidence["warnings"])
    result["map_evidence"] = evidence
    return result
