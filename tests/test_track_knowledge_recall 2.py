"""Regression coverage for track knowledge catalog and follow-up retrieval."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import context
import db
import retrieval


def main():
    original_db_path = db.DB_PATH
    original_hybrid_search = retrieval.hybrid_search
    with TemporaryDirectory(prefix="caddie-track-recall-") as temp:
        db.DB_PATH = Path(temp) / "caddie.db"
        db.init_db()
        track_id = db.create_job_track({
            "company": "国泰海通",
            "role": "股权业务实习生",
            "target": "投行股权业务",
        })
        folders, _ = db.ensure_track_knowledge_folders(track_id)
        interview_folder = next(
            item["id"] for item in folders if item["name"] == "面试准备"
        )
        documents = {
            "2.讯飞医疗IPO案例分析": "讯飞医疗正文：收入、关联交易与应收账款核查。",
            "4.IPO毕业论文设计": "IPO论文正文：审核问询与定价风险。",
            "5.西安旅游公司案例": "西安旅游正文：再融资结构与融资约束。",
        }
        for title, content in documents.items():
            db.create_knowledge_item({
                "title": title,
                "content": content,
                "scope_type": "track",
                "track_id": track_id,
                "folder_id": interview_folder,
                "topic": "投行准备",
                "mastery": "learning",
                "status": "active",
            })

        # Isolate deterministic title recall from the embedding implementation.
        retrieval.hybrid_search = lambda *_args, **_kwargs: []
        data = context.build_context(
            track_id=track_id,
            intent="track_chat:general",
            query="股权知识里面有西安旅游、i o p和讯飞医疗，把这三个放进经历",
        )
        text = data["text"]
        assert "当前岗位完整知识目录" in text
        assert "[面试准备] 2.讯飞医疗IPO案例分析" in text
        assert "[面试准备] 4.IPO毕业论文设计" in text
        assert "[面试准备] 5.西安旅游公司案例" in text
        assert "讯飞医疗正文" in text
        assert "IPO论文正文" in text
        assert "西安旅游正文" in text
        assert retrieval.normalize_query("i o p") == "IPO"
        assert context.infer_track_scope(
            "请读取那三篇内部资料",
            "讯飞医疗IPO案例分析、IPO毕业论文设计、西安旅游公司案例",
        ) == track_id

        sibling_id = db.create_job_track({
            "company": "国泰海通",
            "role": "资本市场实习生",
            "target": "资本市场",
        })
        assert sibling_id != track_id
        assert context.infer_track_scope("打开国泰海通的资料") is None

    retrieval.hybrid_search = original_hybrid_search
    db.DB_PATH = original_db_path
    print("TRACK_KNOWLEDGE_RECALL_OK")


if __name__ == "__main__":
    main()
