"""高德官方 MCP 客户端。每次业务操作独占连接，退出时释放会话与 HTTP 连接。"""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import math
import logging
import re
from urllib.parse import urlencode


class MapToolError(Exception):
    pass


def load_map_config():
    import config
    return dict(getattr(config, "AMAP_MCP_CONFIG", {}))


class _HideMapKey(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(r"([?&]key=)[^&\s\"']+", r"\1[redacted]", record.getMessage())
        record.args = ()
        return True


for _logger_name in ("httpx", "mcp.client.streamable_http"):
    logging.getLogger(_logger_name).addFilter(_HideMapKey())


def coordinates(location):
    """高德字符串坐标采用 经度,纬度。"""
    try:
        lng, lat = map(float, location.split(","))
        if math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90:
            return lng, lat
    except (AttributeError, TypeError, ValueError):
        pass
    raise MapToolError("地图返回了无效坐标")


class AmapMCP:
    ALLOWED_TOOLS = {"maps_text_search", "maps_search_detail", "maps_around_search", "maps_direction_driving", "maps_direction_walking"}

    def __init__(self, session, tools, timeout_sec):
        self.session = session
        self.tools = tools
        self.timeout_sec = timeout_sec

    @classmethod
    @asynccontextmanager
    async def connect(cls, config):
        if not config.get("enabled") or not config.get("api_key"):
            raise MapToolError("高德 MCP 未配置")
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        timeout = float(config.get("timeout_sec", 12))
        url = "https://mcp.amap.com/mcp?" + urlencode({"key": config["api_key"]})
        try:
            async with streamablehttp_client(url, timeout=timeout, sse_read_timeout=timeout) as (reader, writer, _):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                    async with asyncio.timeout(timeout):
                        await session.initialize()
                        tools = {}
                        cursor = None
                        for _ in range(8):
                            page = await session.list_tools(cursor=cursor)
                            tools.update({tool.name: tool.inputSchema for tool in page.tools})
                            cursor = page.nextCursor
                            if not cursor:
                                break
                    yield cls(session, tools, timeout)
        except Exception:
            # HTTP 异常可能含带密钥的 URL，不向调用方转发原异常。
            raise MapToolError("高德 MCP 连接或调用失败，请检查密钥、额度和网络") from None

    async def call(self, name, arguments):
        if name not in self.ALLOWED_TOOLS or name not in self.tools:
            raise MapToolError("地图 MCP 缺少所需工具")
        properties = self.tools[name].get("properties", {})
        arguments = {key: value for key, value in arguments.items() if key in properties}
        if any(key not in arguments for key in self.tools[name].get("required", [])):
            raise MapToolError("地图工具参数与当前服务不兼容")
        try:
            async with asyncio.timeout(self.timeout_sec):
                result = await self.session.call_tool(name, arguments=arguments)
            if result.isError:
                raise MapToolError("地图工具执行失败")
            data = getattr(result, "structuredContent", None)
            if data is None:
                text = "\n".join(block.text for block in result.content if getattr(block, "type", None) == "text")
                data = json.loads(text)
            if not isinstance(data, dict):
                raise MapToolError("地图工具返回格式异常")
            if str(data.get("status", "1")) == "0":
                raise MapToolError("地图服务拒绝请求")
            return data
        except (ValueError, TypeError, TimeoutError):
            raise MapToolError("地图工具返回无效数据或超时") from None

    async def search_places(self, city, keywords):
        if not isinstance(city, str) or not city.strip() or not isinstance(keywords, str) or not keywords.strip():
            raise MapToolError("地点查询需要城市和地点名称")
        data = await self.call("maps_text_search", {"keywords": keywords[:120], "city": city[:64], "citylimit": True})
        places = []
        candidates = [poi for poi in (data.get("pois") or []) if isinstance(poi, dict)]
        exact = [poi for poi in candidates if poi.get("name") == keywords]
        for poi in (exact or candidates)[:5]:
            if not isinstance(poi, dict):
                continue
            # 官方搜索工具部分版本只返回 ID 和名称，坐标需通过地点详情获取。
            if poi.get("id") and (not poi.get("location") or not (poi.get("cityname") or poi.get("city"))):
                detail = await self.call("maps_search_detail", {"id": poi["id"]})
                poi = {**poi, **detail}
            region = str(poi.get("cityname") or poi.get("city") or poi.get("pname") or "")
            if not region or city.removesuffix("市") not in region:
                continue
            try:
                lng, lat = coordinates(poi.get("location"))
            except MapToolError:
                continue
            places.append({"id": poi.get("id"), "name": poi.get("name", ""),
                           "address": poi.get("address", ""), "city": region,
                           "location": f"{lng},{lat}", "lng": lng, "lat": lat,
                           "coordinate_system": "GCJ-02"})
        return places

    async def resolve_place(self, city, name):
        places = await self.search_places(city, name)
        exact = [poi for poi in places if str(poi["name"]).replace(" ", "") == name.replace(" ", "")]
        matches = exact or places
        if len(matches) != 1:
            raise MapToolError("地点无结果或存在同名候选，请提供更具体的地点名称")
        return matches[0]

    async def nearby(self, location, keywords="餐厅", radius=1500):
        coordinates(location)
        data = await self.call("maps_around_search", {
            "location": location, "keywords": keywords[:80], "radius": str(min(max(int(radius), 100), 3000)),
        })
        return [{key: poi.get(key) for key in ("id", "name", "address", "location", "distance")}
                for poi in (data.get("pois") or [])[:5] if isinstance(poi, dict)]

    async def route(self, origin, destination, mode="driving"):
        if mode not in ("driving", "walking"):
            raise MapToolError("目前支持驾车和步行路线")
        coordinates(origin)
        coordinates(destination)
        data = await self.call(f"maps_direction_{mode}", {"origin": origin, "destination": destination})
        route = data.get("route", data.get("data", data))
        paths = route.get("paths") or []
        if not paths or not isinstance(paths[0], dict):
            raise MapToolError("地图未返回可用路线")
        path = paths[0]
        try:
            seconds = float(path.get("duration", (path.get("cost") or {}).get("duration")))
            distance = float(path["distance"])
            if not math.isfinite(seconds) or not math.isfinite(distance) or seconds <= 0 or distance < 0:
                raise ValueError
        except (TypeError, ValueError, KeyError):
            raise MapToolError("路线缺少有效耗时或距离") from None
        return {"duration_min": math.ceil(seconds / 60), "distance_m": distance,
                "mode": mode, "source": "amap_mcp"}
