"""Local hybrid retrieval: SQLite FTS5 + BGE Chinese dense vectors."""
import hashlib
import json
import math
import re
from pathlib import Path

import db

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_CACHE = db.CADDIE_DATA_DIR / "models" / "fastembed"
_model = None


DOMAIN_GROUPS = {
    "投行": {"投行", "股权承做", "IPO", "上市", "保荐", "承销", "尽调", "招股书", "并购", "重组", "发行人"},
    "基金": {"基金", "公募", "私募", "净值", "申赎", "基金经理", "基金产品"},
    "固收": {"固收", "债券", "久期", "凸性", "利率债", "信用债", "收益率", "信用利差"},
    "资管": {"资管", "资产管理", "保险资管", "组合管理", "投资组合"},
    "银行": {"银行", "信贷", "授信", "存款", "贷款", "风控"},
    "实验": {"AB实验", "A/B实验", "实验设计", "对照组", "处理组", "显著性检验"},
    "因果": {"因果推断", "双重差分", "倾向得分", "工具变量", "断点回归"},
    "AI": {"人工智能", "大模型", "LLM", "智能体", "Agent", "RAG", "机器学习"},
    "电商": {"电商", "GMV", "转化率", "客单价", "渗透率", "履约"},
}


def _domain_anchors(text):
    haystack = (text or "").lower()
    anchors = set()
    for terms in DOMAIN_GROUPS.values():
        if any(term.lower() in haystack for term in terms):
            anchors.update(terms)
    return anchors


def _domain_labels(text):
    haystack = (text or "").lower()
    return {label for label, terms in DOMAIN_GROUPS.items()
            if any(term.lower() in haystack for term in terms)}


def _domain_evidence(item, labels):
    """Return strong domain evidence without trusting incidental body words."""
    meta = item.get("metadata") or {}
    identity = " ".join(str(meta.get(k) or "") for k in
                        ("domain_key", "topic", "company_industry", "track_group"))
    title = item.get("title") or ""
    body = item.get("snippet") or ""
    identity_labels = _domain_labels(f"{identity} {title}")
    matched_labels = labels & identity_labels
    if matched_labels:
        return matched_labels, 5
    # Body copy may mention a domain word in passing. Require two independent
    # terms from the same requested domain before treating it as reusable proof.
    for label in labels:
        hits = {term for term in DOMAIN_GROUPS[label] if term.lower() in body.lower()}
        if len(hits) >= 2:
            return {label}, 2
    return set(), 0


def _query_tokens(text):
    """Small local reranker tokens; keeps domain phrases and meaningful ASCII words."""
    value = re.sub(r"[\s，。！？；：、,.!?;:()（）\[\]【】]+", " ", text or "").strip()
    tokens = {x.lower() for x in re.findall(r"[A-Za-z][A-Za-z0-9+/#.-]{1,}|[\u4e00-\u9fff]{2,8}", value)}
    stop = {"帮我", "一下", "相关", "知识", "内容", "文档", "整理", "学习", "讲解", "系统", "专业", "岗位", "当前", "这个"}
    return {x for x in tokens if x not in stop}


def normalize_query(text):
    """Repair common human query variants before lexical/dense retrieval.

    Users often type finance acronyms with spaces or transpose IPO as IOP.
    Normalization is deliberately narrow so it improves recall without
    rewriting ordinary natural-language content.
    """
    value = (text or "").strip()
    value = re.sub(
        r"(?i)(?<![a-z])([a-z])\s+([a-z])(?:\s+([a-z]))?(?![a-z])",
        lambda match: "".join(x for x in match.groups() if x),
        value,
    )
    value = re.sub(r"(?i)(?<![a-z])iop(?![a-z])", "IPO", value)
    return value


def _embedding_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(MODEL_CACHE))
    return _model


def _chunks(text, size=900, overlap=120):
    text = (text or "").strip()
    if not text:
        return []
    result, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        result.append(text[start:end])
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return result


