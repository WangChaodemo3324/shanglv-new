"""Read-only supplemental travel retrieval; no model, network or mutable user data."""
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "skills/ask-question/data/chinatravel"
DISCLAIMER = "ChinaTravel静态测评沙盒数据，仅供学习与规划演示；价格、营业时间及设施并非实时信息，出行前须核实。"


def tokens(text):
    """Chinese unigrams/bigrams plus Latin words, shared by indexing and queries."""
    result = []
    for part in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", text.lower()):
        result.append(part)
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            result.extend(part)
            result.extend(part[i:i + 2] for i in range(len(part) - 1))
    return list(dict.fromkeys(result))


def build_index(documents, path):
    """Build in a temporary file and atomically replace; stable ids prevent duplicates."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".building.sqlite")
    try:
        with closing(sqlite3.connect(temporary)) as conn, conn:
            conn.execute("DROP TABLE IF EXISTS knowledge")
            conn.execute("DROP TABLE IF EXISTS search")
            conn.execute("CREATE TABLE knowledge (id TEXT PRIMARY KEY, city TEXT, content TEXT, metadata TEXT)")
            conn.execute("CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED, tokens)")
            for doc in documents:
                meta = doc["metadata"]
                conn.execute("INSERT INTO knowledge VALUES (?, ?, ?, ?)",
                             (doc["id"], meta["city"], doc["content"], json.dumps(meta, ensure_ascii=False)))
                conn.execute("INSERT INTO search VALUES (?, ?)",
                             (doc["id"], " ".join(tokens(doc["content"].split("\n数据说明：")[0]))))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class TravelKnowledge:
    def __init__(self, path=None):
        self.path = Path(path) if path else DATA_DIR / "knowledge.sqlite"
        self.count = 0
        self.cities = []
        if self.path.exists():
            with closing(self.connect()) as conn:
                self.count = conn.execute("SELECT count(*) FROM knowledge").fetchone()[0]
                self.cities = [r[0] for r in conn.execute("SELECT DISTINCT city FROM knowledge")]

    def connect(self):
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)

    def search(self, query, k=6):
        if not self.count:
            return []
        terms = [term for term in tokens(query) if len(term) >= 2][:128]
        if not terms:
            return []
        expression = " OR ".join('"' + t + '"' for t in terms)
        cities = [city for city in self.cities if city and city in query]
        sql = ("SELECT k.id,k.content,k.metadata,bm25(search) FROM search "
               "JOIN knowledge k ON k.id=search.id WHERE search MATCH ?")
        params = [expression]
        if cities:
            sql += " AND k.city IN (" + ",".join("?" for _ in cities) + ")"
            params.extend(cities)
        categories = [category for category, pattern in (
            ("景点", r"门票|景点|游玩|游览"), ("餐饮", r"餐馆|餐厅|菜系|本帮菜|推荐菜"),
            ("住宿", r"酒店|住宿|床位")) if re.search(pattern, query)]
        if len(categories) == 1:
            sql += " AND json_extract(k.metadata, '$.category') = ?"
            params.append(categories[0])
        sql += (" ORDER BY CASE WHEN instr(?, json_extract(k.metadata, '$.title')) > 0 "
                "THEN 0 ELSE 1 END, bm25(search), k.id LIMIT ?")
        params.extend([query, k])
        with closing(self.connect()) as conn:
            return [{"id": r[0], "content": r[1], "metadata": json.loads(r[2]),
                     "score": 1 / (60 + rank), "distance": 1 / (60 + rank)}
                    for rank, r in enumerate(conn.execute(sql, params), 1)]
