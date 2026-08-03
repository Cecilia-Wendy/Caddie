"""Conservative first-pass transcript decomposition.

It intentionally leaves uncertain speaker roles as unknown.  AI enrichment can
run afterwards, but this pass makes pasted transcripts usable even without a
configured model and gives the user an editable answer sheet immediately.
"""
from __future__ import annotations

import re

import interview_store

_SPEAKER = re.compile(r"^\s*(?P<speaker>[^：:\n]{1,24})\s*[：:]\s*(?P<text>.+)$")
_TIMESTAMPED_SPEAKER = re.compile(
    r"^\s*(?P<speaker>.+?)（(?P<timestamp>\d{1,2}:\d{2}:\d{2})）\s*[：:]\s*(?P<text>.+)$"
)
_QUESTION = re.compile(r"[？?]|(?:请|你|能否|为什么|如何|是否|介绍一下|说一下).{0,45}$")


def _question_meta(text: str) -> tuple[str, str]:
    """Give the library useful first-pass tags even when no model is configured."""
    t = (text or '').replace(' ', '')
    if any(x in t for x in ('自我介绍', '介绍一下你自己')):
        return '自我介绍', '自我定位与表达'
    if interview_store.is_ai_concept_question(t):
        return 'AI认知', 'AI产品判断'
    if any(x in t for x in ('项目', '实习', '经历', '负责', '做了什么')):
        return '项目深挖', '项目推进与证据'
    if any(x in t for x in ('为什么', '动机', '选择', '职业规划', '方向')):
        return '动机', '岗位匹配与稳定性'
    if any(x in t for x in ('业务', '行业', '产品', '市场', '理解')):
        return '业务理解', '行业与业务判断'
    if any(x in t for x in ('冲突', '困难', '协作', '沟通', '压力', '失败')):
        return '行为面试', '协作与问题解决'
    if any(x in t for x in ('你有什么问题', '反问')):
        return '反问', '岗位判断与沟通'
    return '其他', '待进一步识别'


def _role(label: str) -> str:
    normalized = re.sub(r"\s+", "", label).lower()
    if normalized in {'我', '本人', '候选人', 'candidate'}: return 'self'
    if any(x in normalized for x in {'面试官', 'hr', '老师', 'interviewer'}): return 'interviewer'
    if any(x in normalized for x in {'候选人', 'candidate', '同学'}): return 'other_candidate'
    return 'unknown'


def _transcript_lines(raw_text: str) -> list[str]:
    """Remove meeting-note summaries and retain only the actual ASR section."""
    raw = raw_text or ''
    markers = list(re.finditer(r"(?:会议)?转写原文", raw))
    if markers:
        raw = raw[markers[-1].end():]
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _is_likely_interviewer_question(text: str) -> bool:
    """Recover questions when ASR assigns every turn to the meeting owner."""
    compact = re.sub(r"\s+", "", text or '')
    if not _QUESTION.search(compact):
        return False
    # A candidate's own follow-up must not become an interviewer question.
    if re.search(r"(?:我还有(?:一个)?问题|我想知道|我想了解|我想问|我理解|我个人|我自己|我目前|我之前|请问)", compact):
        return False
    # "对吧" and simple acknowledgements are not standalone exam questions.
    primary = re.search(
        r"(?:介绍一下|讲一下|解释一下|为什么|如何|是否|能不能|可以.*吗|怎么(?:样|个)?|哪个|哪种|哪些|什么|多长|多久|最大(?:的)?(?:问题|挑战)|长期方向|职业规划|从哪里|如何获取|你(?:能|会|对|是|在|的|有没有|觉得|属于)|您(?:能|会|对|是|在|的|有没有|觉得|现在))",
        compact,
    )
    return bool(primary)