def _documents(track_id=None):
    docs = []
    tracks = [db.get_job_track(track_id)] if track_id else db.list_job_tracks()
    for track in filter(None, tracks):
        docs.append({"type": "job_track", "id": track["id"], "title": f"{track.get('company','')} · {track.get('role','')}",
                     "content": "\n".join(filter(None, [track.get("jd"), track.get("persona"), track.get("notes")])),
                     "meta": {"track_id": track["id"], "scope": "track"}})
    for item in db.list_knowledge_items(track_id=track_id):
        docs.append({"type": "knowledge_item", "id": item["id"], "title": item.get("title") or "知识",
                     "content": item.get("content") or "", "meta": {"track_id": item.get("track_id"),
                     "scope": item.get("scope_type"), "topic": item.get("topic"),
                     "domain_key": item.get("domain_key"), "company": item.get("company")}})
    for exp in db.get_experiences():
        for brief in exp.get("projects") or []:
            item = db.get_project(brief["id"])
            if item:
                docs.append({"type": "project", "id": item["id"], "title": item.get("name") or "项目",
                             "content": "\n".join(filter(None, [
                                 exp.get("company"), exp.get("role"),
                                 item.get("one_liner"), item.get("document"),
                                 item.get("technologies"), item.get("keywords"),
                             ])),
                             "meta": {"company": exp.get("company"), "role": exp.get("role")}})
    for brief in db.list_sources(limit=200):
        item = db.get_source(brief["id"]) or brief
        if track_id and item.get("track_id") not in (None, track_id):
            continue
        docs.append({"type": "source", "id": item["id"], "title": item.get("title") or item.get("file_name") or "资料",
                     "content": "\n".join(filter(None, [item.get("summary"), item.get("content")])),
                     "meta": {"track_id": item.get("track_id"), "source_type": item.get("source_type")}})
    for item in db.list_assets(track_id=track_id, limit=100):
        docs.append({"type": "asset", "id": item["id"], "title": item.get("title") or "生成产物",
                     "content": item.get("body") or "", "meta": {"track_id": item.get("track_id"), "asset_type": item.get("asset_type")}})
    return [item for item in docs if item.get("content")]


def sync_index(track_id=None):
    docs = _documents(track_id)
    existing = {x["chunk_key"]: x for x in db.get_semantic_chunks()}
    pending, active = [], []
    for doc in docs:
        db.replace_search_document(doc["type"], doc["id"], doc["title"], doc["content"])
        for index, content in enumerate(_chunks(doc["content"])):
            key = f"{doc['type']}:{doc['id']}:{index}"
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            active.append(key)
            if key not in existing or existing[key].get("content_hash") != digest:
                pending.append((doc, key, content, digest))
    if pending:
        vectors = list(_embedding_model().embed([x[2] for x in pending]))
        for (doc, key, content, digest), vector in zip(pending, vectors):
            db.upsert_semantic_chunk({"chunk_key": key, "entity_type": doc["type"], "entity_id": doc["id"],
                "title": doc["title"], "content": content, "content_hash": digest,
                "vector_json": json.dumps([round(float(v), 7) for v in vector]),
                "metadata_json": json.dumps(doc.get("meta") or {}, ensure_ascii=False)})
    # A track-scoped refresh must not delete chunks belonging to other tracks.
    if track_id is None:
        db.delete_stale_search_documents((doc["type"], doc["id"]) for doc in docs)
        db.delete_stale_semantic_chunks(active)
    return {"documents": len(docs), "chunks": len(active), "updated": len(pending)}


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
    return float(dot / (na * nb)) if na and nb else 0.0


