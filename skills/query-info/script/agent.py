"""
信息查询智能体 - 真实检索版（免费API）
支持：天气（Open-Meteo）、网络搜索（DDGS，开启 safesearch + 结果过滤）

使用免费API：
- 天气：Open-Meteo（无需 API Key、无证书问题）
- 搜索：ddgs（Dux Distributed Global Search，可选 bing/duckduckgo 等，需安装：pip install ddgs）
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from typing import Optional, Union, List, Dict, Any
import json
import logging
import re
import sys
import os
from utils.amap_mcp import AmapMCP, MapToolError, load_map_config
from utils.skill_runners import _call_llm_text, _parse_llm_json

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

logger = logging.getLogger(__name__)

# 尝试导入 duckduckgo_search (旧包名) 或 ddgs (新包名)
try:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    DDGS_AVAILABLE = True
except ImportError:
    DDGS_AVAILABLE = False
    logger.warning("ddgs not installed. Install with: pip install ddgs")

# 疑似垃圾/低质域名：多为 SEO 或不良站，不展示给用户
_SUSPICIOUS_DOMAIN_PATTERN = re.compile(
    r"\.(cc|tk|ml|ga|cf|gq|xyz|top|work|click|link|pw|buzz)(/|$)",
    re.I
)
# 域名主体若为长随机字母（无明显词），则过滤
_RANDOM_DOMAIN_PATTERN = re.compile(r"^[a-z0-9]{10,}$", re.I)


def _is_suspicious_url(url: str) -> bool:
    """过滤疑似垃圾/不良站点（如部分 .cc/.tk 等易被滥用的域名）。"""
    if not url or not url.startswith("http"):
        return True
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc or ""
        # 去掉端口
        host = host.split(":")[0].lower()
        if not host:
            return True
        # 可疑 TLD
        if _SUSPICIOUS_DOMAIN_PATTERN.search(host):
            return True
        # 主域名部分（最后一个 . 之前若还有多段则取倒数第二段之前）
        parts = host.rsplit(".", 2)
        name = parts[0] if parts else ""
        if len(name) >= 10 and _RANDOM_DOMAIN_PATTERN.match(name):
            return True
        return False
    except Exception:
        return False


class InformationQueryAgent(AgentBase):
    """
    信息查询智能体（真实检索版）

    核心功能：
    - 天气查询 - 使用 Open-Meteo 免费 API（无需搜索，结果可靠）
    - 网络搜索 - 使用 DDGS（开启 safesearch，过滤可疑来源）

    注意：
    - 差旅标准查询由独立的 RAGKnowledgeAgent 处理
    """

    def __init__(self, name: str = "InformationQueryAgent", model=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model
        from utils.skill_loader import SkillLoader
        self.skill_loader = SkillLoader()

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        if x is None:
            return Msg(name=self.name, content=json.dumps({"query_success": False}), role="assistant")

        # 解析输入
        content = x.content if not isinstance(x, list) else x[-1].content

        if isinstance(content, str):
            try:
                data = json.loads(content)
                context = data.get("context", {})
                user_query = context.get("rewritten_query", "") or content
            except json.JSONDecodeError:
                user_query = content
        else:
            user_query = str(content)

        # 天气类问题优先走 Open-Meteo，避免通用搜索返回低质结果
        if self._is_weather_query(user_query):
            logger.info(f"Weather query: {user_query}")
            try:
                result = await self._weather_query(user_query)
                return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
            except Exception as e:
                logger.warning(f"Weather query failed, fallback to web search: {e}")
                result = None
        else:
            result = None

        if result is None and any(word in user_query for word in ("附近", "周边", "怎么走", "路线", "在哪里", "地址", "到达", "开车", "步行", "多久", "多远")):
            config = load_map_config()
            if config.get("enabled") and config.get("api_key"):
                result = await self._map_query(user_query, config)

        if result is None:
            logger.info(f"Web search query: {user_query}")
            try:
                result = await self._web_search(user_query)
            except Exception as e:
                logger.error(f"Query failed: {e}")
                result = {
                    "query_type": "网络搜索",
                    "query_success": False,
                    "results": {"error": str(e)},
                }

        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

    async def _map_query(self, query, config):
        """模型只抽取查询参数，实际调用限定为地点、周边和驾车/步行工具。"""
        prompt = (
            "从用户问题提取地图参数，输出 JSON，禁止猜测城市或地名。"
            '字段：operation(place/nearby/route)、city、place、origin、destination、'
            'keywords(周边类别，默认餐厅)、mode(driving/walking)。缺少信息给 null。用户问题：' + query
        )
        try:
            request = _parse_llm_json(await _call_llm_text(self.model, [{"role": "user", "content": prompt}]))
            if not isinstance(request, dict) or not request.get("city"):
                raise MapToolError("请说明查询城市和具体地点")
            async with AmapMCP.connect(config) as maps:
                operation = request.get("operation")
                if operation == "place":
                    places = await maps.search_places(request["city"], request.get("place"))
                    summary = "；".join(f"{p['name']}：{p['address']}" for p in places) or "未找到该城市内的地点，请补充地址。"
                    payload = {"places": places}
                elif operation == "nearby":
                    place = await maps.resolve_place(request["city"], request.get("place"))
                    places = await maps.nearby(place["location"], request.get("keywords") or "餐厅")
                    summary = "；".join(f"{p['name']}：{p['address']}" for p in places) or "未找到周边地点。"
                    payload = {"origin": place, "places": places}
                elif operation == "route":
                    origin = await maps.resolve_place(request["city"], request.get("origin"))
                    destination = await maps.resolve_place(request["city"], request.get("destination"))
                    route = await maps.route(origin["location"], destination["location"], request.get("mode") or "driving")
                    summary = f"{origin['name']}至{destination['name']}，{route['mode']}预计{route['duration_min']}分钟，距离{route['distance_m']}米；以实际出行情况为准。"
                    payload = {"origin": origin, "destination": destination, "route": route}
                else:
                    raise MapToolError("请说明要查地点、周边还是路线")
            return {"query_type": "地图查询", "query_success": True,
                    "results": {"summary": summary, **payload, "sources": [{"title": "高德 MCP", "url": "https://www.amap.com"}]}}
        except MapToolError as exc:
            message = str(exc)
        except Exception:
            message = "地图参数提取或查询失败，请补充城市和准确地点。"
        return {"query_type": "地图查询", "query_success": False, "results": {"message": message}}

    def _is_weather_query(self, query: str) -> bool:
        """简单判断是否为天气类问题。"""
        q = (query or "").strip()
        if not q:
            return False
        return "天气" in q or "气温" in q or "下雨" in q or "预报" in q

    async def _weather_query(self, query: str) -> Dict[str, Any]:
        """
        使用 Open-Meteo 免费 API 查询天气（无需 API Key、无证书问题）。
        支持中文城市名，如：杭州、北京。
        """
        import asyncio
        try:
            import httpx
        except ImportError:
            return {
                "query_type": "天气查询",
                "query_success": False,
                "results": {"message": "需要安装 httpx: pip install httpx"},
            }

        # 从问题中提取城市（简单取第一个常见城市名或整句前 10 字中连续中文）
        city = self._extract_city_from_query(query)
        if not city:
            return {
                "query_type": "天气查询",
                "query_success": False,
                "results": {"message": "未识别到城市，请说明具体城市，如：杭州下周的天气怎么样？"},
            }

        return await self._weather_open_meteo(city)



    async def _weather_open_meteo(self, city: str) -> Dict[str, Any]:
        """Open-Meteo 天气查询（免费无 Key、无证书问题；中文城市名先地理编码）"""
        import asyncio
        try:
            import httpx
        except ImportError:
            return {"query_type": "天气查询", "query_success": False,
                    "results": {"message": "需要安装 httpx"}}

        WMO = {0: "晴", 1: "基本晴", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
               51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
               71: "小雪", 73: "中雪", 75: "大雪", 80: "阵雨", 81: "阵雨", 82: "强阵雨",
               95: "雷雨", 96: "雷雨伴冰雹", 99: "雷雨伴冰雹"}
        try:
            loop = asyncio.get_event_loop()

            # 1. 城市地理编码（支持中文）
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=zh"
            geo = await loop.run_in_executor(None, lambda: httpx.get(geo_url, timeout=10.0))
            geo.raise_for_status()
            gdata = geo.json().get("results") or []
            if not gdata:
                return {"query_type": "天气查询", "query_success": False,
                        "results": {"message": f"Open-Meteo 未找到城市「{city}」"}}
            lat, lon = gdata[0]["latitude"], gdata[0]["longitude"]

            # 2. 天气（当前 + 5 日预报）
            wx_url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                      f"&current=temperature_2m,relative_humidity_2m,weather_code"
                      f"&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=auto&forecast_days=5")
            wx = await loop.run_in_executor(None, lambda: httpx.get(wx_url, timeout=10.0))
            wx.raise_for_status()
            wdata = wx.json()

            cur = wdata.get("current", {})
            code = cur.get("weather_code")
            desc = WMO.get(code, f"code {code}")
            text = f"{city}当前天气：{desc}，气温 {cur.get('temperature_2m', '?')}°C，湿度 {cur.get('relative_humidity_2m', '?')}%。"
            daily = wdata.get("daily", {})
            fc = []
            for i, d in enumerate(daily.get("time", [])[:3]):
                dcode = (daily.get("weather_code") or [None] * (i + 1))[i] if len(daily.get("weather_code") or []) > i else None
                fc.append(f"{d}: {WMO.get(dcode, '—')}，"
                          f"{(daily.get('temperature_2m_min') or [None]*(i+1))[i]}~{(daily.get('temperature_2m_max') or [None]*(i+1))[i]}°C")
            if fc:
                text += " 未来几日：" + "；".join(fc)
            return {"query_type": "天气查询", "query_success": True,
                    "results": {"summary": text,
                                "sources": [{"url": "https://open-meteo.com", "title": "Open-Meteo"}]}}
        except Exception as e:
            logger.warning(f"Open-Meteo fallback failed: {e}")
            return {"query_type": "天气查询", "query_success": False,
                    "results": {"message": f"天气接口暂时不可用: {e}",
                                "sources": [{"url": "https://open-meteo.com", "title": "Open-Meteo"}]}}

    def _extract_city_from_query(self, query: str) -> str:
        """从问题中提取城市名（简单实现：常见城市列表匹配）。"""
        common_cities = [
            "北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "武汉", "西安", "苏州",
            "天津", "重庆", "厦门", "青岛", "大连", "宁波", "无锡", "长沙", "郑州", "济南",
            "哈尔滨", "沈阳", "昆明", "合肥", "福州", "石家庄", "南昌", "贵阳", "太原", "南宁",
        ]
        q = (query or "").strip()
        for city in common_cities:
            if city in q:
                return city
        # 否则取前 2～6 个连续中文字作为可能城市名
        m = re.search(r"[\u4e00-\u9fa5]{2,6}", q)
        return m.group(0).strip() if m else ""

    async def _web_search(self, query: str) -> Dict[str, Any]:
        """
        网络搜索 - 使用 DDGS（Dux Distributed Global Search），开启 safesearch，过滤可疑来源。

        Args:
            query: 用户查询

        Returns:
            搜索结果
        """
        if not DDGS_AVAILABLE:
            return {
                "query_type": "网络搜索",
                "query_success": False,
                "results": {
                    "message": "搜索库未安装",
                    "note": "请运行：pip install ddgs",
                },
            }

        try:
            ddgs = DDGS()
            # 开启安全搜索，优先 bing 后端（质量更稳定），多取几条再过滤
            search_results = []
            for backend in ("bing", "duckduckgo", "auto"):
                try:
                    raw = ddgs.text(
                        query,
                        max_results=10,
                        safesearch="on",
                        region="cn-zh",
                        backend=backend,
                    )
                    search_results = list(raw)
                    if search_results:
                        break
                except Exception as e:
                    logger.debug(f"DDGS backend {backend} failed: {e}")
                    continue

            results = []
            for result in search_results:
                href = result.get("href", "")
                if _is_suspicious_url(href):
                    continue
                results.append({
                    "title": result.get("title", ""),
                    "snippet": result.get("body", ""),
                    "url": href,
                })
                if len(results) >= 5:
                    break

            if not results:
                return {
                    "query_type": "网络搜索",
                    "query_success": False,
                    "results": {"message": "未找到相关结果"},
                }

            # 使用 LLM 总结搜索结果
            summary = await self._summarize_search_results(query, results)

            return {
                "query_type": "网络搜索",
                "query_success": True,
                "results": {
                    "summary": summary,
                    "sources": results,
                },
            }
        except Exception as e:
            logger.error(f"Web search failed: {e}")
            return {
                "query_type": "网络搜索",
                "query_success": False,
                "results": {"error": f"搜索失败: {str(e)}"},
            }

    async def _summarize_search_results(self, query: str, results: List[Dict]) -> str:
        """
        使用 LLM 总结搜索结果

        Args:
            query: 用户查询
            results: 搜索结果列表

        Returns:
            总结文本
        """
        if not results:
            return "未找到相关信息"

        # 构建搜索结果文本
        results_text = ""
        for i, result in enumerate(results, 1):
            results_text += f"\n{i}. {result['title']}\n{result['snippet']}\n"

        # 获取当前时间
        from datetime import datetime
        current_date = datetime.now().strftime("%Y年%m月%d日")
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][datetime.now().weekday()]

        # 动态读取 Prompt 指令 (Progressive Disclosure)
        skill_instruction = self.skill_loader.get_skill_content("query-info")
        if not skill_instruction:
            skill_instruction = "请直接回答用户的问题，保持简洁。"

        prompt = f"""根据以下搜索结果，简洁地回答用户的问题。

【当前时间】
{current_date} {weekday}
（用户查询中的相对时间请基于此日期理解，如"明天"、"2月28日"等）

【用户问题】
{query}

【搜索结果】
{results_text}

【任务说明】
{skill_instruction}
"""

        try:
            response = await self.model([{"role": "user", "content": prompt}])

            # 获取响应文本 - 处理异步生成器
            text = ""
            if hasattr(response, '__aiter__'):
                # 异步生成器，需要迭代获取内容
                async for chunk in response:
                    if isinstance(chunk, str):
                        text = chunk
                    elif hasattr(chunk, 'content'):
                        if isinstance(chunk.content, str):
                            text = chunk.content
                        elif isinstance(chunk.content, list):
                            for item in chunk.content:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    text = item.get('text', '')
            elif hasattr(response, 'text'):
                text = response.text
            elif hasattr(response, 'content'):
                text = response.content
            elif isinstance(response, dict) and 'content' in response:
                text = response['content']
            else:
                text = str(response) if response else ""

            return text.strip() if text else "无法生成摘要"
        except Exception as e:
            logger.error(f"Summarization failed: {e}")
            return "搜索成功，但摘要生成失败"