def _question_cluster(text: str) -> tuple[str, str, str, str]:
    """Return a stable interview-sized topic for noisy ASR question fragments.

    D-Chat occasionally puts an interviewer turn and the following candidate
    answer in the same timestamped paragraph.  In that case preserving every
    question mark creates a useless 40-question paper.  These clusters are a
    local, explainable fallback: original fragments stay linked as evidence,
    while the answer sheet uses the main question a human would recognise.
    """
    t = re.sub(r"\s+", "", text or '').lower()
    if any(x in t for x in ('学制', '毕业', '实习时长', '实习时间', 'gapyear', '学校要求', '课程结束')):
        return ('availability', '基础信息', '实习稳定性与时间安排', '请说明你的学制、可到岗时间及稳定实习安排。')
    if any(x in t for x in ('主修', '金融方向', '金融方面学习', '专业方向')):
        return ('finance_background', '教育与方向', '金融学习背景与方向', '你的金融学习背景和关注方向是什么？')
    if any(x in t for x in ('滴滴', '代表性项目', '简单介绍', '面对什么问题', '什么样的问题')):
        return ('project_intro', '项目深挖', '代表项目的背景、思路与落地', '请介绍一个代表项目：面对什么问题、如何思考并推动落地？')
    if any(x in t for x in ('gmv', '建模', '预测', '参数调优', '信息怎么获取', '从哪里来', '数据回溯')):
        return ('gmv_reasoning', '项目深挖', '数据建模与决策过程', '请介绍 GMV 预测项目的拆解、信息来源、关键判断与结果。')
    if any(x in t for x in ('逻辑驱动', '意见坚持', '遇到这种问题', '最大的问题', '最大挑战', '怎么处理')):
        return ('work_style', '行为面试', '分歧处理与工作方式', '遇到判断分歧或不确定信息时，你如何推进决策？')
    if any(x in t for x in ('职业规划', '长期方向', '一级市场', '量化', '互联网方向', '长期的方向')):
        return ('career', '动机', '职业方向与岗位匹配', '这份 PMO 实习在你的长期职业规划中处于什么位置？')
    if any(x in t for x in ('产研协同', '项目管理', '里程碑', '协同的框架', '产品的研发', '协作动线', '协同机制')):
        return ('pmo', '业务理解', '产研协同与项目管理方法', '你如何理解产研协同的完整流程、关键里程碑和推进机制？')
    if any(x in t for x in ('agent', 'ai', 'vibe', 'harness', '组织提效', '工程化')):
        return ('ai_pmo', '业务理解', 'AI 在组织提效中的作用', '你如何看待 AI 工具在项目管理和组织提效中的边界与落地方式？')
    return ('other', '其他', '待确认话题', '')


def _merge_noisy_followups(questions: list[dict]) -> tuple[list[dict], bool]:
    """Merge a sea of ASR pseudo-questions into a readable first-pass paper."""
    if len(questions) <= 20:
        return questions, False
    buckets: dict[str, dict] = {}
    order: list[str] = []
    for question in questions:
        key, q_type, ability, canonical = _question_cluster(question.get('normalized_question') or question.get('original_question') or '')
        # Pure acknowledgements and interviewer monologues should remain in the
        # transcript, rather than pretending to be test questions.
        if key == 'other' or not canonical:
            continue
        if key not in buckets:
            buckets[key] = {
                'asker_participant_id': question.get('asker_participant_id'),
                'original_question': canonical,
                'normalized_question': canonical,
                'question_type': q_type,
                'ability_key': ability,
                'intent': '从连续追问中归并的主问题；可在答卷中继续拆分。',
                'evidence_segment_ids': [],
                'answers': [],
            }
            order.append(key)
        target = buckets[key]
        target['evidence_segment_ids'].extend(question.get('evidence_segment_ids') or [])
        target['answers'].extend(question.get('answers') or [])
    merged = []
    for key in order:
        item = buckets[key]
        item['evidence_segment_ids'] = list(dict.fromkeys(item['evidence_segment_ids']))
        # A noisy question can attach the same segment multiple times; keeping
        # one copy preserves the evidence without making the document unreadable.
        seen_answers = set(); answers = []
        for answer in item['answers']:
            identity = tuple(answer.get('evidence_segment_ids') or [])
            if identity in seen_answers:
                continue
            seen_answers.add(identity); answers.append(answer)
        item['answers'] = answers
        merged.append(item)
    return (merged or questions), bool(merged)