def hybrid_search(query, track_id=None, limit=8):
    query = normalize_query(query)
    if not query:
        return []
    try:
        sync_index(track_id)
    except Exception:
        pass
    lexical = db.search(query, limit=max(30, limit * 4))
    lexical_rank = {(x["entity_type"], int(x["entity_id"])): i for i, x in enumerate(lexical)}
    lexical_by_key = {
        (x["entity_type"], int(x["entity_id"])): x for x in lexical
    }
    exact_title_keys = {
        (x["entity_type"], int(x["entity_id"]))
        for x in lexical if x.get("exact_title_match")
    }
    try:
        query_vector = list(_embedding_model().embed([query]))[0]
    except Exception:
        ranked = [{"entity_type": x["entity_type"], "entity_id": int(x["entity_id"]),
                   "title": x.get("title") or "", "snippet": x.get("snippet") or "",
                   "metadata": {}, "dense_score": 0.0, "score": 1.0 / (60 + rank),
                   "lexical_match": True} for rank, x in enumerate(lexical)]
    else:
        dense = []
        for row in db.get_semantic_chunks():
            meta = json.loads(row.get("metadata_json") or "{}")
            if track_id and meta.get("track_id") not in (None, track_id):
                continue
            dense.append((_cosine(query_vector, json.loads(row["vector_json"])), row, meta))
        dense.sort(key=lambda x: x[0], reverse=True)
        fused = {}
        for rank, (score, row, meta) in enumerate(dense[:max(40, limit * 6)]):
            key = (row["entity_type"], int(row["entity_id"]))
            item = fused.setdefault(key, {"entity_type": key[0], "entity_id": key[1], "title": row.get("title"),
                "snippet": row.get("content"), "metadata": meta, "dense_score": score, "score": 0.0})
            item["score"] += 1.0 / (60 + rank)
            if score > item.get("dense_score", 0):
                item.update({"snippet": row.get("content"), "dense_score": score})
        for key, rank in lexical_rank.items():
            lexical_item = lexical_by_key[key]
            item = fused.setdefault(key, {"entity_type": key[0], "entity_id": key[1],
                "title": lexical_item.get("title") or "",
                "snippet": lexical_item.get("snippet") or "",
                "metadata": {}, "dense_score": 0.0, "score": 0.0})
            item["score"] += 1.0 / (60 + rank)
            item["lexical_match"] = True
            if key in exact_title_keys:
                item["exact_title_match"] = True
                item["score"] += 1.0
                # Dense retrieval stores chunk-sized excerpts. When the user
                # names a document explicitly, the exact lexical row is the
                # authoritative full body and must replace that excerpt.
                item["title"] = lexical_item.get("title") or item.get("title") or ""
                item["snippet"] = lexical_item.get("snippet") or item.get("snippet") or ""
        ranked = sorted(fused.values(), key=lambda x: x["score"], reverse=True)
    # A track supplies the domain even when the user's instruction is vague (e.g.
    # "整理一下"). Dense similarity may improve recall, but it must never override
    # an explicit job-domain mismatch such as investment banking -> A/B testing.
    track = db.get_job_track(track_id) if track_id else None
    track_text = " ".join(str(track.get(k) or "") for k in
                          ("company", "role", "target", "company_industry", "track_group", "jd")) if track else ""
    explicit_labels = _domain_labels(query)
    labels = explicit_labels or _domain_labels(track_text)
    query_tokens = _query_tokens(query)
    if labels:
        filtered = []
        for item in ranked:
            haystack = f"{item.get('title') or ''} {item.get('snippet') or ''}".lower()
            token_hits = sorted(term for term in query_tokens if term in haystack)
            meta = item.get("metadata") or {}
            same_track = bool(track_id and meta.get("track_id") == track_id)
            scope = meta.get("scope")
            matched_labels, domain_score = _domain_evidence(item, labels)
            item_identity = " ".join(str(meta.get(k) or "") for k in
                                     ("domain_key", "topic", "company_industry", "track_group"))
            item_labels = _domain_labels(f"{item_identity} {item.get('title') or ''}")
            direct_query = bool(item.get("exact_title_match") or
                                (explicit_labels and item.get("lexical_match") and token_hits))
            allowed = same_track or bool(matched_labels) or direct_query
            # A stale or misfiled track link must not trump an explicit domain
            # conflict. For example, an A/B-testing note accidentally attached
            # to an investment-banking track is still the wrong evidence.
            if same_track and explicit_labels and item_labels and not (item_labels & explicit_labels):
                allowed = direct_query
            # Global notes are personal scratch material. They do not enter a job
            # context merely because dense retrieval found them nearby.
            if scope == "global" and not direct_query and not matched_labels:
                allowed = False
            if allowed:
                if same_track:
                    reason = "当前岗位资料"
                elif matched_labels:
                    reason = "领域匹配：" + "、".join(sorted(matched_labels))
                else:
                    reason = "用户关键词命中"
                item["match_reason"] = reason
                item["relevance_score"] = (
                    domain_score * 3 + len(token_hits) + (8 if same_track else 0)
                    + (100 if item.get("exact_title_match") else 0)
                )
                filtered.append(item)
        ranked = sorted(filtered, key=lambda x: (x.get("relevance_score", 0), x.get("score", 0)), reverse=True)
    else:
        filtered = []
        for item in ranked:
            haystack = f"{item.get('title') or ''} {item.get('snippet') or ''}".lower()
            token_hits = sorted(term for term in query_tokens if term in haystack)
            meta = item.get("metadata") or {}
            same_track = bool(track_id and meta.get("track_id") == track_id)
            # For vague requests, don't surface arbitrary global dense neighbors.
            if same_track or item.get("lexical_match") or len(token_hits) >= 2:
                item["match_reason"] = (
                    "标题精确命中" if item.get("exact_title_match")
                    else ("当前岗位资料" if same_track
                          else ("关键词命中" if item.get("lexical_match") else "语义相似"))
                )
                filtered.append(item)
        ranked = filtered
    results = ranked[:limit]
    # FastEmbed/Numpy may leave scalar score values in the fused result even
    # after cosine calculation. FastAPI cannot JSON-encode numpy.float32, so
    # normalize the public retrieval contract here for every caller.
    for item in results:
        for key in ("dense_score", "score", "relevance_score"):
            if item.get(key) is not None:
                item[key] = float(item[key])
    def json_safe(value):
        if isinstance(value, dict):
            return {str(key): json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(item) for item in value]
        if hasattr(value, "item"):
            try:
                return value.item()
            except (TypeError, ValueError):
                pass
        return value

    return [json_safe(item) for item in results]
