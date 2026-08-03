/* Caddie v2 interview workspace.  This file keeps the high-frequency work
 * inside a job track; the global interview page remains an archive view. */
(function () {
  const I = window.CaddieInterview = window.CaddieInterview || {};
  // Used by UI smoke tests to distinguish script-load failures from page state.
  document.documentElement.dataset.caddieInterview = 'loaded';
  const bridge = () => window.CaddieBridge || {};
  const ivApi = (...args) => bridge().api(...args);
  const ivToast = (...args) => bridge().toast?.(...args);
  const ivOpenGen = (...args) => bridge().openGen?.(...args);
  const ivCloseGen = (...args) => bridge().closeGen?.(...args);
  const ivOpenBig = (...args) => bridge().openBig?.(...args);
  const ivCloseBig = (...args) => bridge().closeBig?.(...args);
  const ivValue = (...args) => bridge().gv?.(...args);
  const growthValue = (id) => {
    const element = document.getElementById(id);
    return element && 'value' in element ? String(element.value || '').trim() : '';
  };
  const state = () => {
    const appState = window.CaddieState;
    if (!appState) return { trackId: null, rounds: [], selectedRoundId: null, tab: 'overview', workspace: null, review: null, exam: [], sources: [], participants: [], tasks: [], busy: false };
    appState.iv = appState.iv || { trackId: null, rounds: [], selectedRoundId: null, tab: 'overview', workspace: null, review: null, exam: [], sources: [], participants: [], tasks: [], busy: false };
    return appState.iv;
  };
  const $ = (s) => document.querySelector(s);
  const escape = (v) => window.esc ? window.esc(v == null ? '' : String(v)) : String(v || '');
  const markdown = (v) => window.md ? window.md(v || '') : escape(v);
  const coachAvatar = () => window.agentExpertAvatar
    ? window.agentExpertAvatar('interview_coach')
    : '<span class="iv-coach-avatar-fallback"><img src="/static/caddie-mascot.png?v=20260728-3" alt="Caddie 面试教练"></span>';
  const label = (round) => `第 ${round.round_number} 轮${round.round_name ? ' · ' + round.round_name : round.round_type ? ' · ' + round.round_type : ''}`;
  const phase = (round) => round.actual_result === 'cancelled' ? '已取消' : ({ scheduled: '待准备', preparing: '准备中', interviewed: '待整理', processing: '拆解中', review_ready: '可复盘', awaiting_result: '等待结果', completed: '已结束', processing_failed: '需重试' }[round.status] || round.status || '待准备');
  const tabLabel = { overview: '本轮概览', prepare: '面前准备', source: '面后材料', review: '复盘报告', outcome: '结果记录' };
  const tabHint = { overview: '查看状态和下一步', prepare: '编辑本轮文档组', source: '整理证据与答卷', review: '沉淀结论和行动', outcome: '记录真实反馈' };
  const sourceKindLabel = { agent_intake: 'Agent 输入', paste: '粘贴文本', transcript: '面试逐字稿', feedback: '反馈', note: '补充记录' };
  const backgroundJobKey = 'caddie_interview_background_jobs_v1';
  let backgroundJobTimer = null;

  function readBackgroundJobs() {
    try {
      const jobs = JSON.parse(localStorage.getItem(backgroundJobKey) || '[]');
      return Array.isArray(jobs) ? jobs : [];
    } catch (_) { return []; }
  }

  function writeBackgroundJobs(jobs) {
    const retained = (jobs || [])
      .sort((a, b) => Number(b.createdAt || 0) - Number(a.createdAt || 0))
      .slice(0, 20);
    try { localStorage.setItem(backgroundJobKey, JSON.stringify(retained)); } catch (_) {}
    return retained;
  }

  function activeQuestionJob(qid) {
    return readBackgroundJobs().find(job =>
      Number(job.questionId) === Number(qid)
      && ['queued', 'active'].includes(job.status)
    );
  }

  function backgroundJobPanelHtml(jobs) {
    const visible = jobs.filter(job =>
      ['queued', 'active', 'failed'].includes(job.status) || !job.dismissed
    ).slice(0, 4);
    if (!visible.length) return '';
    return `<section class="iv-global-jobs">
      <header><div><span>后台任务</span><b>${visible.some(job => ['queued','active'].includes(job.status)) ? 'Caddie 正在工作' : '最近任务'}</b></div><button onclick="CaddieInterview.dismissFinishedJobs()" title="清除已完成任务">×</button></header>
      <div>${visible.map(job => {
        const active = ['queued', 'active'].includes(job.status);
        const failed = job.status === 'failed';
        const progress = Math.max(4, Math.min(100, Number(job.progress || (active ? 8 : 100))));
        return `<article class="${failed ? 'failed' : job.status === 'completed' ? 'done' : ''}">
          <div class="iv-global-job-copy"><span></span><div><b>${escape(job.label || job.title || '正在处理')}</b><small>${escape(job.detail || (active ? '可继续浏览其他页面' : failed ? '任务没有完成' : '结果已保存'))}</small></div></div>
          <div class="iv-global-job-meter"><i style="width:${progress}%"></i></div>
          <div class="iv-global-job-foot"><strong>${failed ? '失败' : active ? progress + '%' : '已完成'}</strong>${job.questionId ? `<button onclick="CaddieInterview.openBackgroundQuestion(${job.questionId})">${active ? '查看题目' : '查看结果'} →</button>` : job.analysisId ? `<button onclick="CaddieInterview.openGrowthAnalysisPage(${job.analysisId})">${active ? '查看进度' : '查看结果'} →</button>` : ''}</div>
        </article>`;
      }).join('')}</div>
    </section>`;
  }

  I.paintBackgroundJobs = function () {
    let host = document.getElementById('ivGlobalJobCenter');
    const html = backgroundJobPanelHtml(readBackgroundJobs());
    if (!html) {
      host?.remove();
      return;
    }
    if (!host) {
      host = document.createElement('div');
      host.id = 'ivGlobalJobCenter';
      document.body.appendChild(host);
    }
    host.innerHTML = html;
  };

  I.dismissFinishedJobs = function () {
    const jobs = readBackgroundJobs().map(job => (
      ['queued', 'active'].includes(job.status) ? job : {...job, dismissed:true}
    ));
    writeBackgroundJobs(jobs);
    I.paintBackgroundJobs();
  };

  I.openBackgroundQuestion = function (qid) {
    const job = readBackgroundJobs().find(item => Number(item.questionId) === Number(qid));
    state().questionReturn = job?.returnContext || state().questionReturn || {type:'bank', label:'返回真题题库'};
    I.openLibraryQuestion(Number(qid));
  };

  I.openBackgroundGrowth = function (analysisId) {
    I.openGrowthAnalysisPage(Number(analysisId));
  };

  I.registerBackgroundJob = function (task, question, returnContext) {
    const jobs = readBackgroundJobs().filter(job => Number(job.taskId) !== Number(task.id));
    jobs.unshift({
      taskId: Number(task.id),
      questionId: Number(question?.id || 0),
      title: task.title || '面试真题深度批改',
      label: task.current_label || '深度批改已进入后台队列',
      detail: task.current_detail || '可以离开当前页面，完成后会通知你',
      status: task.status || 'queued',
      progress: Number(task.progress || 8),
      returnContext: returnContext || {type:'bank', label:'返回真题题库'},
      createdAt: Date.now(),
      notified: false,
      dismissed: false,
    });
    writeBackgroundJobs(jobs);
    I.paintBackgroundJobs();
    I.startBackgroundJobPolling();
  };

  I.registerGrowthBackgroundJob = function (task, analysisId, title) {
    const jobs = readBackgroundJobs().filter(job => Number(job.taskId) !== Number(task.id));
    jobs.unshift({
      taskId: Number(task.id), analysisId: Number(analysisId),
      title: title || '综合分析与训练',
      label: task.current_label || '正在汇总历史面试',
      detail: task.current_detail || '可以离开页面，完成后会通知你',
      status: task.status || 'queued', progress: Number(task.progress || 4),
      createdAt: Date.now(), notified: false, dismissed: false,
    });
    writeBackgroundJobs(jobs); I.paintBackgroundJobs(); I.startBackgroundJobPolling();
  };

  I.pollBackgroundJobs = async function () {
    const jobs = readBackgroundJobs();
    const active = jobs.filter(job => ['queued', 'active'].includes(job.status));
    if (!active.length) {
      I.paintBackgroundJobs();
      return;
    }
    await Promise.all(active.map(async job => {
      try {
        const result = await ivApi('/v2/interview-tasks/' + job.taskId);
        const task = result.data || {};
        job.status = task.status || job.status;
        job.progress = Number(task.progress || job.progress || 8);
        job.label = task.current_label || task.result_summary || job.label;
        job.detail = task.current_detail || job.detail;
        job.updatedAt = Date.now();
      } catch (error) {
        job.pollError = error?.message || '暂时无法读取任务状态';
      }
    }));
    for (const job of jobs) {
      if (!['completed', 'review', 'failed', 'cancelled'].includes(job.status) || job.notified) continue;
      job.notified = true;
      const succeeded = ['completed', 'review'].includes(job.status);
      const growth = !!job.analysisId;
      bridge().notify?.(
        succeeded ? (growth ? '综合分析与训练已完成' : '深度批改已完成') : (growth ? '综合分析未完成' : '深度批改未完成'),
        succeeded ? (growth ? '六份可编辑文档已保存。' : '结果已经保存，可以随时返回题目查看。') : (job.detail || '请打开任务后重试。'),
        {
          type: succeeded ? 'success' : 'error',
          persist: true,
          actionLabel: succeeded ? '查看批改' : '查看任务',
          action: growth ? `CaddieInterview.openGrowthAnalysisPage(${Number(job.analysisId)})` : `CaddieInterview.openBackgroundQuestion(${Number(job.questionId)})`,
        }
      );
      if (
        succeeded
        && state().questionPageActive
        && Number(state().activeQuestion?.id) === Number(job.questionId)
        && !job.pageRefreshed
      ) {
        job.pageRefreshed = true;
        setTimeout(() => I.openLibraryQuestion(Number(job.questionId)), 0);
      }
    }
    writeBackgroundJobs(jobs);
    I.paintBackgroundJobs();
  };

  I.startBackgroundJobPolling = function () {
    if (backgroundJobTimer) return;
    I.pollBackgroundJobs();
    backgroundJobTimer = window.setInterval(I.pollBackgroundJobs, 1800);
  };

  function reviewAgentMessage(message, index) {
    const content = String(message?.content || '').trim();
    if (message?.role !== 'user') {
      return `<div class="iv-agent-message"><span>C</span><div>${escape(content)}</div></div>`;
    }
    const s = state();
    const expanded = (s.expandedAgentMessages || []).includes(index);
    const long = content.length > 360;
    const preview = long && !expanded ? content.slice(0, 220).trim() + '…' : content;
    return `<div class="iv-agent-message user"><span>我</span><div class="iv-agent-user-content">
      <div class="iv-agent-message-meta"><b>面试材料</b><span>${content.length.toLocaleString()} 字</span></div>
      <div class="${expanded ? 'iv-agent-message-full' : 'iv-agent-message-preview'}">${escape(preview)}</div>
      ${long ? `<button class="iv-agent-expand" onclick="CaddieInterview.toggleAgentMessage(${index})">${expanded ? '收起全文' : '展开全文'}</button>` : ''}
    </div></div>`;
  }

  I.renderTrackReview = function (track, trackId) {
    const s = state();
    if (s.trackId !== trackId) {
      I.destroyEditor();
      Object.assign(s, { trackId, rounds: [], selectedRoundId: null, tab: 'overview', workspace: null, review: null, exam: [], sources: [], participants: [], tasks: [] });
    }
    setTimeout(() => I.loadRounds(trackId), 0);
    return `<section class="iv-shell" id="ivShell"><div class="iv-loading">正在读取面试进程…</div></section>`;
  };

  I.loadRounds = async function (trackId) {
    const s = state();
    try {
      const data = await ivApi('/v2/job-tracks/' + trackId + '/interview-rounds');
      s.rounds = data.items || [];
      if (!s.selectedRoundId && s.rounds[0]) s.selectedRoundId = s.rounds[s.rounds.length - 1].id;
      if (!s.selectedRoundId) return I.paintEmpty();
      await I.loadRound(s.selectedRoundId, false);
    } catch (e) { I.paintError(e.message); }
  };

  I.paintEmpty = function () {
    const el = $('#ivShell'); if (!el) return;
    el.innerHTML = `<header class="iv-head"><div><span class="iv-kicker">面试进程</span><h2>把每一轮变成可复用的准备</h2><p>从本轮准备开始；真实面试后再保存原始资料、整理答卷与复盘。</p></div><button class="btn" onclick="CaddieInterview.createRound()">＋ 新建面试轮次</button></header><section class="iv-empty"><b>还没有面试轮次</b><p>收到邀约时先建一轮。系统会自动建立可编辑的本轮准备、结构化逐字稿和答卷文档。</p><button class="btn" onclick="CaddieInterview.createRound()">建立第一轮</button></section>`;
  };

  I.paintError = function (message) { const el = $('#ivShell'); if (el) el.innerHTML = `<section class="iv-empty"><b>面试工作区加载失败</b><p>${escape(message || '请稍后重试')}</p><button class="btn ghost" onclick="CaddieInterview.loadRounds(${state().trackId})">重试</button></section>`; };

  I.loadRound = async function (rid, scroll = true) {
    const s = state();
    I.destroyEditor();
    s.selectedRoundId = rid;
    try {
      const [workspace, exam, sources, participants, review, tasks, library] = await Promise.all([
        ivApi('/v2/interview-rounds/' + rid + '/workspace'), ivApi('/v2/interview-rounds/' + rid + '/exam'),
        ivApi('/v2/interview-rounds/' + rid + '/transcript-sources'), ivApi('/v2/interview-rounds/' + rid + '/participants'),
        ivApi('/v2/interview-rounds/' + rid + '/review'), ivApi('/v2/interview-rounds/' + rid + '/tasks'),
        ivApi('/v2/interview-library?track_id=' + s.trackId)
      ]);
      s.workspace = workspace.data; s.exam = exam.items || []; s.sources = sources.items || []; s.participants = participants.items || []; s.review = review.data || {}; s.tasks = tasks.items || [];
      s.priorQuestions = ((library.data || {}).questions || []).filter(q => q.round_id !== rid && q.confirmed);
      I.paintWorkspace();
      const latestTask = [...s.tasks].sort((a, b) => Number(b.id || 0) - Number(a.id || 0))[0];
      if (latestTask?.is_active) I.pollTask(latestTask.id, 0, r.id, s.trackId);
      if (scroll) $('#ivShell')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    } catch (e) { I.paintError(e.message); }
  };

  I.paintWorkspace = function () {
    const s = state(), root = $('#ivShell');
    if (!root || !s.workspace) return;
    const r = s.workspace.round;
    const visibleTab = s.tab === 'exam' ? 'source' : s.tab;
    const stageComplete = {
      overview: false,
      prepare: !!(s.workspace.documents || []).find(x => x.document_type === 'round_preparation'),
      source: s.sources.length > 0 && s.exam.length > 0,
      review: !!s.review?.review_document,
      outcome: !!s.review?.outcome
    };
    root.innerHTML = `<header class="iv-head"><div><span class="iv-kicker">面试进程 · ${escape(s.workspace.track?.company || '')}</span><h2>${escape(label(r))}</h2><p>${escape(phase(r))}${r.scheduled_at ? ' · ' + escape(r.scheduled_at) : ''}</p></div><div class="iv-head-actions"><div class="iv-round-switcher">${s.rounds.map(x => `<button class="${x.id === r.id ? 'on' : ''}" onclick="CaddieInterview.loadRound(${x.id})"><b>${escape(label(x))}</b><small>${escape(phase(x))}</small></button>`).join('')}</div><button class="btn ghost sm" onclick="CaddieInterview.createRound()">＋ 新建轮次</button><button class="btn ghost sm" onclick="CaddieInterview.editRound()">编辑本轮</button>${r.status !== 'completed' ? `<button class="btn ghost sm iv-cancel-btn" onclick="CaddieInterview.cancelRound(${r.id},false)">取消面试</button>` : ''}</div></header>
      <nav class="iv-stage-nav" aria-label="本轮面试工作区">${Object.entries(tabLabel).map(([key, name]) => `<button data-iv-tab="${key}" class="${visibleTab === key ? 'on' : ''} ${stageComplete[key] ? 'done' : ''}" onclick="CaddieInterview.setTab('${key}')"><span><b>${name}</b><small>${tabHint[key]}</small></span></button>`).join('')}</nav>
      <div id="ivTaskRail">${I.taskRailHtml()}</div>
      <main class="iv-workspace" id="ivWorkspace"></main>`;
    I.paintTab();
  };

  I.syncStageNav = function (tab) {
    const visibleTab = tab === 'exam' ? 'source' : tab;
    document.querySelectorAll('#ivShell .iv-stage-nav [data-iv-tab]').forEach(button => {
      button.classList.toggle('on', button.dataset.ivTab === visibleTab);
    });
  };

  I.setTab = function (tab) {
    state().tab = tab;
    I.syncStageNav(tab);
    I.destroyEditor();
    I.paintTab();
  };

  I.openRoundResult = async function (roundId, tab = 'exam', trackId = null, attempt = 0) {
    const s = state();
    const targetTrackId = Number(trackId || s.trackId || 0);
    const targetRoundId = Number(roundId || s.selectedRoundId || 0);
    if (!targetRoundId) return ivToast('暂时找不到对应的面试轮次');

    if (targetTrackId && (!$('#ivShell') || Number(s.trackId) !== targetTrackId)) {
      if (typeof window.openInterviewRoundFromLibrary === 'function') {
        if (attempt === 0) window.openInterviewRoundFromLibrary(targetTrackId, targetRoundId);
        if (attempt >= 20) return ivToast('页面仍在加载，请从求职工作台打开这轮面试');
        setTimeout(() => I.openRoundResult(targetRoundId, tab, targetTrackId, attempt + 1), 150);
        return;
      }
    }

    s.tab = tab;
    await I.loadRound(targetRoundId, true);
    I.syncStageNav(tab);
  };
  I.taskRailHtml = function () {
    const task = [...(state().tasks || [])].sort((a, b) => Number(b.id || 0) - Number(a.id || 0))[0];
    if (!task || (!task.is_active && task.status !== 'failed')) return '';
    const failed = task.status === 'failed';
    const progress = Math.max(4, Math.min(100, Number(task.progress || 0)));
    return `<section class="iv-task-rail ${failed ? 'failed' : ''}">
      <div class="iv-task-copy"><span class="iv-task-pulse"></span><div><b>${escape(task.current_label || task.title || '正在处理')}</b><small>${escape(task.current_detail || (failed ? task.result_summary : '可以继续浏览其他页面，完成后会通知你'))}</small></div></div>
      <div class="iv-task-meter"><span style="width:${progress}%"></span></div>
      <strong>${failed ? '失败' : progress + '%'}</strong>
    </section>`;
  };
  I.paintTaskRail = function () {
    const host = $('#ivTaskRail');
    if (host) host.innerHTML = I.taskRailHtml();
  };
  I.selectPreparationDocument = function (did) { state().selectedPrepDocumentId = did; I.destroyEditor(); I.paintTab(); };
  const roundDocument = (type) => (state().workspace?.documents || []).find(x => x.document_type === type);
  const docEditor = (document, emptyText) => document ? `<section class="iv-document"><div class="iv-document-head"><div><b>${escape(document.title)}</b><span>可直接编辑，自动保留版本</span></div><button class="btn ghost sm" onclick="CaddieInterview.openDocument(${document.document_id || document.id})">全屏编辑</button></div><div class="iv-doc-editor" data-document-id="${document.document_id || document.id}"></div></section>` : `<section class="iv-empty compact"><b>${escape(emptyText)}</b></section>`;

  I.paintTab = function () {
    const s = state(), el = $('#ivWorkspace'); if (!el || !s.workspace) return;
    const r = s.workspace.round;
    if (s.tab === 'overview') {
      const docs = s.workspace.documents || [];
      const prepDocs = docs.filter(x => ['round_preparation', 'next_plan', 'project_pitch', 'professional_knowledge', 'interview_answer'].includes(x.document_type));
      const inherited = docs.filter(x => x.use_type === 'inherited');
      const prediction = s.review?.prediction;
      const next = !s.sources.length ? ['继续面前准备', '整理本轮重点、预计追问和引用材料', 'prepare']
        : !s.exam.length ? ['继续整理面后材料', '确认说话人并生成结构化答卷', 'source']
        : !s.review?.review_document ? ['开始本轮复盘', '从真实答卷提炼结论和下一步行动', 'review']
        : !s.review?.outcome ? ['等待并记录结果', '收到反馈后记录事实并校准判断', 'outcome']
        : ['查看本轮结果', '本轮资料已经完整归档', 'outcome'];
      el.innerHTML = `<section class="iv-overview-hero"><div><span class="iv-kicker">当前下一步</span><h3>${escape(next[0])}</h3><p>${escape(next[1])}</p></div><button class="btn" onclick="CaddieInterview.setTab('${next[2]}')">${escape(next[0])} →</button></section>
        <div class="iv-overview-grid">
          <section class="iv-overview-section"><div class="iv-section-title"><div><h3>本轮工作区</h3><p>围绕这一轮组织文档，内容可以从岗位资料和上一轮持续继承。</p></div></div><div class="iv-overview-stats"><button onclick="CaddieInterview.setTab('prepare')"><b>${prepDocs.length}</b><span>准备文档</span><small>${inherited.length} 篇来自继承</small></button><button onclick="CaddieInterview.setTab('source')"><b>${s.sources.length}</b><span>原始材料</span><small>${s.exam.length} 个结构化问题</small></button><button onclick="CaddieInterview.setTab('review')"><b>${s.review?.review_document ? '已生成' : '待生成'}</b><span>复盘报告</span><small>${(s.review?.actions || []).length} 项后续行动</small></button></div></section>
          <section class="iv-overview-section"><div class="iv-section-title"><div><h3>本轮判断</h3><p>只显示基于已确认材料形成的结论。</p></div></div>${prediction ? `<div class="iv-overview-prediction"><strong>${Math.round(prediction.probability || 0)}<small>/100</small></strong><div><b>${escape({ likely_pass:'倾向通过',borderline:'仍不确定',likely_fail:'风险偏高' }[prediction.label] || prediction.label)}</b><span>信心：${escape(prediction.confidence || 'low')}</span></div></div>` : '<div class="iv-overview-empty">确认答卷并完成复盘后，这里会显示整场判断。</div>'}</section>
        </div>
        <section class="iv-round-map"><div class="iv-section-title"><div><h3>从本轮到长期资料</h3><p>准备从既有内容出发，面后只把你确认过的增量沉淀回去。</p></div></div><div class="iv-round-map-flow"><span>岗位文档与经历知识</span><i>→</i><span>本轮准备文档组</span><i>→</i><span>真实答卷与复盘</span><i>→</i><span>确认后回流长期资料</span></div></section>`;
      return;
    }
    if (s.tab === 'prepare') {
      const prep = roundDocument('round_preparation');
      const allDocs = s.workspace.documents || [];
      const generated = allDocs.filter(x => x.use_type === 'generated' && !['organized_transcript', 'answer_sheet', 'review_report'].includes(x.document_type));
      const inherited = allDocs.filter(x => x.use_type === 'inherited');
      const references = allDocs.filter(x => x.use_type === 'reference');
      const editableDocs = [...generated];
      if (prep && !editableDocs.some(d => (d.document_id || d.id) === (prep.document_id || prep.id))) editableDocs.unshift(prep);
      const selectedId = s.selectedPrepDocumentId && editableDocs.some(d => (d.document_id || d.id) === s.selectedPrepDocumentId) ? s.selectedPrepDocumentId : (editableDocs[0]?.document_id || editableDocs[0]?.id);
      s.selectedPrepDocumentId = selectedId;
      const selected = editableDocs.find(d => (d.document_id || d.id) === selectedId);
      const coreDocs = prep ? [prep] : [];
      const materialDocs = editableDocs.filter(d => (d.document_id || d.id) !== (prep?.document_id || prep?.id));
      const navDoc = (d, source, isCore = false) => { const did = d.document_id || d.id; return `<div class="iv-prep-tree-doc ${(did === selectedId) ? 'on' : ''}"><button class="iv-prep-tree-doc-main" onclick="CaddieInterview.selectPreparationDocument(${did})"><span>文</span><div><b>${escape(d.title)}</b><small>${escape(source)}</small></div></button><button class="iv-prep-tree-doc-remove" title="${isCore ? '重置为空白文档' : '从本轮删除'}" aria-label="${isCore ? '重置文档' : '删除文档'}" onclick="event.stopPropagation();CaddieInterview.removePreparationDocument(${did},${isCore})">${isCore ? '↺' : '×'}</button></div>`; };
      el.innerHTML = `<section class="iv-prep-commandbar"><div><span class="iv-kicker">本轮准备</span><b>${escape(label(r))}</b><small>${editableDocs.length} 篇独立文档 · 自动保存版本</small></div><div class="iv-prep-actions"><button class="btn ghost sm" onclick="CaddieInterview.inherit()">继承上一轮</button><button class="btn ghost sm" onclick="CaddieInterview.pickDocument()">从岗位资料复制</button><button class="btn sm" onclick="CaddieInterview.createPreparationDocument()">＋ 新建文档</button></div></section>
        <section class="iv-prep-knowledge-workspace refined"><aside class="iv-prep-tree"><div class="iv-prep-tree-head"><div><b>本轮资料</b><small>按用途组织</small></div><span>${editableDocs.length} 篇</span></div><section class="iv-prep-doc-group"><header><b>核心准备</b><span>面试前总纲</span></header><div class="iv-prep-tree-list">${coreDocs.map(d => navDoc(d, '系统核心 · 可重置', true)).join('')}</div></section><section class="iv-prep-doc-group"><header><b>专项材料</b><span>${materialDocs.length} 篇</span></header><div class="iv-prep-tree-list">${materialDocs.map(d => navDoc(d, d.source_type === 'inheritance' ? '复制 / 继承材料' : '本轮新建')).join('') || '<div class="iv-prep-doc-empty">暂无专项材料<br><small>可从岗位资料复制或新建</small></div>'}</div></section><details class="iv-prep-tree-section" open><summary>历史真题 <span>${(s.priorQuestions||[]).length}</span></summary><div class="iv-prior-questions compact">${(s.priorQuestions||[]).slice(0,6).map(q=>`<div><button onclick="CaddieInterview.openQuestionFromHistory(${q.id})">${escape(q.normalized_question||q.original_question)}</button><a title="加入本轮准备" onclick="CaddieInterview.useQuestionInPreparation(${q.id})">＋</a></div>`).join('')||'<small>暂无同岗位历史真题</small>'}</div></details><details class="iv-prep-tree-section"><summary>外部引用 <span>${inherited.length+references.length}</span></summary><div class="iv-prep-tree-group">${inherited.map(d => `<button onclick="CaddieInterview.openDocument(${d.document_id || d.id})">↳ ${escape(d.title)}</button>`).join('')}${references.map(d => `<button onclick="CaddieInterview.openDocument(${d.document_id || d.id})">↗ ${escape(d.title)}</button>`).join('') || '<small>暂无引用资料</small>'}</div></details></aside><main class="iv-prep-editor">${selected ? docEditor(selected, '') : '<section class="iv-empty"><b>选择或新建一篇文档开始准备</b><p>正文会自动保存为本轮独立版本。</p></section>'}</main><aside class="iv-prep-context"><div class="iv-prep-context-head"><b>版本与来源</b><span>当前文档</span></div><div class="iv-version-facts"><div><span>编辑范围</span><b>仅当前轮次</b></div><div><span>来源</span><b>${escape(selected?.source_type === 'inheritance' ? '复制 / 继承材料' : '本轮创建')}</b></div><div><span>保存</span><b>自动保留版本</b></div></div><details class="iv-context-details"><summary>内容如何回流</summary><p>面试复盘后，系统只会提出候选更新；经过你确认后才写回岗位知识库或经历。</p></details><details class="iv-context-details"><summary>为什么是独立版本</summary><p>本轮修改不会覆盖岗位主知识库，也不会改变上一轮已经确认的内容。</p></details></aside></section>`;
      I.mountVisibleEditor(); return;
    }
    if (s.tab === 'source') {
      const items = s.sources;
      const latest = items[items.length - 1];
      const postStep = !items.length ? 1 : !s.exam.length ? 2 : 3;
      el.innerHTML = `<section class="iv-post-status"><div><span class="iv-kicker">面后整理</span><h3>${postStep === 1 ? '先保存真实材料' : postStep === 2 ? '材料已保存，继续整理答卷' : '答卷已经生成，等待你的确认'}</h3><p>原始材料、结构化答卷和复盘结论分开保存，任何整理都不会覆盖现场记录。</p></div><div class="iv-post-steps"><span class="${postStep >= 1 ? 'on' : ''}">1 保存材料</span><span class="${postStep >= 2 ? 'on' : ''}">2 整理答卷</span><span class="${postStep >= 3 ? 'on' : ''}">3 用户确认</span></div></section>
      <section class="iv-agent-entry">
        <div class="iv-agent-entry-copy">
          <span class="iv-kicker">原始材料</span>
          <h3>逐字稿、面后回忆和反馈统一归档</h3>
          <p>直接提交未经整理的内容。Caddie 会先锁定原始证据，再重建问题、回答、连续追问和现场信号。</p>
          <div class="iv-agent-entry-actions">
            <button class="btn" onclick="CaddieInterview.openReviewAgent()">＋ 添加面后材料</button>
            ${latest ? `<button class="btn ghost" onclick="CaddieInterview.chooseAnalysis(${latest.id})">重新整理最近资料</button>` : ''}
            ${s.exam.length ? `<button class="btn ghost" onclick="CaddieInterview.setTab('exam')">打开并确认答卷</button>` : ''}
          </div>
        </div>
        <div class="iv-agent-entry-status">
          <div><b>${items.length}</b><span>已归档资料</span></div>
          <div><b>${s.exam.length}</b><span>已识别主问题</span></div>
          <div><b>${phase(r)}</b><span>当前状态</span></div>
        </div>
      </section>
      <section class="iv-agent-records">
        <header><div><h3>本轮资料与答卷</h3><p>先核对说话人，再确认结构化答卷；确认后才进入复盘。</p></div><div class="iv-agent-record-actions"><button class="btn ghost sm" onclick="CaddieInterview.confirmParticipants()">确认说话人${s.participants.length ? '（' + s.participants.length + '）' : ''}</button>${s.exam.length ? `<button class="btn sm" onclick="CaddieInterview.setTab('exam')">确认答卷 · ${s.exam.length} 题</button>` : ''}</div></header>
        ${items.length ? `<div class="iv-agent-record-list">${items.map(x => `<div class="iv-agent-record"><div><b>${escape(x.title || '面试资料')}</b><span>${escape(sourceKindLabel[x.source_kind] || x.source_kind)} · ${escape(x.created_at || '')}</span></div><div class="iv-source-actions"><button class="btn ghost sm" onclick="CaddieInterview.viewSource(${x.id})">查看</button><button class="btn ghost sm" onclick="CaddieInterview.chooseAnalysis(${x.id})">重新整理</button><button class="iv-delete-source" onclick="CaddieInterview.deleteSource(${x.id})" title="删除本轮资料">删除</button></div></div>`).join('')}</div>` : '<div class="iv-agent-record-empty">还没有资料。打开复盘 Agent，直接粘贴未经整理的面试记录即可。</div>'}
      </section>`;
      return;
    }
    if (s.tab === 'exam') {
      const answerDoc = roundDocument('answer_sheet');
      const latestSource = s.sources[s.sources.length - 1];
      const organizeAction = latestSource ? `<button class="btn" onclick="CaddieInterview.chooseAnalysis(${latestSource.id})">交给 Agent 重整答卷</button>` : '<button class="btn" onclick="CaddieInterview.setTab(\'source\')">打开复盘 Agent</button>';
      const rebuildAction = s.exam.length ? `<button class="btn ghost sm" onclick="CaddieInterview.rebuildAnswerSheet()">刷新文档结构</button>` : '';
      el.innerHTML = `<section class="iv-answer-back"><button class="btn ghost sm" onclick="CaddieInterview.setTab('source')">← 返回面后材料</button><span>确认真实回答后，再进入复盘报告。</span></section><section class="iv-answer-hero"><div><span class="iv-kicker">可编辑答卷</span><h3>先读整场节奏，再逐题核对真实回答</h3><p>答卷会还原面试如何推进、面试官在各阶段验证什么，再保留主问题、真实回答、连续追问和现场信号。逐句转写只留在原始材料。</p></div><div class="iv-answer-actions">${organizeAction}${rebuildAction}<span>所有正式内容均可编辑并自动保存版本</span></div></section><div class="iv-exam-layout"><div>${docEditor(answerDoc, '先导入原始资料，再生成本轮答卷')}</div><aside class="iv-aside iv-answer-outline"><h3>主问题与标签</h3><p>标签决定真题会沉淀到哪段经历，以及下轮准备何时召回。</p>${s.exam.length ? s.exam.map(q => `<button class="iv-question-index" onclick="CaddieInterview.openQuestion(${q.id})"><span>${String(q.question_order).padStart(2, '0')}</span><b>${escape(q.normalized_question || q.original_question)}</b><small>${escape(q.question_type || '待识别')} · ${escape((q.entity_links || [])[0]?.entity_title || q.ability_key || '待关联经历')}</small>${q.confirmed ? '<em>已确认</em>' : '<em class="pending">待确认</em>'}</button>`).join('') : '<div class="iv-muted">生成完整答卷后，会在这里形成可回看证据的主问题索引。</div>'}<hr><p class="iv-muted">点开题目可修改考察类型、关联经历和追问角度。确认后进入面试总题库。</p></aside></div>`;
      I.mountVisibleEditor(); return;
    }
    if (s.tab === 'review') {
      const reviewDoc = s.review?.review_document;
      const prediction = s.review?.prediction;
      const actions = s.review?.actions || [];
      const positives = prediction ? jsonArray(prediction.positive_evidence_json) : [];
      const negatives = prediction ? jsonArray(prediction.negative_evidence_json) : [];
      const unknowns = prediction ? jsonArray(prediction.uncertainty_json) : [];
      el.innerHTML = `<section class="iv-review-overview"><div class="iv-review-overview-head"><div><span class="iv-kicker">整场结论</span><h3>${prediction ? escape({ likely_pass: '表现具备通过信号，仍需等待真实结果', borderline: '表现有亮点，但关键风险尚未解除', likely_fail: '当前风险偏高，优先沉淀可改进行动' }[prediction.label] || '复盘结论已生成') : '先确认真实答卷，再形成整场判断'}</h3><p>先看结论、证据和下一步，再按需进入完整复盘文档。</p></div>${prediction ? `<div class="iv-review-score"><span>通过倾向</span><strong>${Math.round(prediction.probability || 0)}<small>/100</small></strong><em>信心：${escape(prediction.confidence || 'low')}</em></div>` : ''}</div>${prediction ? `<div class="iv-review-signals"><div><span>明确正向信号</span><b>${escape(positives[0] || '尚未识别')}</b></div><div><span>主要风险</span><b>${escape(negatives[0] || '尚未识别')}</b></div><div><span>仍未知</span><b>${escape(unknowns[0] || '等待实际反馈')}</b></div></div>` : ''}</section>
      <div class="iv-review-toolbar"><div><h3>完整复盘</h3><p>复盘文档可以编辑；系统不会改写原始答卷。</p></div>${s.exam.length ? `<button class="btn" onclick="CaddieInterview.runReview()">生成 / 更新复盘</button>` : '<button class="btn ghost" disabled>先确认答卷</button>'}</div>
      <div class="iv-split"><div>${reviewDoc ? docEditor(reviewDoc, '') : '<section class="iv-empty"><b>还没有复盘报告</b><p>确认答卷后再开始复盘，分析会引用当前轮次的证据片段。</p></section>'}</div><aside class="iv-aside iv-review-aside"><div class="iv-action-headline"><div><h3>下一步行动</h3><p>每项行动都关联具体经历或准备文档。</p></div>${roundDocument('next_plan') ? `<button onclick="CaddieInterview.openDocument(${roundDocument('next_plan').document_id || roundDocument('next_plan').id})">打开计划</button>` : ''}</div>${actions.length ? actions.map(a => `<div class="iv-action ${a.status === 'completed' ? 'completed' : a.status === 'accepted' ? 'done' : ''}"><button class="iv-action-check" onclick="CaddieInterview.toggleAction(${a.id},'${a.status === 'completed' ? 'accepted' : 'completed'}')" title="${a.status === 'completed' ? '恢复为待完成' : '标记完成'}">${a.status === 'completed' ? '✓' : ''}</button><div><b>${escape(a.title)}</b><p>${escape(a.detail || '')}</p>${reviewActionTarget(a)}<div class="iv-action-tools"><button onclick="CaddieInterview.editAction(${a.id})">编辑</button>${a.status === 'proposed' ? `<button class="primary" onclick="CaddieInterview.applyAction(${a.id})">写入准备</button>` : `<span>${a.status === 'completed' ? '已完成' : '已写入准备'}</span>`}</div></div></div>`).join('') : '<div class="iv-muted">完成复盘后生成可确认行动。</div>'}<hr><h3>本轮证据</h3><div class="iv-evidence-summary"><span>原始资料 <b>${s.sources.length}</b></span><span>结构化问题 <b>${s.exam.length}</b></span><span>说话人 <b>${s.participants.length ? '已确认' : '待确认'}</b></span></div><button class="btn ghost sm" onclick="CaddieInterview.setTab('source')">查看原始资料</button></aside></div>`;
      I.mountVisibleEditor(); return;
    }
    const out = s.review?.outcome;
    el.innerHTML = `<div class="iv-split"><section class="iv-result-panel"><h3>记录实际结果</h3><p>只有你确认的实际结果才会参与校准；没有 HR 反馈时，系统不会替你编造落选原因。</p><div class="iv-result-options">${[['passed','通过'],['failed','未通过'],['pending','等待结果'],['withdrawn','主动撤回'],['cancelled','已取消']].map(([value,name]) => `<button class="${out?.actual_result === value ? 'on' : ''}" onclick="CaddieInterview.setOutcome('${value}')">${name}</button>`).join('')}</div><label>HR / 面试官反馈（可选）<textarea id="ivOutcomeEvidence" placeholder="粘贴原话，或写你确认的信息">${escape(out?.evidence_text || '')}</textarea></label><label>自己的复盘备注（可选）<textarea id="ivOutcomeNote" placeholder="例如：二面通过，下一轮需要补产品案例">${escape(out?.user_note || '')}</textarea></label><button class="btn" onclick="CaddieInterview.saveOutcome()">保存结果与校准</button></section><aside class="iv-aside"><h3>预测校准</h3>${out ? `<p>${escape(out.calibration_note || '已记录')}</p>` : '<p class="iv-muted">记录实际结果后，系统会把预测与事实对照，并在面试总库统计准确度。</p>'}<hr><h3>本轮资料去向</h3><p class="iv-muted">原始逐字稿 → 本轮归档；答卷与复盘 → 岗位知识；被确认的高价值题目 → 全局面试总库。</p></aside></div>`;
  };

  function jsonArray(value) { try { const x = JSON.parse(value || '[]'); return Array.isArray(x) ? x : []; } catch (_) { return []; } }
  function reviewActionTarget(a) {
    const payload = a.proposed_payload || {};
    const label = payload.target_label || '';
    if (!a.target_id || !label) return '';
    return `<button class="iv-action-target" onclick="CaddieInterview.openActionTarget(${a.id})"><span>${escape(payload.target_kind_label || '关联资料')}</span><b>${escape(label)}</b><i>打开 →</i></button>`;
  }
  const interviewStageOptions = [
    ['业务一面','业务一面'],['业务二面','业务二面'],['业务三面','业务三面'],
    ['HR 面','HR 面'],['终面','终面'],['群面','群面'],['笔试','笔试'],['待确认','待确认']
  ];
  const interviewTypeOptions = [
    ['business','业务面'],['professional','专业面'],['case','案例面'],['hr','HR 面'],
    ['group','群面'],['written','笔试'],['comprehensive','综合面'],['unknown','待确认']
  ];
  const interviewModeOptions = [
    ['one_to_one','单人面试'],['panel','多面试官'],['group','群面 / 多候选人'],['unknown','待确认']
  ];
  const interviewTimeOptions = Array.from({length:29},(_,i)=>{
    const minutes=8*60+i*30,h=String(Math.floor(minutes/60)).padStart(2,'0'),m=String(minutes%60).padStart(2,'0');
    return [`${h}:${m}`,`${h}:${m}`];
  });
  const interviewTypeLabel = value => Object.fromEntries(interviewTypeOptions)[value] || value || 'unknown';
  function interviewScheduleParts(value) {
    const match=String(value||'').replace('T',' ').match(/^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})/);
    if(!match)return {date:'',time:'09:00'};
    return {date:match[1],time:`${String(match[2]).padStart(2,'0')}:${match[3]}`};
  }
  function interviewRoundFields(round=null) {
    const schedule=interviewScheduleParts(round?.scheduled_at);
    const knownType=interviewTypeOptions.some(([value,label])=>value===round?.round_type||label===round?.round_type);
    const typeValue=knownType?(interviewTypeOptions.find(([value,label])=>value===round?.round_type||label===round?.round_type)?.[0]||'unknown'):'unknown';
    return [
      {k:'round_name',label:'面试环节',type:'select',val:round?.round_name||'业务一面',options:interviewStageOptions},
      {k:'round_type',label:'考核类型',type:'select',val:typeValue,options:interviewTypeOptions},
      {k:'scheduled_date',label:'面试日期',type:'date',val:schedule.date},
      {k:'scheduled_time',label:'开始时间',type:'select',val:schedule.time,options:interviewTimeOptions},
      {k:'interview_mode',label:'面试形式',type:'select',val:round?.interview_mode||'one_to_one',options:interviewModeOptions},
      {k:'notes',label:'本轮备注（可选）',type:'textarea',val:round?.notes||'',ph:'只记录已确认的准备重点或会议信息'}
    ];
  }
  I.createRound = function () {
    const s = state();
    ivOpenGen('新建面试轮次', interviewRoundFields(), async () => {
      const date=ivValue('scheduled_date'),time=ivValue('scheduled_time');
      if(!date)return ivToast('请选择面试日期');
      const res = await ivApi('/v2/job-tracks/' + s.trackId + '/interview-rounds', { method: 'POST', body: JSON.stringify({ round_name: ivValue('round_name'), round_type: interviewTypeLabel(ivValue('round_type')), scheduled_at: `${date} ${time}`, interview_mode: ivValue('interview_mode'), notes: ivValue('notes'), status: 'preparing' }) });
      ivCloseGen(); s.selectedRoundId = res.data.id; await I.loadRounds(s.trackId); ivToast('已建立本轮准备文档');
    });
  };
  I.editRound = function () {
    const r = state().workspace.round;
    ivOpenGen('编辑本轮面试', interviewRoundFields(r), async () => {
      const date=ivValue('scheduled_date'),time=ivValue('scheduled_time');
      if(!date)return ivToast('请选择面试日期');
      await ivApi('/v2/interview-rounds/' + r.id, {method:'PUT',body:JSON.stringify({round_name:ivValue('round_name'),round_type:interviewTypeLabel(ivValue('round_type')),scheduled_at:`${date} ${time}`,interview_mode:ivValue('interview_mode'),notes:ivValue('notes')})}); ivCloseGen(); await I.loadRounds(state().trackId);
    });
  };
  I.inherit = async function () { try { await ivApi('/v2/interview-rounds/' + state().selectedRoundId + '/inherit', {method:'POST'}); await I.loadRound(state().selectedRoundId, false); ivToast('已引用上一轮有效材料'); } catch (e) { ivToast(e.message); } };
  I.pickDocument = async function () {
    const s = state();
    try {
      const data = await ivApi('/v2/interview-rounds/' + s.selectedRoundId + '/document-candidates');
      const existing = new Set((s.workspace.documents || []).map(x => x.document_id));
      const rows = (data.items || []).filter(x => !existing.has(x.id));
      ivOpenBig('引用准备资料', `<div class="iv-picker"><p>选择的是引用，不会复制、移动或改写原文。</p>${rows.length ? rows.map(d => `<button onclick="CaddieInterview.linkDocument(${d.id})"><b>${escape(d.title)}</b><span>${escape(d.document_type)} · ${d.track_id === s.trackId ? '当前岗位' : '共享 / 其他岗位'}</span></button>`).join('') : '<div class="iv-muted">没有可新增的统一文档。后续可从准备知识或项目页创建文档。</div>'}</div>`);
    } catch (e) { ivToast(e.message); }
  };
  I.createPreparationDocument = function () {
    const s = state();
    ivOpenGen('新建本轮准备文档', [
      { k:'prep_title', label:'文档名称', ph:'如：项目深挖准备 / 业务知识清单 / 反问问题' },
      { k:'prep_purpose', label:'准备用途', ph:'这篇文档主要解决什么问题' }
    ], async () => {
      const title = String(ivValue('prep_title') || '').trim();
      if (!title) return ivToast('请填写文档名称');
      const purpose = String(ivValue('prep_purpose') || '').trim();
      const created = await ivApi('/v2/documents', {method:'POST', body:JSON.stringify({
        title, document_type:'round_preparation', body:`# ${title}\n\n${purpose ? purpose + '\n\n' : ''}## 已有内容\n\n- \n\n## 本轮需要补充\n\n- \n\n## 面试前确认\n\n- \n`,
        scope_type:'track', track_id:s.trackId, round_id:s.selectedRoundId, source_type:'manual',
        editable:true, locked_source:false, change_summary:'新建本轮准备文档'
      })});
      await ivApi('/v2/interview-rounds/' + s.selectedRoundId + '/documents', {method:'POST', body:JSON.stringify({
        document_id:created.data.id, use_type:'generated', purpose:purpose || '本轮专项准备'
      })});
      ivCloseGen();
      await I.loadRound(s.selectedRoundId, false);
      ivToast('已加入本轮准备文档组');
    });
  };
  I.removePreparationDocument = function (did, isCore) {
    const title = isCore ? '重置核心准备文档？' : '从本轮删除这篇文档？';
    const copy = isCore ? '当前内容会进入归档，并立即生成一篇空白的核心准备文档。此操作不会影响岗位资料。' : '这篇本轮独立文档会进入归档，不会影响它复制前的岗位资料或上一轮原文。';
    ivOpenBig(title, `<section class="iv-remove-prep-confirm"><div class="iv-remove-prep-icon">${isCore ? '↺' : '−'}</div><h3>${escape(title)}</h3><p>${escape(copy)}</p><div><button class="btn ghost" onclick="CaddieInterview.cancelRemovePreparationDocument()">取消</button><button class="btn danger" onclick="CaddieInterview.confirmRemovePreparationDocument(${did})">${isCore ? '确认重置' : '确认删除'}</button></div></section>`);
  };
  I.cancelRemovePreparationDocument = function () { ivCloseBig(); };
  I.confirmRemovePreparationDocument = async function (did) {
    const s = state();
    try {
      const result = await ivApi('/v2/interview-rounds/' + s.selectedRoundId + '/documents/' + did, {method:'DELETE'});
      ivCloseBig(); s.selectedPrepDocumentId = null; await I.loadRound(s.selectedRoundId, false);
      ivToast(result.data.action === 'reset' ? '核心准备文档已重置' : '已从本轮删除文档');
    } catch (e) { ivToast(e.message); }
  };
  I.linkDocument = async function (did) { try { await ivApi('/v2/interview-rounds/' + state().selectedRoundId + '/documents', {method:'POST',body:JSON.stringify({document_id:did,use_type:'copied',purpose:'从岗位主知识库复制'})}); ivCloseBig(); await I.loadRound(state().selectedRoundId,false); ivToast('已复制为本轮独立版本'); } catch(e) {ivToast(e.message);} };
  I.saveSource = async function () {
    const raw = $('#ivRawText')?.value.trim(); if (!raw) return ivToast('请先贴入逐字稿或现场记录');
    const s = state();
    try {
      const source = await ivApi('/v2/interview-rounds/' + s.selectedRoundId + '/transcript-sources', {method:'POST',body:JSON.stringify({source_kind:'paste',title:'面试逐字稿',raw_text:raw,locked:true})});
      $('#ivRawText').value = ''; await I.loadRound(s.selectedRoundId, false); I.chooseAnalysis(source.data.id);
    } catch (e) { ivToast(e.message); }
  };
  I.fillAgentPrompt = function (text) {
    const input = $('#ivReviewAgentInput');
    if (!input) return;
    input.value = text;
    input.focus();
  };
  I.reviewAgentHtml = function () {
    const s = state(), r = s.workspace?.round, items = s.sources || [], history = s.reviewAgentMessages || [];
    const intakeMessages = history.length ? history.map(reviewAgentMessage).join('') : `<div class="iv-agent-message"><span>C</span><div><b>我是本轮复盘 Agent。</b><br>直接粘贴逐字稿、HR 反馈或面后回忆。我会先保存原始证据，再结合当前岗位和轮次重建完整答卷。多人面试中，其他候选人的回答只作为上下文，不计入你的表现。</div></div>`;
    return `<div class="iv-agent-modal"><div class="iv-agent-layout"><section class="iv-review-agent"><header><div><span class="iv-kicker">本轮协作 · 自动归档</span><h3>复盘 Agent</h3><p>无需整理格式。长内容发送后会压缩为摘要展示，原文仍完整保存在本轮资料中。</p></div><span class="iv-agent-context">${escape(r ? label(r) : '')}</span></header><div class="iv-agent-thread" id="ivAgentThread">${intakeMessages}</div><div class="iv-agent-suggestions"><button onclick="CaddieInterview.fillAgentPrompt('这是刚结束的面试逐字稿，请重建完整答卷并分析面试官的考察路径。\\n\\n')">整理逐字稿</button><button onclick="CaddieInterview.fillAgentPrompt('这是 HR 或面试官的反馈，请结合本轮答卷更新复盘重点。\\n\\n')">录入反馈</button><button onclick="CaddieInterview.fillAgentPrompt('这是我面试后补充的现场回忆，请作为答卷证据并标记不确定处。\\n\\n')">补充回忆</button></div><div class="iv-agent-compose"><textarea id="ivReviewAgentInput" placeholder="说明材料是什么，然后直接粘贴原文…"></textarea><div><small>原文自动留存；答卷与复盘以可编辑文档产出。处理期间可以关闭窗口继续做其他事情。</small><button class="btn" onclick="CaddieInterview.sendToReviewAgent()">交给 Agent</button></div></div></section><aside class="iv-aside iv-agent-archive"><h3>本轮资料</h3><p>Agent 已接收 ${items.length} 份原始证据。</p>${items.length ? items.slice().reverse().slice(0, 5).map(x => `<div class="iv-agent-source"><b>${escape(x.title || '面试资料')}</b><span>${escape(sourceKindLabel[x.source_kind] || x.source_kind)} · ${escape(x.created_at || '')}</span><div class="iv-agent-source-actions"><button onclick="CaddieInterview.viewSource(${x.id})">查看</button><button class="danger" onclick="CaddieInterview.deleteSource(${x.id})">删除</button></div></div>`).join('') : '<div class="iv-muted">还没有本轮材料</div>'}<hr><h3>处理边界</h3><p class="iv-muted">系统保留真实回答的完整语义，清理口头语；优化答案只进入逐题复盘，不改写原始答卷。</p></aside></div></div>`;
  };
  I.openReviewAgent = function () {
    ivOpenBig('本轮复盘 Agent', I.reviewAgentHtml());
    const modal = $('#bigMask .big-modal');
    if (modal) modal.classList.add('iv-agent-modal-shell');
    setTimeout(() => {
      const thread = $('#ivAgentThread');
      if (thread) thread.scrollTop = thread.scrollHeight;
      $('#ivReviewAgentInput')?.focus();
    }, 0);
  };
  I.toggleAgentMessage = function (index) {
    const s = state(), expanded = new Set(s.expandedAgentMessages || []);
    if (expanded.has(index)) expanded.delete(index); else expanded.add(index);
    s.expandedAgentMessages = [...expanded];
    I.openReviewAgent();
  };
  I.sendToReviewAgent = async function () {
    const input = $('#ivReviewAgentInput'), raw = input?.value.trim();
    if (!raw) return ivToast('把逐字稿、反馈或面后回忆发给复盘 Agent');
    const s = state();
    s.reviewAgentMessages = [...(s.reviewAgentMessages || []), { role: 'user', content: raw }, { role: 'assistant', content: '已收到。我会先把原始材料归档到本轮，再按当前岗位和轮次重建答卷。你可以离开这里继续准备，完成后会提醒你。' }];
    input.value = '';
    I.openReviewAgent();
    try {
      const source = await ivApi('/v2/interview-rounds/' + s.selectedRoundId + '/transcript-sources', {
        method: 'POST', body: JSON.stringify({ source_kind: 'agent_intake', title: '复盘 Agent 接收的面试材料', raw_text: raw, locked: true })
      });
      await I.loadRound(s.selectedRoundId, false);
      I.openReviewAgent();
      I.startAnalysis(source.data.id, true, true);
    } catch (e) {
      s.reviewAgentMessages = [...(s.reviewAgentMessages || []), { role: 'assistant', content: '归档失败：' + (e.message || '请重试') }];
      I.openReviewAgent();
      ivToast(e.message || 'Agent 暂时无法接收这份材料');
    }
  };
  I.viewSource = async function (sourceId) {
    const s = state();
    try {
      const response = await ivApi('/v2/interview-rounds/' + s.selectedRoundId + '/transcript-sources/' + sourceId);
      const source = response.data || {};
      ivOpenBig('查看本轮资料', `<article class="iv-source-view">
        <header><div><span class="iv-kicker">${escape(sourceKindLabel[source.source_kind] || source.source_kind || '原始资料')}</span><h2>${escape(source.title || '面试资料')}</h2><p>${escape(source.created_at || '')} · 原文只读，不会被答卷整理覆盖</p></div><span>${String(source.raw_text || '').length.toLocaleString()} 字</span></header>
        <pre>${escape(source.raw_text || '这份资料没有可显示的文本内容。')}</pre>
        <footer><button class="btn ghost" onclick="CaddieInterview.deleteSource(${sourceId}, true)">删除资料</button><button class="btn" onclick="CaddieInterview.closeSourceView()">关闭</button></footer>
      </article>`);
    } catch (e) { ivToast(e.message || '读取资料失败'); }
  };
  I.closeSourceView = function () {
    ivCloseBig();
  };
  I.deleteSource = async function (sourceId, closeAfter = false) {
    if (!confirm('确定删除这份本轮资料？原始内容和机器切分片段将被删除，已有可编辑答卷会保留。')) return;
    const s = state();
    try {
      const response = await ivApi('/v2/interview-rounds/' + s.selectedRoundId + '/transcript-sources/' + sourceId, { method: 'DELETE' });
      if (closeAfter) ivCloseBig();
      await I.loadRound(s.selectedRoundId, false);
      ivToast(response.data?.answer_sheet_preserved ? '原始资料已删除，已有答卷已保留' : '本轮资料已删除');
    } catch (e) { ivToast(e.message || '删除资料失败'); }
  };
  I.chooseAnalysis = function (sourceId) {
    ivOpenBig('选择资料整理方式', `<section class="iv-analysis-choice"><span class="iv-kicker">从原始资料到答卷</span><h2>要如何处理这份面试材料？</h2><p>原始逐字稿始终留在本地。答卷与复盘是独立的、可编辑的文档。</p><div class="iv-analysis-options"><button onclick="CaddieInterview.startAnalysis(${sourceId},true)"><b>AI 整理为完整答卷</b><span>识别混乱说话人、合并连续追问，重建“问题 - 我的回答 - 现场信号”。会将这份资料发送给当前配置的模型。</span><i>推荐用于 ASR 转写、多人面试和长逐字稿</i></button><button onclick="CaddieInterview.startAnalysis(${sourceId},false)"><b>仅做本地初步整理</b><span>不发送资料到模型；按说话人和问句生成保守初稿，之后仍可再做 AI 整理。</span><i>适合敏感内容或只想先归档</i></button></div></section>`);
  };
  I.startAnalysis = async function (sourceId, useAi, keepModal = false) {
    try {
      const task = await ivApi('/v2/interview-rounds/' + state().selectedRoundId + '/decompose', {method:'POST',body:JSON.stringify({source_id:sourceId,use_ai:useAi})});
      if (!keepModal) ivCloseBig();
      ivToast(useAi ? '正在重建完整答卷，你可以继续浏览其他内容。' : '正在生成本地初步整理。'); I.pollTask(task.data.task_id, 0, state().selectedRoundId, state().trackId);
    } catch (e) { ivToast(e.message); }
  };
  I.redecompose = async function (sourceId, useAi = false) {
    try {
      const task = await ivApi('/v2/interview-rounds/' + state().selectedRoundId + '/decompose', {method:'POST',body:JSON.stringify({source_id:sourceId,use_ai:useAi})});
      ivToast('已按当前说话人角色重新拆解。你可以继续浏览其他内容。'); I.pollTask(task.data.task_id, 0, state().selectedRoundId, state().trackId);
    } catch (e) { ivToast(e.message); }
  };
  I.rebuildAnswerSheet = async function () {
    try {
      await ivApi('/v2/interview-rounds/' + state().selectedRoundId + '/answer-sheet/rebuild', {method:'POST'});
      await I.loadRound(state().selectedRoundId, false);
      ivToast('已更新为整场节奏优先的结构化答卷');
    } catch (e) { ivToast(e.message || '刷新答卷结构失败'); }
  };
  I.deepOrganize = async function (sourceId) {
    I.chooseAnalysis(sourceId);
  };
  I.pollTask = async function (taskId, tries = 0, roundId = null, trackId = null) {
    const targetRoundId = Number(roundId || state().selectedRoundId || 0);
    const targetTrackId = Number(trackId || state().trackId || 0);
    // AI 深度归并允许模型在较长转写上工作；本地任务通常数秒完成。
    if (tries > 160) { ivToast('任务仍在后台运行，稍后返回本轮即可查看结果。'); return; }
    try {
      const roundTasks = await ivApi('/v2/interview-rounds/' + targetRoundId + '/tasks');
      state().tasks = roundTasks.items || [];
      const task = state().tasks.find(item => item.id === taskId);
      I.paintTaskRail();
      if (!task) return;
      if (['completed','review','failed'].includes(task.status)) {
        if (task.status === 'failed') ivToast('后台处理失败：' + (task.result_summary || '请重试'));
        else {
          const reviewing = task.task_type === 'interview_review';
          const message = reviewing ? '面试复盘已完成：可编辑报告与行动已生成' : '面试拆解已完成：已生成可编辑答卷';
          const completedRoundId = Number(task.round_id || targetRoundId);
          const completedTrackId = targetTrackId;
          if (window.notify) {
            window.notify(reviewing ? '复盘已完成' : '逐字稿拆解已完成', message, {
              type:'success',
              persist:true,
              actionLabel: reviewing ? '查看复盘' : '查看答卷',
              action:`CaddieInterview.openRoundResult(${completedRoundId},'${reviewing ? 'review' : 'exam'}',${completedTrackId})`
            });
          } else ivToast(message);
          if (Number(state().selectedRoundId) === completedRoundId) await I.loadRound(completedRoundId, false);
        }
        return;
      }
    } catch (_) {}
    setTimeout(() => I.pollTask(taskId, tries + 1, targetRoundId, targetTrackId), 1200);
  };
  I.runReview = async function () { try { const r = await ivApi('/v2/interview-rounds/' + state().selectedRoundId + '/review', {method:'POST'}); ivToast('复盘已进入后台，可继续浏览其他页面。'); I.pollTask(r.data.task_id, 0, state().selectedRoundId, state().trackId); } catch(e) {ivToast(e.message);} };
  I.applyAction = async function (id) { try { await ivApi('/v2/review-actions/' + id + '/apply', {method:'POST'}); await I.loadRound(state().selectedRoundId, false); ivToast('已采纳到本轮准备，可继续编辑'); } catch(e) {ivToast(e.message);} };
  I.editAction = function (id) {
    const action = (state().review?.actions || []).find(item => item.id === id);
    if (!action) return;
    ivOpenGen('编辑下一步行动', [
      { k:'iv_action_title', label:'行动标题', val:action.title || '' },
      { k:'iv_action_detail', label:'完成标准与具体内容', val:action.detail || '', type:'textarea' }
    ], async () => {
      const title = String(ivValue('iv_action_title') || '').trim();
      if (!title) return ivToast('行动标题不能为空');
      await ivApi('/v2/review-actions/' + id, { method:'PATCH', body:JSON.stringify({ title, detail:String(ivValue('iv_action_detail') || '').trim() }) });
      ivCloseGen(); await I.loadRound(state().selectedRoundId, false); ivToast('行动已更新');
    });
  };
  I.toggleAction = async function (id, status) {
    try {
      await ivApi('/v2/review-actions/' + id, { method:'PATCH', body:JSON.stringify({ status }) });
      await I.loadRound(state().selectedRoundId, false);
      ivToast(status === 'completed' ? '已标记完成' : '已恢复为待完成');
    } catch (e) { ivToast(e.message || '行动状态更新失败'); }
  };
  I.openActionTarget = function (id) {
    const action = (state().review?.actions || []).find(item => Number(item.id) === Number(id));
    if (!action?.target_id) return ivToast('这个行动还没有关联具体资料');
    if (action.target_type === 'project' && window.openProject) return window.openProject(Number(action.target_id));
    if (action.target_type === 'experience' && window.openWrittenExperience) return window.openWrittenExperience(Number(action.target_id));
    if (action.target_type === 'knowledge_item' && window.openKnowledgeWorkspace) return window.openKnowledgeWorkspace(Number(action.target_id));
    ivToast('暂时无法打开关联资料');
  };
  I.confirmParticipants = function () {
    const s = state();
    if (!s.participants.length) return ivToast('请先保存并拆解一份逐字稿');
    const options = [['self','我'],['interviewer','面试官'],['other_candidate','其他候选人'],['unknown','待确认']];
    ivOpenBig('确认说话人', `<div class="iv-participant-form"><p>这一步决定哪些回答会计入你的答卷与预测。多人面试中请把其他候选人明确标记出来。</p>${s.participants.map((p,i) => `<div class="iv-participant-row"><label>${escape(p.display_name || p.participant_key)}</label><select id="ivParticipant_${p.id}">${options.map(o => `<option value="${o[0]}" ${p.role === o[0] ? 'selected' : ''}>${o[1]}</option>`).join('')}</select></div>`).join('')}<button class="btn" onclick="CaddieInterview.saveParticipants()">保存角色</button></div>`);
  };
  I.saveParticipants = async function () {
    const s = state(); const bodies = s.participants.map(p => ({participant_key:p.participant_key,display_name:p.display_name,role:$('#ivParticipant_'+p.id)?.value || p.role,organization_role:p.organization_role,confidence:p.confidence,confirmed:true}));
    if (bodies.filter(x => x.role === 'self').length > 1) return ivToast('一场面试只能标记一位本人');
    try { await ivApi('/v2/interview-rounds/' + s.selectedRoundId + '/participants', {method:'PUT',body:JSON.stringify(bodies)}); ivCloseBig(); await I.loadRound(s.selectedRoundId,false); ivToast('已确认说话人；如需更新题目归属，可重新拆解最近资料'); } catch(e) {ivToast(e.message);}
  };
  I.setOutcome = function (result) { state().pendingOutcome = result; document.querySelectorAll('.iv-result-options button').forEach(x => x.classList.toggle('on', x.getAttribute('onclick').includes(`'${result}'`))); };
  I.saveOutcome = async function () { const s = state(), existing=s.review?.outcome; const actual=s.pendingOutcome || existing?.actual_result; if (!actual) return ivToast('先选择实际结果'); try { await ivApi('/v2/interview-rounds/' + s.selectedRoundId + '/outcome', {method:'PUT',body:JSON.stringify({actual_result:actual,evidence_text:$('#ivOutcomeEvidence')?.value.trim() || null,user_note:$('#ivOutcomeNote')?.value.trim() || null})}); await I.loadRound(s.selectedRoundId,false);ivToast('已记录结果并更新校准'); } catch(e){ivToast(e.message);} };

  I.cancelRound = function (rid, returnToLibrary) {
    ivOpenGen('取消这场面试', [
      {k:'reason',label:'取消原因',type:'textarea',ph:'例如：公司取消岗位、时间冲突、主动放弃或其他已确认原因'},
      {k:'note',label:'备注（可选）',type:'textarea',ph:'保留 HR 原话或后续安排'}
    ], async () => {
      const reason=ivValue('reason');
      if (!reason) return ivToast('请填写取消原因');
      try {
        await ivApi('/v2/interview-rounds/' + rid + '/outcome', {method:'PUT',body:JSON.stringify({actual_result:'cancelled',evidence_type:'user_confirmed',evidence_text:reason,user_note:ivValue('note') || null})});
        ivCloseGen();ivToast('面试已取消，原因已保留');
        if (returnToLibrary) await I.renderLibrary(document.getElementById('view'));
        else await I.loadRound(rid,false);
      } catch(e) { ivToast(e.message || '取消面试失败'); }
    });
  };
  I.openQuestion = async function (qid) {
    const q = state().exam.find(x => x.id === qid); if (!q) return;
    const answers = q.answers || [];
    try {
      const options = (await ivApi('/v2/interview-questions/classification-options')).data;
      state().questionOptions = options;
      const link = (q.entity_links || [])[0] || {};
      const tagText = (q.tag_items || []).filter(item => !['知识领域','追问角度'].includes(item.tag_type)).map(item => item.tag_value).join('，');
      const selectedTopics = questionKnowledgeTopics(q);
      const selectedEntity = link.entity_type && link.entity_id != null ? `${link.entity_type}:${link.entity_id}` : '';
      const entityOptions = (options.entities || []).map(item => {
        const value = `${item.entity_type}:${item.entity_id}`;
        return `<option value="${escape(value)}" ${selectedEntity === value ? 'selected' : ''}>${escape(item.entity_title)}${item.subtitle ? ' · ' + escape(item.subtitle) : ''}</option>`;
      }).join('');
      ivOpenBig(`Q${q.question_order} · 真题归档`, `<article class="iv-question-detail iv-question-classifier">
        <header><span class="iv-kicker">${q.confirmed ? '已进入面试总题库' : 'AI 初步分类 · 等待确认'}</span><h2>${escape(q.normalized_question || q.original_question)}</h2><p>面试官意图：${escape(q.intent || '待复盘识别')}</p></header>
        <div class="iv-question-columns"><div class="iv-question-answer">${answers.map(a => `<section><b>${a.is_self ? '我的真实回答' : (a.display_name || '其他候选人 / 待确认')}</b><div>${escape(a.organized_answer || a.original_answer || '')}</div></section>`).join('') || '<div class="iv-muted">尚未可靠识别到回答。</div>'}</div>
        <aside class="iv-classify-panel"><h3>这道题考了什么</h3>
          <label>考察类型<select id="ivQuestionType">${(options.question_types || []).map(value => `<option ${q.question_type === value ? 'selected' : ''}>${escape(value)}</option>`).join('')}</select></label>
          <label>核心能力<select id="ivAbilityKey"><option value="">待确认</option>${(options.ability_keys || []).map(value => `<option ${q.ability_key === value ? 'selected' : ''}>${escape(value)}</option>`).join('')}</select></label>
          <label>知识主题<div class="iv-class-topic-picks">${(options.knowledge_topics || []).map(topic => `<button type="button" class="${selectedTopics.includes(topic) ? 'on' : ''}" data-topic="${escape(topic)}" onclick="CaddieInterview.toggleQuestionTopic(this)">${escape(topic)}</button>`).join('')}</div></label>
          <label>关联经历 / 项目<select id="ivQuestionEntity"><option value="">暂不关联</option>${entityOptions}</select></label>
          <label>追问角度<input id="ivQuestionAspect" value="${escape(link.aspect || '')}" placeholder="如：选题动机 / 研究方法 / 结果边界"></label>
          <label>检索标签<input id="ivQuestionTags" value="${escape(tagText)}" placeholder="用逗号分隔，如：毕业论文，IPO，选题动机"></label>
          <button class="btn" onclick="CaddieInterview.saveQuestionClassification(${qid})">确认并进入总题库</button>
          <small>确认后仍可修改。系统会在相关经历和下一轮准备中召回这道真题。</small>
        </aside></div></article>`);
    } catch (e) { ivToast(e.message || '题目分类读取失败'); }
  };
  I.saveQuestionClassification = async function (qid) {
    const s = state();
    const fromLibrary = !!s.editingLibraryQuestion;
    const fromQuestionPage = !!s.questionPageActive;
    const options = state().questionOptions || {};
    const entityValue = $('#ivQuestionEntity')?.value || '';
    const entity = (options.entities || []).find(item => `${item.entity_type}:${item.entity_id}` === entityValue);
    const tags = String($('#ivQuestionTags')?.value || '').split(/[，,]/).map(value => value.trim()).filter(Boolean)
      .map(value => ({ type:'主题', value, source:'user', confirmed:true }));
    document.querySelectorAll('.iv-class-topic-picks button.on').forEach(button => {
      const value = String(button.dataset.topic || '').trim();
      if (value) tags.push({ type:'知识领域', value, source:'user', confirmed:true });
    });
    const aspect = String($('#ivQuestionAspect')?.value || '').trim();
    if (aspect && !tags.some(tag => tag.value === aspect)) tags.push({ type:'追问角度', value:aspect, source:'user', confirmed:true });
    const links = entity ? [{ entity_type:entity.entity_type, entity_id:entity.entity_id, entity_title:entity.entity_title, relation:'asked_about', aspect, source:'user', confirmed:true }] : [];
    try {
      await ivApi('/v2/interview-questions/' + qid + '/classification', { method:'PUT', body:JSON.stringify({
        question_type:$('#ivQuestionType')?.value || null,
        ability_key:$('#ivAbilityKey')?.value || null,
        tags, links, confirmed:true
      })});
      if (!fromQuestionPage) ivCloseBig();
      s.editingLibraryQuestion = false;
      if (fromQuestionPage) {
        s.questionPageActive = false;
        await I.openLibraryQuestion(qid);
      } else if (fromLibrary) {
        const view = document.querySelector('#view');
        if (view) await I.renderLibrary(view);
      } else {
        await I.loadRound(s.selectedRoundId, false);
      }
      ivToast('真题标签已确认，并进入面试总题库');
    } catch (e) { ivToast(e.message || '保存题目标签失败'); }
  };
  I.toggleQuestionTopic = function (button) {
    button.classList.toggle('on');
  };
  I.openQuestionFromHistory = function (qid) {
    const s = state(), q = (s.priorQuestions || []).find(item => item.id === qid);
    if (!q) return;
    s.exam = [...(s.exam || []).filter(item => item.id !== qid), q];
    I.openQuestion(qid);
  };
  I.useQuestionInPreparation = async function (qid) {
    try {
      await ivApi('/v2/interview-rounds/' + state().selectedRoundId + '/questions/' + qid + '/use-in-preparation', {method:'POST'});
      await I.loadRound(state().selectedRoundId, false);
      state().tab = 'prepare'; I.paintTab();
      ivToast('历史真题已加入本轮准备文档');
    } catch (e) { ivToast(e.message || '引用真题失败'); }
  };

  I.destroyEditor = function () { const s = state(); if (s.editor?.destroy) try { s.editor.destroy(); } catch (_) {} s.editor = null; s.editorDocId = null; };
  I.destroyQuestionAnswerEditor = function () {
    const s = state();
    if (s.questionAnswerEditor?.destroy) {
      try { s.questionAnswerEditor.destroy(); } catch (_) {}
    }
    s.questionAnswerEditor = null;
    s.questionAnswerMarkdown = null;
  };
  I.currentQuestionAnswer = function () {
    const s = state();
    if (typeof s.questionAnswerMarkdown === 'string') return s.questionAnswerMarkdown.trim();
    return String($('#ivQuestionAnswerFallback')?.value || '').trim();
  };
  I.mountQuestionAnswerEditor = function (initialMarkdown, attempt = 0) {
    const host = $('#ivQuestionAnswer');
    if (!host) return;
    I.destroyQuestionAnswerEditor();
    const s = state();
    const initial = String(initialMarkdown || '');
    s.questionAnswerMarkdown = initial;
    if (window.CaddieKnowledgeEditor?.mount) {
      try {
        s.questionAnswerEditor = window.CaddieKnowledgeEditor.mount(host, {
          initialMarkdown: initial,
          onChange: markdown => { s.questionAnswerMarkdown = markdown; },
          onReady: async editor => {
            if (editor?.document && typeof editor.blocksToMarkdownLossy === 'function') {
              s.questionAnswerMarkdown = await editor.blocksToMarkdownLossy(editor.document);
            }
            host.dataset.editor = 'blocknote';
          }
        });
        return;
      } catch (_) {}
    }
    if (attempt < 12) {
      host.innerHTML = `<div class="iv-editor-loading">正在加载可视化编辑器…</div>`;
      setTimeout(() => {
        if ($('#ivQuestionAnswer') === host && !state().questionAnswerEditor) {
          I.mountQuestionAnswerEditor(initial, attempt + 1);
        }
      }, 100);
      return;
    }
    host.innerHTML = `<textarea id="ivQuestionAnswerFallback" class="iv-answer-editor-fallback" placeholder="输入或整理你的答案…">${escape(initial)}</textarea>`;
    host.querySelector('textarea')?.addEventListener('input', event => {
      s.questionAnswerMarkdown = event.target.value;
    });
  };
  I.replaceQuestionAnswer = function (markdownValue) {
    const value = String(markdownValue || '');
    I.mountQuestionAnswerEditor(value);
    requestAnimationFrame(() => {
      const editable = $('#ivQuestionAnswer')?.querySelector('[contenteditable="true"]');
      (editable || $('#ivQuestionAnswerFallback'))?.focus();
    });
  };
  I.mountVisibleEditor = function () { const host = $('.iv-doc-editor'); if (!host) return; const did = +host.dataset.documentId; I.mountDocument(host, did); };
  I.mountDocument = async function (host, did) {
    let doc; try { doc = (await ivApi('/v2/documents/' + did)).data; } catch (e) { host.innerHTML = `<div class="iv-muted">文档读取失败：${escape(e.message)}</div>`; return; }
    const s = state(); I.destroyEditor(); s.editorDocId = did; s.editorDoc = doc; let ready = false; let timer = null;
    const save = async (body) => { if (!ready || body === doc.body) return; clearTimeout(timer); timer = setTimeout(async () => { try { const r = await ivApi('/v2/documents/' + did, {method:'PUT',body:JSON.stringify({body,expected_version:doc.current_version,change_summary:'编辑面试工作文档'})}); doc = r.data; s.editorDoc = doc; } catch(e) { ivToast('文档保存失败：' + e.message); } }, 650); };
    if (window.CaddieKnowledgeEditor?.mount) {
      const editor = window.CaddieKnowledgeEditor.mount(host, { initialMarkdown: doc.body || '', onChange: save, onReady: async (instance) => { s.editor = instance; ready = true; } });
      s.editor = editor;
    } else {
      host.innerHTML = `<textarea class="iv-doc-fallback">${escape(doc.body || '')}</textarea>`;
      host.querySelector('textarea').addEventListener('input', e => save(e.target.value)); ready = true;
    }
  };
  I.openDocument = async function (did) { try { const doc = (await ivApi('/v2/documents/' + did)).data; ivOpenBig(doc.title, `<div class="iv-full-editor" id="ivFullEditor" data-document-id="${did}"></div>`); setTimeout(() => { const host = $('#ivFullEditor'); if (host) I.mountDocument(host, did); }, 0); } catch(e) {ivToast(e.message);} };

  function questionSearchText(q) {
    const link = (q.entity_links || [])[0] || {};
    const tags = (q.tag_items || []).map(item => item.tag_value);
    return `${q.normalized_question || q.original_question || ''} ${q.company || ''} ${q.role || ''} ${q.question_type || ''} ${q.ability_key || ''} ${link.entity_title || ''} ${link.aspect || ''} ${tags.join(' ')}`.toLowerCase();
  }

  function questionKnowledgeTopics(q) {
    return [...new Set((q.tag_items || [])
      .filter(item => item.tag_type === '知识领域')
      .map(item => String(item.tag_value || '').trim())
      .filter(Boolean))];
  }

  I.openQuestionBankPage = async function () {
    const view = $('#view'); if (!view) return;
    state().questionPageActive = false;
    state().editingLibraryQuestion = false;
    state().questionReturn = {type:'bank', label:'返回真题题库'};
    view.innerHTML = `<div class="iv-loading">正在打开真题题库…</div>`;
    try {
      const data = (await ivApi('/v2/interview-library')).data;
      const questions = data.questions || [];
      const typeCounts = questions.reduce((counts, question) => {
        const type = String(question.question_type || '待分类').trim();
        counts[type] = (counts[type] || 0) + 1;
        return counts;
      }, {});
      const topicCounts = questions.reduce((counts, question) => {
        questionKnowledgeTopics(question).forEach(topic => {
          counts[topic] = (counts[topic] || 0) + 1;
        });
        return counts;
      }, {});
      const typeFilters = Object.entries(typeCounts).sort((a, b) => b[1] - a[1]);
      const topicFilters = Object.entries(topicCounts).sort((a, b) => {
        const aiDelta = Number(b[0].startsWith('AI')) - Number(a[0].startsWith('AI'));
        return aiDelta || b[1] - a[1] || a[0].localeCompare(b[0], 'zh-CN');
      });
      state().libraryQuestions = questions;
      state().questionBankFilters = {query:'', status:'all', type:'all', topic:'all'};
      view.innerHTML = `<section class="iv-bank-page">
        <header class="iv-bank-page-head"><div><button class="iv-text-back" onclick="CaddieInterview.renderLibrary(document.querySelector('#view'))">← 返回面试总览</button><span class="iv-kicker">真实面试资产</span><h1>真题题库</h1><p>题目、真实回答、个人答案和 AI 优化版本统一留存。按岗位、经历和追问角度随时检索。</p></div><div class="iv-bank-count"><b>${questions.length}</b><span>道真实题目</span></div></header>
        <section class="iv-bank-toolbar"><div class="iv-hub-search"><span>⌕</span><input placeholder="搜索题目、岗位、经历或标签…" oninput="CaddieInterview.filterQuestionBank(this.value)"></div><div class="iv-bank-filter"><button class="on" onclick="CaddieInterview.filterQuestionStatus(this,'all')">全部</button><button onclick="CaddieInterview.filterQuestionStatus(this,'answered')">有答案</button><button onclick="CaddieInterview.filterQuestionStatus(this,'missing')">待补答案</button><button onclick="CaddieInterview.filterQuestionStatus(this,'pending')">待归档</button></div></section>
        <section class="iv-bank-tagbar"><span>按题型</span><div class="iv-bank-tag-filter"><button class="on" onclick="CaddieInterview.filterQuestionType(this,'all')">全部题型 <b>${questions.length}</b></button>${typeFilters.map(([type,count]) => `<button onclick="CaddieInterview.filterQuestionType(this,'${escape(type)}')">${escape(type)} <b>${count}</b></button>`).join('')}</div></section>
        <section class="iv-bank-tagbar iv-bank-topicbar"><span>按知识主题</span><div class="iv-bank-topic-filter"><button class="on" onclick="CaddieInterview.filterQuestionTopic(this,'all')">全部主题</button>${topicFilters.map(([topic,count]) => `<button class="${topic.startsWith('AI') ? 'ai-topic' : ''}" onclick="CaddieInterview.filterQuestionTopic(this,'${escape(topic)}')">${escape(topic)} <b>${count}</b></button>`).join('')}</div></section>
        <div class="iv-question-bank-list iv-bank-page-list">${questions.length ? questions.map(q => {
          const link=(q.entity_links||[])[0], workspace=q.answer_workspace||{}, hasAnswer=!!String(workspace.current_answer||'').trim();
          const knowledgeTopics=questionKnowledgeTopics(q);
          const tags=[...new Set((q.tag_items||[]).map(x=>x.tag_value))].filter(tag=>tag!==link?.entity_title&&tag!==link?.aspect&&!knowledgeTopics.includes(tag));
          return `<article class="iv-bank-question iv-bank-page-row" data-search="${escape(questionSearchText(q))}" data-answer="${hasAnswer?'answered':'missing'}" data-confirmed="${q.confirmed?'confirmed':'pending'}" data-type="${escape(q.question_type || '待分类')}" data-topics="${escape(knowledgeTopics.join('|'))}" onclick="CaddieInterview.openLibraryQuestion(${q.id})">
            <div class="iv-bank-question-copy"><span>${escape(q.question_type || '待分类')}</span><h3>${escape(q.normalized_question || q.original_question)}</h3><p>${escape(q.company || '')} · ${escape(q.role || '')} · 第 ${q.round_number} 轮</p>${hasAnswer ? `<small>${escape(String(workspace.current_answer).slice(0,100))}${String(workspace.current_answer).length>100?'…':''}</small>` : '<small class="missing">尚未形成可编辑答案</small>'}</div>
            <div class="iv-bank-tags">${link ? `<b>${escape(link.entity_title)}</b>${link.aspect ? `<em>${escape(link.aspect)}</em>` : ''}` : '<i>待关联经历</i>'}${knowledgeTopics.slice(0,2).map(topic=>`<em class="knowledge">${escape(topic)}</em>`).join('')}${tags.slice(0,1).map(tag=>`<em>${escape(tag)}</em>`).join('')}</div>
            <div class="iv-bank-row-status"><span class="${hasAnswer?'ready':'missing'}">${hasAnswer?'已有答案':'待补答案'}</span><button>打开题目 →</button></div>
          </article>`;
        }).join('') : '<div class="iv-empty"><b>还没有真实面试题</b><p>完成一轮逐字稿拆解后，题目会自动进入这里。</p></div>'}</div>
      </section>`;
    } catch (e) {
      view.innerHTML = `<div class="iv-empty"><b>题库加载失败</b><p>${escape(e.message)}</p><button class="btn ghost" onclick="CaddieInterview.openQuestionBankPage()">重试</button></div>`;
    }
  };

  I.applyQuestionBankFilters = function () {
    const filters = state().questionBankFilters || {query:'', status:'all', type:'all', topic:'all'};
    document.querySelectorAll('.iv-bank-page-row').forEach(row => {
      const searchHidden = !!filters.query && !String(row.dataset.search || '').includes(filters.query);
      const statusHidden = filters.status !== 'all' && (
        filters.status === 'pending'
          ? row.dataset.confirmed !== 'pending'
          : row.dataset.answer !== filters.status
      );
      const typeHidden = filters.type !== 'all' && row.dataset.type !== filters.type;
      const topics = String(row.dataset.topics || '').split('|').filter(Boolean);
      const topicHidden = filters.topic !== 'all' && !topics.includes(filters.topic);
      row.hidden = searchHidden || statusHidden || typeHidden || topicHidden;
    });
  };

  I.filterQuestionStatus = function (button, status) {
    document.querySelectorAll('.iv-bank-filter button').forEach(item => item.classList.toggle('on', item === button));
    const filters = state().questionBankFilters || {query:'', status:'all', type:'all'};
    filters.status = status;
    state().questionBankFilters = filters;
    I.applyQuestionBankFilters();
  };

  I.filterQuestionType = function (button, type) {
    document.querySelectorAll('.iv-bank-tag-filter button').forEach(item => item.classList.toggle('on', item === button));
    const filters = state().questionBankFilters || {query:'', status:'all', type:'all'};
    filters.type = type;
    state().questionBankFilters = filters;
    I.applyQuestionBankFilters();
  };

  I.filterQuestionTopic = function (button, topic) {
    document.querySelectorAll('.iv-bank-topic-filter button').forEach(item => item.classList.toggle('on', item === button));
    const filters = state().questionBankFilters || {query:'', status:'all', type:'all', topic:'all'};
    filters.topic = topic;
    state().questionBankFilters = filters;
    I.applyQuestionBankFilters();
  };

  function questionClassifierHtml(q, options) {
    const link = (q.entity_links || [])[0] || {};
    const tagText = (q.tag_items || []).filter(item => !['知识领域','追问角度'].includes(item.tag_type)).map(item => item.tag_value).join('，');
    const selectedTopics = questionKnowledgeTopics(q);
    const selectedEntity = link.entity_type && link.entity_id != null ? `${link.entity_type}:${link.entity_id}` : '';
    const entityOptions = (options.entities || []).map(item => {
      const value = `${item.entity_type}:${item.entity_id}`;
      return `<option value="${escape(value)}" ${selectedEntity === value ? 'selected' : ''}>${escape(item.entity_title)}${item.subtitle ? ' · ' + escape(item.subtitle) : ''}</option>`;
    }).join('');
    return `<section class="iv-question-meta-panel"><div><h3>题目归档</h3><p>修改后会影响后续检索和面试准备召回。</p></div>
      <label>考察类型<select id="ivQuestionType">${(options.question_types || []).map(value => `<option ${q.question_type === value ? 'selected' : ''}>${escape(value)}</option>`).join('')}</select></label>
      <label>核心能力<select id="ivAbilityKey"><option value="">待确认</option>${(options.ability_keys || []).map(value => `<option ${q.ability_key === value ? 'selected' : ''}>${escape(value)}</option>`).join('')}</select></label>
      <label>知识主题<div class="iv-class-topic-picks">${(options.knowledge_topics || []).map(topic => `<button type="button" class="${selectedTopics.includes(topic) ? 'on' : ''}" data-topic="${escape(topic)}" onclick="CaddieInterview.toggleQuestionTopic(this)">${escape(topic)}</button>`).join('')}</div></label>
      <label>关联经历 / 项目<select id="ivQuestionEntity"><option value="">暂不关联</option>${entityOptions}</select></label>
      <label>追问角度<input id="ivQuestionAspect" value="${escape(link.aspect || '')}" placeholder="如：选题动机 / 研究方法 / 结果边界"></label>
      <label>检索标签<input id="ivQuestionTags" value="${escape(tagText)}" placeholder="用逗号分隔"></label>
      <button class="btn ghost" onclick="CaddieInterview.saveQuestionClassification(${q.id})">保存归档信息</button>
    </section>`;
  }

  function coachingList(items, emptyText) {
    const values = Array.isArray(items) ? items.filter(Boolean) : [];
    if (!values.length) return `<p class="iv-coach-empty">${escape(emptyText || '暂无')}</p>`;
    return `<ul>${values.map(item => `<li>${escape(item)}</li>`).join('')}</ul>`;
  }

  function coachingProposalHtml(message, qid) {
    const proposal = message.proposal || {};
    const labels = {
      interviewer_intent:'面试官意图', why_asked:'提问原因',
      issues:'原回答问题', satisfaction_criteria:'满意标准',
      answer_strategy:'作答路径', improved_answer:'参考答法',
      followups:'可能追问', evidence_gaps:'事实缺口'
    };
    const fields = Object.keys(proposal).filter(key => proposal[key] && (!Array.isArray(proposal[key]) || proposal[key].length));
    if (!fields.length) return '';
    return `<div class="iv-coach-proposal"><div><span>建议更新批改文档</span><small>${fields.map(key => labels[key] || key).join('、')}</small></div>
      ${message.applied ? '<b>已采纳</b>' : `<button onclick="CaddieInterview.applyCoachProposal(${qid},${message.id})">查看并采纳</button>`}
      <details><summary>查看候选修改</summary>${fields.map(key => {
        const value = Array.isArray(proposal[key]) ? proposal[key].map(item => `• ${item}`).join('\n') : proposal[key];
        return `<section><strong>${escape(labels[key] || key)}</strong><p>${escape(value).replace(/\n/g,'<br>')}</p></section>`;
      }).join('')}</details></div>`;
  }

  function questionCoachThreadHtml(messages, qid) {
    const items = Array.isArray(messages) ? messages : [];
    return `<section class="iv-coach-thread ${items.length ? 'has-messages' : 'is-empty'}">
      <div class="iv-coach-messages" id="ivCoachMessages" aria-live="polite">${items.length ? items.map(message => `
        <article class="${message.role === 'user' ? 'user' : 'assistant'}">
          <header>${message.role === 'user' ? '<span class="iv-coach-user-avatar">我</span>' : coachAvatar()}<div><b>${message.role === 'user' ? '你' : '面试教练'}</b>${message.role === 'assistant' ? '<small>Caddie 专家团 · 逐题校准</small>' : ''}</div></header>
          <div class="iv-coach-message-body">${message.role === 'user' ? `<p>${escape(message.content || '').replace(/\n/g,'<br>')}</p>` : markdown(message.content || '')}</div>
          ${message.role === 'assistant' && message.proposal && !message.applied ? '<small>已形成候选修改，请在右侧审阅。</small>' : ''}
          ${message.role === 'assistant' && message.applied ? '<small class="applied">本轮修改已采纳并写入新版本。</small>' : ''}
        </article>`).join('') : `<div class="iv-coach-thread-empty"><span class="iv-kicker">开始校准</span><b>选择一个分歧，或直接告诉教练你的判断</b><p>没有讨论时不展开空白会话。教练会先解释依据，需要修改文档时再生成候选版本。</p><div>
          <button onclick="CaddieInterview.setCoachPrompt('我不认同当前的面试官意图判断。请结合这道题前后的追问链，重新解释面试官真正想验证什么。')">重新判断提问意图</button>
          <button onclick="CaddieInterview.setCoachPrompt('请区分我回答中的事实、观点和推断，并指出哪些批评可能建立在错误事实之上。')">核对事实与推断</button>
          <button onclick="CaddieInterview.setCoachPrompt('先不要生成答案。请解释我原回答为什么不能让面试官满意，以及缺少了哪一层判断。')">解释原回答的问题</button>
        </div></div>`}</div>
      <div class="iv-coach-composer">
        <textarea id="ivCoachMessage" placeholder="例如：我不认同这是项目深挖，面试官更像在判断我的 AI 工具认知。请结合追问链重新解释。"></textarea>
        <div><small>AI 会先回应你的观点；需要改文档时只生成候选修改，由你确认。</small><button class="btn" onclick="CaddieInterview.sendCoachMessage(${qid})">发送</button></div>
        <div id="ivCoachMessageProgress" class="iv-ai-progress"></div>
      </div>
    </section>`;
  }

  function coachCurrentConclusionHtml(coaching) {
    if (!coaching) return '<div class="iv-coach-output-empty">还没有批改基线。先关闭工作台并完成一次深度批改。</div>';
    return `<section class="iv-coach-baseline">
      <span>当前批改 · v${coaching.version || 1}</span>
      <h3>面试官在判断什么</h3>
      <div>${markdown(coaching.interviewer_intent || '暂无可靠判断')}</div>
      <details><summary>当前改答路径</summary><div>${markdown(coaching.answer_strategy || '暂无可靠建议')}</div></details>
    </section>`;
  }

  function coachOutputPaneHtml(messages, coaching, qid) {
    const proposals = (Array.isArray(messages) ? messages : [])
      .filter(message => message.role === 'assistant' && message.proposal)
      .slice().reverse();
    return `<div class="iv-coach-output-head"><span class="iv-kicker">决策区</span><h2>结论与候选修改</h2><p>讨论不会直接改文档。只有你确认采纳，才会生成新的批改版本。</p></div>
      ${coachCurrentConclusionHtml(coaching)}
      <section class="iv-coach-candidates">
        <div class="iv-coach-pane-title"><b>待确认修改</b><span>${proposals.filter(item => !item.applied).length}</span></div>
        ${proposals.length ? proposals.map(message => coachingProposalHtml(message, qid)).join('') : '<div class="iv-coach-output-empty">对话中形成的结构化修改会出现在这里。</div>'}
      </section>
      <section class="iv-coach-knowledge-callout"><span class="iv-kicker">知识沉淀</span><h3>把讨论变成可复用知识</h3><p>从题目、回答和本轮讨论中提炼一个主题文档。先预览并编辑，确认后才写入知识库。</p><button class="btn" onclick="CaddieInterview.extractCoachKnowledge(${qid})">提炼知识主题</button></section>
      <footer class="iv-coach-output-foot"><button class="btn ghost" onclick="CaddieInterview.closeCoachWorkspace()">返回批改文档</button></footer>`;
  }

  function questionCoachWorkspaceHtml(messages, qid, coaching, question, workspace) {
    const hasDiscussion = Array.isArray(messages) && messages.length > 0;
    const answer = String(workspace?.current_answer || '').trim();
    const original = ((question?.answers || []).find(item => item.is_self) || {});
    const evidence = String(original.organized_answer || original.original_answer || '').trim();
    return `<div class="iv-coach-workspace ${hasDiscussion ? 'has-discussion' : 'is-empty'}" id="ivCoachWorkspace" hidden>
      <header class="iv-coach-workspace-head">
        <div class="iv-coach-brand">${coachAvatar()}<div><b>面试教练</b><small>Caddie 专家团 · 逐题深度校准</small></div></div>
        <div class="iv-coach-workspace-title"><h1>${escape(question?.normalized_question || question?.original_question || '本题讨论')}</h1><p>${escape(question?.company || '')} · 第 ${question?.round_number || '-'} 轮 · 所有修改均需手动确认</p></div>
        <button class="btn ghost sm" onclick="CaddieInterview.closeCoachWorkspace()">关闭工作台</button>
      </header>
      <aside class="iv-coach-context-pane">
        <div class="iv-coach-pane-title"><b>本题上下文</b><span>自动读取</span></div>
        <section class="iv-coach-context-primary"><span>面试题</span><p>${escape(question?.normalized_question || question?.original_question || '')}</p></section>
        <details open><summary>真实回答证据</summary><p>${escape(evidence || '逐字稿中没有识别到可靠回答。')}</p></details>
        <details><summary>当前可编辑答案 · v${workspace?.current_version || 0}</summary><p>${escape(answer || '尚未形成可编辑答案。')}</p></details>
        <details><summary>当前批改基线 · v${coaching?.version || 0}</summary><p>${escape(coaching?.interviewer_intent || '尚未形成批改结论。')}</p></details>
        <div class="iv-coach-context-note"><b>讨论边界</b><p>你可以质疑判断、补充事实或要求重组答案。教练不会改写原始逐字稿，也不会未经确认覆盖批改文档。</p></div>
      </aside>
      <button class="iv-coach-resizer" data-side="left" aria-label="拖动调整上下文面板宽度" title="拖动调整宽度，双击复位" onpointerdown="CaddieInterview.startCoachResize(event,'left')" ondblclick="CaddieInterview.resetCoachResize('left')"></button>
      <main class="iv-coach-conversation-pane">
        <div class="iv-coach-conversation-bar"><div><b>${hasDiscussion ? '校准这道题' : '发起一次校准'}</b><span>${hasDiscussion ? '围绕意图、事实、诊断与答法持续讨论' : '先明确分歧，再决定是否修改批改文档'}</span></div>${hasDiscussion ? '<button class="btn ghost sm" onclick="CaddieInterview.focusCoachComposer()">提出新问题</button>' : ''}</div>
        ${questionCoachThreadHtml(messages, qid)}
      </main>
      <button class="iv-coach-resizer" data-side="right" aria-label="拖动调整决策面板宽度" title="拖动调整宽度，双击复位" onpointerdown="CaddieInterview.startCoachResize(event,'right')" ondblclick="CaddieInterview.resetCoachResize('right')"></button>
      <aside class="iv-coach-output-pane">${coachOutputPaneHtml(messages, coaching, qid)}</aside>
    </div>`;
  }

  function questionCoachingHtml(coaching, messages, qid) {
    const activeJob = activeQuestionJob(qid);
    const taskState = activeJob ? `<div class="iv-question-background-task">
      <div><span></span><p><b>${escape(activeJob.label || '正在后台批改')}</b><small>${escape(activeJob.detail || '可以离开当前页面，完成后会通知你')}</small></p><strong>${Math.max(4, Number(activeJob.progress || 8))}%</strong></div>
      <i><em style="width:${Math.max(4, Number(activeJob.progress || 8))}%"></em></i>
    </div>` : '';
    if (!coaching) {
      return `<section class="iv-deep-coach iv-deep-coach-empty">
        <div class="iv-deep-coach-head"><div><span class="iv-kicker">逐题批改</span><h2>先理解问题，再修改答案</h2></div></div>
        <p>系统会结合题目、真实回答、目标岗位和关联经历，解释面试官为什么问、原回答哪里没有答到位，以及怎样回答才能形成闭环。</p>
        <textarea id="ivQuestionCoachInstruction" placeholder="可选：补充面试上下文，或注明哪些事实不能改"></textarea>
        <button class="btn iv-coach-primary" onclick="CaddieInterview.analyzeQuestion(${qid})" ${activeJob ? 'disabled' : ''}>${activeJob ? '后台批改中' : '开始深度批改'}</button>
        ${taskState}
        <div id="ivQuestionCoachProgress" class="iv-ai-progress"></div>
      </section>`;
    }
    const paragraph = value => `<div class="iv-coach-prose">${markdown(value || '暂无可靠结论')}</div>`;
    const discussionCount = (Array.isArray(messages) ? messages : []).filter(item => item.role === 'user').length;
    return `<section class="iv-deep-coach">
      <div class="iv-deep-coach-head"><div><span class="iv-kicker">逐题批改 · v${coaching.version || 1}</span><h2>本题批改结论</h2><small>基于答案 v${coaching.source_answer_version || 0} · ${escape(coaching.model_provider || '已配置模型')}${coaching.model_name ? ' / ' + escape(coaching.model_name) : ''}</small></div><div class="iv-coach-head-actions"><button class="btn sm" onclick="CaddieInterview.openCoachWorkspace()">${discussionCount ? `继续讨论 · ${discussionCount}` : '打开教练工作台'}</button><button class="btn ghost sm" onclick="CaddieInterview.analyzeQuestion(${qid})" ${activeJob ? 'disabled' : ''}>${activeJob ? '重建中' : '整份重建'}</button></div></div>
      ${taskState}
      <article class="iv-coach-lead"><span>01 · 核心判断</span><h3>面试官在判断什么</h3>${paragraph(coaching.interviewer_intent)}<details><summary>为什么会问到这里</summary>${paragraph(coaching.why_asked)}</details></article>
      <section class="iv-coach-document-section"><div class="iv-coach-section-title"><span>02</span><div><h3>当前答案诊断</h3><p>先保留有效信息，再修正没有闭环的部分。</p></div></div><div class="iv-coach-columns"><div><h4>已经答到的</h4>${coachingList(coaching.strengths, '尚未识别到明确优势')}</div><div class="issue"><h4>需要重写的</h4>${coachingList(coaching.issues, '尚未形成可靠诊断')}</div></div></section>
      <section class="iv-coach-document-section"><div class="iv-coach-section-title"><span>03</span><div><h3>改答方案</h3><p>明确面试官的满意标准，再按路径组织答案。</p></div></div><div class="iv-coach-plan"><div><h4>答好这题需要满足</h4>${coachingList(coaching.satisfaction_criteria, '暂无明确标准')}</div><div><h4>建议作答路径</h4>${paragraph(coaching.answer_strategy)}</div></div></section>
      <section class="iv-coach-reference"><div class="iv-coach-section-title"><span>04</span><div><h3>参考答法</h3><p>这是可继续编辑的候选版本，不会自动覆盖你的答案。</p></div></div>${paragraph(coaching.improved_answer)}${coaching.improved_answer ? '<button class="btn ghost sm" onclick="CaddieInterview.adoptCoachedAnswer()">采用到左侧继续编辑</button>' : ''}</section>
      <section class="iv-coach-next"><details open><summary>面试官可能继续追问</summary>${coachingList(coaching.followups, '暂无')}</details>
      ${(coaching.evidence_gaps || []).length ? `<details class="iv-coach-warning" open><summary>仍需补充确认的事实</summary>${coachingList(coaching.evidence_gaps)}</details>` : ''}</section>
      <div class="iv-coach-confidence">${escape(coaching.confidence_note || '')}</div>
      <div id="ivQuestionCoachProgress" class="iv-ai-progress"></div>
    </section>`;
  }

  I.openLibraryQuestion = async function (qid, origin) {
    const view = $('#view'); if (!view) return;
    if (origin) {
      state().questionReturn = typeof origin === 'string'
        ? {type:origin, label:origin === 'experience' ? '返回我的经历' : '返回真题题库'}
        : origin;
    }
    const returnContext = state().questionReturn || {type:'bank', label:'返回真题题库'};
    const returnLabel = returnContext.label || (returnContext.type === 'experience' ? '返回我的经历' : '返回真题题库');
    I.destroyQuestionAnswerEditor();
    view.innerHTML = `<div class="iv-loading">正在打开题目…</div>`;
    try {
      const [detail, optionResult] = await Promise.all([
        ivApi('/v2/interview-questions/' + qid + '/detail'),
        ivApi('/v2/interview-questions/classification-options')
      ]);
      const q = detail.data, options = optionResult.data || {}, ws = q.answer_workspace || {};
      state().activeQuestion = q; state().questionOptions = options; state().editingLibraryQuestion = true; state().questionPageActive = true;
      const rawAnswers = (q.answers || []).filter(answer => answer.is_self);
      view.innerHTML = `<section class="iv-question-page">
        <header class="iv-question-page-head"><div><button class="iv-text-back" onclick="CaddieInterview.returnFromQuestion()">← ${escape(returnLabel)}</button><span class="iv-kicker">${escape(q.question_type || '待分类')} · ${escape(q.company || '')} · 第 ${q.round_number} 轮</span><h1>${escape(q.normalized_question || q.original_question)}</h1><p>${escape(q.intent || '这道题的考察意图尚待补充')}</p></div><div class="iv-question-page-state"><span class="${q.confirmed?'ready':'pending'}">${q.confirmed?'已归档':'待确认归档'}</span><b>答案 v${ws.current_version || 0}</b></div></header>
        <details class="iv-question-archive iv-question-archive-top"><summary><div><span class="iv-kicker">题目资产</span><b>归档与检索信息</b><small>${escape(q.question_type || '待分类')} · ${escape(q.ability_key || '待确认能力')} · ${questionKnowledgeTopics(q).map(escape).join(' / ') || '待补知识主题'}</small></div><span>展开编辑</span></summary>${questionClassifierHtml(q, options)}</details>
        <div class="iv-answer-workbench">
          <main class="iv-answer-main">
            <section class="iv-answer-editor-card"><div class="iv-answer-card-head"><div><span class="iv-kicker">我的答案</span><h2>可编辑答法</h2><p>以真实回答为起点持续打磨；保存会新增版本，不覆盖原始逐字稿。</p></div><button class="btn sm" onclick="CaddieInterview.saveQuestionAnswer(${qid})">保存答案</button></div>
              <div id="ivQuestionAnswer" class="iv-answer-editor-host" aria-label="可编辑答案"></div>
              <div class="iv-answer-save-state" id="ivAnswerSaveState">${ws.has_detected_answer ? '已从真实回答建立初稿' : '没有可靠识别到回答，可手动填写或让 AI 从逐字稿找回'}</div>
            </section>
            <details class="iv-evidence-answer" ${ws.has_detected_answer?'':'open'}><summary><span>原始回答证据</span><small>只读，不会被后续编辑覆盖</small></summary><div>${rawAnswers.length ? rawAnswers.map(a=>`<article><b>${escape(a.display_name || '我')}</b><p>${escape(a.organized_answer || a.original_answer || '')}</p></article>`).join('') : `<div class="iv-muted">拆解结果没有可靠挂接回答。<button class="iv-inline-action" onclick="CaddieInterview.generateQuestionAnswer(${qid},'recover')">从逐字稿重新识别</button></div>`}</div></details>
          </main>
          <button class="iv-answer-resizer" aria-label="拖动调整答案与批改区域宽度" title="拖动调整宽度，双击恢复默认" onpointerdown="CaddieInterview.startAnswerResize(event)" ondblclick="CaddieInterview.resetAnswerResize()"></button>
          <aside class="iv-answer-assistant">
            ${questionCoachingHtml(ws.question_coaching, ws.coaching_messages, qid)}
          </aside>
        </div>
        ${ws.question_coaching ? questionCoachWorkspaceHtml(ws.coaching_messages, qid, ws.question_coaching, q, ws) : ''}
      </section>`;
      requestAnimationFrame(() => {
        I.restoreAnswerWorkbenchWidth();
        I.mountQuestionAnswerEditor(ws.current_answer || '');
      });
    } catch (e) {
      view.innerHTML = `<div class="iv-empty"><b>题目打开失败</b><p>${escape(e.message)}</p><button class="btn ghost" onclick="CaddieInterview.returnFromQuestion()">${escape(returnLabel)}</button></div>`;
    }
  };

  I.returnFromQuestion = function () {
    const returnContext = state().questionReturn || {type:'bank'};
    I.destroyQuestionAnswerEditor();
    state().questionPageActive = false;
    state().editingLibraryQuestion = false;
    if (returnContext.type === 'experience' && bridge().returnToExperienceQuestion) {
      return bridge().returnToExperienceQuestion(returnContext);
    }
    if (returnContext.type === 'overview') {
      const view = $('#view');
      return view ? I.renderLibrary(view) : null;
    }
    return I.openQuestionBankPage();
  };

  I.saveQuestionAnswer = async function (qid) {
    const body = I.currentQuestionAnswer();
    if (!body) return ivToast('答案不能为空');
    const stateEl = $('#ivAnswerSaveState'); if (stateEl) stateEl.textContent = '正在保存…';
    try {
      const result = await ivApi('/v2/interview-questions/' + qid + '/answer', {method:'PUT', body:JSON.stringify({body})});
      state().activeQuestion = result.data.question;
      state().questionAnswerMarkdown = body;
      if (stateEl) stateEl.textContent = `已保存为 v${result.data.version.version}`;
      ivToast('答案已保存并保留版本');
    } catch (e) {
      if (stateEl) stateEl.textContent = '保存失败';
      ivToast(e.message || '答案保存失败');
    }
  };

  I.generateQuestionAnswer = async function (qid, mode) {
    const progress = $('#ivAIAnswerProgress');
    if (progress) progress.innerHTML = '<span></span> AI 正在读取上下文并生成…';
    document.querySelectorAll('.iv-ai-coach-panel button').forEach(button => button.disabled = true);
    try {
      if (mode === 'improve') await I.saveQuestionAnswer(qid);
      const result = await ivApi('/v2/interview-questions/' + qid + '/ai-answer', {method:'POST', body:JSON.stringify({mode,instruction:$('#ivAIAnswerInstruction')?.value.trim() || null})});
      state().activeQuestion = result.data.question;
      if (mode === 'recover') {
        I.replaceQuestionAnswer(result.data.answer || '');
        const saveState = $('#ivAnswerSaveState');
        if (saveState) saveState.textContent = '已从逐字稿恢复候选答案，请核对后保存';
        ivToast('已从逐字稿重新识别回答');
        return;
      }
      const card = $('#ivAIAnswerCard'); if (card) card.hidden = false;
      const answer = $('#ivAIAnswer'); if (answer) answer.textContent = result.data.answer || '';
      const coaching = $('#ivAICoaching'); if (coaching) coaching.innerHTML = markdown(result.data.coaching || '');
      if (progress) progress.textContent = 'AI 候选版本已生成并留存';
    } catch (e) {
      if (progress) progress.textContent = e.message || 'AI 辅助失败';
    } finally {
      document.querySelectorAll('.iv-ai-coach-panel button').forEach(button => button.disabled = false);
    }
  };

  I.analyzeQuestion = async function (qid) {
    const progress = $('#ivQuestionCoachProgress');
    const button = document.querySelector('.iv-coach-primary, .iv-deep-coach-head .iv-coach-head-actions button:last-child');
    if (activeQuestionJob(qid)) return ivToast('这道题已经在后台批改');
    if (progress) progress.innerHTML = '<span></span> 正在保存当前答案并创建后台任务…';
    if (button) button.disabled = true;
    try {
      const current = I.currentQuestionAnswer();
      const saved = String(state().activeQuestion?.answer_workspace?.current_answer || '').trim();
      if (current && current !== saved) await I.saveQuestionAnswer(qid);
      const instruction = String($('#ivQuestionCoachInstruction')?.value || '').trim() || null;
      const result = await ivApi('/v2/interview-questions/' + qid + '/coach/tasks', {
        method:'POST', body:JSON.stringify({instruction})
      });
      I.registerBackgroundJob(
        result.data,
        state().activeQuestion,
        state().questionReturn
      );
      if (progress) progress.textContent = '已转入后台，可以继续浏览其他页面';
      ivToast('深度批改已在后台开始');
      await I.openLibraryQuestion(qid);
    } catch (e) {
      if (progress) progress.textContent = e.message || '后台任务创建失败';
    } finally {
      if (button) button.disabled = false;
    }
  };

  I.adoptCoachedAnswer = function () {
    const coaching = state().activeQuestion?.answer_workspace?.question_coaching;
    if (!coaching?.improved_answer) return;
    I.replaceQuestionAnswer(coaching.improved_answer);
    const saveState = $('#ivAnswerSaveState');
    if (saveState) saveState.textContent = '已采用批改后的参考答法，修改后点击保存';
  };

  I.openCoachWorkspace = function () {
    const workspace = $('#ivCoachWorkspace');
    if (!workspace) return;
    workspace.hidden = false;
    document.body.classList.add('iv-coach-workspace-open');
    requestAnimationFrame(() => {
      workspace.classList.add('on');
      I.restoreCoachPanelWidths();
      const thread = $('#ivCoachMessages');
      if (thread) thread.scrollTop = thread.scrollHeight;
    });
  };

  I.openCoachDrawer = I.openCoachWorkspace;

  I.setCoachPrompt = function (value) {
    const input = $('#ivCoachMessage');
    if (!input) return;
    input.value = value;
    input.focus();
  };

  I.focusCoachComposer = function () {
    $('#ivCoachMessage')?.focus();
  };

  I.closeCoachWorkspace = function () {
    const workspace = $('#ivCoachWorkspace');
    if (!workspace) return;
    workspace.classList.remove('on');
    document.body.classList.remove('iv-coach-workspace-open');
    setTimeout(() => {
      if (!workspace.classList.contains('on')) workspace.hidden = true;
    }, 180);
  };

  I.closeCoachDrawer = I.closeCoachWorkspace;

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  I.restoreAnswerWorkbenchWidth = function () {
    const workbench = $('.iv-answer-workbench');
    if (!workbench || window.innerWidth <= 1100) return;
    const bounds = workbench.getBoundingClientRect();
    const fallback = Math.round(bounds.width * .48);
    const left = clamp(
      Number(localStorage.getItem('caddie-answer-left-width')) || fallback,
      360,
      Math.max(360, bounds.width - 505)
    );
    workbench.style.setProperty('--iv-answer-left', `${left}px`);
  };

  I.startAnswerResize = function (event) {
    const workbench = event.currentTarget?.closest('.iv-answer-workbench');
    if (!workbench || window.innerWidth <= 1100) return;
    event.preventDefault();
    const startX = event.clientX;
    const bounds = workbench.getBoundingClientRect();
    const initial = workbench.querySelector('.iv-answer-main')?.getBoundingClientRect().width || bounds.width * .48;
    document.body.classList.add('iv-answer-resizing');
    const move = moveEvent => {
      const next = clamp(initial + moveEvent.clientX - startX, 360, Math.max(360, bounds.width - 505));
      workbench.style.setProperty('--iv-answer-left', `${Math.round(next)}px`);
    };
    const stop = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', stop);
      document.body.classList.remove('iv-answer-resizing');
      const value = Math.round(workbench.querySelector('.iv-answer-main')?.getBoundingClientRect().width || initial);
      localStorage.setItem('caddie-answer-left-width', String(value));
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', stop, {once:true});
  };

  I.resetAnswerResize = function () {
    const workbench = $('.iv-answer-workbench');
    if (!workbench) return;
    localStorage.removeItem('caddie-answer-left-width');
    workbench.style.removeProperty('--iv-answer-left');
    I.restoreAnswerWorkbenchWidth();
  };

  I.restoreCoachPanelWidths = function () {
    const workspace = $('#ivCoachWorkspace');
    if (!workspace || window.innerWidth <= 840) return;
    const left = clamp(Number(localStorage.getItem('caddie-coach-left-width')) || 300, 230, 520);
    const right = clamp(Number(localStorage.getItem('caddie-coach-right-width')) || 370, 280, 560);
    workspace.style.setProperty('--iv-coach-left', `${left}px`);
    workspace.style.setProperty('--iv-coach-right', `${right}px`);
  };

  I.startCoachResize = function (event, side) {
    const workspace = event.currentTarget?.closest('.iv-coach-workspace');
    if (!workspace || window.innerWidth <= 840) return;
    event.preventDefault();
    const startX = event.clientX;
    const bounds = workspace.getBoundingClientRect();
    const css = getComputedStyle(workspace);
    const property = side === 'left' ? '--iv-coach-left' : '--iv-coach-right';
    const initial = parseFloat(css.getPropertyValue(property)) || (side === 'left' ? 300 : 370);
    const minimum = side === 'left' ? 230 : 280;
    const maximum = side === 'left' ? 520 : 560;
    const middleMinimum = 430;
    document.body.classList.add('iv-coach-resizing');
    const move = moveEvent => {
      const delta = moveEvent.clientX - startX;
      let next = side === 'left' ? initial + delta : initial - delta;
      const other = parseFloat(css.getPropertyValue(side === 'left' ? '--iv-coach-right' : '--iv-coach-left')) || (side === 'left' ? 370 : 300);
      const maxByViewport = Math.max(minimum, bounds.width - other - middleMinimum - 10);
      next = clamp(next, minimum, Math.min(maximum, maxByViewport));
      workspace.style.setProperty(property, `${Math.round(next)}px`);
    };
    const stop = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', stop);
      document.body.classList.remove('iv-coach-resizing');
      const value = Math.round(parseFloat(getComputedStyle(workspace).getPropertyValue(property)) || initial);
      localStorage.setItem(side === 'left' ? 'caddie-coach-left-width' : 'caddie-coach-right-width', String(value));
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', stop, {once:true});
  };

  I.resetCoachResize = function (side) {
    const workspace = $('#ivCoachWorkspace');
    if (!workspace) return;
    const value = side === 'left' ? 300 : 370;
    workspace.style.setProperty(side === 'left' ? '--iv-coach-left' : '--iv-coach-right', `${value}px`);
    localStorage.removeItem(side === 'left' ? 'caddie-coach-left-width' : 'caddie-coach-right-width');
  };

  I.closeCoachKnowledgeDraft = function () {
    const s = state();
    if (s.coachKnowledgeEditor?.destroy) {
      try { s.coachKnowledgeEditor.destroy(); } catch (_) {}
    }
    s.coachKnowledgeEditor = null;
    s.coachKnowledgeMarkdown = null;
    $('#ivCoachKnowledgeOverlay')?.remove();
  };

  I.extractCoachKnowledge = async function (qid) {
    const existing = $('#ivCoachKnowledgeOverlay');
    if (existing) existing.remove();
    const overlay = document.createElement('div');
    overlay.id = 'ivCoachKnowledgeOverlay';
    overlay.className = 'iv-coach-knowledge-overlay';
    overlay.innerHTML = `<section class="iv-coach-knowledge-dialog"><header><div><span class="iv-kicker">知识沉淀</span><h2>正在提炼可复用主题</h2><p>AI 会区分通用知识、你的事实和教练判断。生成后仍需你编辑确认。</p></div><button class="iv-icon-close" aria-label="关闭" onclick="CaddieInterview.closeCoachKnowledgeDraft()">×</button></header><div class="iv-coach-knowledge-loading"><span></span><b>正在读取题目与讨论上下文…</b><p>这一步只生成候选文档，不会直接写入知识库。</p></div></section>`;
    document.body.appendChild(overlay);
    try {
      const result = await ivApi('/v2/interview-questions/' + qid + '/knowledge-draft', {
        method:'POST', body:JSON.stringify({instruction:null})
      });
      const draft = result.data || {};
      const dialog = overlay.querySelector('.iv-coach-knowledge-dialog');
      dialog.innerHTML = `<header><div><span class="iv-kicker">知识沉淀</span><h2>编辑后确认入库</h2><p>默认保存到当前岗位的“面试准备”；也可以改为个人通用知识。</p></div><button class="iv-icon-close" aria-label="关闭" onclick="CaddieInterview.closeCoachKnowledgeDraft()">×</button></header>
        <div class="iv-coach-knowledge-fields">
          <label><span>标题</span><input id="ivCoachKnowledgeTitle" value="${escape(draft.title || '')}"></label>
          <label><span>主题</span><input id="ivCoachKnowledgeTopic" value="${escape(draft.topic || '')}"></label>
          <label><span>保存范围</span><select id="ivCoachKnowledgeScope"><option value="track" ${draft.track_id ? 'selected' : ''}>当前岗位</option><option value="global" ${draft.track_id ? '' : 'selected'}>个人通用</option></select></label>
        </div>
        <div class="iv-coach-knowledge-editor" id="ivCoachKnowledgeEditor"></div>
        <footer><span id="ivCoachKnowledgeStatus">可直接修改正文，底层自动保存为结构化文档。</span><div><button class="btn ghost" onclick="CaddieInterview.closeCoachKnowledgeDraft()">取消</button><button class="btn" onclick="CaddieInterview.saveCoachKnowledge(${qid})">确认存入知识库</button></div></footer>`;
      const host = $('#ivCoachKnowledgeEditor');
      const s = state();
      s.coachKnowledgeMarkdown = draft.content || '';
      if (window.CaddieKnowledgeEditor?.mount) {
        s.coachKnowledgeEditor = window.CaddieKnowledgeEditor.mount(host, {
          initialMarkdown: draft.content || '',
          onChange: markdown => { s.coachKnowledgeMarkdown = markdown; },
          onReady: async editor => {
            if (editor?.document && typeof editor.blocksToMarkdownLossy === 'function') {
              s.coachKnowledgeMarkdown = await editor.blocksToMarkdownLossy(editor.document);
            }
            host.dataset.editor = 'blocknote';
          }
        });
      } else {
        host.innerHTML = `<textarea>${escape(draft.content || '')}</textarea>`;
        host.querySelector('textarea')?.addEventListener('input', event => {
          s.coachKnowledgeMarkdown = event.target.value;
        });
      }
    } catch (error) {
      const loading = overlay.querySelector('.iv-coach-knowledge-loading');
      if (loading) loading.innerHTML = `<b>知识提炼失败</b><p>${escape(error.message || '请稍后重试')}</p><button class="btn ghost" onclick="CaddieInterview.closeCoachKnowledgeDraft()">关闭</button>`;
    }
  };

  I.saveCoachKnowledge = async function (qid) {
    const title = String($('#ivCoachKnowledgeTitle')?.value || '').trim();
    const topic = String($('#ivCoachKnowledgeTopic')?.value || '').trim();
    const scope_type = String($('#ivCoachKnowledgeScope')?.value || 'track');
    const content = String(state().coachKnowledgeMarkdown || '').trim();
    if (!title || !content) return ivToast('标题和知识正文不能为空');
    const status = $('#ivCoachKnowledgeStatus');
    const button = $('#ivCoachKnowledgeOverlay footer .btn:not(.ghost)');
    if (status) status.textContent = '正在写入知识库并保存版本…';
    if (button) button.disabled = true;
    try {
      await ivApi('/v2/interview-questions/' + qid + '/knowledge', {
        method:'POST', body:JSON.stringify({title, topic, content, scope_type})
      });
      ivToast('知识已保存，可在准备知识中继续编辑');
      I.closeCoachKnowledgeDraft();
    } catch (error) {
      if (status) status.textContent = error.message || '保存失败，请重试';
      if (button) button.disabled = false;
    }
  };

  function showPendingCoachExchange(message) {
    const workspace = $('#ivCoachWorkspace');
    const thread = $('#ivCoachMessages');
    const threadShell = thread?.closest('.iv-coach-thread');
    if (!thread || !threadShell) return;
    workspace?.classList.remove('is-empty');
    workspace?.classList.add('has-discussion');
    threadShell.classList.remove('is-empty');
    threadShell.classList.add('has-messages');
    thread.innerHTML = `${thread.querySelector('.iv-coach-thread-empty') ? '' : thread.innerHTML}
      <article class="user iv-coach-live-message"><span>我</span><p>${escape(message).replace(/\n/g,'<br>')}</p><small>已发送</small></article>
      <article class="assistant pending iv-coach-live-message" id="ivCoachPendingReply">
        <span>面试教练</span>
        <p><b class="iv-coach-thinking-dots"><i></i><i></i><i></i></b> 正在理解你的补充，并核对题目、原回答与当前批改结论…</p>
        <small>分析完成后会在这里直接回复</small>
      </article>`;
    const bar = workspace?.querySelector('.iv-coach-conversation-bar');
    if (bar) bar.innerHTML = '<div><b>校准这道题</b><span>围绕意图、事实、诊断与答法持续讨论</span></div>';
    requestAnimationFrame(() => { thread.scrollTop = thread.scrollHeight; });
  }

  function showCoachExchangeError(message) {
    const pending = $('#ivCoachPendingReply');
    if (!pending) return;
    pending.classList.remove('pending');
    pending.classList.add('error');
    pending.innerHTML = `<span>面试教练</span><p>${escape(message || '本轮分析失败，请稍后重试。')}</p><small>你的输入已保留，可以重新发送</small>`;
  }

  I.sendCoachMessage = async function (qid) {
    const input = $('#ivCoachMessage');
    const message = String(input?.value || '').trim();
    if (!message) return ivToast('先写下你不同意或想继续追问的地方');
    const progress = $('#ivCoachMessageProgress');
    const button = input?.parentElement?.querySelector('button');
    const originalButtonText = button?.textContent || '发送';
    showPendingCoachExchange(message);
    if (input) input.value = '';
    if (progress) progress.innerHTML = '<span></span> 输入已接收，正在生成教练回复…';
    if (button) {
      button.disabled = true;
      button.textContent = '发送中';
    }
    try {
      const current = I.currentQuestionAnswer();
      const saved = String(state().activeQuestion?.answer_workspace?.current_answer || '').trim();
      if (current && current !== saved) await I.saveQuestionAnswer(qid);
      const result = await ivApi('/v2/interview-questions/' + qid + '/coach/messages', {
        method:'POST', body:JSON.stringify({message})
      });
      state().activeQuestion = result.data.question;
      await I.openLibraryQuestion(qid);
      requestAnimationFrame(() => {
        I.openCoachWorkspace();
      });
    } catch (e) {
      const errorMessage = e.message || '教练对话失败';
      if (input) input.value = message;
      if (progress) progress.textContent = errorMessage;
      showCoachExchangeError(errorMessage);
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = originalButtonText;
      }
    }
  };

  I.applyCoachProposal = async function (qid, messageId) {
    try {
      const result = await ivApi('/v2/interview-questions/' + qid + '/coach/messages/' + messageId + '/apply', {
        method:'POST'
      });
      state().activeQuestion = result.data.question;
      ivToast('候选修改已生成新的批改版本');
      await I.openLibraryQuestion(qid);
      requestAnimationFrame(() => I.openCoachWorkspace());
    } catch (e) {
      ivToast(e.message || '候选修改采纳失败');
    }
  };

  I.adoptAIAnswer = function () {
    const source = $('#ivAIAnswer');
    if (!source) return;
    I.replaceQuestionAnswer(source.textContent || '');
    $('#ivAnswerSaveState').textContent = '已采用 AI 候选版本，修改后点击保存';
  };

  const growthDocumentMeta = {
    profile: {label:'能力画像', hint:'高频考点、稳定优势、反复短板与未验证项'},
    trend: {label:'时间趋势', hint:'按面试时间追踪已解决、改善中和新出现的问题'},
    preparation: {label:'下次准备', hint:'按优先级给出可执行动作和完成标准'},
    mock: {label:'模拟训练', hint:'面试官逐题提问、动态追问，可随时离开后继续'},
    answer_sheet: {label:'模拟答卷', hint:'结束模拟后自动归档的可编辑完整答卷'},
    reference: {label:'参考答案', hint:'结构、可用历史证据、待补事实和可能追问'},
  };

  function growthCandidateHtml(item, checked=true) {
    return `<label class="iv-growth-candidate"><input type="checkbox" value="${item.id}" ${checked?'checked':''}><span><b>${escape(item.company || '')} · ${escape(item.role || '')}</b><small>${escape(item.date || '日期未知')} · 第 ${item.round_number || '?'} 轮 · ${item.question_count || 0} 题</small><em>${(item.match_reasons || []).map(escape).join(' · ')}</em></span></label>`;
  }

  function growthFeatureAvailable() {
    return typeof featureAvailable === 'function' && featureAvailable('interview_growth_analysis');
  }

  function showGrowthFeatureLocked() {
    if (typeof showLockedFeature === 'function') showLockedFeature('interview_growth_analysis');
    else ivToast('综合分析与训练尚未开放，敬请期待');
  }

  I.openGrowthAnalysisIndex = async function () {
    if (!growthFeatureAvailable()) return showGrowthFeatureLocked();
    const view = $('#view'); if (!view) return;
    state().activeGrowthId = null;
    state().questionPageActive = false;
    view.innerHTML = `<div class="iv-growth-loading">正在读取历史综合分析…</div>`;
    try {
      const result = await ivApi('/v2/interview-growth-analyses');
      const items = result.items || result.data?.items || [];
      view.innerHTML = `<section class="iv-growth-page iv-growth-index">
        <header class="iv-growth-head"><div><button class="iv-text-back" onclick="CaddieInterview.renderLibrary(document.querySelector('#view'))">← 返回面试总览</button><span class="iv-kicker">跨场学习</span><h1>综合分析与训练</h1><p>不再只看单次得失，而是比较同类岗位的历史面试，找出长期问题、进步证据和下一轮训练重点。</p></div><button class="btn" onclick="CaddieInterview.openGrowthCreate()">＋ 新建分析</button></header>
        <div class="iv-growth-list">${items.length ? items.map(item => `<button class="iv-growth-row" onclick="CaddieInterview.openGrowthAnalysisPage(${item.id})"><span class="iv-growth-status ${escape(item.status || '')}">${item.status === 'completed' ? '已完成' : item.status === 'failed' ? '需重试' : '进行中'}</span><div><b>${escape(item.title)}</b><small>${escape(item.role_family)}${item.role_subtype ? ' · ' + escape(item.role_subtype) : ''} · ${(item.selected_round_ids || []).length} 场面试</small><p>${escape(item.summary || item.stage || '等待生成')}</p></div><em>${item.progress || 0}% →</em></button>`).join('') : `<div class="iv-growth-empty"><b>还没有跨场分析</b><p>先选择一类岗位，确认要纳入的历史面试。</p><button class="btn" onclick="CaddieInterview.openGrowthCreate()">建立第一份分析</button></div>`}</div>
      </section>`;
    } catch (e) { view.innerHTML = `<div class="iv-empty"><b>读取失败</b><p>${escape(e.message)}</p></div>`; }
  };

  I.openGrowthCreate = function () {
    if (!growthFeatureAvailable()) return showGrowthFeatureLocked();
    const view = $('#view'); if (!view) return;
    state().activeGrowthId = null;
    state().growthCandidates = [];
    view.innerHTML = `<section class="iv-growth-page iv-growth-create">
      <header class="iv-growth-head"><div><button class="iv-text-back" onclick="CaddieInterview.openGrowthAnalysisIndex()">← 返回分析列表</button><span class="iv-kicker">建立分析范围</span><h1>你下一场要准备什么？</h1><p>系统先给出候选面试，由你确认范围。AI 产品经理的不同细分方向不会默认混在一起。</p></div></header>
      <div class="iv-growth-create-grid"><section class="iv-growth-form"><label><span>岗位类别</span><input id="ivGrowthFamily" value="AI产品经理" placeholder="如：AI产品经理" autocomplete="off"></label><label><span>细分方向 <i>可选</i></span><input id="ivGrowthSubtype" placeholder="如：AI Coding、企业智能、内容电商" autocomplete="off"></label><label class="iv-growth-date-toggle"><input id="ivGrowthUseDates" type="checkbox" onchange="CaddieInterview.toggleGrowthDates(this.checked)"><span>只分析指定时间范围</span></label><div id="ivGrowthDates" class="iv-growth-dates" hidden><label><span>开始日期</span><input id="ivGrowthFrom" type="date" value="" autocomplete="off"></label><label><span>结束日期</span><input id="ivGrowthTo" type="date" value="" autocomplete="off"></label></div><label><span>下一场岗位 / JD <i>可选</i></span><textarea id="ivGrowthJD" placeholder="粘贴新岗位的 JD 或你想重点验证的方向。它只会用于生成增量准备和模拟题。"></textarea></label><label><span>模拟题数量</span><input id="ivGrowthMockCount" type="number" min="5" max="30" value="10"></label><button class="btn" onclick="CaddieInterview.matchGrowthScope()">匹配历史面试</button></section><section class="iv-growth-scope"><div class="iv-growth-scope-head"><div><span class="iv-kicker">证据范围</span><h2>由你确认哪些面试可以比较</h2></div><span id="ivGrowthCount">0 场</span></div><div id="ivGrowthCandidates" class="iv-growth-candidates"><div class="iv-growth-scope-empty">填写岗位类别后点击“匹配历史面试”。</div></div><button id="ivGrowthCreateButton" class="btn iv-growth-submit" disabled onclick="CaddieInterview.createGrowthAnalysis()">生成综合分析与训练</button></section></div>
    </section>`;
  };

  I.toggleGrowthDates = function (enabled) {
    const dates = document.getElementById('ivGrowthDates');
    if (dates) dates.hidden = !enabled;
    if (!enabled) {
      const from = document.getElementById('ivGrowthFrom');
      const to = document.getElementById('ivGrowthTo');
      if (from) from.value = '';
      if (to) to.value = '';
    }
  };

  I.matchGrowthScope = async function () {
    if (!growthFeatureAvailable()) return showGrowthFeatureLocked();
    const family = growthValue('ivGrowthFamily'); if (!family) return ivToast('请先填写岗位类别');
    const params = new URLSearchParams({role_family:family});
    const subtype = growthValue('ivGrowthSubtype');
    const useDates = document.getElementById('ivGrowthUseDates')?.checked;
    const from = useDates ? growthValue('ivGrowthFrom') : '';
    const to = useDates ? growthValue('ivGrowthTo') : '';
    if (subtype) params.set('role_subtype', subtype); if (from) params.set('date_from', from); if (to) params.set('date_to', to);
    const host = $('#ivGrowthCandidates'); host.innerHTML = '<div class="iv-growth-scope-empty">正在检索同类历史面试…</div>';
    try {
      const result = await ivApi('/v2/interview-growth/scope?' + params.toString());
      const items = result.data?.candidates || []; state().growthCandidates = items;
      host.innerHTML = items.length ? items.map(item => growthCandidateHtml(item)).join('') : '<div class="iv-growth-scope-empty">没有匹配到已拆解的历史面试，可以调整细分方向或日期。</div>';
      $('#ivGrowthCount').textContent = `${items.length} 场`; $('#ivGrowthCreateButton').disabled = !items.length;
      host.querySelectorAll('input').forEach(input => input.addEventListener('change', () => { const n=host.querySelectorAll('input:checked').length; $('#ivGrowthCount').textContent=`${n} 场已选`; $('#ivGrowthCreateButton').disabled=!n; }));
    } catch (e) { host.innerHTML = `<div class="iv-growth-scope-empty">匹配失败：${escape(e.message)}</div>`; }
  };

  I.createGrowthAnalysis = async function () {
    if (!growthFeatureAvailable()) return showGrowthFeatureLocked();
    const selected = [...document.querySelectorAll('#ivGrowthCandidates input:checked')].map(input => Number(input.value));
    if (!selected.length) return ivToast('请至少选择一场历史面试');
    const family = growthValue('ivGrowthFamily'), subtype = growthValue('ivGrowthSubtype');
    const useDates = document.getElementById('ivGrowthUseDates')?.checked;
    const button = $('#ivGrowthCreateButton'); button.disabled=true; button.textContent='正在建立分析…';
    try {
      const response = await ivApi('/v2/interview-growth-analyses', {method:'POST', body:JSON.stringify({
        title:`${family}${subtype ? '·'+subtype : ''}综合分析与训练`, role_family:family, role_subtype:subtype || null,
        target_jd:growthValue('ivGrowthJD') || null, date_from:useDates ? growthValue('ivGrowthFrom') || null : null, date_to:useDates ? growthValue('ivGrowthTo') || null : null,
        selected_round_ids:selected, mock_question_count:Number(growthValue('ivGrowthMockCount') || 10),
      })});
      const data=response.data||{}; I.registerGrowthBackgroundJob({id:data.task_id,status:'queued',progress:4},data.analysis_id,`${family}综合分析`); I.openGrowthAnalysisPage(data.analysis_id);
    } catch (e) { button.disabled=false; button.textContent='生成综合分析与训练'; ivToast(e.message); }
  };

  function growthProgressHtml(analysis) {
    return `<section class="iv-growth-progress"><div><span class="iv-growth-spinner"></span><div><b>${escape(analysis.stage || '正在生成')}</b><small>任务已在后台运行，可以离开本页</small></div></div><strong>${analysis.progress || 0}%</strong><i><span style="width:${Math.max(3,analysis.progress||0)}%"></span></i></section>`;
  }

  function growthMockShellHtml(analysis) {
    return `<section id="ivGrowthMock" class="iv-growth-mock" data-analysis-id="${analysis.id}">
      <div class="iv-growth-mock-loading"><span class="iv-growth-spinner"></span><p>正在读取模拟进度…</p></div>
    </section>`;
  }

  function growthMockMessageHtml(item) {
    const interviewer=item.role==='assistant';
    const voice=item.voice||null, metrics=voice?.metrics||{};
    const voiceMeta=voice?`<small class="iv-growth-mock-message-meta">语音回答 · ${formatGrowthDuration(voice.duration_ms||0)}${metrics.chars_per_minute?` · ${metrics.chars_per_minute} 字/分钟`:''}${Number(metrics.filler_total||0)?` · ${metrics.filler_total} 个口头词`:''}</small>`:'';
    return `<article class="iv-growth-mock-message ${interviewer?'interviewer':'candidate'}">
      <span>${interviewer?'面试官':'我'}</span><div>${escape(item.content||'').replace(/\n/g,'<br>')}</div>${voiceMeta}
    </article>`;
  }

  function formatGrowthDuration(durationMs) {
    const seconds=Math.max(0,Math.round(Number(durationMs||0)/1000));
    return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
  }

  I.paintGrowthMock = function (data) {
    const host=$('#ivGrowthMock'); if(!host)return;
    state().growthMock=data;
    const status=data.status||'not_started', messages=data.messages||[];
    const answerCount=Number(data.answer_count||0), target=Number(data.question_target||10);
    const progress=Math.min(100,Math.round(answerCount/Math.max(1,target)*100));
    if(status==='not_started') {
      host.innerHTML=`<div class="iv-growth-mock-empty"><span class="iv-kicker">对话式模拟</span><h3>像真实面试一样，一次只回答一个问题</h3><p>面试官会读取本次综合分析、历史薄弱点和目标 JD，再根据你的回答继续追问。对话自动保存，可以随时离开。</p><div><span><b>${target}</b>道目标主题</span><span><b>动态</b>追问链</span><span><b>结束后</b>答卷与表达报告</span></div><button class="btn" onclick="CaddieInterview.startGrowthMock(false)">开始模拟面试</button></div>`;
      return;
    }
    const completed=status==='completed';
    host.innerHTML=`<header class="iv-growth-mock-toolbar"><div><span>${completed?'已归档':'模拟进行中'}</span><b>${completed?'本轮对话已生成答卷与表达报告':`已回答 ${answerCount} 题 · 目标 ${target} 题`}</b></div><div><button class="btn ghost" onclick="CaddieInterview.startGrowthMock(true)">重新开始</button>${completed?`<button class="btn" onclick="CaddieInterview.openGrowthDocument(${data.analysis_id},'answer_sheet')">查看模拟答卷</button>`:`<button class="btn ghost" onclick="CaddieInterview.finishGrowthMock()">结束并生成报告</button>`}</div><i><span style="width:${completed?100:progress}%"></span></i></header>
      <div id="ivGrowthMockStream" class="iv-growth-mock-stream">${messages.map(growthMockMessageHtml).join('')||'<div class="iv-growth-mock-loading">面试官正在准备第一个问题…</div>'}</div>
      ${completed?'':`<footer class="iv-growth-mock-composer">
        <div class="iv-growth-mock-compose-head"><div><button class="btn ghost" onclick="CaddieInterview.readGrowthMockQuestion()">朗读问题</button><button class="btn ghost" onclick="CaddieInterview.showWechatVoiceHelp()">微信语音输入</button></div><small>使用微信输入法快捷键口述，不额外产生转写费用</small></div>
        <div id="ivGrowthWechatHelp" class="iv-growth-wechat-help" hidden><b>使用微信输入法的语音快捷键</b><p>把光标放在答案框后，使用你在微信输入法设置中配置的语音快捷键口述。为了让表达批改识别语气词和重复，建议关闭自动润色，或保留原始口述再发送。</p></div>
        <textarea id="ivGrowthMockInput" placeholder="输入回答，或将光标放在这里后使用微信输入法语音快捷键口述。Enter 发送，Shift + Enter 换行。" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();CaddieInterview.sendGrowthMock()}"></textarea>
        <div class="iv-growth-mock-compose-foot"><small>回答自动保存；结束后生成表达批改报告。文字可分析语气词、重复、长句和结构，无法判断真实语速与音量。</small><button id="ivGrowthMockSend" class="btn" onclick="CaddieInterview.sendGrowthMock()">发送回答</button></div>
      </footer>`}`;
    const stream=$('#ivGrowthMockStream'); if(stream)stream.scrollTop=stream.scrollHeight;
  };

  I.loadGrowthMock = async function (analysisId) {
    try { const result=await ivApi(`/v2/interview-growth-analyses/${analysisId}/mock`); I.paintGrowthMock(result.data||{}); }
    catch(e){ const host=$('#ivGrowthMock'); if(host)host.innerHTML=`<div class="iv-growth-mock-error"><b>模拟训练读取失败</b><p>${escape(e.message)}</p></div>`; }
  };

  I.startGrowthMock = async function (reset) {
    if (!growthFeatureAvailable()) return showGrowthFeatureLocked();
    const current=state().growthMock||{};
    if(reset&&(current.messages||[]).length&&!window.confirm('重新开始会清空当前模拟对话，是否继续？'))return;
    const analysisId=Number(current.analysis_id||$('#ivGrowthMock')?.dataset.analysisId); if(!analysisId)return;
    const host=$('#ivGrowthMock'); if(host)host.innerHTML='<div class="iv-growth-mock-loading"><span class="iv-growth-spinner"></span><b>面试官正在阅读历史面试与目标岗位…</b><p>开场问题生成后会自动显示。</p></div>';
    try { const result=await ivApi(`/v2/interview-growth-analyses/${analysisId}/mock/start?reset=${reset?'true':'false'}`,{method:'POST'}); I.paintGrowthMock(result.data||{}); }
    catch(e){ivToast(e.message); I.loadGrowthMock(analysisId);}
  };

  I.sendGrowthMock = async function () {
    if (!growthFeatureAvailable()) return showGrowthFeatureLocked();
    if(state().growthMockBusy)return;
    const input=$('#ivGrowthMockInput'), message=String(input?.value||'').trim(); if(!message)return ivToast('请先输入你的回答');
    const analysisId=Number(state().growthMock?.analysis_id||0); if(!analysisId)return;
    state().growthMockBusy=true; input.disabled=true;
    const button=$('#ivGrowthMockSend'); if(button){button.disabled=true;button.textContent='面试官思考中…';}
    const stream=$('#ivGrowthMockStream'); if(stream){stream.insertAdjacentHTML('beforeend',growthMockMessageHtml({role:'user',content:message})+'<div class="iv-growth-mock-thinking"><span></span><span></span><span></span>面试官正在根据回答继续追问</div>');stream.scrollTop=stream.scrollHeight;}
    try { const result=await ivApi(`/v2/interview-growth-analyses/${analysisId}/mock/respond`,{method:'POST',body:JSON.stringify({message})}); I.paintGrowthMock(result.data||{}); }
    catch(e){ivToast(e.message); if(input){input.disabled=false;input.value=message;} I.loadGrowthMock(analysisId);}
    finally{state().growthMockBusy=false;}
  };

  I.readGrowthMockQuestion = function () {
    const messages=state().growthMock?.messages||[], question=[...messages].reverse().find(item=>item.role==='assistant')?.content;
    if(!question)return ivToast('还没有可朗读的面试问题');
    if(!('speechSynthesis' in window))return ivToast('当前设备不支持题目朗读');
    window.speechSynthesis.cancel();
    const utterance=new SpeechSynthesisUtterance(question); utterance.lang='zh-CN'; utterance.rate=.96;
    window.speechSynthesis.speak(utterance);
  };

  I.showWechatVoiceHelp = function () {
    const help=$('#ivGrowthWechatHelp'); if(help)help.hidden=!help.hidden;
    const input=$('#ivGrowthMockInput'); if(input)input.focus();
  };

  I.finishGrowthMock = async function () {
    if (!growthFeatureAvailable()) return showGrowthFeatureLocked();
    const analysisId=Number(state().growthMock?.analysis_id||0); if(!analysisId)return;
    if(!window.confirm('结束后会把完整对话整理为可编辑模拟答卷，并生成表达批改报告，是否继续？'))return;
    const host=$('#ivGrowthMock'); if(host)host.insertAdjacentHTML('afterbegin','<div id="ivGrowthMockFinish" class="iv-growth-mock-finish"><span class="iv-growth-spinner"></span><div><b>正在整理答卷与表达批改报告</b><small>真实回答、追问链与表达习惯会分层归档</small></div></div>');
    try { const result=await ivApi(`/v2/interview-growth-analyses/${analysisId}/mock/finish`,{method:'POST'}); I.paintGrowthMock(result.data||{}); ivToast('模拟面试已归档，答卷与表达批改报告已更新'); }
    catch(e){ivToast(e.message); $('#ivGrowthMockFinish')?.remove();}
  };

  I.openGrowthAnalysisPage = async function (analysisId, documentKey) {
    if (!growthFeatureAvailable()) return showGrowthFeatureLocked();
    const view=$('#view'); if(!view)return; state().activeGrowthId=Number(analysisId);
    view.innerHTML='<div class="iv-growth-loading">正在打开综合分析…</div>';
    try {
      const response=await ivApi('/v2/interview-growth-analyses/'+analysisId), analysis=response.data||{};
      const docs=analysis.documents||{}, available=Object.keys(growthDocumentMeta).filter(key=>docs[key]);
      const activeKey=(documentKey&&docs[documentKey]?documentKey:available[0])||'profile';
      const snapshot=analysis.source_snapshot||{}, rounds=snapshot.rounds||[];
      view.innerHTML=`<section class="iv-growth-page iv-growth-detail">
        <header class="iv-growth-detail-head"><div><button class="iv-text-back" onclick="CaddieInterview.openGrowthAnalysisIndex()">← 返回综合分析</button><span class="iv-kicker">${escape(analysis.role_family||'')}</span><h1>${escape(analysis.title||'')}</h1><p>${escape(analysis.summary||analysis.stage||'正在生成')}</p></div><button class="btn ghost" onclick="CaddieInterview.regenerateGrowth(${analysis.id})">重新生成</button></header>
        ${!['completed','failed'].includes(analysis.status)?growthProgressHtml(analysis):analysis.status==='failed'?`<div class="iv-growth-error"><b>生成未完成</b><span>${escape(analysis.error_message||'请重试')}</span></div>`:''}
        <div class="iv-growth-workspace"><nav class="iv-growth-docnav"><div><span>结构化产物</span>${Object.entries(growthDocumentMeta).map(([key,meta])=>`<button class="${key===activeKey?'on':''}" ${docs[key]?`onclick="CaddieInterview.openGrowthDocument(${analysis.id},'${key}')"`:''} ${docs[key]?'':'disabled'}><b>${escape(meta.label)}</b><small>${escape(meta.hint)}</small></button>`).join('')}</div><section><span>证据快照</span><b>${rounds.length || (analysis.selected_round_ids||[]).length} 场面试</b><small>${rounds.reduce((n,r)=>n+(r.questions||[]).length,0)} 道真题</small><button onclick="CaddieInterview.toggleGrowthEvidence()">查看范围</button><div id="ivGrowthEvidence" hidden>${rounds.map((r,i)=>`<p><b>I${i+1} ${escape(r.company||'')} · ${escape(r.role||'')}</b><span>${escape(r.date||'')} · 第 ${r.round_number||'?'} 轮 · ${(r.questions||[]).length} 题</span></p>`).join('')}</div></section></nav><main class="iv-growth-document"><header><div><span>${escape(growthDocumentMeta[activeKey]?.label||'等待生成')}</span><h2>${escape(activeKey==='mock'?`${analysis.role_family||''}对话式模拟面试`:docs[activeKey]?.title||'分析产物尚未完成')}</h2></div>${docs[activeKey]?`<small>${activeKey==='mock'?'对话自动保存 · 可随时离开后继续':'可直接编辑 · 自动保留版本'}</small>`:''}</header>${activeKey==='mock'?growthMockShellHtml(analysis):`<div id="ivGrowthDocument" class="iv-doc-editor iv-growth-editor" ${docs[activeKey]?`data-document-id="${docs[activeKey].id}"`:''}>${docs[activeKey]?'':'<div class="iv-growth-doc-empty">生成完成后，六份可编辑文档会出现在这里。</div>'}</div>`}</main></div>
      </section>`;
      if(activeKey==='mock') I.loadGrowthMock(analysis.id);
      else if(docs[activeKey]) I.mountDocument($('#ivGrowthDocument'),docs[activeKey].id);
      if(!['completed','failed'].includes(analysis.status)) window.setTimeout(()=>{ if(Number(state().activeGrowthId)===Number(analysisId)) I.openGrowthAnalysisPage(analysisId,activeKey); },1800);
    } catch(e){view.innerHTML=`<div class="iv-empty"><b>分析读取失败</b><p>${escape(e.message)}</p></div>`;}
  };

  I.openGrowthDocument = function (analysisId,key) { I.openGrowthAnalysisPage(analysisId,key); };
  I.toggleGrowthEvidence = function () { const node=$('#ivGrowthEvidence'); if(node)node.hidden=!node.hidden; };
  I.regenerateGrowth = async function (analysisId) { if (!growthFeatureAvailable()) return showGrowthFeatureLocked(); try { const response=await ivApi(`/v2/interview-growth-analyses/${analysisId}/generate`,{method:'POST',body:JSON.stringify({mock_question_count:10})}); const data=response.data||{}; I.registerGrowthBackgroundJob({id:data.task_id,status:'queued',progress:4},analysisId,'更新综合分析'); I.openGrowthAnalysisPage(analysisId); } catch(e){ivToast(e.message);} };

  I.renderLibrary = async function (view) {
    state().activeGrowthId = null;
    state().questionPageActive = false;
    state().editingLibraryQuestion = false;
    view.innerHTML = `<div class="view-head iv-library-head"><div><span class="iv-kicker">跨岗位面试驾驶舱</span><h1>面试与复盘</h1><p>先处理下一场和待复盘事项，再从真实面试中沉淀题目、薄弱点与可复用答法。</p></div></div><div id="ivLibrary"><div class="iv-loading">正在读取面试进程…</div></div>`;
    try {
      const data = (await ivApi('/v2/interview-library')).data;
      const calibration = data.calibration || {};
      const rounds = data.rounds || [], questions = data.questions || [];
      const now = Date.now();
      const timeOf = r => {
        const value = String(r.scheduled_at || '').trim();
        const parsed = value ? new Date(value.replace(' ', 'T')).getTime() : NaN;
        return Number.isFinite(parsed) ? parsed : Infinity;
      };
      const openRounds = rounds.filter(r => !['completed'].includes(r.status) && !['passed', 'failed'].includes(r.actual_result));
      const next = openRounds.filter(r => timeOf(r) >= now).sort((a, b) => timeOf(a) - timeOf(b))[0] || openRounds[0];
      const active = openRounds.filter(r => !next || r.id !== next.id).slice(0, 5);
      const needsReview = rounds.filter(r => ['interviewed', 'processing_failed', 'review_ready'].includes(r.status));
      const waiting = rounds.filter(r => ['awaiting_result'].includes(r.status) || r.actual_result === 'pending');
      const knowledgeGroups = {};
      questions.forEach(q => {
        const key = q.question_type || q.ability_key || '待分类';
        knowledgeGroups[key] = (knowledgeGroups[key] || 0) + 1;
      });
      const knowledge = Object.entries(knowledgeGroups).sort((a,b) => b[1] - a[1]).slice(0, 5);
      const roundCard = r => `<button class="iv-hub-round" data-status="${escape(r.status || '')}" data-result="${escape(r.actual_result || '')}" data-search="${escape(`${r.company || ''} ${r.role || ''} ${r.round_name || ''} ${r.round_type || ''}`.toLowerCase())}" onclick="CaddieInterview.openRoundFromLibrary(${r.track_id},${r.id})"><span class="iv-hub-status">${escape(phase(r))}</span><div><b>${escape(r.company || '')} · ${escape(r.role || '')}</b><small>${escape(label(r))}${r.scheduled_at ? ' · ' + escape(r.scheduled_at) : ''}${r.actual_result === 'cancelled' && r.evidence_text ? ` · 取消原因：${escape(r.evidence_text)}` : ''}</small></div><em>${r.question_count || 0} 题 →</em></button>`;
      $('#ivLibrary').innerHTML = `<section class="iv-hub-next">${next ? `<div class="iv-hub-next-copy"><span class="iv-kicker">下一场需要准备</span><h2>${escape(next.company || '')} · ${escape(next.role || '')}</h2><p>${escape(label(next))} · ${escape(phase(next))}${next.scheduled_at ? ' · ' + escape(next.scheduled_at) : ''}</p><div class="iv-hub-next-meta"><span><b>${next.question_count || 0}</b> 已沉淀题目</span><span><b>${questions.filter(q => q.track_id === next.track_id).length}</b> 岗位相关考点</span><span><b>${needsReview.length}</b> 场等待复盘</span></div></div><div class="iv-hub-next-actions"><button class="btn" onclick="CaddieInterview.openRoundFromLibrary(${next.track_id},${next.id})">继续本轮准备</button><button class="iv-cancel-interview" onclick="CaddieInterview.cancelRound(${next.id},true)">取消面试</button><small>进入岗位工作台的对应轮次</small></div>` : `<div class="iv-hub-next-copy"><span class="iv-kicker">当前没有面试安排</span><h2>从求职工作台建立第一轮面试</h2><p>收到邀约后，在对应岗位中新建轮次。</p></div>`}</section>
        <section class="iv-hub-main"><div class="iv-hub-active"><div class="iv-hub-section-head"><div><h2>进行中的面试</h2><p>跨岗位查看准备、复盘和等待结果的轮次</p></div><div class="iv-hub-search"><span>⌕</span><input placeholder="搜索公司、岗位或轮次…" oninput="CaddieInterview.filterLibrary(this.value)"></div></div><div id="ivHubRounds">${active.length ? active.map(roundCard).join('') : '<div class="iv-empty compact"><b>没有其他进行中面试</b><p>新的面试轮次会自动出现在这里。</p></div>'}</div><div class="iv-hub-history-head"><b>全部面试</b><span>${rounds.length} 轮 · ${questions.length} 道题</span></div><div class="iv-hub-history">${rounds.length ? rounds.map(roundCard).join('') : '<div class="iv-muted">还没有面试记录</div>'}</div></div>
        <aside class="iv-hub-aside"><section><div class="iv-hub-section-head"><div><h2>最近需要处理</h2><p>只显示影响下一步的事项</p></div></div><div class="iv-hub-tasks">${needsReview.slice(0,3).map(r => `<button onclick="CaddieInterview.openRoundFromLibrary(${r.track_id},${r.id})"><i></i><span><b>完成 ${escape(r.company || '')} 复盘</b><small>${escape(label(r))} · 整理答卷和结论</small></span></button>`).join('')}${waiting.slice(0,2).map(r => `<button onclick="CaddieInterview.openRoundFromLibrary(${r.track_id},${r.id})"><i></i><span><b>记录 ${escape(r.company || '')} 结果</b><small>${escape(label(r))} · 用真实结果校准</small></span></button>`).join('') || (!needsReview.length ? '<div class="iv-muted">目前没有待处理事项</div>' : '')}</div></section><section class="iv-hub-knowledge"><div class="iv-hub-section-head"><div><h2>面试知识地图</h2><p>从所有真实面试中归纳</p></div></div><div class="iv-hub-map"><strong>${questions.length}<small>道真实题目</small></strong>${knowledge.length ? knowledge.map(([name,count]) => `<span>${escape(name)}<b>${count}</b></span>`).join('') : '<span>拆解面试后形成考点</span>'}</div><div class="iv-hub-calibration"><span>结果校准</span><b>${calibration.accuracy == null ? '样本不足' : calibration.accuracy + '%'}</b><small>${calibration.sample_size || 0} 个可比较样本</small></div></section></aside></section>
        <section class="iv-question-bank"><div class="iv-hub-section-head"><div><h2>真实面试题库</h2><p>按岗位、经历与追问角度检索；标签不准确时可以随时修正。</p></div><div class="iv-hub-search"><span>⌕</span><input placeholder="搜索题目、经历或标签…" oninput="CaddieInterview.filterQuestionBank(this.value)"></div></div><div class="iv-question-bank-list">${questions.length ? questions.map(q => { const link=(q.entity_links||[])[0],rawTags=(q.tag_items||[]).map(x=>x.tag_value),tags=[...new Set(rawTags)].filter(tag=>tag!==link?.entity_title&&tag!==link?.aspect); const search=`${q.normalized_question||q.original_question||''} ${q.company||''} ${q.role||''} ${q.question_type||''} ${link?.entity_title||''} ${link?.aspect||''} ${tags.join(' ')}`.toLowerCase(); return `<article class="iv-bank-question" data-search="${escape(search)}"><div><span>${escape(q.question_type || '待分类')}</span><h3>${escape(q.normalized_question || q.original_question)}</h3><p>${escape(q.company || '')} · ${escape(q.role || '')} · 第 ${q.round_number} 轮</p></div><div class="iv-bank-tags">${link ? `<b>${escape(link.entity_title)}</b>${link.aspect ? `<em>${escape(link.aspect)}</em>` : ''}` : '<i>待关联经历</i>'}${tags.slice(0,2).map(tag=>`<em>${escape(tag)}</em>`).join('')}</div><button onclick="CaddieInterview.openLibraryQuestion(${q.id})">${q.confirmed ? '编辑标签' : '确认归档'}</button></article>`; }).join('') : '<div class="iv-empty compact"><b>还没有真实面试题</b><p>拆解并确认一份答卷后，题目会沉淀到这里。</p></div>'}</div></section>`;
      const bank = document.querySelector('.iv-question-bank');
      if (bank) {
        bank.classList.add('iv-question-bank-entry');
        const growthLocked = !growthFeatureAvailable();
        bank.innerHTML = `<div class="iv-question-bank-entry-copy"><span class="iv-kicker">真实面试资产</span><h2>真题题库</h2><p>把所有岗位的真实问题、回答版本、经历关联和 AI 优化答法集中管理。</p><div><span><b>${questions.length}</b> 道真实题目</span><span><b>${questions.filter(q => String(q.answer_workspace?.current_answer || '').trim()).length}</b> 道已有答案</span><span><b>${questions.filter(q => !q.confirmed).length}</b> 道待确认</span></div></div><div class="iv-question-bank-entry-actions"><button class="btn" onclick="CaddieInterview.openQuestionBankPage()">进入真题题库 →</button>${growthLocked ? '<button class="iv-growth-entry-locked" onclick="CaddieInterview.openGrowthAnalysisIndex()" title="综合分析与训练尚未开放"><span><b>综合分析与训练</b><small>跨场趋势、能力诊断与模拟答卷</small></span><em>完善中</em></button>' : ''}</div>`;

        if (!growthLocked) {
          const growthEntry = document.createElement('section');
          growthEntry.className = 'iv-growth-entry';
          let growthItems = [];
          try {
            const growthResult = await ivApi('/v2/interview-growth-analyses?limit=4');
            growthItems = growthResult.items || growthResult.data?.items || [];
          } catch (_) {
            // The interview library remains usable if the optional analysis read model fails.
          }
          const completed = growthItems.filter(item => item.status === 'completed').length;
          const latest = growthItems[0];
          growthEntry.innerHTML = `<div class="iv-growth-entry-main"><span class="iv-kicker">跨场学习</span><h2>综合分析与训练</h2><p>把同类岗位的历史面试放到同一条时间线上，识别反复问题、进步证据，并生成下一轮模拟试卷与答卷。</p><button class="btn" onclick="CaddieInterview.openGrowthAnalysisIndex()">打开综合分析 →</button></div><div class="iv-growth-entry-proof"><span>已完成分析 <b>${completed}</b></span><span>可用历史面试 <b>${rounds.filter(r => Number(r.question_count || 0) > 0).length}</b></span>${latest ? `<button onclick="CaddieInterview.openGrowthAnalysisPage(${latest.id})"><small>最近一次</small><b>${escape(latest.title)}</b><em>${latest.status === 'completed' ? '查看结果' : `${latest.progress || 0}% · ${escape(latest.stage || '进行中')}`} →</em></button>` : '<p>先选择一类岗位，建立第一份跨场分析。</p>'}</div>`;
          bank.parentNode.insertBefore(growthEntry, bank);
        }
      }
      state().libraryQuestions = questions;
    } catch (e) { $('#ivLibrary').innerHTML = `<div class="iv-empty"><b>面试总库加载失败</b><p>${escape(e.message)}</p></div>`; }
  };
  I.filterLibrary = function (value) {
    const query = String(value || '').trim().toLowerCase();
    document.querySelectorAll('.iv-hub-round').forEach(row => {
      row.hidden = !!query && !String(row.dataset.search || '').includes(query);
    });
  };
  I.filterQuestionBank = function (value) {
    const query = String(value || '').trim().toLowerCase();
    const filters = state().questionBankFilters || {query:'', status:'all', type:'all'};
    filters.query = query;
    state().questionBankFilters = filters;
    I.applyQuestionBankFilters();
  };
  I.openRoundFromLibrary = function (trackId, roundId) {
    const navigate = bridge().openInterviewRoundFromLibrary;
    if (navigate) navigate(trackId, roundId);
  };

  // Resume durable AI jobs after navigation or a full page refresh. The task
  // itself runs on the server; localStorage only remembers what to surface.
  window.setTimeout(() => I.startBackgroundJobPolling(), 400);
})();
