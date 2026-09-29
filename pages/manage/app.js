import { escapeHTML as esc, icon, button, field, choice, toggle, chinaDate, fullDate, modal, toast } from "./components.js";

const app = document.getElementById("app");
const bridge = window.AstrBotPluginPage;
window.addEventListener("error", () => console.error("CatLottery UI interaction failed unexpectedly."));
window.addEventListener("unhandledrejection", () => console.error("CatLottery UI request failed unexpectedly."));
const catURL = document.getElementById("cat-template").content.querySelector("img").getAttribute("src");
const state = { lotteries: [], settings: { manager_ids: [], llm_tools_enabled: true }, platforms: [], filter: "all", search: "", loadingPlatforms: false, error: "", refreshed: null };
const phaseLabel = { open: "报名中", closed: "等待开奖", drawn: "已开奖", cancelled: "已取消" };
const copy = value => JSON.parse(JSON.stringify(value));
const accountKey = target => `${target.platform_id}::${target.bot_id}`;

function accountOptions() {
  const options = [];
  for (const platform of state.platforms) for (const account of platform.accounts) options.push({ value: `${platform.id}::${account.bot_id}`, label: `${platform.name} · ${account.nickname || "机器人"} (${account.bot_id})`, platform, account });
  return options;
}

async function loadState({ quiet = false } = {}) {
  try {
    const data = await bridge.apiGet("state");
    state.lotteries = data.lotteries;
    state.settings = data.settings;
    state.refreshed = data.server_time;
    state.error = "";
    const typing = app.contains(document.activeElement) && document.activeElement.tagName === "INPUT";
    if (!typing) render();
  } catch (error) {
    state.error = error.message || "暂时无法读取抽奖，请刷新重试。";
    if (!quiet) toast(state.error, "error");
    render();
  }
}

async function loadPlatforms() {
  state.loadingPlatforms = true;
  render();
  try {
    const data = await bridge.apiGet("platforms");
    state.platforms = data.platforms;
  } catch (error) { toast(error.message || "平台读取失败", "error"); }
  finally { state.loadingPlatforms = false; render(); }
}

