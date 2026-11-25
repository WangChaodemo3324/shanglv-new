import copy
from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import httpx

from support import IsolatedCase
from utils.amap_mcp import AmapMCP, MapToolError
from utils.map_planning import enrich_activities, bind_schedule

PROJECT = Path(__file__).resolve().parents[2]


class MapTests(IsolatedCase):
    def test_streamable_http_initializes_discovers_and_calls_tool(self):
        async def scenario():
            from mcp.client.streamable_http import streamable_http_client
            seen = []
            schema = {"type": "object", "properties": {"city": {"type": "string"}, "keywords": {"type": "string"}},
                      "required": ["keywords"]}
            def remote(request):
                if request.method != "POST":
                    return httpx.Response(405)
                rpc = json.loads(request.content)
                seen.append(rpc["method"])
                if "id" not in rpc:
                    return httpx.Response(202)
                if rpc["method"] == "initialize":
                    data = {"protocolVersion": rpc["params"]["protocolVersion"],
                            "capabilities": {"tools": {}}, "serverInfo": {"name": "offline", "version": "1"}}
                elif rpc["method"] == "tools/list":
                    data = {"tools": [{"name": "maps_text_search", "inputSchema": schema}]}
                else:
                    self.assertEqual(rpc["params"]["name"], "maps_text_search")
                    self.assertEqual(rpc["params"]["arguments"], {"city": "杭州", "keywords": "西湖"})
                    data = {"content": [{"type": "text", "text": '{"pois": []}'}], "isError": False}
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc["id"], "result": data})
            @asynccontextmanager
            async def transport(url, **kwargs):
                self.assertTrue(url.startswith("https://mcp.amap.com/mcp?key="))
                async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
                    async with streamable_http_client(url, http_client=client) as streams:
                        yield streams
                self.assertTrue(client.is_closed)
            with patch("mcp.client.streamable_http.streamablehttp_client", transport):
                async with AmapMCP.connect({"enabled": True, "api_key": "offline-test"}) as maps:
                    self.assertEqual(await maps.search_places("杭州", "西湖"), [])
            self.assertEqual(seen, ["initialize", "notifications/initialized", "tools/list", "tools/call"])
        self.async_run(scenario())

    def test_planning_agent_uses_mcp_route_before_narrating(self):
        async def scenario():
            from agentscope.message import Msg
            spec = importlib.util.spec_from_file_location("offline_routed_agent", PROJECT / "skills/plan-trip/script/agent.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            calls = []
            class Model:
                async def __call__(self, messages):
                    calls.append(messages)
                    return SimpleNamespace(content=json.dumps({
                        "constraints": {"days": 1},
                        "activities": [
                            {"name": "A", "duration_min": 60, "open_from": 540, "open_to": 600, "user_marked": True},
                            {"name": "B", "duration_min": 60, "open_from": 630, "open_to": 690, "user_marked": True},
                        ],
                    }))
            route_calls = []
            class Maps:
                async def resolve_place(self, city, name):
                    return {"id": name, "name": name, "lat": 30, "lng": 120,
                            "location": "120,30" if name == "A" else "121,31"}
                async def route(self, origin, destination, mode):
                    route_calls.append((origin, destination))
                    return {"duration_min": 60, "distance_m": 10000, "mode": mode, "source": "amap_mcp"}
                async def nearby(self, location):
                    return []
            @asynccontextmanager
            async def connect(config):
                yield Maps()
            config = ModuleType("config")
            config.SYSTEM_CONFIG = {}
            config.AMAP_MCP_CONFIG = {"enabled": True, "api_key": "test"}
            agent = module.ItineraryPlanningAgent(model=Model())
            with patch.dict("sys.modules", {"config": config}), patch.object(AmapMCP, "connect", connect):
                response = await agent.reply(Msg(name="test", role="user", content=json.dumps({"context": {
                    "key_entities": {"destination": "杭州", "duration_days": 1}, "rewritten_query": "一天去A和B",
                }})))
            result = json.loads(response.content)
            self.assertIsNone(result["itinerary"])
            self.assertEqual(result["map_evidence"]["status"], "queried")
            self.assertEqual(route_calls, [("120,30", "121,31"), ("121,31", "120,30")])
            self.assertEqual(len(calls), 1)
        self.async_run(scenario())

    def test_information_query_calls_map_route_and_returns_source(self):
        async def scenario():
            from agentscope.message import Msg
            spec = importlib.util.spec_from_file_location("offline_map_query", PROJECT / "skills/query-info/script/agent.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            class Model:
                async def __call__(self, messages):
                    return SimpleNamespace(content=json.dumps({"operation": "route", "city": "杭州",
                        "origin": "西湖", "destination": "灵隐寺", "mode": "driving"}))
            class Maps:
                async def resolve_place(self, city, name):
                    return {"name": name, "location": "120,30"}
                async def route(self, origin, destination, mode):
                    return {"duration_min": 35, "distance_m": 8000, "mode": mode}
            @asynccontextmanager
            async def connect(config):
                yield Maps()
            config = ModuleType("config")
            config.SYSTEM_CONFIG = {}
            config.AMAP_MCP_CONFIG = {"enabled": True, "api_key": "test"}
            with patch.dict("sys.modules", {"config": config}), patch.object(AmapMCP, "connect", connect):
                agent = module.InformationQueryAgent(model=Model())
                response = await agent.reply(Msg(name="test", role="user", content="杭州西湖到灵隐寺开车多久"))
            result = json.loads(response.content)
            self.assertTrue(result["query_success"])
            self.assertIn("35分钟", result["results"]["summary"])
            self.assertEqual(result["results"]["sources"][0]["title"], "高德 MCP")
        self.async_run(scenario())

    def test_real_mcp_session_discovers_tools_and_resolves_official_search_shape(self):
        async def scenario():
            from mcp.server.fastmcp import FastMCP
            from mcp.shared.memory import create_connected_server_and_client_session
            server = FastMCP("offline-amap")

            @server.tool()
            async def maps_text_search(keywords: str, city: str) -> dict:
                self.assertEqual((keywords, city), ("西湖", "杭州"))
                return {"pois": [{"id": "poi-1", "name": "西湖", "address": "杭州"}]}

            @server.tool()
            async def maps_search_detail(id: str) -> dict:
                self.assertEqual(id, "poi-1")
                return {"id": id, "name": "西湖", "city": "杭州市", "location": "120.15,30.25"}

            @server.tool()
            async def maps_direction_driving(origin: str, destination: str) -> dict:
                return {"route": {"paths": [{"duration": "2101", "distance": "8000"}]}}

            async with create_connected_server_and_client_session(server) as session:
                page = await session.list_tools()
                maps = AmapMCP(session, {tool.name: tool.inputSchema for tool in page.tools}, 1)
                place = await maps.resolve_place("杭州", "西湖")
                self.assertEqual(place["coordinate_system"], "GCJ-02")
                self.assertEqual((place["lng"], place["lat"]), (120.15, 30.25))
                route = await maps.route(place["location"], "120.1,30.2")
                self.assertEqual(route["duration_min"], 36)
        self.async_run(scenario())

    def test_wrong_city_and_ambiguous_places_are_not_silently_selected(self):
        async def scenario():
            class Session:
                async def call_tool(self, name, arguments):
                    return SimpleNamespace(isError=False, structuredContent={"pois": [
                        {"id": "1", "name": "公园", "cityname": "北京市", "location": "116,39"},
                        {"id": "2", "name": "公园", "cityname": "杭州市", "location": "120,30"},
                        {"id": "3", "name": "公园", "cityname": "杭州市", "location": "120.1,30.1"},
                    ]})
            maps = AmapMCP(Session(), {"maps_text_search": {"properties": {"city": {}, "keywords": {}}}}, 1)
            self.assertEqual(len(await maps.search_places("杭州", "公园")), 2)
            with self.assertRaises(MapToolError):
                await maps.resolve_place("杭州", "公园")
        self.async_run(scenario())

    def test_enrichment_routes_are_directional_and_partial_failure_is_labelled(self):
        async def scenario():
            class Maps:
                async def resolve_place(self, city, name):
                    return {"id": name, "name": name, "location": "120,30", "lat": 30, "lng": 120}
                async def route(self, origin, destination, mode):
                    raise MapToolError("route unavailable")
                async def nearby(self, location):
                    return [{"name": "就近餐厅"}]
            @asynccontextmanager
            async def connect(config):
                yield Maps()
            source = [{"name": "A"}, {"name": "B"}]
            with patch.object(AmapMCP, "connect", connect):
                activities, matrix, evidence = await enrich_activities("杭州", source, {"enabled": True, "api_key": "test"})
            self.assertEqual(matrix, {})
            self.assertEqual(evidence["status"], "estimated")
            self.assertTrue(any("估算" in text for text in evidence["warnings"]))
            self.assertEqual(activities[0]["coordinate_system"], "GCJ-02")
            self.assertEqual(source, [{"name": "A"}, {"name": "B"}])
        self.async_run(scenario())

    def test_disabled_maps_do_not_open_connection(self):
        async def scenario():
            with patch.object(AmapMCP, "connect", side_effect=AssertionError("unexpected connection")):
                _, matrix, evidence = await enrich_activities("杭州", [{"name": "A"}], {})
            self.assertEqual(matrix, {})
            self.assertEqual(evidence["status"], "estimated")
        self.async_run(scenario())

    def solver(self):
        spec = importlib.util.spec_from_file_location("offline_map_solver", PROJECT / "skills/plan-trip/script/solver.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.solve_itinerary

    def test_long_directional_route_makes_tight_required_schedule_infeasible(self):
        activities = [
            {"name": "A", "duration_min": 60, "open_from": 540, "open_to": 600, "user_marked": True},
            {"name": "B", "duration_min": 60, "open_from": 630, "open_to": 690, "user_marked": True},
        ]
        constraints = {"days": 1, "travel_times_min": {"0:1": 60, "1:0": 1}}
        self.assertEqual(self.solver()(constraints, copy.deepcopy(activities))["status"], "infeasible")
        constraints["travel_times_min"]["0:1"] = 25
        self.assertEqual(self.solver()(constraints, activities)["status"], "solved")

    def test_nonadjacent_route_does_not_constrain_intermediate_stop(self):
        activities = [{"name": name, "duration_min": 60, "open_from": 540 + 70*i,
                       "open_to": 600 + 70*i, "user_marked": True} for i, name in enumerate("ABC")]
        result = self.solver()({"days": 1, "travel_times_min": {"0:1": 10, "1:2": 10, "0:2": 999}}, activities)
        self.assertEqual(result["status"], "solved")
        items = result["days"][0]["items"]
        self.assertEqual([item["name"] for item in items], list("ABC"))
        self.assertEqual(items[2]["travel_from_previous"]["duration_min"], 10)

    def test_model_cannot_change_solved_time_cost_or_coordinates(self):
        result = {"itinerary": {"daily_plans": [{"activities": [{"location": "A", "time": "00:00-01:00", "lat": 1, "cost": 0}]}]}}
        solution = {"days": [{"items": [{"name": "A", "start": "09:00", "end": "10:00", "cost": 50,
                                          "lat": 30, "lng": 120, "coordinate_system": "GCJ-02"}]}]}
        bound = bind_schedule(result, solution, {"warnings": ["交通估算"]})
        activity = bound["itinerary"]["daily_plans"][0]["activities"][0]
        self.assertEqual((activity["time"], activity["lat"], activity["cost"]), ("09:00-10:00", 30, 50))
        self.assertEqual(bound["itinerary"]["tips"], ["交通估算"])