def ai_exam_prompt(raw_text: str, job_context: str = "") -> str:
    """Model-neutral contract for reconstructing a noisy interview.

    The model discovers the question chain from evidence. Local keyword
    clusters are deliberately *not* passed in as mandatory anchors: they are a
    fallback, not the shape of the final paper.
    """
    context = f"\n岗位上下文（只用于理解考察目的，不能补写现场内容）：\n{job_context[:5000]}\n" if job_context else ""
    return f"""你是面试证据重建员。输入可能来自 ASR：所有发言可能被错标成同一个人，一段文字可能混有面试官提问、候选人回答和面试官解释。

你的目标不是逐句切分，而是还原整场面试的“判断链”和一份忠于现场的可编辑答卷。

必须遵守：
1. 先按语义识别面试阶段，再识别每个阶段中的主问题与连续追问。不要按问号数量切题；同一考点的追问必须合并。
2. 说话人标签不可信。根据“谁在询问候选人经历”“谁在回答自己的经历”“谁在介绍岗位”判断角色。
3. ANSWER 只包含候选人现场实际表达的语义。删除语气词、重复、口误和无意义寒暄，可以调整句序使语义完整，但禁止增加现场不存在的事实、数字、行动或结论。
4. 绝不能把面试官的提问、提示、复述、岗位科普或建议写入 ANSWER。
5. FOLLOW_UPS 保留关键追问；INTERVIEWER_SIGNAL 保留质疑、打断、反馈、预期校准和未获回答的点。
6. 候选人的反问也要形成 QUESTION，TYPE 写“反问”，ANSWER 留空，把面试官的回答放入 INTERVIEWER_SIGNAL。
7. 面试官对岗位、团队、业务或能力模型的有效介绍，另写成 ROLE_INSIGHT。它不是候选人的回答。
8. SOURCE_QUOTE 提供1-2个来自原稿的短语作为证据锚点，不要杜撰时间戳。
9. 通常形成 5-15 个主问题。宁可保留一个“待确认”，也不要错配说话人。
10. 只输出以下分隔格式；不要 JSON、代码块或额外前言。字段名保持英文，正文可以中文。

<<<OVERVIEW>>>
SUMMARY: 用2-4句话说明面试如何推进，只写可观察事实
JUDGMENT_PATH: 面试官依次验证了什么，用“→”连接
<<<END>>>

<<<STAGE>>>
NAME: 阶段名称
GOAL: 本阶段面试官要做出的判断
EVIDENCE: 本阶段最关键的现场信号
<<<END>>>

<<<QUESTION>>>
STAGE: 所属阶段名称
QUESTION: 归并后的清晰主问题
TYPE: 自我介绍/基础信息/项目深挖/动机/业务理解/AI认知/行为面试/反问/其他
ABILITY: 一个核心能力考点
INTENT: 面试官真正想验证什么
ANSWER:
清理语气词后的候选人真实回答，保留原意，可为多段；未可靠识别则留空
FOLLOW_UPS:
关键连续追问；没有则留空
INTERVIEWER_SIGNAL:
面试官明确反馈、质疑、提示或岗位讲解；没有则留空
SOURCE_QUOTE:
原稿中的1-2个短语，用“｜”分隔
<<<END>>>

<<<ROLE_INSIGHT>>>
TITLE: 一条岗位或业务认知
CONTENT: 面试官现场提供的信息
SOURCE_QUOTE: 原稿短语
<<<END>>>
{context}
逐字稿：
{raw_text}
"""


