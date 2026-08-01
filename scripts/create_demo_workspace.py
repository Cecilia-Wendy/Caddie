#!/usr/bin/env python3
"""Create a self-contained Caddie demo workspace with fictional data."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path


DEMO_NOTICE = "本工作区中的姓名、公司经历、项目、数字、岗位和面试记录均为虚构，仅用于产品演示。"
NOW = datetime.now().replace(microsecond=0)


def iso(days: int = 0, hours: int = 0) -> str:
    return (NOW + timedelta(days=days, hours=hours)).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description="创建 Caddie 全量虚拟测试数据")
    parser.add_argument("--data-dir", required=True, help="独立 demo 数据目录")
    parser.add_argument("--reset", action="store_true", help="重建已有 demo 数据库")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser().resolve()
    if data_dir == Path.home() / ".caddie":
        raise SystemExit("拒绝写入真实 Caddie 数据目录 ~/.caddie")
    db_path = data_dir / "caddie.db"
    if args.reset and data_dir.exists():
        shutil.rmtree(data_dir)
    if db_path.exists():
        print(f"Demo workspace already exists: {db_path}")
        return

    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["CADDIE_DATA_DIR"] = str(data_dir)
    app_dir = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(app_dir))
    import db  # noqa: PLC0415

    db.init_db()
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys = ON")

    def insert(table: str, **values):
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        clean = {k: v for k, v in values.items() if k in columns and v is not None}
        keys = ", ".join(clean)
        marks = ", ".join("?" for _ in clean)
        cur = conn.execute(
            f"INSERT INTO {table} ({keys}) VALUES ({marks})", tuple(clean.values())
        )
        return cur.lastrowid

    # Remove the intentionally tiny first-run sample; retain system expert seeds.
    for table in ("applications", "projects", "experiences"):
        conn.execute(f"DELETE FROM {table}")

    profile = {
        "name": "林知夏",
        "headline": "复旦大学管理科学硕士｜AI 产品与商业分析复合型候选人",
        "phone": "138-0000-2468",
        "email": "zhixia.lin@example.test",
        "location": "上海",
        "birth_year": "2001",
        "gender": "女",
        "target_roles": "AI 产品经理、策略产品经理、商业分析",
        "target_cities": "上海、杭州、深圳",
        "work_authorization": "中国大陆",
        "summary": "擅长把模糊业务问题拆成可验证的产品与数据方案；有互联网 AI 产品、咨询和消费行业分析经历。所有信息均为虚拟测试数据。",
        "education": [
            {
                "school": "复旦大学",
                "degree": "管理科学与工程 硕士",
                "start_date": "2024-09",
                "end_date": "2027-06",
                "location": "上海",
                "gpa": "3.72/4.0（前 15%）",
                "courses": ["机器学习", "运营管理", "因果推断", "产品创新"],
                "honors": ["研究生一等奖学金", "商业分析挑战赛全国二等奖"],
                "activities": ["产品创新协会副会长", "校友创业营产品负责人"],
            },
            {
                "school": "中山大学",
                "degree": "信息管理与信息系统 本科",
                "start_date": "2020-09",
                "end_date": "2024-06",
                "location": "广州",
                "gpa": "3.78/4.0（前 10%）",
                "courses": ["数据库", "统计学", "用户研究", "管理信息系统"],
                "honors": ["优秀毕业生", "国家奖学金"],
                "activities": ["学生咨询社项目部长"],
            },
        ],
        "skills": "SQL、Python、Tableau、Figma、Axure、A/B 测试、用户研究、PRD、英语 CET-6 612",
        "links": "作品集：https://demo.invalid/linzhixia；GitHub：https://github.invalid/linzhixia",
        "notice": DEMO_NOTICE,
    }
    settings = {
        "user_profile": json.dumps(profile, ensure_ascii=False),
        "demo_mode": "true",
        "demo_notice": DEMO_NOTICE,
        "onboarding_completed": "true",
        "workspace_name": "林知夏的求职工作台（虚拟演示）",
    }
    for key, value in settings.items():
        conn.execute(
            "INSERT OR REPLACE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
            (key, value, iso()),
        )

    experiences = [
        ("星河智能科技（虚构）", "AI 产品经理实习生", "2026-03", "2026-07", "上海"),
        ("远见咨询（虚构）", "数字化战略咨询实习生", "2025-07", "2025-11", "上海"),
        ("青禾消费（虚构）", "商业分析实习生", "2024-12", "2025-04", "广州"),
        ("复旦大学数据智能实验室（虚构）", "研究助理", "2024-09", "2026-02", "上海"),
        ("校园创业项目「拾光」", "联合创始人 / 产品负责人", "2023-03", "2024-05", "广州"),
    ]
    exp_ids = [
        insert("experiences", company=c, role=r, start_date=s, end_date=e, location=l,
               created_at=iso(-400), updated_at=iso(-2))
        for c, r, s, e, l in experiences
    ]

    projects = [
        (0, "企业知识助手 0-1 MVP", "8 周从需求研究到灰度上线，试点问答采纳率达到 71%",
         "Python, RAG, Figma, SQL", "AI产品,RAG,知识库,用户研究",
         """# 企业知识助手 0-1 MVP