function render() {
  const openCount = state.lotteries.filter(item => item.phase === "open").length;
  const waitingCount = state.lotteries.filter(item => item.phase === "closed").length;
  const pendingCount = state.lotteries.reduce((sum, item) => sum + item.pending_deliveries, 0);
  const filtered = state.lotteries.filter(item => (state.filter === "all" || item.phase === state.filter) && `${item.title} ${item.id} ${item.prize}`.toLowerCase().includes(state.search.toLowerCase()));
  const accounts = accountOptions();
  app.setAttribute("aria-busy", "false");
  app.innerHTML = `<div class="workspace">
    <aside class="sidebar"><a class="brand" href="#" aria-label="喵喵抽奖首页"><img src="${esc(catURL)}" alt=""/><span>喵喵抽奖<small>把每一份期待收好</small></span></a><p class="sidebar-label">我的工作台</p><nav aria-label="主导航"><button class="nav-item active" data-action="home" aria-label="抽奖小屋">${icon("gift")}<span>抽奖小屋</span><span class="nav-count">${state.lotteries.length}</span></button><button class="nav-item" data-action="settings" aria-label="插件设置">${icon("settings")}<span>插件设置</span></button><button class="nav-item" data-action="guide" aria-label="使用指南">${icon("file")}<span>使用指南</span></button></nav><div class="sidebar-note"><div class="tiny-paws">${icon("paw")}${icon("paw")}</div><strong>好运也有小规则</strong><p>从群聊报名，在私聊填写。<br/>每个 QQ 号，一次机会。</p><code>/抽奖</code></div><footer class="sidebar-footer"><span class="online-dot"></span>AstrBot 插件 Pages<small>清蒸云鸭 · v1.0.0</small></footer></aside>
    <main class="main"><header class="topbar"><div><span class="eyebrow">喵喵抽奖 / 抽奖小屋</span><h1>今天，也有好事发生。</h1></div><div class="topbar-actions"><span class="time-label">${icon("clock")}北京时间 UTC+8</span>${button("刷新", { action: "refresh", style: "text", glyph: "refresh" })}</div></header>
    ${state.error ? `<div class="error-banner" role="alert">${icon("paw")}<span>${esc(state.error)} 已显示的记录可能不是最新状态。</span>${button("重试", { action: "refresh", style: "text" })}</div>` : ""}
    <section class="hero"><div class="hero-copy"><span class="hero-stamp">一张抽奖券，一点小期待</span><h2>让猫猫<br/>替你保管好运。</h2><p>为不同的群准备不同的惊喜。<br/>报名、填写、开奖，都按你设定的时间进行。</p>${button("创建一场抽奖", { action: "create", glyph: "plus", style: "primary" })}<span class="hero-caption">${openCount ? `${openCount} 场抽奖正在收集期待` : "从第一场小小的惊喜开始"}${waitingCount ? ` · ${waitingCount} 场等待揭晓` : ""}</span></div><div class="hero-art" aria-hidden="true"><div class="floating-star star-one">✦</div><div class="floating-star star-two">✧</div><div class="raffle-ticket"><div class="ticket-top"><span>MEOW · LUCKY TICKET</span><span>♡</span></div><img src="${esc(catURL)}" alt=""/><strong>好运签收处</strong><p>愿你遇见恰好的惊喜</p><div class="ticket-stitch"></div><div class="ticket-bottom"><span>一人一签 · 用心开奖</span>${icon("paw")}</div></div><div class="ticket-label">你的下一份惊喜，正在路上。</div></div></section>
    <div class="content-grid"><section class="lotteries"><div class="section-heading"><div><span class="eyebrow">抽奖清单</span><h2>收集期待，准时揭晓 <span>${state.lotteries.length}</span></h2></div></div><div class="filterbar"><div class="segmented" role="group" aria-label="按活动状态筛选">${[["all", "全部"], ["open", "报名中"], ["closed", "待开奖"], ["drawn", "已开奖"], ["cancelled", "已取消"]].map(([value, label]) => `<button data-filter="${value}" aria-pressed="${state.filter === value}" class="${state.filter === value ? "selected" : ""}">${label}</button>`).join("")}</div><label class="search">${icon("search")}<input type="text" placeholder="搜索名称或编号" aria-label="搜索抽奖" value="${esc(state.search)}" maxlength="100"/></label></div><div class="lottery-grid">${filtered.length ? filtered.map(lotteryCard).join("") : `<div class="empty-state"><img src="${esc(catURL)}" alt=""/><h3>${state.search || state.filter !== "all" ? "还没有匹配的好运" : "抽奖券，等你写下第一笔"}</h3><p>${state.search || state.filter !== "all" ? "试试其他关键词，或切换活动状态。" : "设置奖品、时间和参与群，猫猫就可以开始收集期待了。"}</p>${button("创建抽奖", { action: "create", glyph: "plus" })}</div>`}</div></section>
    <aside class="right-rail"><section class="rail-panel"><div class="rail-title"><h3>${icon("group")}已开启的平台</h3><button class="icon-btn ${state.loadingPlatforms ? "spinning" : ""}" data-action="platforms" aria-label="刷新平台列表" ${state.loadingPlatforms ? "disabled" : ""}>${icon("refresh")}</button></div><p class="muted">自动读取 AstrBot 的 AioCqhttp 连接。每场抽奖单独选择允许的平台和群。</p>${state.loadingPlatforms ? '<div class="loading-line">正在读取机器人与群列表…</div>' : state.platforms.length ? state.platforms.map(platform => `<div class="platform-card"><span class="status-dot ${platform.online ? "online" : "offline"}"></span><div><strong>${esc(platform.name)}</strong><small>${platform.online ? platform.accounts.map(account => `QQ ${esc(account.bot_id)} · ${account.groups.length} 个群`).join("<br/>") : esc(platform.error || "协议端未连接")}</small></div><span class="platform-tag">AIO</span></div>`).join("") : '<div class="rail-empty">还没有开启的 AioCqhttp 平台。<br/>请先在 AstrBot 的消息平台中开启连接。</div>'}<div class="rail-footnote">${accounts.length} 个在线机器人 · 支持 SnowLuma / NapCat</div></section>
    <section class="rail-panel pink-panel"><div class="rail-title"><h3>${icon("send")}群通知投递</h3><span class="count-pill">${pendingCount}</span></div><p>${pendingCount ? "有通知等待发送，离线或发送失败会自动重试。" : "通知队列已收好，开奖后会自动投递到对应的群。"}</p><span class="rail-footnote">查看抽奖详情，可检查每条通知的状态。</span></section><section class="rail-panel quiet-panel"><span class="eyebrow">从这里开始</span><ol class="onboarding"><li><b>选好奖品与时间</b><span>截止报名与开奖可以是不同时间。</span></li><li><b>选好平台与群列表</b><span>每场活动都有自己的参与范围。</span></li><li><b>发出你的抽奖券</b><span>发布到群，发送 /抽奖 参与 编号。</span></li></ol></section></aside></div><footer class="page-footer"><span>每一份期待，都认真收好。</span><span>${state.refreshed ? `最近更新 ${fullDate(state.refreshed)}` : "连接管理页面中"} · 北京时间</span></footer></main></div>`;
  app.querySelector(".search input")?.addEventListener("input", event => {
    state.search = event.target.value;
    const position = event.target.selectionStart;
    render();
    const search = app.querySelector(".search input");
    search.focus(); search.setSelectionRange(position, position);
  });
}