def _normalise_contract(text: str) -> str:
    normalized = (text or '').replace('：', ':')
    marker_aliases = {
        r"(?:概览|总览|overview)": "OVERVIEW",
        r"(?:阶段|stage)": "STAGE",
        r"(?:问题|题目|question)": "QUESTION",
        r"(?:岗位认知|岗位信息|role[_ ]?insight)": "ROLE_INSIGHT",
        r"(?:结束|end)": "END",
    }
    for source, target in marker_aliases.items():
        normalized = re.sub(rf"<<<\s*{source}\s*>>>", f"<<<{target}>>>", normalized, flags=re.I)
    aliases = {
        '概述': 'SUMMARY', '总结': 'SUMMARY', '判断路径': 'JUDGMENT_PATH',
        '阶段': 'STAGE', '阶段名称': 'NAME', '目标': 'GOAL', '阶段目标': 'GOAL',
        '阶段证据': 'EVIDENCE', '问题': 'QUESTION', '题目': 'QUESTION',
        '类型': 'TYPE', '考察类型': 'TYPE', '能力': 'ABILITY', '考察能力': 'ABILITY',
        '意图': 'INTENT', '面试官意图': 'INTENT', '回答': 'ANSWER',
        '候选人回答': 'ANSWER', '追问': 'FOLLOW_UPS', '面试官反馈': 'INTERVIEWER_SIGNAL',
        '面试官信号': 'INTERVIEWER_SIGNAL', '证据短语': 'SOURCE_QUOTE',
        '证据': 'SOURCE_QUOTE', '标题': 'TITLE', '内容': 'CONTENT',
    }
    for source, target in aliases.items():
        normalized = re.sub(rf"(?m)^{re.escape(source)}\s*:", f"{target}:", normalized)
    return normalized


def _block_field(block: str, name: str, next_names: tuple[str, ...]) -> str:
    if next_names:
        tail = "|".join(re.escape(x) for x in next_names)
        pattern = rf"^{name}:\s*(.*?)(?=^({tail}):|\Z)"
    else:
        pattern = rf"^{name}:\s*(.*)\Z"
    found = re.search(pattern, block, re.M | re.S | re.I)
    return found.group(1).strip() if found else ''


