from unittest.mock import patch

from server import (
    INTERVIEW_SOURCE_KINDS,
    TranscriptSourceV2In,
    add_transcript_source_v2,
    delete_transcript_source_v2,
    get_transcript_source_v2,
)


def test_review_agent_intake_is_a_supported_source_kind():
    payload = TranscriptSourceV2In(
        source_kind="agent_intake",
        title="复盘 Agent 接收的面试材料",
        raw_text="面试官：请介绍一下自己。候选人：您好，我是王玺。",
    )

    assert payload.source_kind in INTERVIEW_SOURCE_KINDS
    assert payload.raw_text


def test_review_agent_intake_reaches_the_storage_layer():
    payload = TranscriptSourceV2In(
        source_kind="agent_intake",
        title="复盘 Agent 接收的面试材料",
        raw_text="面试官：请介绍一下自己。候选人：您好，我是王玺。",
    )
    with (
        patch("server.interview_store.get_round", return_value={"id": 7}),
        patch("server.interview_store.create_transcript_source", return_value=23) as create_source,
        patch("server.interview_store.update_round"),
        patch("server._commit"),
    ):
        response = add_transcript_source_v2(7, payload)

    assert response["data"]["id"] == 23
    assert create_source.call_args.args[1]["source_kind"] == "agent_intake"


def test_transcript_source_can_be_viewed():
    source = {"id": 23, "round_id": 7, "raw_text": "完整逐字稿"}
    with (
        patch("server.interview_store.get_round", return_value={"id": 7}),
        patch("server.interview_store.get_transcript_source", return_value=source),
    ):
        response = get_transcript_source_v2(7, 23)

    assert response["data"]["raw_text"] == "完整逐字稿"


def test_transcript_source_delete_preserves_existing_answer_sheet():
    source = {"id": 23, "round_id": 7, "raw_text": "完整逐字稿"}
    with (
        patch("server.interview_store.get_round", return_value={"id": 7}),
        patch("server.interview_store.get_transcript_source", return_value=source),
        patch("server.db.list_agent_tasks", return_value=[]),
        patch("server.interview_store.get_exam", return_value=[{"id": 1}]),
        patch("server.interview_store.delete_transcript_source", return_value=(True, 0)),
        patch("server.interview_store.update_round") as update_round,
        patch("server._commit"),
    ):
        response = delete_transcript_source_v2(7, 23)

    assert response["data"]["answer_sheet_preserved"] is True
    update_round.assert_not_called()
