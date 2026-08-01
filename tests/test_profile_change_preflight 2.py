import server


def test_preflight_rejects_knowledge_id_as_source(monkeypatch):
    monkeypatch.setattr(server.db, "get_project", lambda _pid: {"id": 23, "document": "old"})
    monkeypatch.setattr(server.db, "get_source", lambda _sid: None)
    monkeypatch.setattr(
        server.db, "get_knowledge_item",
        lambda kid: {"id": kid, "title": "华兴证券项目介绍", "content": "knowledge"},
    )

    errors = server._preflight_profile_changes([{
        "type": "update_project",
        "project_id": 23,
        "primary_source_id": 80,
    }])

    assert errors == ["第 1 项 update_project：#80 是知识文档，不是原始资料"]


def test_preflight_requires_independent_document_for_knowledge(monkeypatch):
    monkeypatch.setattr(server.db, "get_project", lambda _pid: {"id": 23, "document": "old"})
    monkeypatch.setattr(
        server.db, "get_knowledge_item",
        lambda kid: {"id": kid, "title": "综合说明", "content": "covers several projects"},
    )

    errors = server._preflight_profile_changes([{
        "type": "update_project",
        "project_id": 23,
        "reference_knowledge_id": 80,
    }])

    assert "参考知识文档时必须生成该项目独立的完整正文" in errors[0]


def test_preflight_rejects_duplicate_project_updates(monkeypatch):
    monkeypatch.setattr(server.db, "get_project", lambda pid: {"id": pid, "document": "old"})

    errors = server._preflight_profile_changes([
        {"type": "update_project", "project_id": 23, "document": "first"},
        {"type": "update_project", "project_id": 23, "document": "second"},
    ])

    assert errors == ["第 2 项 update_project：项目 #23 在同一批次被重复更新"]