def parse_ai_exam_bundle(text: str, segments: list[dict], participants: list[dict]) -> dict:
    """Parse a provider-independent delimiter response with evidence links."""
    self_participant = next((p for p in participants if p.get('role') == 'self'), None)
    interviewer = next((p for p in participants if p.get('role') == 'interviewer'), None)
    if not self_participant:
        # Unlabelled ASR commonly produces one provisional ``unknown`` speaker.
        # The model can still rebuild a useful paper; the UI will ask the user
        # to confirm identity before review. Discarding every model question
        # here made exactly those messy transcripts return an empty result.
        self_participant = next(
            (p for p in participants if p.get('role') in {'unknown', 'other_candidate'}
             and (not interviewer or p.get('id') != interviewer.get('id'))),
            None,
        )
    if not self_participant:
        return {"questions": [], "stages": [], "role_insights": [], "overview": {}}

    def evidence_ids(value: str) -> list[int]:
        compact = re.sub(r"\s+", "", value or '')
        if len(compact) < 4:
            return []
        phrases = [re.sub(r"\s+", "", item) for item in re.split(r"[｜|]", value or '') if len(re.sub(r"\s+", "", item)) >= 4]
        if not phrases:
            phrases = [compact[:20]]
        matched = []
        for segment in segments:
            body = re.sub(r"\s+", "", segment.get('edited_text') or segment.get('raw_text') or '')
            if any(phrase in body for phrase in phrases):
                matched.append(segment['id'])
        if not matched:
            # Providers may lightly normalize an ASR quote. Fall back to
            # character n-gram overlap so evidence remains traceable without
            # requiring byte-for-byte copying.
            query_grams = {
                compact[index:index + 4]
                for index in range(max(0, len(compact) - 3))
            }
            ranked = []
            for segment in segments:
                body = re.sub(r"\s+", "", segment.get('edited_text') or segment.get('raw_text') or '')
                if len(body) < 4:
                    continue
                body_grams = {body[index:index + 4] for index in range(len(body) - 3)}
                overlap = len(query_grams & body_grams) / max(1, min(len(query_grams), 12))
                if overlap >= .25:
                    ranked.append((overlap, segment['id']))
            matched = [segment_id for _, segment_id in sorted(ranked, reverse=True)[:2]]
        return matched[:4]

    normalized = _normalise_contract(text)
    overview = {}
    overview_blocks = re.findall(r"<<<OVERVIEW>>>(.*?)<<<END>>>", normalized, re.S | re.I)
    if overview_blocks:
        block = overview_blocks[0]
        overview = {
            "summary": _block_field(block, "SUMMARY", ("JUDGMENT_PATH",)),
            "judgment_path": _block_field(block, "JUDGMENT_PATH", ()),
        }
    stages = []
    for block in re.findall(r"<<<STAGE>>>(.*?)<<<END>>>", normalized, re.S | re.I):
        name = _block_field(block, "NAME", ("GOAL", "EVIDENCE"))
        if name:
            stages.append({
                "name": name,
                "goal": _block_field(block, "GOAL", ("EVIDENCE",)),
                "evidence": _block_field(block, "EVIDENCE", ()),
            })
    questions = []
    for block in re.findall(r"<<<QUESTION>>>(.*?)<<<END>>>", normalized, re.S | re.I):
        order = ("STAGE", "QUESTION", "TYPE", "ABILITY", "INTENT", "ANSWER", "FOLLOW_UPS", "INTERVIEWER_SIGNAL", "SOURCE_QUOTE")
        field = lambda name: _block_field(block, name, tuple(order[order.index(name) + 1:]))
        question = field('QUESTION')
        answer = field('ANSWER')
        if not question or len(question) < 4:
            continue
        question = re.sub(r"^[（(]\s*(?:候选人)?(?:反问|追问)\s*[）)]\s*", "", question).strip()
        semantic_label = re.sub(r"^[（(\s]+|[）)\s]+$", "", question)
        if re.match(r"^面试官(?:反馈|补充|点评|建议|说明|科普)", semantic_label):
            # Some providers turn a commentary block into a synthetic question.
            # Commentary belongs to the previous answer signal or role insight,
            # never to the candidate's exam paper.
            continue
        q_type = field('TYPE') or '其他'
        ability = field('ABILITY') or '待确认'
        intent = field('INTENT') or '待确认'
        follow_ups = field('FOLLOW_UPS')
        signal = field('INTERVIEWER_SIGNAL')
        source_quote = field('SOURCE_QUOTE')
        question_evidence = evidence_ids(source_quote or question)
        answer_evidence = evidence_ids(answer) if answer else []
        questions.append({
            'asker_participant_id': interviewer.get('id') if interviewer else None,
            'original_question': question, 'normalized_question': question,
            'question_type': q_type, 'ability_key': ability,
            'intent': intent, 'evidence_segment_ids': question_evidence,
            'tags': {"stage": field('STAGE') or "待确认", "source_quote": source_quote},
            'answers': ([{
                'participant_id': self_participant['id'], 'is_self': True,
                'original_answer': answer, 'organized_answer': answer,
                'evidence_segment_ids': answer_evidence,
                'interviewer_signal': '\n'.join(x for x in (follow_ups, signal) if x),
            }] if answer and q_type != '反问' else []),
        })
    # Providers sometimes reorder topics by perceived importance. The evidence
    # anchors restore the actual interview sequence deterministically.
    questions = [
        item for _, item in sorted(
            enumerate(questions),
            key=lambda pair: (
                min(pair[1].get("evidence_segment_ids") or [10**9]),
                pair[0],
            ),
        )
    ]
    role_insights = []
    for block in re.findall(r"<<<ROLE_INSIGHT>>>(.*?)<<<END>>>", normalized, re.S | re.I):
        title = _block_field(block, "TITLE", ("CONTENT", "SOURCE_QUOTE"))
        content = _block_field(block, "CONTENT", ("SOURCE_QUOTE",))
        quote = _block_field(block, "SOURCE_QUOTE", ())
        if title and content:
            role_insights.append({"title": title, "content": content, "source_quote": quote,
                                  "evidence_segment_ids": evidence_ids(quote)})
    return {"overview": overview, "stages": stages, "questions": questions, "role_insights": role_insights}


