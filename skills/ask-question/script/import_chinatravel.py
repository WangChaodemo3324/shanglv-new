"""Download only three Chinese tables; pin revision, verify counts and build local retrieval.

Run: .venv-offline/Scripts/python.exe skills/ask-question/script/import_chinatravel.py
Import-time dependencies: requests, duckdb. Runtime retrieval uses only stdlib SQLite.
"""
import hashlib
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from utils.travel_knowledge import DATA_DIR, DISCLAIMER, build_index

REPO = "LAMDA-NeSy/ChinaTravel-Sandbox"
EXPECTED = {"attractions": 3413, "restaurants": 4655, "accommodations": 3866}
LABELS = {"city": "城市", "name": "名称", "type": "类型", "lat": "纬度", "lon": "经度",
          "price": "参考价格（元）", "opentime": "参考开放时间", "endtime": "参考结束时间",
          "recommendmintime": "建议最短游玩时间（小时）", "recommendmaxtime": "建议最长游玩时间（小时）",
          "cuisine": "菜系", "recommendedfood": "推荐菜", "hotelname_en": "酒店英文名称",
          "featurehoteltype": "酒店特色", "numbed": "床位数"}
CATEGORIES = {"attractions": "景点", "restaurants": "餐饮", "accommodations": "住宿"}


def convert(row, category, revision, fetched):
    document_id = f"chinatravel:{category}:zh:{row['city']}:{row['id']}"
    metadata = {"source": REPO, "source_url": f"https://huggingface.co/datasets/{REPO}/blob/{revision}/data/zh/{category}.parquet",
                "revision": revision, "retrieved_at": fetched, "license": "CC-BY-4.0",
                "static_snapshot": True, "scope": "benchmark_sandbox", "category": CATEGORIES[category],
                "city": row["city"], "title": row["name"], "source_record_id": row["id"],
                "knowledge_id": document_id}
    lines = [f"{row['city']} · {CATEGORIES[category]} · {row['name']}"]
    for key, value in row.items():
        if key in ("language", "id") or value is None or value == "":
            continue
        lines.append(f"{LABELS.get(key, key)}：{value}")
    lines.append("数据说明：" + DISCLAIMER)
    return {"id": document_id,
            "content": "\n".join(lines), "metadata": metadata}


def main():
    import duckdb
    import requests
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="Rebuild from verified downloaded files; no network")
    args = parser.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    prior = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8")) if args.local else None
    if prior:
        revision, fetched = prior["revision"], prior["retrieved_at"]
    else:
        response = requests.get(f"https://huggingface.co/api/datasets/{REPO}", timeout=30)
        response.raise_for_status()
        revision = response.json()["sha"]
        fetched = datetime.now(timezone.utc).isoformat()
    documents, files = [], {}
    for category, expected in EXPECTED.items():
        name = category + ".parquet"
        path = DATA_DIR / name
        if prior:
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != prior["files"][name]["sha256"]:
                raise ValueError(f"Checksum mismatch: {name}")
        else:
            response = requests.get(f"https://huggingface.co/datasets/{REPO}/resolve/{revision}/data/zh/{name}", timeout=60)
            response.raise_for_status()
            content = response.content
            path.write_bytes(content)
        with duckdb.connect() as conn:
            cursor = conn.execute("SELECT * FROM read_parquet(?)", [str(path)])
            columns = [c[0] for c in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        if len(rows) != expected:
            raise ValueError(f"Unexpected row count for {category}: {len(rows)} != {expected}; review release before import")
        documents.extend(convert(row, category, revision, fetched) for row in rows)
        files[name] = {"rows": len(rows), "bytes": len(content),
                       "sha256": hashlib.sha256(content).hexdigest()}
        print(category, len(rows), flush=True)
    if len({d['id'] for d in documents}) != len(documents):
        raise ValueError("Duplicate knowledge IDs")
    normalized = "".join(json.dumps(d, ensure_ascii=False, allow_nan=False) + "\n" for d in documents)
    temporary = DATA_DIR / "documents.jsonl.tmp"
    temporary.write_bytes(normalized.encode("utf-8"))
    build_index(documents, DATA_DIR / "knowledge.sqlite")
    temporary.replace(DATA_DIR / "documents.jsonl")
    (DATA_DIR / "manifest.json").write_text(json.dumps({"dataset": REPO, "revision": revision,
        "retrieved_at": fetched, "license": "CC-BY-4.0", "disclaimer": DISCLAIMER,
        "documents": len(documents), "files": files,
        "documents_sha256": hashlib.sha256(normalized.encode()).hexdigest()}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Indexed", len(documents), "records at", DATA_DIR)


if __name__ == "__main__":
    main()
