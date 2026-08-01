"""Regression checks for domain-safe, JSON-safe hybrid retrieval."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import retrieval


class FakeEmbedding:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def main():
    retrieval.sync_index = lambda track_id=None: {"documents": 0}
    retrieval._model = FakeEmbedding()
    retrieval.db.search = lambda query, limit=30: []
    retrieval.db.get_job_track = lambda track_id: {
        "id": track_id,
        "company": "中金公司",
        "role": "投资银行部医疗组实习生",
        "target": "投行股权承做",
        "jd": "参与 IPO 执行、尽调、招股书和股权融资项目",
    }
    retrieval.db.get_semantic_chunks = lambda: [
        {
            "entity_type": "knowledge_item", "entity_id": 1,
            "title": "AB实验与因果推断", "content": "处理组、对照组与显著性检验",
            "vector_json": "[1.0, 0.0]",
            "metadata_json": json.dumps({"track_id": 18, "scope": "track", "domain_key": "实验"}),
        },
        {
            "entity_type": "knowledge_item", "entity_id": 2,
            "title": "IPO执行与投行尽调", "content": "发行人尽调、招股书和承销流程",
            "vector_json": "[1.0, 0.0]",
            "metadata_json": json.dumps({"track_id": 18, "scope": "track", "domain_key": "投行"}),
        },
    ]

    items = retrieval.hybrid_search("讲解投行股权融资和 IPO 执行", track_id=18, limit=8)
    titles = [item["title"] for item in items]
    assert "IPO执行与投行尽调" in titles, titles
    assert "AB实验与因果推断" not in titles, titles
    json.dumps(items, ensure_ascii=False)
    assert all(isinstance(item.get("score"), float) for item in items)
    print("RETRIEVAL_CONTRACT_OK", titles)


if __name__ == "__main__":
    main()
