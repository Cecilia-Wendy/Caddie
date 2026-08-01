from pathlib import Path
from tempfile import TemporaryDirectory
import json, sqlite3, sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
import ai, db, server, vcs


def main():
    reconciled=server._reconcile_experience_reply("分析内容。\n下一步：请点击右上角写入职业档案。",True)
    assert "写入职业档案" not in reconciled and "拆解数据来源" in reconciled and "尚未进入可确认写入状态" in reconciled
    assert server._normalize_experience_question_plan(["追问一",{"text":"追问二","rationale":"补证据","options":[{"label":"有记录","description":"可以提供会议记录"},"暂时没有"]},{"reason":"无问题"}])==[
        {"question":"追问一","reason":"","options":[]},{"question":"追问二","reason":"补证据","options":[{"label":"有记录","description":"可以提供会议记录"},{"label":"暂时没有","description":""}]}]
    malformed=server._normalize_experience_dispatch({
        "intent":"add_fact","references":{"source":"current_project"},
        "operations":"propose_change","affected_fields":{"field":"document"},
        "evidence":"user statement","confidence":"unknown","expert_plan":"experience_detective",
    },{}, {"id":7,"name":"项目"})
    assert malformed["intent"]=="add_fact" and malformed["references"]==[{"source":"current_project"}]
    assert malformed["operations"]==["propose_change"] and malformed["affected_fields"]==[{"field":"document"}]
    assert malformed["evidence"]==["user statement"] and malformed["confidence"]==.5
    assert malformed["expert_plan"]["primary"]=="experience_detective"
    ui=(ROOT/"static"/"index.html").read_text(encoding="utf-8")
    assert "打开专家团" not in ui and "项目专家团" not in ui
    assert ui.count("function applyExperienceCandidates(")==1
    assert all(x in ui for x in ("继续调整","失败重试","确认写入所选字段","直达项目"))
    assert all(x in ui for x in ("answerExperienceQuestion","自行回答","已选择，可补充后发送","is-selected"))
    assert all(x in ui for x in ("待补充口径","拆解数据来源","补充已有信息","先用定性表达","candidate-draft-details","工作草稿","仅供查看","draft-chevron"))
    assert all(x in ui for x in ('data-experience-action="plan"',"area.onclick=event=>","scrollIntoView({behavior:'smooth'","已带入输入框，可补充后发送"))
    assert "latestFailed=new Map()" in ui
    assert all(x in ui for x in ("latestCandidateByState=new Map()","c.status==='failed'||c.status==='pending_confirmation'","已暂不处理，这条草稿不会写入项目"))
    old_chat,old_resolve=ai.chat,ai.resolve_model_profile
    with TemporaryDirectory(prefix="caddie-exp-e2e-") as temp:
        root=Path(temp); db.DB_PATH=root/"data"/"fresh.db"; db._JOURNAL_CONFIGURED=False
        vcs.CADDIE_DIR=root/"data";vcs.VAULT=vcs.CADDIE_DIR/"vault";server._commit=lambda _m:None
        ai.resolve_model_profile=lambda _k:{"profile_key":"writing","provider_name":"test","model":"test","provider":{"id":"test"}}
        captured=[]
        replies=iter([
            json.dumps({"intent":"improve_expression","operations":"generate_revision_candidate","affected_fields":{"field":"one_liner"},"references":{"source":"current_project"},"confidence":.93,"evidence":"绑定当前项目","expert_plan":{"primary":"resume_editor","supporting":"experience_detective"},"question_plan":[]},ensure_ascii=False),
            json.dumps({"reply":"我已按你确认的职责边界生成一项候选。","question_plan":[],"change_candidates":[{"field":"one_liner","proposed_value":"负责状态机与日志追踪，系统其余部分由团队共同完成。","evidence_source":"user_statement","evidence":[{"source":"user_message","content":"状态机和日志追踪是我负责的，但整个系统不是我独立完成的。"}],"unverified_claims":[]}]},ensure_ascii=False),
            json.dumps({"intent":"simulate_interview","confidence":.8,"expert_plan":{"primary":"experience_detective","supporting":[]}},ensure_ascii=False),
            "现在由压力面试官继续追问。",
            json.dumps({"intent":"improve_expression","confidence":.9,"expert_plan":{"primary":"resume_editor","supporting":[]}},ensure_ascii=False),
            json.dumps({"reply":"先生成候选。","question_plan":[],"change_candidates":[{"field":"one_liner","proposed_value":"效率提升 37%","evidence_source":"wording_only","evidence":[],"unverified_claims":[]}]},ensure_ascii=False),
            json.dumps({"intent":"improve_expression","confidence":.9,"expert_plan":{"primary":"resume_editor","supporting":[]}},ensure_ascii=False),
            json.dumps({"reply":"先生成候选。","question_plan":[],"change_candidates":[{"field":"technologies","proposed_value":"独立主导 Kubernetes 技术平台上线","evidence_source":"wording_only","evidence":[],"unverified_claims":[]}]},ensure_ascii=False),
            json.dumps({"field":"one_liner","proposed_value":"参与系统设计。","evidence_source":"wording_only","evidence":[],"unverified_claims":[]},ensure_ascii=False),
            json.dumps({"field":"technologies","proposed_value":"独立负责 Rust 核心平台上线","evidence_source":"wording_only","evidence":[],"unverified_claims":[]},ensure_ascii=False),
            json.dumps({"intent":"add_fact","operations":"propose_change","affected_fields":{"field":"document"},"confidence":.6,"expert_plan":{"primary":"experience_detective","supporting":[]},"question_plan":[]},ensure_ascii=False),
            json.dumps({"reply":"当前无依据，请先补充口径。","question_plan":[],"change_candidates":[]},ensure_ascii=False),
            "全局对话已读取经历概览。",
        ])
        def fake_chat(messages,system="",**kwargs): captured.append({"messages":messages,"system":system}); return next(replies)
        ai.chat=fake_chat
        with TestClient(server.app) as client:
            # Fresh schema must work with FK enforcement and expose real project FKs.
            conn=sqlite3.connect(db.DB_PATH); assert conn.execute("PRAGMA foreign_keys").fetchone()[0]==0; conn.execute("PRAGMA foreign_keys=ON")
            fk={r[2] for r in conn.execute("PRAGMA foreign_key_list(experience_workspace_state)")}; assert "projects" in fk; conn.close()
            parent=client.post("/api/experiences",json={"company":"示例公司","role":"AI 产品经理"}).json()["id"]
            p1=client.post(f"/api/experiences/{parent}/projects",json={"name":"Workflow 工程化","one_liner":"参与系统设计。","document":"# 背景\n完整项目文档 A\n# 贡献\n参与系统设计。","technologies":"Workflow, logging"}).json()["id"]
            p2=client.post(f"/api/experiences/{parent}/projects",json={"name":"隔离项目 B","one_liner":"B_ONE_LINER_SECRET","document":"B_DOCUMENT_SECRET","technologies":"B_TECH_SECRET","keywords":"B_KEYWORD_SECRET"}).json()["id"]
            def resolve(pid):
                r=client.post("/api/workspace-sessions/resolve",json={"workspace_task_key":f"experience:project:{pid}","mode":"experience","title":"经历 Agent","target_type":"project","target_id":pid,"project_id":pid});assert r.status_code==200,r.text;return r.json()
            s1,s2=resolve(p1),resolve(p2); sid=s1["session"]["id"]
            assert s1["session"]["project_id"]==p1 and s1["session"]["id"]!=s2["session"]["id"]
            mismatch=client.post("/api/chat",json={"session_id":sid,"message":"test","workspace_mode":"experience","target_type":"project","target_id":p2})
            assert mismatch.status_code==409
            message="状态机和日志追踪是我负责的，但整个系统不是我独立完成的。"
            answer=client.post("/api/chat",json={"session_id":sid,"message":message,"workspace_mode":"experience","target_type":"project","target_id":p1,"response_style":"standard"})
            assert answer.status_code==200,answer.text; data=answer.json()
            assert data["dispatch"]["expert_plan"]["primary"]==data["expert"]["key"]=="resume_editor"
            assert "完整项目文档 A" in captured[1]["system"] and "Workflow, logging" in captured[1]["system"]
            assert "示例公司" in captured[1]["system"] and "AI 产品经理" in captured[1]["system"]
            assert all(secret not in captured[1]["system"] for secret in ("隔离项目 B","B_ONE_LINER_SECRET","B_DOCUMENT_SECRET","B_TECH_SECRET","B_KEYWORD_SECRET"))
            assert "当前是全局职业对话" not in captured[1]["system"]
            candidates=data["change_candidates"]; assert len(candidates)==1 and candidates[0]["evidence_source"]=="user_statement"
            cid=candidates[0]["id"]
            manual=client.post("/api/chat",json={"session_id":sid,"message":"开始模拟面试","expert_key":"pressure_interviewer"})
            assert manual.status_code==200 and manual.json()["expert"]["key"]==manual.json()["dispatch"]["expert_plan"]["primary"]=="pressure_interviewer"
            restored=resolve(p1); assert restored["experience_state"]["dispatch"]["expert_plan"]["primary"]=="pressure_interviewer" and restored["experience_candidates"][0]["id"]==cid
            # Continue adjustment updates the same candidate and revision.
            revised=client.patch(f"/api/workspace-sessions/{sid}/experience-candidates/{cid}",json={"proposed_value":"状态机与日志追踪由我负责，其他模块由团队共同完成。","evidence_source":"user_statement","evidence":[{"source":"user_message","content":message}],"unverified_claims":[]})
            assert revised.status_code==200 and revised.json()["item"]["revision"]==2
            # Separate session has no candidates; reject works without touching project.
            assert client.get(f"/api/workspace-sessions/{s2['session']['id']}/experience-candidates").json()["items"]==[]
            reject_copy=client.post(f"/api/workspace-sessions/{sid}/experience-candidates",json={"target_id":p1,"field":"one_liner","proposed_value":"参与系统设计。","evidence_source":"wording_only","evidence":[]}).json()["candidate_id"]
            assert client.post(f"/api/workspace-sessions/{sid}/experience-candidates/{reject_copy}/reject").status_code==200
            # Invalid model output is persisted as failed and never writes the project.
            numeric=client.post("/api/chat",json={"session_id":sid,"message":"帮我加一个量化结果"}); assert numeric.status_code==200,numeric.text
            numeric_failed=numeric.json()["change_candidates"][-1]
            assert numeric_failed["status"]=="failed" and "待补充数据口径" in numeric_failed["failure_reason"]
            textual=client.post("/api/chat",json={"session_id":sid,"message":"帮我补充职责和技术"}); assert textual.status_code==200,textual.text
            textual_failed=textual.json()["change_candidates"][-1]
            assert textual_failed["status"]=="failed" and "待确认表达口径" in textual_failed["failure_reason"]
            assert db.get_project(p1)["one_liner"]=="参与系统设计。"
            before_revision=numeric_failed["revision"]
            retried=client.post(f"/api/workspace-sessions/{sid}/experience-candidates/{numeric_failed['id']}/retry")
            assert retried.status_code==200,retried.text; retried_item=retried.json()["item"]
            assert retried_item["status"]=="pending_confirmation" and retried_item["revision"]==before_revision+1
            assert retried_item["failure_reason"] is None and db.get_project(p1)["one_liner"]=="参与系统设计。"
            retry_failed=client.post(f"/api/workspace-sessions/{sid}/experience-candidates/{textual_failed['id']}/retry")
            assert retry_failed.status_code==200,retry_failed.text; retry_failed_item=retry_failed.json()["item"]
            assert retry_failed_item["status"]=="failed" and retry_failed_item["revision"]==textual_failed["revision"]+1
            assert "待确认表达口径" in retry_failed_item["failure_reason"] and db.get_project(p1)["technologies"]=="Workflow, logging"
            # An explicit edit request must remain visible even when the model omits candidates.
            omitted=client.post("/api/chat",json={"session_id":sid,"message":"请补充为每周节省 8 小时","workspace_mode":"experience","target_type":"project","target_id":p1})
            assert omitted.status_code==200,omitted.text
            omitted_item=omitted.json()["change_candidates"][-1]
            assert omitted_item["status"]=="failed" and "待核验请求" in omitted_item["failure_reason"]
            assert db.get_project(p1)["document"].startswith("# 背景")
            # Unsupported number and unsupported textual ownership are both blocked.
            assert client.post(f"/api/workspace-sessions/{sid}/experience-candidates",json={"target_id":p1,"field":"one_liner","proposed_value":"效率提升 37%","evidence_source":"wording_only","evidence":[]}).status_code==422
            assert client.post(f"/api/workspace-sessions/{sid}/experience-candidates",json={"target_id":p1,"field":"one_liner","proposed_value":"独立主导 Kubernetes 技术平台上线","evidence_source":"wording_only","evidence":[]}).status_code==422
            # Normal global chat keeps the legacy experience overview behavior.
            global_answer=client.post("/api/chat",json={"session_id":"global-context-test","message":"概览我的经历"})
            assert global_answer.status_code==200,global_answer.text
            assert "隔离项目 B" in captured[-1]["system"] and "当前是全局职业对话" in captured[-1]["system"]
            applied=client.post(f"/api/workspace-sessions/{sid}/experience-candidates/apply",json={"candidate_ids":[cid]}); assert applied.status_code==200,applied.text
            receipt=applied.json()["results"][0]; assert receipt["before"]=="参与系统设计。" and receipt["after"].startswith("状态机") and receipt["applied_at"]
            stale=client.post(f"/api/workspace-sessions/{sid}/experience-candidates",json={"target_id":p1,"field":"one_liner","proposed_value":receipt["after"],"evidence_source":"wording_only","evidence":[]}).json()["candidate_id"]
            db.update_project(p1,{**db.get_project(p1),"one_liner":"用户手动改值"})
            assert client.post(f"/api/workspace-sessions/{sid}/experience-candidates/apply",json={"candidate_ids":[stale]}).status_code==409
        ai.chat,ai.resolve_model_profile=old_chat,old_resolve
    print("EXPERIENCE_WORKSPACE_E2E_OK")

if __name__=="__main__": main()