function lotteryCard(item) {
  const targets = [...new Set(item.targets.map(target => target.group_id))];
  const platforms = new Set(item.targets.map(accountKey));
  const pending = item.entry_count - (item.submitted_count ?? item.complete_count);
  return `<article class="lottery-card phase-${item.phase}" data-id="${esc(item.id)}"><div class="card-meta"><span class="phase-badge">${phaseLabel[item.phase]}</span><code>#${item.id}</code></div><h3>${esc(item.title)}</h3><div class="prize-line">${icon("gift")}<span>${esc(item.prize)}</span></div><div class="ticket-divider"></div><dl class="card-details"><div><dt>报名截止</dt><dd>${fullDate(item.close_at)}</dd></div><div><dt>自动开奖</dt><dd>${fullDate(item.draw_at)}</dd></div><div><dt>参与范围</dt><dd>${platforms.size} 个机器人 · ${targets.length} 个群</dd></div><div><dt>报名资料</dt><dd>${item.questions.length ? `${item.questions.length} 项私聊问题` : "无需填写资料"}</dd></div></dl><div class="participation"><span><strong>${item.complete_count}</strong> 份有效报名${pending ? `<small> · ${pending} 份待填写</small>` : ""}${item.review_pending_count ? `<small> · ${item.review_pending_count} 份待审核</small>` : ""}${item.rejected_count ? `<small> · ${item.rejected_count} 份未通过</small>` : ""}</span><span>${item.status === "drawn" ? `${item.winners.length} 人中奖` : `抽取 ${item.winner_count} 位`}</span></div>${item.pending_deliveries ? `<div class="delivery-hint">${icon("clock")}${item.pending_deliveries} 条通知待发送</div>` : ""}<footer class="card-actions">${button("查看详情", { action: "detail", style: "text", glyph: "arrow" })}${item.status === "open" ? button("管理", { action: "edit", style: "tonal", glyph: "edit" }) : ""}</footer></article>`;
}

function guide() {
  modal("每一份好运，怎么参与？", "所有 QQ 指令都以图片回复。", `<div class="guide-grid"><section><h3>群聊报名</h3><p>在允许的机器人平台和群内，发送以下指令。</p><code>/抽奖</code><code>/抽奖 列表</code><code>/抽奖 详情 编号</code><code>/抽奖 参与 编号</code><code>/抽奖 状态 编号</code><code>/抽奖 退出 编号</code></section><section><h3>私聊填写</h3><p>先在群内预留报名，再私聊同一个机器人。未添加好友时，会根据好友列表提醒。</p><code>/抽奖 填写 编号</code><code>/抽奖 待办</code><code>/抽奖 取消填写</code><p>最多 20 项，逐题发送文本、图片或图文消息。文字保留空格与换行。开启当场答对时，全部填写完成即成功参与；关闭时先提交，再由管理员审核。提交与审核结果分别通过群聊和私聊图片通知。</p></section></div><div class="notice">同一场抽奖在不同群共享参与名单，每个 QQ 号只计一次。截止后不能报名、退出或继续填写；到开奖时间自动抽取并发送到所有设置的群。</div>`, button("知道啦", { action: "dismiss" }), { wide: true });
}

function settings() {
  const ids = [...state.settings.manager_ids];
  const dialog = modal("插件设置", "管理额外 QQ 管理员与 LLM 工具调用。AstrBot 全局管理员始终具有管理权限。", `<section class="settings-tools"><div><h3>LLM 工具调用</h3><p>同时启用或关闭本插件的 8 个工具，默认开启。关闭后，QQ 指令和自动开奖仍正常使用。</p></div>${toggle("llm_tools_enabled", state.settings.llm_tools_enabled ?? true, "LLM 工具调用")}</section><div class="notice">这些管理员可以通过指令和已启用的工具创建、发布、截止、开奖或取消抽奖。群成员默认仅可管理自己的报名。</div><div id="manager-list"></div><div class="inline-entry">${field("添加管理员 QQ", "manager", "", { placeholder: "输入 QQ 号", maxlength: 20 })}${button("添加", { action: "add-manager", glyph: "plus" })}</div><p class="field-hint">所有设置都保存在插件独立的数据目录，不使用 AstrBot 的配置 schema。</p><p class="form-error" role="alert"></p>`, button("保存设置", { action: "save-settings", glyph: "check", style: "primary" }));
  const paint = () => { dialog.holder.querySelector("#manager-list").innerHTML = ids.length ? ids.map(id => `<div class="list-row"><div class="avatar">${icon("paw")}</div><div><strong>QQ ${id}</strong><small>抽奖管理员</small></div><button class="icon-btn" data-remove="${id}" aria-label="移除 QQ ${id}">${icon("close")}</button></div>`).join("") : '<div class="small-empty">没有额外管理员，仅 AstrBot 全局管理员可以管理。</div>'; };
  paint();
  dialog.holder.addEventListener("click", async event => {
    if (event.target.closest("[data-remove]")) { ids.splice(ids.indexOf(event.target.closest("[data-remove]").dataset.remove), 1); paint(); }
    if (event.target.closest('[data-action="add-manager"]')) {
      const input = dialog.holder.querySelector('[name="manager"]');
      const id = input.value.trim();
      if (!/^[1-9]\d{4,19}$/.test(id)) { toast("请填写有效 QQ 号", "error"); return; }
      if (!ids.includes(id)) ids.push(id);
      input.value = ""; paint();
    }
    const save = event.target.closest('[data-action="save-settings"]');
    if (save) {
      save.disabled = true;
      try { await bridge.apiPost("settings", { manager_ids: ids, llm_tools_enabled: dialog.holder.querySelector('meow-switch[name="llm_tools_enabled"]').value }); dialog.close(); toast("插件设置已保存"); await loadState(); }
      catch (error) { dialog.holder.querySelector(".form-error").textContent = error.message; }
      finally { save.disabled = false; }
    }
  });
}