## 背景与目标
售前团队每天重复查询产品参数和案例，平均找资料耗时 18 分钟。目标是在不牺牲引用可信度的前提下缩短查找时间。

## 我的职责
负责需求研究、产品方案、评测集设计、灰度运营；算法架构由算法同学负责，我不把模型训练记作个人贡献。

## 关键动作
- 访谈 12 名售前与 4 名产品专家，归纳 6 类高频任务和 3 类高风险回答。
- 设计“答案 + 原文引用 + 置信提示”交互及低置信度拒答策略。
- 与算法同学共建 180 题评测集，按正确性、可追溯性、完整性评分。
- 组织 30 人两轮灰度，基于日志把无答案问题回流为知识缺口。

## 结果
试点问题采纳率 71%，平均检索耗时由 18 分钟降至 4.6 分钟；数字来自两周灰度日志，样本 486 次查询。

## 证据边界
本人负责产品与评测，RAG 技术实现由 2 名算法工程师完成。"""),
        (0, "AI 回答质量评测体系", "建立 180 题黄金集和五维评分卡，使版本验收从主观讨论变为量化门槛",
         "Python, Excel, LLM Eval", "AI评测,黄金集,幻觉,质量体系",
         """# AI 回答质量评测体系

把业务问题按事实查询、方案推荐、流程指引分层，定义正确性、完整性、引用一致性、拒答合理性和表达清晰度五维指标。推动每次发布前跑离线集并抽样复核；发现“答案看似流畅但引用不支持”的主要风险后，增加引用一致性硬门槛。"""),
        (1, "连锁零售会员增长策略", "定位沉睡会员激活机会并设计三阶段增长路线图",
         "SQL, Tableau, 访谈", "咨询,会员增长,零售,数据分析",
         """# 连锁零售会员增长策略

## 情境
客户有 820 万注册会员，但月活率持续下降。

## 分析
清洗 24 个月交易与触达数据，按生命周期和品类偏好分群；访谈 8 位区域负责人，识别门店执行约束。

## 产出与结果
提出新客首月、成长期复购、沉睡召回三阶段方案。客户采纳沉睡召回试点，在 20 家门店四周复购率提升 4.2pp。本人负责数据分析与工作坊材料，不声称独立决定客户策略。"""),
        (1, "制造业销售数字化蓝图", "梳理 5 类角色与 17 个关键流程，形成 CRM 一期需求优先级",
         "Process Mining, Excel, PowerPoint", "咨询,CRM,流程,需求优先级",
         """# 制造业销售数字化蓝图

通过 15 场访谈绘制线索到回款流程，发现报价版本混乱和客户信息重复录入两类主要损耗。用价值、紧迫度、实施依赖三维评分，协助确定 12 项一期需求。"""),
        (2, "新品上市渠道诊断", "用门店与电商数据识别铺货覆盖不足，而非营销触达不足",
         "SQL, Python, Tableau", "消费品,渠道,上市复盘",
         """# 新品上市渠道诊断

合并 1,200 家门店周度动销、库存与活动数据，拆解曝光—到店—上架—动销漏斗。发现核心城市 A 类门店铺货率仅 63%，纠正团队最初“内容曝光不足”的判断。补货与陈列调整后，试点区域六周单店周销提升 18%。"""),
        (3, "对话式推荐可解释性研究", "设计 3 组实验验证解释长度与用户信任的非线性关系",
         "Python, R, 实验设计", "科研,推荐系统,可解释AI",
         """# 对话式推荐可解释性研究

负责文献综述、实验设计、问卷与统计分析。收集 312 份有效样本，发现中等长度解释在信任和决策效率上表现最好。研究为实验室内部工作论文，尚未正式发表。"""),
        (4, "校园闲置物品平台「拾光」", "带 5 人团队完成小程序 MVP，累计 2,300 名注册用户",
         "微信小程序, Figma, 用户运营", "创业,校园,交易平台,MVP",
         """# 校园闲置物品平台「拾光」

## 角色
联合创始人兼产品负责人，负责用户研究、路线图、交互和增长实验；开发由 2 名技术合伙人完成。

## 过程
从 46 次访谈中发现“交易安全”和“约时间成本”是关键阻碍，设计校园认证、信用记录和集中交付点。上线后用社团合作和毕业季专题冷启动。

## 结果
累计注册 2,300 人，发布 4,800 件商品，成交 1,160 单；因毕业后团队投入不足停止运营。"""),
        (4, "毕业季集中回收实验", "以 3 个线下交付点降低履约摩擦，成交转化提升 9pp",
         "用户研究, A/B Test, 运营", "增长实验,履约,校园",
         """# 毕业季集中回收实验

