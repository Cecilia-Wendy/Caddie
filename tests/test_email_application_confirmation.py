from server import _email_application_candidates


def test_email_candidates_include_same_company_roles_and_ai_suggestion():
    applications = [
        {"id": 17, "company": "国泰海通", "role": "股权业务实习生", "status": "interview"},
        {"id": 18, "company": "国泰海通证券", "role": "资本市场实习生", "status": "applied"},
        {"id": 19, "company": "其他公司", "role": "资本市场实习生", "status": "applied"},
    ]

    candidates = _email_application_candidates(
        {"company": "国泰海通", "application_id": 17},
        applications,
    )

    assert [item["id"] for item in candidates] == [17, 18]
    assert candidates[0]["suggested_by_ai"] is True
    assert candidates[1]["suggested_by_ai"] is False


def test_email_candidates_keep_suggested_application_visible_for_review():
    candidates = _email_application_candidates(
        {"company": "邮件中的别名", "application_id": 7},
        [{"id": 7, "company": "正式公司名", "role": "产品经理", "status": "screening"}],
    )

    assert candidates == [{
        "id": 7,
        "company": "正式公司名",
        "role": "产品经理",
        "status": "screening",
        "suggested_by_ai": True,
    }]