def parse_ai_exam(text: str, segments: list[dict], participants: list[dict]) -> list[dict]:
    """Backward-compatible question-only parser."""
    return parse_ai_exam_bundle(text, segments, participants)["questions"]


_INTERVIEWER_LEAK_PATTERNS = (
    r"你(?:这边|能|会|觉得|给我|可以|是否|有没有)",
    r"您(?:这边|能|会|觉得|可以|是否|有没有)",
    r"我想(?:问|了解)一下你", r"给我(?:介绍|讲)一下",
    r"我们先(?:停|看|聊)", r"你可以考虑一下",
)


def validate_exam_bundle(bundle: dict, source_text: str = "") -> dict:
    """Provider-independent quality gate for adopting a model reconstruction."""
    questions = bundle.get("questions") or []
    issues = []
    fatal = []
    source_length = len((source_text or "").strip())
    minimum_questions = 7 if source_length >= 15000 else 5 if source_length >= 6000 else 3
    if not minimum_questions <= len(questions) <= 20:
        fatal.append(f"主问题数量异常：{len(questions)}")
    normalized = [re.sub(r"\W+", "", q.get("normalized_question") or q.get("original_question") or "") for q in questions]
    duplicate_count = len(normalized) - len(set(x for x in normalized if x))
    if duplicate_count:
        issues.append(f"存在 {duplicate_count} 个重复问题")
    scored = [q for q in questions if q.get("question_type") != "反问"]
    answered = [q for q in scored if any((a.get("organized_answer") or "").strip() for a in q.get("answers") or [])]
    answer_ratio = len(answered) / max(1, len(scored))
    if answer_ratio < .55:
        fatal.append(f"本人回答覆盖率过低：{answer_ratio:.0%}")
    contaminated = []
    for index, question in enumerate(questions, 1):
        for answer in question.get("answers") or []:
            value = answer.get("organized_answer") or ""
            hits = sum(bool(re.search(pattern, value)) for pattern in _INTERVIEWER_LEAK_PATTERNS)
            if hits >= 2:
                contaminated.append(index)
    if contaminated:
        fatal.append("疑似混入面试官话语：" + "、".join(f"Q{x}" for x in contaminated))
    missing_intent = sum(1 for q in questions if (q.get("intent") or "") in {"", "待确认"})
    if missing_intent > max(2, len(questions) // 3):
        issues.append("较多问题缺少面试官意图")
    evidence_ratio = sum(bool(q.get("evidence_segment_ids")) for q in questions) / max(1, len(questions))
    if evidence_ratio < .35:
        issues.append(f"证据锚点覆盖较低：{evidence_ratio:.0%}")
    answer_evidence = []
    misaligned = []
    for index, question in enumerate(scored, 1):
        q_ids = question.get("evidence_segment_ids") or []
        answer = next((item for item in question.get("answers") or [] if item.get("organized_answer")), {})
        a_ids = answer.get("evidence_segment_ids") or []
        if answer:
            answer_evidence.append(bool(a_ids))
        if q_ids and a_ids and abs(min(q_ids) - min(a_ids)) > 28:
            misaligned.append(index)
    answer_evidence_ratio = sum(answer_evidence) / max(1, len(answer_evidence))
    if answer_evidence and answer_evidence_ratio < .45:
        fatal.append(f"回答证据覆盖率过低：{answer_evidence_ratio:.0%}")
    elif answer_evidence and answer_evidence_ratio < .7:
        issues.append(f"回答证据覆盖偏低：{answer_evidence_ratio:.0%}")
    if misaligned:
        fatal.append("疑似问答跨段错配：" + "、".join(f"Q{x}" for x in misaligned))
    score = 1.0
    score -= .18 * len(fatal)
    score -= .07 * len(issues)
    score -= min(.18, duplicate_count * .06)
    return {
        "accepted": not fatal and score >= .72,
        "score": round(max(0.0, score), 2),
        "fatal": fatal,
        "issues": issues,
        "question_count": len(questions),
        "minimum_question_count": minimum_questions,
        "answer_ratio": round(answer_ratio, 3),
        "evidence_ratio": round(evidence_ratio, 3),
        "answer_evidence_ratio": round(answer_evidence_ratio, 3),
    }


def decompose(round_id: int, source_id: int):
    sources = {x['id']: x for x in interview_store.list_transcript_sources(round_id)}
    source = sources.get(source_id)
    if not source:
        raise ValueError('逐字稿资料不存在')
    existing_participants = interview_store.list_participants(round_id)
    confirmed_self_names = {
        re.split(r"[（(:]", (p.get('display_name') or p.get('participant_key') or ''))[0].strip()
        for p in existing_participants if p.get('role') == 'self' and p.get('confirmed')
    }
    participants = {}
    inferred_interviewer = None
    items=[]
    for raw in _transcript_lines(source.get('raw_text') or ''):
        timestamped = _TIMESTAMPED_SPEAKER.match(raw)
        match = timestamped or _SPEAKER.match(raw)
        label=(match.group('speaker') if match else 'unknown').strip()
        text=(match.group('text') if match else raw).strip()
        inferred_role = _role(label)
        if inferred_role == 'unknown' and any(label == name or label.startswith(name) for name in confirmed_self_names if name):
            inferred_role = 'self'
        if label not in participants:
            participants[label]=interview_store.upsert_participant(round_id, {
                'participant_key':label.lower().replace(' ','_'), 'display_name':label,
                'role':inferred_role, 'confidence':0.92 if match else 0.35, 'confirmed':False})
        participant = participants[label]
        if participant.get('role') == 'self' and _is_likely_interviewer_question(text):
            if inferred_interviewer is None:
                inferred_interviewer = interview_store.upsert_participant(round_id, {
                    'participant_key': 'inferred_interviewer', 'display_name': '疑似面试官',
                    'role': 'interviewer', 'confidence': 0.45, 'confirmed': False,
                    'metadata': {'inferred_from': 'question_pattern'},
                })
            participant = inferred_interviewer
        items.append({'participant_id':participant['id'], 'raw_text':text, 'confidence':0.92 if match else 0.35,
                      'metadata':{'speaker_label':label, 'timestamp': timestamped.group('timestamp') if timestamped else None}})
    interview_store.replace_segments(source_id,round_id,items)
    segments=interview_store.list_segments(round_id)
    self_ids={x['id'] for x in interview_store.list_participants(round_id) if x['role']=='self'}
    questions=[]; current=None
    for segment in segments:
        role=segment.get('role') or 'unknown'; text=segment.get('edited_text') or segment['raw_text']
        is_question=role=='interviewer' and bool(_QUESTION.search(text))
        if is_question:
            question_type, ability_key = _question_meta(text)
            current={'asker_participant_id':segment.get('participant_id'),'original_question':text,'normalized_question':text,'question_type':question_type,'ability_key':ability_key,'evidence_segment_ids':[segment['id']],'answers':[]}
            questions.append(current); continue
        if current and role in {'self','other_candidate'}:
            current['answers'].append({'participant_id':segment.get('participant_id'),'is_self':segment.get('participant_id') in self_ids,'original_answer':text,'organized_answer':text,'evidence_segment_ids':[segment['id']]})
    questions, merged_noisy_followups = _merge_noisy_followups(questions)
    interview_store.replace_exam(round_id,questions)
    return {
        'participants': interview_store.list_participants(round_id),
        'segments': interview_store.list_segments(round_id),
        'questions': interview_store.get_exam(round_id),
        'needs_confirmation': not bool(self_ids),
        'parser_note': '本地归并连续追问' if merged_noisy_followups else '规则切分',
    }
