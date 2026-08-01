from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

import db
import server
import vcs


ROOT = Path(__file__).resolve().parents[1]


def main():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "caddie-ui.css").read_text(encoding="utf-8")

    assert 'id="productTourMask"' in html
    assert 'id="productTourNav"' in html
    assert 'id="productTourContent"' in html
    assert "const PRODUCT_TOUR_STEPS=[" in html
    assert html.split("const PRODUCT_TOUR_STEPS=[", 1)[1].split("];", 1)[0].count("{key:") == 8
    assert all(text in html for text in (
        "一条会积累的求职线",
        "先整理一段你真正做过的经历",
        "创建目标岗位，把 JD 变成准备清单",
        "用真实证据准备知识与表达",
        "把每轮面试变成可复用的训练材料",
        "让内置与外部 Agent 接力",
        "把旧 Agent 里的资料迁进来",
        "每天只看真正的下一步",
    ))
    assert "openProductTour(0)" in html
    assert "product_tour_completed" in html
    assert "ArrowRight" in html and "ArrowLeft" in html and "Escape" in html
    assert ".product-tour-shell" in css
    assert "@media(max-width:850px)" in css
    assert "@media(prefers-reduced-motion:reduce)" in css

    original = (db.DB_PATH, db._JOURNAL_CONFIGURED, vcs.CADDIE_DIR, vcs.VAULT, server._commit)
    with TemporaryDirectory(prefix="caddie-product-tour-") as temp:
        root = Path(temp)
        db.DB_PATH = root / "data" / "caddie.db"
        db._JOURNAL_CONFIGURED = False
        vcs.CADDIE_DIR = root / "data"
        vcs.VAULT = root / "data" / "vault"
        server._commit = lambda _message: True
        db.init_db()
        with TestClient(server.app) as client:
            initial = client.get("/api/onboarding/status")
            assert initial.status_code == 200, initial.text
            payload = initial.json()
            assert payload["product_tour_completed"] is False
            assert set((payload.get("counts") or {}).keys()) >= {
                "sources", "tracks", "assets", "experiences", "projects", "knowledge", "interviews",
            }
            complete = client.post("/api/onboarding/tour-complete")
            assert complete.status_code == 200, complete.text
            assert complete.json()["product_tour_completed"] is True
            assert client.get("/api/onboarding/status").json()["product_tour_completed"] is True
    db.DB_PATH, db._JOURNAL_CONFIGURED, vcs.CADDIE_DIR, vcs.VAULT, server._commit = original
    print("PRODUCT_TOUR_CONTRACT_OK steps=8")


if __name__ == "__main__":
    main()