将“自由约见”与“固定交付点”进行分校区对照，固定点组从意向到成交的转化率为 34%，对照组为 25%。实验非随机分流，结论只作为方向性证据。"""),
    ]
    project_ids = []
    for exp_i, name, one_liner, tech, keywords, document in projects:
        project_ids.append(insert(
            "projects", experience_id=exp_ids[exp_i], name=name, one_liner=one_liner,
            document=document, technologies=tech, keywords=keywords,
            kind="work" if exp_i < 4 else "entrepreneurship",
            build_status="complete", created_at=iso(-300), updated_at=iso(-3),
        ))

    tracks = [
        ("字节跳动（演示）", "AI 产品经理-豆包企业服务", "AI 产品", "interview", "重点冲刺", 82, "high",
         "负责企业级 AI 助手需求分析、评测与迭代；要求有 AI 产品实习、数据分析和跨团队推动经验。"),
        ("蚂蚁集团（演示）", "策略产品经理-智能服务", "策略产品", "screening", "重点冲刺", 78, "high",
         "通过数据和实验提升智能客服解决率，能够定义指标、识别策略问题并推动算法与运营协作。"),
        ("腾讯（演示）", "产品培训生-企业产品", "产品经理", "applied", "稳健匹配", 74, "medium",
         "面向企业客户完成产品规划、需求管理、商业化分析和客户反馈闭环。"),
        ("美团（演示）", "商业分析师-到店事业群", "商业分析", "assessment", "能力迁移", 71, "medium",
         "负责经营分析、专题研究和策略落地，以 SQL 和商业判断支持业务决策。"),
        ("小红书（演示）", "产品经理-搜索与推荐", "产品经理", "saved", "探索机会", 68, "medium",
         "负责搜索体验、用户需求洞察与推荐策略协同，关注内容生态。"),
        ("星云科技（演示）", "AI 产品经理", "AI 产品", "offer", "保底选择", 76, "low",
         "从 0 到 1 建设行业知识助手，负责需求、评测和交付。"),
        ("澄海咨询（演示）", "商业分析顾问", "咨询", "rejected", "复盘样本", 64, "low",
         "围绕消费与零售行业开展市场研究、数据分析与客户汇报。"),
    ]
    track_ids, app_ids = [], []
    for i, (company, role, target, status, persona, readiness, priority, jd) in enumerate(tracks):
        tid = insert(
            "job_tracks", company=company, role=role, target=target, jd=jd, status=status,
            notes=f"{DEMO_NOTICE} 当前策略：{persona}", persona=persona, readiness=readiness,
            priority=priority, track_group="2027 秋招", apply_url="https://jobs.example.test/demo",
            company_type="互联网/科技", company_industry="AI 与数字服务", job_type="校招",
            created_at=iso(-35 + i), updated_at=iso(-i),
        )
        aid = insert(
            "applications", company=company, role=role, industry="互联网/咨询",
            applied_date=(NOW - timedelta(days=25 - i * 2)).date().isoformat(),
            status=status, source=["内推", "官网", "校园招聘", "实习转正"][i % 4],
            notes=f"虚拟投递记录；岗位匹配度 {readiness}/100", job_description=jd,
            gap_analysis="需继续补充技术原理深度、商业化判断和失败案例。",
            company_type="互联网/科技", company_industry="AI 与数字服务",
            job_type="校招", apply_url="https://jobs.example.test/demo", remark_tag=persona,
            evaluation=f"当前综合匹配度 {readiness}，建议围绕企业知识助手项目准备证据。",
            track_id=tid, created_at=iso(-30 + i), updated_at=iso(-i),
        )
        track_ids.append(tid)
        app_ids.append(aid)

    # Sources and their links provide provenance for projects and jobs.
    source_specs = [
        ("document", "企业知识助手灰度复盘（脱敏虚构）", "两周 486 次查询；采纳率 71%；平均耗时 4.6 分钟。", 0),
        ("spreadsheet", "AI 评测集结果摘要（虚构）", "180 道黄金集，五维评分；引用一致性是主要短板。", 0),
        ("document", "会员增长客户汇报节选（虚构）", "沉睡召回试点覆盖 20 家门店，四周复购率提升 4.2pp。", 1),
        ("transcript", "字节 AI 产品一面录音转写（虚构）", "围绕 RAG 产品、评测、失败案例和职业选择展开。", 0),
        ("job_description", "字节 AI 产品经理 JD（虚构）", tracks[0][7], 0),
        ("research", "企业级 AI 助手行业笔记（虚构）", "关注知识治理、权限、引用可信度、成本与评测闭环。", 0),
    ]
    source_ids = []
    for i, (stype, title, content, track_index) in enumerate(source_specs):
        sid = insert(
            "sources", source_type=stype, title=title, content=content,
            summary=content[:120], status="ready", track_id=track_ids[track_index],
            origin="demo_seed", content_hash=f"demo-source-{i}", content_date=iso(-20 + i)[:10],
            tags="虚拟数据,演示", confidence=0.92, pinned=1 if i in (0, 4) else 0,
            lang="zh-CN", ingested_at=iso(-10 + i), created_at=iso(-20), updated_at=iso(-2),
        )
        source_ids.append(sid)
    for sid, pid in zip(source_ids[:3], project_ids[:3]):
        insert("source_links", source_id=sid, entity_type="project", entity_id=pid,
               relation="evidence", reason="项目结果与过程证据", created_at=iso(-9))
    insert("source_links", source_id=source_ids[4], entity_type="track", entity_id=track_ids[0],
           relation="job_description", reason="目标岗位 JD", created_at=iso(-9))

    # Knowledge hierarchy: personal, professional, company and job-specific.
    folder_specs = [
        ("个人通用", None, "personal", "general", None),
        ("AI 产品", None, "professional", "ai_product", None),
        ("商业分析", None, "professional", "business_analysis", None),
        ("字节跳动（演示）", None, "company", "company", "字节跳动（演示）"),
        ("公司研究", track_ids[0], "track", "company_research", None),
        ("岗位理解", track_ids[0], "track", "role", None),
        ("专业准备", track_ids[0], "track", "professional", None),
        ("面试准备", track_ids[0], "track", "interview", None),
        ("复盘沉淀", track_ids[0], "track", "review", None),
    ]
    folder_ids = []
    for order, (name, tid, scope, domain, company) in enumerate(folder_specs):
        folder_ids.append(insert(
            "knowledge_folders", name=name, scope_type=scope, domain_key=domain,
            company=company, track_id=tid, sort_order=order * 10,
            created_at=iso(-30), updated_at=iso(-2),
        ))
    knowledge_specs = [
        ("三分钟自我介绍", 0, "个人通用", "表达", "熟练",
         "我叫林知夏，目前在复旦大学攻读管理科学硕士。我的核心特点是能把模糊业务问题转化为可验证的产品方案……"),
        ("RAG 产品经理知识地图", 1, "professional", "RAG", "掌握",
         "从用户任务、知识治理、召回与生成、引用、权限、评测、反馈闭环和成本八个模块理解 RAG 产品。"),
        ("AI 回答评测方法", 1, "professional", "AI评测", "熟练",
         "离线黄金集验证版本底线，在线行为指标验证真实价值，人工抽检识别自动指标覆盖不到的可信度风险。"),
        ("商业分析问题拆解", 2, "professional", "分析框架", "掌握",
         "先定义业务结果，再拆公式和漏斗，区分结构变化与效率变化，最后用实验或自然对照验证策略。"),
        ("字节跳动企业服务研究（虚构）", 3, "company", "公司研究", "了解",
         "演示用虚构材料：企业 AI 产品的核心竞争可能来自模型能力、知识治理、组织渗透和交付效率。"),
        ("目标岗位能力模型", 5, "track", "岗位理解", "掌握",
         "四项核心能力：用户问题定义、AI 能力边界判断、评测与数据闭环、跨算法和业务推动。"),
        ("RAG 高频追问题库", 7, "track", "面试准备", "掌握",
         "为什么用 RAG？评测集怎么构建？如何处理低置信度？产品经理与算法的边界是什么？"),
        ("一面复盘与改进", 8, "track", "复盘", "待加强",
         "优点：项目结构清晰、证据意识强。问题：召回指标与业务指标的连接解释不够；失败案例缺少当时决策依据。"),
    ]
    knowledge_ids = []
    for i, (title, folder_i, scope, topic, mastery, content) in enumerate(knowledge_specs):
        tid = track_ids[0] if scope == "track" else None
        knowledge_ids.append(insert(
            "knowledge_items", title=title, content=content, scope_type=scope,
            domain_key=folder_specs[folder_i][3], company="字节跳动（演示）" if scope == "company" else None,
            track_id=tid, topic=topic, mastery=mastery, source_type="demo_seed",
            source_ref_id=source_ids[min(i, len(source_ids)-1)], folder_id=folder_ids[folder_i],
            status="active", created_at=iso(-18 + i), updated_at=iso(-i),
        ))

    followup_specs = [
        (0, "为什么企业知识助手选择 RAG，而不是微调？", "知识更新频繁且必须提供原文引用；RAG 更适合快速更新和权限控制。", "solid", "strategy"),
        (0, "71% 采纳率的口径是什么？", "用户点击“有帮助”或复制答案，分母为产生有效回答的查询；需同时披露覆盖率。", "solid", "metric"),
        (0, "你与算法工程师的边界是什么？", "我定义任务、交互、评测和验收；算法同学负责检索链路、模型与工程优化。", "solid", "ownership"),
        (0, "灰度中最大的失败是什么？", "第一版只看答案正确率，忽略引用是否支持结论，导致看似流畅但证据错位。", "needs_work", "reflection"),
        (2, "如何排除季节性对复购率的影响？", "", "todo", "methodology"),
        (6, "创业项目为什么停止？", "团队毕业后的时间承诺下降，单位履约成本又未跑通；我们选择停止而非虚增规模。", "solid", "reflection"),
    ]
    for i, (pi, question, answer, status, category) in enumerate(followup_specs):
        insert(
            "followups", project_id=project_ids[pi], question=question, answer=answer,
            status=status, category=category, asked_count=i % 3, origin="demo_seed",
            source_track_id=track_ids[0], kind="deep_dive" if category != "reflection" else "reflection",
            category_source="seed", created_at=iso(-16 + i), updated_at=iso(-i),
        )

    gap_specs = [
        ("AI 技术", "能解释 RAG 召回、重排、上下文构建及常见失败模式", "产品指标熟，技术链路深度一般", "high", "knowledge"),
        ("商业化", "能判断企业 AI 的定价、交付成本与续费驱动", "缺少真实商业化项目", "high", "knowledge"),
        ("项目复盘", "准备一个有具体错误决策和修正过程的失败案例", "已有素材但决策依据不够具体", "medium", "document"),
        ("表达", "90 秒内回答项目难点且先给结论", "容易先铺背景", "medium", "practice"),
        ("公司研究", "理解目标业务的客户、场景与竞争差异", "已有基础资料，需补近期产品证据", "low", "research"),
    ]
    for dimension, requirement, mine, severity, plan_type in gap_specs:
        insert(
            "track_gaps", track_id=track_ids[0], dimension=dimension, requirement=requirement,
            my_status=mine, severity=severity, plan_type=plan_type, status="open",
            note="虚拟差距项，用于演示准备闭环", created_at=iso(-12), updated_at=iso(-2),
        )

    assets = [
        ("self_intro", track_ids[0], None, "字节 AI 产品 90 秒自我介绍",
         "我叫林知夏，目前在复旦大学读管理科学硕士。过去两年我持续在做一件事：把模糊业务问题转化为可验证的产品和数据方案……", "active"),
        ("project_story", track_ids[0], project_ids[0], "企业知识助手 STAR 话术",
         "情境：售前找资料平均需要 18 分钟。任务：在 8 周内验证知识助手能否可信地缩短检索时间……", "active"),
        ("job_research", track_ids[0], None, "目标岗位研究摘要",
         "岗位核心不是简单做聊天界面，而是把模型能力嵌入企业知识工作流，并对可信度和业务价值负责。", "active"),
        ("cover_letter", track_ids[1], None, "蚂蚁策略产品申请动机",
         "我希望把 AI 产品评测和商业分析经验用于智能服务的策略迭代。", "draft"),
    ]
    for i, (atype, tid, pid, title, body, status) in enumerate(assets):
        insert(
            "assets", asset_type=atype, track_id=tid, project_id=pid, title=title, body=body,
            provenance_json=json.dumps({"source": "demo_seed", "project_ids": [pid] if pid else [], "notice": DEMO_NOTICE}, ensure_ascii=False),
            version=1, status=status, derived_from_json="[]", created_at=iso(-8 + i), updated_at=iso(-i),
        )

    # Resume version metadata uses obviously non-real demo paths.
    for i, (tid, name, status, summary) in enumerate([
        (track_ids[0], "V3_AI产品_字节", "submitted", "突出评测体系与企业知识助手，弱化纯咨询表达"),
        (track_ids[1], "V2_策略产品_蚂蚁", "active", "强化数据实验与跨团队推动"),
        (track_ids[3], "V1_商业分析_美团", "draft", "突出 SQL、经营拆解与策略落地"),
    ]):
        insert(
            "resume_versions", track_id=tid, version_name=name,
            docx_path=f"{data_dir}/demo_files/{name}.docx", pdf_path=f"{data_dir}/demo_files/{name}.pdf",
            extracted_text=f"林知夏｜{profile['headline']}｜{DEMO_NOTICE}", status=status,
            change_summary=summary, submitted_at=iso(-6 + i) if status == "submitted" else None,
            created_at=iso(-10 + i), updated_at=iso(-i),
        )

    # Calendar, opportunities and action plans cover past, present and upcoming states.
    calendar_specs = [
        (track_ids[0], "字节 AI 产品二面", "interview", 2, 14, "blue", "scheduled"),
        (track_ids[1], "蚂蚁在线测评截止", "deadline", 1, 21, "orange", "open"),
        (track_ids[0], "完成 RAG 技术补课", "prep", 0, 19, "green", "in_progress"),
        (None, "本周投递复盘", "review", 4, 20, "purple", "scheduled"),
    ]
    for tid, title, item_type, days, hour, color, status in calendar_specs:
        insert(
            "career_calendar_items", track_id=tid, title=title, item_type=item_type,
            starts_at=iso(days, hour - NOW.hour), duration_minutes=60, color=color,
            target_count=1, status=status, notes="虚拟日程", created_at=iso(-7), updated_at=iso(),
        )
    for i, tid in enumerate(track_ids[:5]):
        oid = insert(
            "job_opportunities", company=tracks[i][0], role=tracks[i][1], direction=tracks[i][2],
            company_industry="AI 与数字服务", company_type="互联网/科技", batch_type="秋招",
            status=tracks[i][3], priority=tracks[i][6], fit_score=tracks[i][5],
            source_type="demo", source_title="虚拟校招机会库", source_url="https://jobs.example.test/list",
            apply_url="https://jobs.example.test/demo", location="上海/杭州",
            deadline_date=(NOW + timedelta(days=8 + i * 4)).date().isoformat(),
            deadline_type="明确截止", flow_days=21, buffer_days=3, jd=tracks[i][7],
            notes=DEMO_NOTICE, evidence="虚构 JD，仅用于功能测试", track_id=tid,
            application_id=app_ids[i], created_at=iso(-20), updated_at=iso(-i),
        )
        insert(
            "opportunity_rules", opportunity_id=oid, rule_status="confirmed",
            early_batch_impact="中", locks_choice=0, multi_apply_allowed=1, cooldown_days=30,
            rolling_review=1, referral_required=0, resume_editable=1,
            assessment_trigger="投递后 48 小时内", evidence="虚构规则", created_at=iso(-19), updated_at=iso(-2),
        )
        insert(
            "opportunity_plan_items", opportunity_id=oid, title="完成岗位定制简历",
            action_type="resume", due_date=(NOW + timedelta(days=3 + i)).date().isoformat(),
            status="done" if i == 0 else "todo", notes="虚拟行动项",
            calendar_scope="career", created_at=iso(-6), updated_at=iso(-1),
        )

    # Legacy interview + rich Interview v2 evidence chain.
    legacy_interview_id = insert(
        "interviews", application_id=app_ids[0], round_number=1, round_type="业务一面",
        interview_date=iso(-5), feedback="项目清晰，技术深度需加强",
        ai_analysis="回答有事实边界意识；RAG 召回与重排解释偏表层。",
        outcome="passed", duration_minutes=48, location="线上",
        meeting_link="https://meeting.example.test/demo", notes="虚拟面试记录",
        created_at=iso(-6), updated_at=iso(-4),
    )
    review_id = insert(
        "interview_reviews", scope="track", track_id=track_ids[0],
        title="字节 AI 产品一面复盘（虚拟）", role_type="AI 产品经理",
        round="业务一面", source_text=source_specs[3][2],
        summary="项目故事可信，但技术机制与失败决策需要更具体。",
        prediction_json=json.dumps({"next_round_probability": 0.72}, ensure_ascii=False),
        report_knowledge_id=knowledge_ids[-1], tags="AI产品,RAG,项目深挖",
        created_at=iso(-4), updated_at=iso(-3),
    )
    questions = [
        ("请介绍你做的企业知识助手。", "从问题、职责、动作和结果展开。", "验证候选人是否真正负责过 AI 产品闭环", "low", "项目深度"),
        ("为什么选择 RAG？你们怎么做评测？", "知识更新与引用要求决定 RAG；用 180 题黄金集做五维评测。", "判断技术边界和质量意识", "medium", "AI产品"),
        ("如果重做一次，你会先改什么？", "第一周就建立引用一致性门槛，而不是只看答案正确率。", "反思与迭代能力", "medium", "复盘"),
    ]
    for order, (q, answer, intent, risk, ability) in enumerate(questions, 1):
        insert(
            "interview_questions", review_id=review_id, track_id=track_ids[0],
            question_order=order, question=q, answer=answer, intent=intent,
            answer_review="结论明确；可增加当时的错误判断和日志证据。",
            better_answer=answer + " 我会补充具体指标定义和取舍依据。",
            risk_level=risk, ability=ability, tags="虚拟,一面",
            created_at=iso(-4), updated_at=iso(-3),
        )

    round_id = insert(
        "interview_rounds", track_id=track_ids[0], round_number=1, round_name="业务一面",
        round_type="business", interview_mode="video", language="zh-CN",
        scheduled_at=iso(-5), started_at=iso(-5), ended_at=iso(-5, 1),
        status="reviewed", notes=f"关联 legacy interview {legacy_interview_id}；{DEMO_NOTICE}",
        created_at=iso(-6), updated_at=iso(-2),
    )
    interviewer_id = insert(
        "interview_participants", round_id=round_id, participant_key="interviewer_1",
        display_name="陈老师（虚构）", role="interviewer", organization_role="AI 产品负责人",
        confidence=0.8, confirmed=1, metadata_json="{}", created_at=iso(-5), updated_at=iso(-4),
    )
    self_id = insert(
        "interview_participants", round_id=round_id, participant_key="candidate",
        display_name="林知夏", role="candidate", organization_role="候选人",
        confidence=1.0, confirmed=1, metadata_json="{}", created_at=iso(-5), updated_at=iso(-4),
    )
    transcript_id = insert(
        "transcript_sources", round_id=round_id, source_kind="audio_transcript",
        title="字节 AI 产品一面逐字稿（虚构）",
        raw_text="面试官：请介绍企业知识助手。\\n林知夏：这个项目解决售前找资料慢的问题……",
        content_hash="demo-transcript-001", locked=1,
        metadata_json=json.dumps({"notice": DEMO_NOTICE}, ensure_ascii=False),
        created_at=iso(-5), updated_at=iso(-4),
    )
    segments = [
        (interviewer_id, "请介绍一下你做的企业知识助手。", 0, 9000),
        (self_id, "这个项目解决售前找资料平均需要十八分钟的问题。我负责需求研究、产品方案、评测和灰度。", 10000, 38000),
        (interviewer_id, "为什么选择 RAG？评测怎么做？", 39000, 48000),
        (self_id, "知识更新频繁并且必须引用原文，所以选择 RAG。我们建立了一百八十题黄金集。", 49000, 79000),
    ]
    segment_ids = []
    for order, (pid, text, start_ms, end_ms) in enumerate(segments, 1):
        segment_ids.append(insert(
            "transcript_segments", source_id=transcript_id, round_id=round_id,
            segment_order=order, participant_id=pid, raw_text=text, edited_text=text,
            start_ms=start_ms, end_ms=end_ms, confidence=0.96, confirmed=1,
            metadata_json="{}", created_at=iso(-5), updated_at=iso(-4),
        ))
    exam_qid = insert(
        "exam_questions", round_id=round_id, question_order=1,
        asker_participant_id=interviewer_id, original_question=questions[0][0],
        normalized_question="请用结构化方式介绍企业知识助手项目",
        question_type="project_deep_dive", ability_key="product_ownership",
        intent="验证职责边界与结果可信度", evaluation_criteria="背景、职责、动作、结果、边界",
        difficulty="medium", evidence_segment_ids_json=json.dumps([segment_ids[0]]),
        tags="项目深挖,AI产品", confirmed=1, status="reviewed",
        created_at=iso(-5), updated_at=iso(-3),
    )
    answer_id = insert(
        "question_answers", question_id=exam_qid, participant_id=self_id, is_self=1,
        original_answer=segments[1][1], organized_answer="售前检索慢；我负责需求、产品、评测和灰度；最终采纳率 71%。",
        evidence_segment_ids_json=json.dumps([segment_ids[1]]), interviewer_signal="继续追问技术选择",
        confirmed=1, created_at=iso(-5), updated_at=iso(-3),
    )
    insert(
        "question_reviews", question_id=exam_qid, answer_id=answer_id,
        directness_score=4, structure_score=4, evidence_score=3, relevance_score=5,
        credibility_score=5, overall_score=4.2, strengths="职责边界清晰，结果有口径",
        weaknesses="未先解释核心产品判断", better_approach="先给一句话结论，再按问题—判断—行动—结果展开",
        better_answer="我负责把售前知识检索问题转化为可验证的 RAG 产品方案，并通过评测和灰度证明价值。",
        evidence_segment_ids_json=json.dumps([segment_ids[1]]), inference_level="evidence_based",
        status="confirmed", created_at=iso(-4), updated_at=iso(-3),
    )
    insert(
        "interview_predictions", round_id=round_id, label="进入下一轮", probability=0.72,
        confidence="medium", positive_evidence_json=json.dumps(["项目真实", "产品闭环"]),
        negative_evidence_json=json.dumps(["技术机制深度不足"]),
        uncertainty_json=json.dumps(["岗位团队偏好未知"]), model_provider="demo",
        model_name="seed", prompt_version="demo-v1", created_at=iso(-3),
    )
    insert(
        "interview_outcomes", round_id=round_id, actual_result="passed",
        result_at=iso(-2), evidence_type="email", evidence_text="虚构：恭喜进入下一轮",
        user_note="二面重点补技术机制与商业化", calibration_note="预测方向正确",
        created_at=iso(-2), updated_at=iso(-2),
    )
    insert(
        "review_actions", round_id=round_id, question_id=exam_qid,
        action_type="practice", target_type="knowledge_item", target_id=knowledge_ids[1],
        title="补强 RAG 技术链路解释", detail="能用两分钟说明召回、重排、生成、引用和拒答",
        priority="high", status="in_progress", proposed_payload_json="{}",
        created_at=iso(-3), updated_at=iso(-1),
    )

    # Work journal, feedback memory, career facts, chats, agent history.
    for log_date, exp_i, content, project_i in [
        ("2026-06-18", 0, "灰度用户反馈答案可信，但希望一键打开原文位置。", 0),
        ("2026-06-24", 0, "发现引用与答案结论不一致，决定新增引用一致性指标。", 1),
        ("2025-09-12", 1, "客户工作坊确认沉睡会员是优先试点人群。", 2),
    ]:
        wid = insert("work_logs", log_date=log_date, experience_id=exp_ids[exp_i],
                     content=content, created_at=iso(-40))
        insert("work_log_project_links", work_log_id=wid, project_id=project_ids[project_i],
               relation="evidence", created_at=iso(-39))
    for scope, scope_id, content, category, strength in [
        ("global", None, "所有数字必须说明口径和样本范围，不能把团队结果写成个人结果。", "事实边界", 5),
        ("track", track_ids[0], "回答 AI 产品问题时先讲用户价值，再讲技术实现。", "表达偏好", 4),
        ("project", project_ids[0], "始终明确算法架构由算法同学负责。", "贡献边界", 5),
    ]:
        insert(
            "feedback_notes", scope=scope, scope_id=scope_id, note_type="constraint",
            content=content, category=category, polarity="positive", strength=strength,
            directive=content, status="active", impact_json="{}",
            created_at=iso(-15), updated_at=iso(-2),
        )
    fact_specs = [
        ("profile", 0, "target_role", "AI 产品经理 / 策略产品经理"),
        ("project", project_ids[0], "metric_adoption_rate", "71%，分母为产生有效回答的查询"),
        ("project", project_ids[0], "ownership_boundary", "产品、评测和灰度由本人负责；算法实现由算法同学负责"),
        ("profile", 0, "location_preference", "上海、杭州、深圳"),
    ]
    for subject_type, subject_id, predicate, value in fact_specs:
        insert(
            "career_facts", subject_type=subject_type, subject_id=subject_id,
            scope_type="global", scope_id=0, predicate=predicate, value_text=value,
            state="confirmed", confidence=1.0,
            evidence_json=json.dumps(["demo_seed"], ensure_ascii=False),
            provenance_json=json.dumps({"notice": DEMO_NOTICE}, ensure_ascii=False),
            created_by="demo_seed", created_at=iso(-20), updated_at=iso(-2),
            confirmed_at=iso(-20),
        )

    session_id = insert(
        "sessions", title="字节 AI 产品面试准备", folder="岗位准备",
        created_at=iso(-4), updated_at=iso(-1),
    )
    for role, content, hours in [
        ("user", "帮我检查企业知识助手项目还有哪些高风险追问。", -6),
        ("assistant", "目前最高风险有三项：71% 采纳率口径、RAG 技术选择、你与算法同学的贡献边界。", -6),
        ("user", "先练贡献边界。", -5),
        ("assistant", "请用 45 秒回答：这个项目中，哪些是你亲自完成的，哪些是团队共同完成的？", -5),
    ]:
        insert("chat_messages", session_id=session_id, role=role, content=content, created_at=iso(0, hours))

    task_id = insert(
        "agent_tasks", task_type="knowledge_review", title="补强 RAG 技术知识",
        instruction="基于当前岗位和一面反馈，检查 RAG 知识地图并提出补充建议。",
        object_type="job_track", object_id=track_ids[0], track_id=track_ids[0],
        company=tracks[0][0], status="completed", priority="high",
        assigned_expert="knowledge_coach", context_json="{}",
        result_summary="提出召回、重排、权限和评测四项补强建议。",
        created_by="demo_seed", created_at=iso(-3), updated_at=iso(-2),
        completed_at=iso(-2), execution_owner="local", actor_type="system", actor_key="demo",
    )
    run_id = insert(
        "agent_runs", task_id=task_id, run_type="knowledge_review",
        expert_key="knowledge_coach", model_profile="writing", model_name="demo-seed",
        status="completed", attempt=1, input_json="{}", output_json="{}",
        summary="RAG 知识地图补强建议已生成（虚拟运行）。",
        started_at=iso(-3), completed_at=iso(-2), created_at=iso(-3), updated_at=iso(-2),
        actor_type="system", actor_key="demo",
    )
    insert(
        "agent_events", task_id=task_id, run_id=run_id, sequence=1,
        event_type="completed", label="知识审阅完成", detail="识别 4 个补强点",
        status="completed", payload_json="{}", created_at=iso(-2),
    )

    conn.commit()
    counts = {}
    for table in (
        "experiences", "projects", "job_tracks", "applications", "sources",
        "knowledge_items", "followups", "track_gaps", "assets", "resume_versions",
        "job_opportunities", "career_calendar_items", "interview_rounds",
        "exam_questions", "chat_messages", "career_facts",
    ):
        counts[table] = conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
    conn.close()

    manifest = {
        "workspace": "Caddie Demo",
        "fictional_candidate": "林知夏",
        "created_at": iso(),
        "database": str(db_path),
        "notice": DEMO_NOTICE,
        "counts": counts,
    }
    (data_dir / "DEMO_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
