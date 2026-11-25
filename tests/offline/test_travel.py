import importlib.util
import uuid
from pathlib import Path
from unittest.mock import patch

import support
from support import IsolatedCase
from utils.travel_knowledge import TravelKnowledge, build_index, DISCLAIMER

PROJECT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location("test_travel_" + name,
                                                PROJECT / "skills/ask-question/script" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TravelTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.path = support.ROOT / ("travel-" + uuid.uuid4().hex + ".sqlite")
        self.importer = load_script("import_chinatravel")
        self.docs = [self.importer.convert(row, "attractions", "test-revision", "2026-01-01") for row in [
            {"city": "北京", "id": 0, "name": "环球度假区", "type": "游乐园", "price": 503},
            {"city": "上海", "id": 0, "name": "环球游乐场", "type": "游乐园", "price": 100},
            {"city": "杭州", "id": 1, "name": "西湖", "type": "湖泊", "price": 0}]]
        build_index(self.docs, self.path)

    def test_city_scope_and_source_warning(self):
        hits = TravelKnowledge(self.path).search("北京环球度假区的参考价格", 2)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["metadata"]["city"], "北京")
        self.assertIn("503", hits[0]["content"])
        self.assertIn(DISCLAIMER, hits[0]["content"])
        self.assertTrue(hits[0]["metadata"]["static_snapshot"])
        self.assertIn("test-revision", hits[0]["metadata"]["source_url"])

    def test_reimport_is_idempotent_and_failed_build_keeps_old_index(self):
        build_index(self.docs, self.path)
        self.assertEqual(TravelKnowledge(self.path).count, 3)
        with self.assertRaises(Exception):
            build_index(self.docs + [self.docs[0]], self.path)
        self.assertEqual(TravelKnowledge(self.path).count, 3)

    def test_search_handles_syntax_and_empty_queries(self):
        store = TravelKnowledge(self.path)
        self.assertEqual(store.search('"() * OR'), [])
        self.assertEqual(store.search(""), [])
        self.assertEqual(store.search("杭州西湖" + '"(*)'), store.search("杭州西湖"))

    def test_named_attraction_beats_nearby_hotel(self):
        hotel = self.importer.convert({"city": "北京", "id": 0, "name": "环球度假区酒店", "price": 1000},
                                      "accommodations", "test-revision", "2026-01-01")
        build_index(self.docs + [hotel], self.path)
        store = TravelKnowledge(self.path)
        self.assertEqual(store.search("北京环球度假区")[0]["metadata"]["title"], "环球度假区")
        self.assertTrue(all(d["metadata"]["category"] == "景点"
                            for d in store.search("北京环球度假区的门票参考价格和开放时间")))
        self.assertEqual(store.search("北京环球度假区酒店价格")[0]["metadata"]["category"], "住宿")

    def test_agent_retrieves_without_vector_or_llm(self):
        module = load_script("agent")
        store = TravelKnowledge(self.path)
        with patch.object(module, "DEPENDENCIES_AVAILABLE", False), \
                patch("utils.travel_knowledge.TravelKnowledge", return_value=store):
            agent = module.RAGKnowledgeAgent(model=None)
        self.assertTrue(agent.initialized)
        hits = agent.search_knowledge("杭州西湖")
        self.assertEqual(hits[0]["metadata"]["title"], "西湖")
        answer = self.async_run(agent._generate_answer("杭州西湖", hits))
        self.assertIn(DISCLAIMER, answer)
        self.assertEqual(agent.get_stats()["retrieval_mode"], "supplemental_keyword_only")