function editor(existing = null) {
  const frozen = !!existing?.entry_count;
  const draft = existing ? copy(existing) : { title: "", prize: "", description: "", winner_count: 1, close_at: Date.now() / 1000 + 86400, draw_at: Date.now() / 1000 + 90000, targets: [], questions: [], require_correct: false };
  draft.require_correct ??= true;
  const dialog = modal(existing ? "管理这一场好运" : "创建一场新的好运", existing ? `抽奖编号 ${existing.id} · 设置按活动独立保存` : "写下奖品，选好群，让猫猫准时开奖。", `<div class="editor-sections"><section class="editor-section"><div class="editor-section-title"><span class="section-number">01</span><div><h3>准备这份惊喜</h3><p>奖品和中奖人数会显示在群公告中。</p></div></div><div class="form-grid">${field("抽奖标题", "title", draft.title, { placeholder: "例如：午后的小小好运", maxlength: 80 })}<div class="field"><span class="field-label">中奖人数</span><meow-stepper name="winner_count" label="中奖人数" value="${draft.winner_count}" min="1" max="100" ${frozen ? "disabled" : ""}></meow-stepper><span class="field-hint">不足名额时，所有有效参与者中奖。</span></div><div class="full-width">${field("奖品", "prize", draft.prize, { placeholder: "例如：猫猫贴纸一套，每人一份", maxlength: 300, disabled: frozen })}${field("活动说明", "description", draft.description, { multiline: true, placeholder: "补充领取方式或参与说明（可选）", maxlength: 1500 })}</div></div></section>
    <section class="editor-section"><div class="editor-section-title"><span class="section-number">02</span><div><h3>约定揭晓的时间</h3><p>全部时间采用北京时间（UTC+8），不受浏览器时区影响。</p></div></div><div class="form-grid"><div class="field"><span class="field-label">报名截止时间</span><meow-calendar name="close_at" value="${chinaDate(draft.close_at)}"></meow-calendar><span class="field-hint">截止后停止报名与私聊资料填写。</span></div><div class="field"><span class="field-label">自动开奖时间</span><meow-calendar name="draw_at" value="${chinaDate(draft.draw_at)}"></meow-calendar><span class="field-hint">可以等于或晚于报名截止时间。</span></div></div></section>
    <section class="editor-section"><div class="editor-section-title"><span class="section-number">03</span><div><h3>把好运送到哪些群？</h3><p>列表中的机器人平台和群，才可以参与本次抽奖。开奖发往所有这些群。</p></div></div>${frozen ? '<div class="notice">已有报名，平台、群、问题、答题模式、奖品与中奖人数已锁定，保证参与规则一致。</div>' : '<div class="notice blue">平台自动读取自 AstrBot。可在列表中添加多个平台，每个平台再选择自己的群；不同抽奖互不影响。</div>'}<div id="target-list"></div>${!frozen ? button("添加平台与群", { action: "add-target", glyph: "plus", style: "text" }) : ""}<p class="field-hint">同一 QQ 号跨平台、跨群只计一次。私聊资料必须发给报名时的那个机器人。</p></section>
    <section class="editor-section"><div class="editor-section-title"><span class="section-number">04</span><div><h3>报名之前，想了解什么？</h3><p>最多 20 项，可混合添加答题、文本、图片与图文资料；按顺序逐题填写。</p></div></div><section class="answer-mode"><div><h4>必须当场答对</h4><p class="answer-mode-text">${draft.require_correct ? "开启：答题当场核对，答错需重答；全部资料完成即成功参与。" : "关闭：答案提交即进入下一题，不当场判断；全部资料提交后由管理员逐题审核，全部通过才有开奖资格。"}</p></div>${toggle("require_correct", draft.require_correct, "必须当场答对", { disabled: frozen })}</section><div id="question-list"></div>${!frozen ? button("添加一个问题", { action: "add-question", glyph: "plus", style: "text" }) : ""}<p class="field-hint">不添加问题时，群聊报名直接成功。开启当场答对时，答题必须设置文字正确答案；关闭时可留空供人工审核。选择题参考答案填写完整选项文本，每行一个；仅管理员可见。</p></section></div><p class="form-error" role="alert"></p>`, button("先返回", { action: "dismiss", style: "text" }) + button(existing ? "保存修改" : "创建抽奖", { action: "save-lottery", glyph: "check", style: "primary" }), { wide: true });

  function paintTargets() {
    const choices = accountOptions();
    dialog.holder.querySelector("#target-list").innerHTML = draft.targets.length ? draft.targets.map((target, index) => {
      const key = accountKey(target);
      const selected = choices.find(item => item.value === key);
      const accounts = choices.map(({ value, label }) => ({ value, label }));
      if (target.platform_id && !selected) accounts.push({ value: key, label: `${target.platform_id} · QQ ${target.bot_id}（离线）` });
      const groups = selected?.account.groups.map(group => ({ value: group.group_id, label: `${group.group_name || "QQ群"} (${group.group_id})` })) || [];
      if (target.group_id && !groups.some(group => group.value === target.group_id)) groups.push({ value: target.group_id, label: `${target.group_name || "QQ群"} (${target.group_id})` });
      return `<div class="target-row" data-target="${index}"><span class="list-index">${String(index + 1).padStart(2, "0")}</span><div class="target-fields"><label class="field"><span class="field-label">允许的机器人平台</span>${choice("account", key, accounts, { placeholder: "选择 AioCqhttp 平台与机器人", disabled: frozen, label: "允许的机器人平台" })}</label><label class="field"><span class="field-label">允许的群</span>${choice("group", target.group_id, groups, { placeholder: "选择参与群", disabled: frozen, label: "允许的群" })}</label></div>${!frozen ? `<button class="icon-btn" data-action="remove-target" aria-label="移除第 ${index + 1} 个群">${icon("trash")}</button>` : ""}</div>`;
    }).join("") : '<div class="small-empty">还没添加群。点击下方按钮，为这场抽奖选择参与范围。</div>';
  }

  function paintQuestions() {
    dialog.holder.querySelector("#question-list").innerHTML = draft.questions.length ? draft.questions.map((question, index) => `<div class="question-card" data-question="${index}"><div class="question-header"><span class="question-number">第 ${index + 1} 项</span>${choice("kind", question.kind, [{ value: "quiz", label: "答题 · 选择或简答" }, { value: "text", label: "文本资料 · 可附图片" }, { value: "image", label: "图片资料 · 可附文字" }, { value: "mixed", label: "图文资料 · 文字图片都必填" }], { disabled: frozen })}${!frozen ? `<button class="icon-btn" data-action="move-question" aria-label="向上移动第 ${index + 1} 项" ${index === 0 ? "disabled" : ""}>↑</button><button class="icon-btn" data-action="remove-question" aria-label="删除第 ${index + 1} 项">${icon("trash")}</button>` : ""}</div>${field("问题内容", "prompt", question.prompt, { placeholder: "清晰地告诉参与者需要回答或提供什么", maxlength: 300, disabled: frozen })}${question.kind === "quiz" ? `<div class="form-grid">${field("选项（可选）", "options", question.options.join("\n"), { multiline: true, placeholder: "每行一个选项，最多 8 个\n留空则为简答题", disabled: frozen })}${field("文字正确答案（后审模式可留空）", "answers", question.answers.join("\n"), { multiline: true, placeholder: "每行一个答案，最多 20 个\n选择题请填完整的正确选项文本", disabled: frozen })}</div>` : `<p class="field-hint">${question.kind === "image" ? "参与者需要发送一张图片，最大 8 MB，可在同一条消息中附带 1000 字以内文字。" : question.kind === "mixed" ? "参与者必须在同一条消息中发送 1–1000 字文字和一张图片，最大 8 MB。" : "参与者发送 1–1000 字文本资料，可在同一条消息中附带一张图片。"}</p>`}</div>`).join("") : '<div class="small-empty"><strong>轻轻松松参加</strong><span>没有私聊问题，群聊发送参与指令即可成功。</span></div>';
  }

  const readQuestions = () => dialog.holder.querySelectorAll("[data-question]").forEach(element => {
    const question = draft.questions[Number(element.dataset.question)];
    question.prompt = element.querySelector('[name="prompt"]').value;
    if (question.kind === "quiz") {
      question.options = element.querySelector('[name="options"]').value.split("\n").map(text => text.trim()).filter(Boolean);
      question.answers = element.querySelector('[name="answers"]').value.split("\n").map(text => text.trim()).filter(Boolean);
    }
  });
  paintTargets(); paintQuestions();
  dialog.holder.addEventListener("change", event => {
    if (event.target.matches('meow-switch[name="require_correct"]')) {
      draft.require_correct = event.target.value;
      dialog.holder.querySelector(".answer-mode-text").textContent = draft.require_correct ? "开启：答题当场核对，答错需重答；全部资料完成即成功参与。" : "关闭：答案提交即进入下一题，不当场判断；全部资料提交后由管理员逐题审核，全部通过才有开奖资格。";
    }
    const targetRow = event.target.closest("[data-target]");
    if (targetRow && event.target.matches("meow-choice")) {
      const target = draft.targets[Number(targetRow.dataset.target)];
      if (event.target.getAttribute("name") === "account") {
        const selected = accountOptions().find(item => item.value === event.target.value);
        if (selected) { target.platform_id = selected.platform.id; target.bot_id = selected.account.bot_id; target.group_id = ""; target.group_name = ""; }
        paintTargets();
      } else {
        const selected = accountOptions().find(item => item.value === accountKey(target));
        target.group_id = event.target.value;
        target.group_name = selected?.account.groups.find(group => group.group_id === target.group_id)?.group_name || "";
      }
    }
    const questionRow = event.target.closest("[data-question]");
    if (questionRow && event.target.matches('meow-choice[name="kind"]')) {
      readQuestions();
      const question = draft.questions[Number(questionRow.dataset.question)];
      question.kind = event.target.value;
      question.options = []; question.answers = [];
      paintQuestions();
    }
  });
  dialog.holder.addEventListener("click", async event => {
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (!action) return;
    if (action === "add-target") {
      if (draft.targets.length >= 50) { toast("最多允许 50 个群", "error"); return; }
      const first = accountOptions()[0];
      if (!first) { toast("请先连接 QQ 协议端并刷新平台列表", "error"); return; }
      draft.targets.push({ platform_id: first.platform.id, bot_id: first.account.bot_id, group_id: "", group_name: "" }); paintTargets();
    }
    if (action === "remove-target") { draft.targets.splice(Number(event.target.closest("[data-target]").dataset.target), 1); paintTargets(); }
    if (["add-question", "remove-question", "move-question"].includes(action)) {
      readQuestions();
      if (action === "add-question") {
        if (draft.questions.length >= 20) { toast("最多添加 20 项问题", "error"); return; }
        draft.questions.push({ kind: "quiz", prompt: "", options: [], answers: [] });
      } else {
        const index = Number(event.target.closest("[data-question]").dataset.question);
        if (action === "remove-question") draft.questions.splice(index, 1);
        else if (index > 0) [draft.questions[index - 1], draft.questions[index]] = [draft.questions[index], draft.questions[index - 1]];
      }
      paintQuestions();
    }
    if (action === "save-lottery") {
      readQuestions();
      for (const name of ["title", "prize", "description"]) draft[name] = dialog.holder.querySelector(`[name="${name}"]`).value.trim();
      draft.winner_count = dialog.holder.querySelector("meow-stepper").value;
      draft.require_correct = dialog.holder.querySelector('meow-switch[name="require_correct"]').value;
      draft.close_at = `${dialog.holder.querySelector('[name="close_at"]').value.replace(" ", "T")}:00+08:00`;
      draft.draw_at = `${dialog.holder.querySelector('[name="draw_at"]').value.replace(" ", "T")}:00+08:00`;
      const save = event.target.closest("button");
      save.disabled = true;
      const errorField = dialog.holder.querySelector(".form-error");
      errorField.textContent = "";
      try {
        const item = await bridge.apiPost("lotteries", draft);
        dialog.close(); toast(existing ? "修改已保存" : "抽奖已创建，接下来发布到群吧");
        await loadState(); await detail(item.id);
      } catch (error) { errorField.textContent = error.message || "保存失败，请检查填写内容。"; errorField.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
      finally { save.disabled = false; }
    }
  });
}

async function detail(id) {
  const item = state.lotteries.find(lottery => lottery.id === id);
  if (!item) { toast("记录已更新，请刷新后重试", "error"); return; }
  let data;
  try { data = await bridge.apiGet(`lotteries/${id}`); }
  catch (error) { toast(error.message, "error"); return; }
  const laterReview = item.require_correct === false;
  const canReview = laterReview && item.status === "open" && state.refreshed < item.draw_at;
  const scope = item.targets.map(target => `<span class="scope-tag">${icon("group")}${esc(target.group_name || target.group_id)}<small>${esc(target.platform_id)} / QQ ${target.bot_id}</small></span>`).join("");
  const actions = item.status === "open" ? button("发布到所有群", { action: "publish", glyph: "send", style: "primary" }) + button("修改设置", { action: "detail-edit", glyph: "edit" }) + button("截止报名", { action: "close", style: "text" }) + button("立即开奖", { action: "draw", style: "text" }) + button("取消抽奖", { action: "cancel", style: "text" }) : button("删除记录与资料", { action: "delete", style: "text", glyph: "trash" });
  const dialog = modal(item.title, `编号 ${id} · ${phaseLabel[item.phase]} · 仅管理页展示填写资料`, `
    <div class="detail-ticket"><span class="phase-badge phase-${item.phase}">${phaseLabel[item.phase]}</span><h3>${esc(item.prize)}</h3><div class="detail-dates"><span>截止 <b>${chinaDate(item.close_at)}</b></span><span>计划开奖 <b>${chinaDate(item.draw_at)}</b></span></div><p>${esc(item.description || "愿每一份期待都有回响。")}</p><div class="scope-list">${scope}</div><p class="field-hint">答题模式：${laterReview ? "先提交，后审核资格" : "必须当场答对"}</p></div>
    ${item.status === "drawn" ? `<section class="detail-section"><h3>${icon("gift")}接住好运的人</h3>${item.winners.map((winner, index) => `<div class="winner-row"><span class="winner-rank">${index + 1}</span><strong>${esc(winner.nickname)}</strong><span>QQ ${winner.user_id}</span></div>`).join("") || '<div class="small-empty">无人符合开奖资格，本次无人中奖。结果已保存。</div>'}<p class="field-hint">实际开奖 ${chinaDate(item.drawn_at)} · 有效参与 ${item.eligible_count} 人<br/>有效名单 SHA-256 摘要（用于核对名单一致性）：<br/><code>${item.pool_hash}</code></p></section>` : ""}
    <section class="detail-section"><div class="section-heading"><h3>${icon("group")}报名与私聊资料 <span class="count-pill">${data.entries.length}</span></h3>${canReview ? button("按文字答案匹配", { action: "match-answers", style: "tonal", glyph: "check" }) : ""}</div><p class="muted">资料仅此管理页面可见。群聊公告不会显示用户填写内容。</p>${laterReview && item.questions.length ? `<div class="notice blue">${canReview ? "提交后可逐题标记；所有题目通过才算成功参与。文字匹配仅处理有参考答案、尚未标记的纯文字答题，不覆盖已有标记。带图片的答题与资料题请人工核对。" : "审核已锁定，以保存的开奖资格与结果为准。"}${canReview ? `<br/>请在 ${chinaDate(item.draw_at)} 开奖前完成审核。未填写完、待审核或未通过者均不进入开奖名单。` : ""}</div>` : ""}<p class="review-summary muted" aria-live="polite"></p><div class="entry-list"></div></section>
    <section class="detail-section"><div class="section-heading"><h3>${icon("send")}通知发送记录</h3>${button("重试待发送通知", { action: "retry", glyph: "refresh", style: "text" })}</div><div class="delivery-list"></div><p class="field-hint">显示最近 200 条。群聊和私聊分别重试；开奖结果不会再次抽取。</p></section><p class="form-error" role="alert"></p>`, `<div class="detail-actions">${actions}</div>`, { wide: true });

  function paintEntries() {
    const opened = new Set([...dialog.holder.querySelectorAll('[data-expand][aria-expanded="true"]')].map(element => element.dataset.expand));
    const approved = data.entries.filter(entry => entry.status === "complete" && (entry.review_status ?? "approved") === "approved").length;
    const pending = data.entries.filter(entry => entry.review_status === "pending").length;
    const rejected = data.entries.filter(entry => entry.review_status === "rejected").length;
    dialog.holder.querySelector(".review-summary").textContent = `成功参与 ${approved} 人 · 待审核 ${pending} 人 · 未通过 ${rejected} 人`;
    dialog.holder.querySelector(".entry-list").innerHTML = data.entries.length ? data.entries.map(entry => {
      const eligibility = entry.status === "complete" ? (entry.review_status ?? "approved") : "incomplete";
      const statusLabel = { approved: "参与成功", pending: "等待审核", rejected: "审核未通过", incomplete: `待填写 ${entry.answers.length}/${item.questions.length}` }[eligibility];
      const expanded = opened.has(entry.user_id);
      return `<div class="entry-card" data-user="${entry.user_id}"><button class="entry-trigger" data-expand="${entry.user_id}" aria-expanded="${expanded}"><span class="avatar">${icon("paw")}</span><span><strong>${esc(entry.nickname)}</strong><small>QQ ${entry.user_id} · 来源群 ${entry.group_id}</small></span><span class="entry-status ${eligibility}">${statusLabel}</span>${icon("chevron")}</button><div class="entry-answers" id="entry-${entry.user_id}" ${expanded ? "" : "hidden"}>${entry.answers.length ? entry.answers.map((answer, index) => {
        const mark = answer.correct === true ? "correct" : answer.correct === false ? "wrong" : "pending";
        return `<div class="answer-row" data-answer="${index}"><span>${index + 1}. ${esc(item.questions[index]?.prompt || "资料")}</span>${answer.kind === "image" ? (answer.text ? `<p>${esc(answer.text)}</p>` : "") : `<p>${esc(answer.value)}</p>`}${answer.kind === "image" || answer.image ? button("下载私聊图片", { action: "download-image", style: "text", glyph: "file", attrs: `data-file="${esc(answer.kind === "image" ? answer.value : answer.image)}"` }) : ""}${laterReview ? `<div class="answer-review"><span>审核标记</span>${choice("review", mark, [{ value: "pending", label: "待审核" }, { value: "correct", label: "正确 / 符合要求" }, { value: "wrong", label: "错误 / 不符合要求" }], { disabled: !canReview || entry.status !== "complete", label: `QQ ${entry.user_id} 第 ${index + 1} 题审核标记` })}</div>${answer.reviewed_at ? `<small class="field-hint">${answer.review_method === "match" ? "文字匹配" : "人工标记"} · ${chinaDate(answer.reviewed_at)} · ${esc(answer.reviewed_by)}</small>` : ""}` : ""}</div>`;
      }).join("") : '<p class="muted">尚未填写资料。</p>'}</div></div>`;
    }).join("") : '<div class="small-empty">本场暂无报名记录。</div>';
    dialog.holder.querySelector(".delivery-list").innerHTML = data.deliveries.length ? data.deliveries.map(delivery => `<div class="delivery-row"><div><strong>${({ success: "参与成功", submitted: "资料已提交", review: "资格审核结果", result: "开奖通知", announcement: "群公告", cancelled: "取消通知", closed: "截止通知" })[delivery.kind] || "通知"}</strong><small>${delivery.target.channel === "group" ? "群" : "私聊 QQ"} ${delivery.target.recipient} · ${esc(delivery.target.platform_id)}</small></div><span class="${delivery.delivered_at ? "delivered" : "pending"}">${delivery.delivered_at ? "已发送" : delivery.attempts ? `待重试 · ${delivery.attempts} 次` : "等待发送"}</span></div>`).join("") : '<div class="small-empty">还没有发送记录。</div>';
  }
  paintEntries();
  dialog.holder.addEventListener("change", async event => {
    if (!event.target.matches('meow-choice[name="review"]')) return;
    const select = event.target;
    const user_id = select.closest("[data-user]").dataset.user;
    const question_index = Number(select.closest("[data-answer]").dataset.answer);
    const correct = select.value === "pending" ? null : select.value === "correct";
    dialog.holder.querySelectorAll('meow-choice[name="review"] button, [data-action="match-answers"]').forEach(button => { button.disabled = true; });
    const errorField = dialog.holder.querySelector(".form-error");
    errorField.textContent = "";
    try {
      await bridge.apiPost(`lotteries/${id}/review`, { action: "mark", user_id, question_index, correct });
      data = await bridge.apiGet(`lotteries/${id}`);
      paintEntries(); await loadState({ quiet: true }); toast("审核标记已保存");
    } catch (error) { errorField.textContent = error.message; paintEntries(); }
    finally { dialog.holder.querySelector('[data-action="match-answers"]')?.removeAttribute("disabled"); }
  });
  dialog.holder.addEventListener("click", async event => {
    const expand = event.target.closest("[data-expand]");
    if (expand) { const content = dialog.holder.querySelector(`#entry-${expand.dataset.expand}`); content.hidden = !content.hidden; expand.setAttribute("aria-expanded", String(!content.hidden)); }
    const target = event.target.closest("[data-action]");
    const action = target?.dataset.action;
    if (!action) return;
    if (action === "download-image") {
      target.disabled = true;
      try { await bridge.download(`images/${target.dataset.file}`, {}, `喵喵抽奖-${id}-${target.dataset.file}`); }
      catch (error) { toast(error.message, "error"); }
      finally { target.disabled = false; }
    } else if (action === "match-answers") {
      dialog.holder.querySelectorAll('meow-choice[name="review"] button, [data-action="match-answers"]').forEach(button => { button.disabled = true; });
      dialog.holder.querySelector(".form-error").textContent = "";
      try {
        const result = await bridge.apiPost(`lotteries/${id}/review`, { action: "match" });
        data = await bridge.apiGet(`lotteries/${id}`);
        paintEntries(); await loadState({ quiet: true });
        toast(result.marked_answers ? `已匹配 ${result.marked_answers} 个文字答案；图片与未标记资料请人工审核` : "没有可匹配的未审核文字答案，请人工标记");
      } catch (error) { dialog.holder.querySelector(".form-error").textContent = error.message; paintEntries(); }
      finally { target.disabled = false; }
    } else if (action === "detail-edit") { dialog.close(); editor(item); }
    else if (["publish", "retry", "close", "draw", "cancel", "delete"].includes(action)) {
      const execute = async () => {
        target.disabled = true;
        try {
          await bridge.apiPost(`lotteries/${id}/action`, { action, confirmed: true });
          dialog.close(); toast(action === "delete" ? "抽奖和资料已删除" : action === "retry" ? "待发送通知已重新排队" : "操作已保存，群通知已排队");
          await loadState();
        } catch (error) { dialog.holder.querySelector(".form-error").textContent = error.message; }
        finally { target.disabled = false; }
      };
      if (["publish", "retry"].includes(action)) await execute();
      else {
        const info = { close: ["现在截止报名？", "群内报名与私聊填写会立即停止。后审活动仍可在开奖前审核。只抽取成功参与者，仍按原开奖时间自动开奖。"], draw: ["现在揭晓中奖名单？", "报名和审核会立即锁定，只抽取成功参与者。未填写完、待审核或未通过者均不参与；没有合格者时保存空结果并通知所有群。开奖结果不能再次抽取。"], cancel: ["取消这场抽奖？", "活动会结束，不再接受报名、审核或开奖，并向所有设置的群发送取消通知。"], delete: ["删除抽奖与私聊资料？", "这会永久移除活动记录、报名资料、私聊图片及发送记录。此操作无法撤销。"] }[action];
        const confirm = modal(info[0], `抽奖 ${item.title} · ${id}`, `<p class="confirm-copy">${info[1]}</p>`, button("先返回", { action: "dismiss", style: "text" }) + button("确认操作", { action: "confirm-action", style: "tonal" }));
        confirm.holder.querySelector('[data-action="confirm-action"]').addEventListener("click", async () => { confirm.close(); await execute(); });
      }
    }
  });
}

app.addEventListener("click", async event => {
  const filter = event.target.closest("[data-filter]");
  if (filter) { state.filter = filter.dataset.filter; render(); return; }
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (action === "refresh") await loadState();
  if (action === "platforms" && !state.loadingPlatforms) await loadPlatforms();
  if (action === "home") { state.filter = "all"; state.search = ""; render(); }
  if (action === "guide") guide();
  if (action === "settings") settings();
  if (action === "create") editor();
  if (["detail", "edit"].includes(action)) {
    const id = event.target.closest("[data-id]").dataset.id;
    if (action === "detail") await detail(id);
    else editor(state.lotteries.find(item => item.id === id));
  }
});

if (!bridge) {
  app.setAttribute("aria-busy", "false");
  app.innerHTML = `<div class="boot"><img src="${esc(catURL)}" alt=""/><h1>请从 AstrBot 打开抽奖小屋</h1><p>登录 AstrBot → 插件 → 喵喵抽奖 → 管理工作台。</p><p>这个页面通过 AstrBot 插件 Pages 安全访问管理数据。</p></div>`;
} else {
  try {
    await bridge.ready();
    await loadState();
    await loadPlatforms();
    const timer = setInterval(() => { if (!document.hidden) loadState({ quiet: true }); }, 20000);
    window.addEventListener("beforeunload", () => clearInterval(timer));
  } catch (error) {
    app.setAttribute("aria-busy", "false");
    app.innerHTML = `<div class="boot"><img src="${esc(catURL)}" alt=""/><h1>工作台暂时没有连上</h1><p>${esc(error.message || "请在 AstrBot 中重新打开此页面。")}</p></div>`;
  }
}
