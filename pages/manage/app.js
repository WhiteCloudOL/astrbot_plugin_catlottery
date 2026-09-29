import { escapeHTML as esc, icon, button, field, choice, toggle, artwork, loadArtwork, chinaDate, fullDate, modal, toast } from "./components.js";

const app = document.getElementById("app");
const bridge = window.AstrBotPluginPage;
window.addEventListener("error", () => console.error("CatLottery UI interaction failed unexpectedly."));
window.addEventListener("unhandledrejection", () => console.error("CatLottery UI request failed unexpectedly."));
const catURL = document.getElementById("cat-template").content.querySelector("img").getAttribute("src");
const state = { lotteries: [], settings: { manager_ids: [], llm_tools_enabled: true }, settingsDraft: null, view: ["home", "settings", "guide"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "home", platforms: [], filter: "all", search: "", loadingPlatforms: false, error: "", refreshed: null };
const phaseLabel = { open: "报名中", closed: "等待开奖", drawn: "已开奖", cancelled: "已取消" };
const copy = value => JSON.parse(JSON.stringify(value));
const cleanMessage = value => String(value || "").replace(/[。.]+\s*$/, "");
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
    const typing = app.contains(document.activeElement) && ["INPUT", "TEXTAREA"].includes(document.activeElement.tagName);
    if (!typing && !(quiet && state.view === "settings")) render();
  } catch (error) {
    state.error = cleanMessage(error.message) || "暂时无法读取抽奖，请刷新重试";
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
  } catch (error) { toast(cleanMessage(error.message) || "平台读取失败", "error"); }
  finally { state.loadingPlatforms = false; render(); }
}

function render() {
  const openCount = state.lotteries.filter(item => item.phase === "open").length;
  const waitingCount = state.lotteries.filter(item => item.phase === "closed").length;
  const pendingCount = state.lotteries.reduce((sum, item) => sum + item.pending_deliveries, 0);
  const filtered = state.lotteries.filter(item => (state.filter === "all" || item.phase === state.filter) && `${item.title} ${item.id} ${item.prize}`.toLowerCase().includes(state.search.toLowerCase()));
  const accounts = accountOptions();
  const pageInfo = { home: ["抽奖小屋", "今天，也有好事发生"], settings: ["插件设置", "收好规则，放心送出好运"], guide: ["使用指南", "从一张抽奖券开始"] }[state.view];
  app.setAttribute("aria-busy", "false");
  app.innerHTML = `<div class="workspace">
    <aside class="sidebar"><a class="brand" href="#home" data-action="home" aria-label="喵喵抽奖首页"><img src="${esc(catURL)}" alt=""/><span>喵喵抽奖<small>把每一份期待收好</small></span></a><p class="sidebar-label">我的工作台</p><nav aria-label="主导航"><button class="nav-item ${state.view === "home" ? "active" : ""}" aria-current="${state.view === "home" ? "page" : "false"}" data-action="home" aria-label="抽奖小屋">${icon("gift")}<span>抽奖小屋</span><span class="nav-count">${state.lotteries.length}</span></button><button class="nav-item ${state.view === "settings" ? "active" : ""}" aria-current="${state.view === "settings" ? "page" : "false"}" data-action="settings" aria-label="插件设置">${icon("settings")}<span>插件设置</span></button><button class="nav-item ${state.view === "guide" ? "active" : ""}" aria-current="${state.view === "guide" ? "page" : "false"}" data-action="guide" aria-label="使用指南">${icon("file")}<span>使用指南</span></button></nav><div class="sidebar-note"><div class="tiny-paws">${icon("paw")}${icon("paw")}</div><strong>好运也有小规则</strong><p>从群聊报名，在私聊填写<br/>每个 QQ 号，一次机会</p><code>/抽奖</code></div><footer class="sidebar-footer"><span class="online-dot"></span>AstrBot 插件 Pages<small>清蒸云鸭 · v1.1.0</small></footer></aside>
    <main class="main"><header class="topbar"><div><span class="eyebrow">喵喵抽奖 / ${pageInfo[0]}</span><h1>${pageInfo[1]}</h1></div><div class="topbar-actions"><span class="time-label">${icon("clock")}北京时间 UTC+8</span>${button("刷新", { action: "refresh", style: "text", glyph: "refresh" })}</div></header>
    ${state.error ? `<div class="error-banner" role="alert">${icon("paw")}<span>${esc(state.error)} 已显示的记录可能不是最新状态</span>${button("重试", { action: "refresh", style: "text" })}</div>` : ""}
    ${state.view === "home" ? `<section class="hero"><div class="hero-copy"><span class="hero-stamp">一张抽奖券，一点小期待</span><h2>让猫猫<br/>替你保管好运</h2><p>为不同的群准备不同的惊喜<br/>报名、填写、开奖，都按你设定的时间进行</p>${button("创建一场抽奖", { action: "create", glyph: "plus", style: "primary" })}<span class="hero-caption">${openCount ? `${openCount} 场抽奖正在收集期待` : "从第一场小小的惊喜开始"}${waitingCount ? ` · ${waitingCount} 场等待揭晓` : ""}</span></div><div class="hero-art" aria-hidden="true"><div class="floating-star star-one">✦</div><div class="floating-star star-two">✧</div><div class="raffle-ticket"><div class="ticket-top"><span>MEOW · LUCKY TICKET</span><span>♡</span></div><img src="${esc(catURL)}" alt=""/><strong>好运签收处</strong><p>愿你遇见恰好的惊喜</p><div class="ticket-stitch"></div><div class="ticket-bottom"><span>一人一签 · 用心开奖</span>${icon("paw")}</div></div><div class="ticket-label">你的下一份惊喜，正在路上</div></div></section>
    <div class="content-grid"><section class="lotteries"><div class="section-heading"><div><span class="eyebrow">抽奖清单</span><h2>收集期待，准时揭晓 <span>${state.lotteries.length}</span></h2></div></div><div class="filterbar"><div class="segmented" role="group" aria-label="按活动状态筛选">${[["all", "全部"], ["open", "报名中"], ["closed", "待开奖"], ["drawn", "已开奖"], ["cancelled", "已取消"]].map(([value, label]) => `<button data-filter="${value}" aria-pressed="${state.filter === value}" class="${state.filter === value ? "selected" : ""}">${label}</button>`).join("")}</div><label class="search">${icon("search")}<input type="text" placeholder="搜索名称或编号" aria-label="搜索抽奖" value="${esc(state.search)}" maxlength="100"/></label></div><div class="lottery-grid">${filtered.length ? filtered.map(lotteryCard).join("") : `<div class="empty-state"><img src="${esc(catURL)}" alt=""/><h3>${state.search || state.filter !== "all" ? "还没有匹配的好运" : "抽奖券，等你写下第一笔"}</h3><p>${state.search || state.filter !== "all" ? "试试其他关键词，或切换活动状态" : "设置奖品、时间和参与群，猫猫就可以开始收集期待了"}</p>${button("创建抽奖", { action: "create", glyph: "plus" })}</div>`}</div></section>
    <aside class="right-rail"><section class="rail-panel"><div class="rail-title"><h3>${icon("group")}已开启的平台</h3><button class="icon-btn ${state.loadingPlatforms ? "spinning" : ""}" data-action="platforms" aria-label="刷新平台列表" ${state.loadingPlatforms ? "disabled" : ""}>${icon("refresh")}</button></div><p class="muted">自动读取 AstrBot 的 AioCqhttp 连接；每场抽奖单独选择允许的平台和群</p>${state.loadingPlatforms ? '<div class="loading-line">正在读取机器人与群列表…</div>' : state.platforms.length ? state.platforms.map(platform => `<div class="platform-card"><span class="status-dot ${platform.online ? "online" : "offline"}"></span><div><strong>${esc(platform.name)}</strong><small>${platform.online ? platform.accounts.map(account => `QQ ${esc(account.bot_id)} · ${account.groups.length} 个群`).join("<br/>") : esc(platform.error || "协议端未连接")}</small></div><span class="platform-tag">AIO</span></div>`).join("") : '<div class="rail-empty">还没有开启的 AioCqhttp 平台<br/>请先在 AstrBot 的消息平台中开启连接</div>'}<div class="rail-footnote">${accounts.length} 个在线机器人 · 支持 SnowLuma / NapCat</div></section>
    <section class="rail-panel pink-panel"><div class="rail-title"><h3>${icon("send")}群通知投递</h3><span class="count-pill">${pendingCount}</span></div><p>${pendingCount ? "有通知等待发送，离线或发送失败会自动重试" : "通知队列已收好，开奖后会自动投递到对应的群"}</p><span class="rail-footnote">查看抽奖详情，可检查每条通知的状态</span></section><section class="rail-panel quiet-panel"><span class="eyebrow">从这里开始</span><ol class="onboarding"><li><b>选好奖品与时间</b><span>截止报名与开奖可以是不同时间</span></li><li><b>选好平台与群列表</b><span>每场活动都有自己的参与范围</span></li><li><b>发出你的抽奖券</b><span>发布到群，发送 /抽奖 参与 编号</span></li></ol></section></aside></div>` : state.view === "settings" ? settings() : guide()}<footer class="page-footer"><span>每一份期待，都认真收好</span><span>${state.refreshed ? `最近更新 ${fullDate(state.refreshed)}` : "连接管理页面中"} · 北京时间</span></footer></main></div>`;
  loadArtwork(app);
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
  return `<article class="lottery-card phase-${item.phase}" data-id="${esc(item.id)}">${item.cover ? `<div class="lottery-cover"><img data-artwork="${esc(item.cover)}" alt="${esc(item.title)}封面"/></div>` : ""}<div class="card-meta"><span class="phase-badge">${phaseLabel[item.phase]}</span><code>#${item.id}</code></div><h3>${esc(item.title)}</h3><div class="prize-line">${icon("gift")}<span>${esc(item.prize)}</span></div><div class="tier-chips">${item.prize_tiers.map(tier => `<span>${esc(tier.name)} <b>× ${tier.count}</b></span>`).join("")}${item.tier_draws?.length && item.status === "open" ? `<span class="early-chip">已揭晓 ${item.tier_draws.length} 项</span>` : ""}</div><div class="ticket-divider"></div><dl class="card-details"><div><dt>报名截止</dt><dd>${fullDate(item.close_at)}</dd></div><div><dt>自动开奖</dt><dd>${fullDate(item.draw_at)}</dd></div><div><dt>参与范围</dt><dd>${platforms.size} 个机器人 · ${targets.length} 个群</dd></div><div><dt>报名资料</dt><dd>${item.questions.length ? `${item.questions.length} 项私聊问题` : "无需填写资料"}</dd></div></dl><div class="participation"><span><strong>${item.complete_count}</strong> 份有效报名${pending ? `<small> · ${pending} 份待填写</small>` : ""}${item.review_pending_count ? `<small> · ${item.review_pending_count} 份待审核</small>` : ""}${item.rejected_count ? `<small> · ${item.rejected_count} 份未通过</small>` : ""}</span><span>${item.status === "drawn" ? `${item.winners.length} 人中奖` : `抽取 ${item.winner_count} 位`}</span></div>${item.pending_deliveries ? `<div class="delivery-hint">${icon("clock")}${item.pending_deliveries} 条通知待发送</div>` : ""}<footer class="card-actions">${button("查看详情", { action: "detail", style: "text", glyph: "arrow" })}${item.status === "open" ? button("管理", { action: "edit", style: "tonal", glyph: "edit" }) : ""}</footer></article>`;
}

function guide() {
  const commands = (rows) => rows.map(([command, help]) => `<div class="guide-command"><code>${esc(command)}</code><span>${help}</span></div>`).join("");
  return `<div class="subpage guide-page"><section class="page-intro"><div><span class="eyebrow">一人一签 · 认真开奖</span><h2>让每份期待<br/>都有清楚的下一步</h2><p>从群聊报名，在私聊填写<br/>到约定的时间，把好运送回群里</p></div><img src="${esc(catURL)}" alt="猫猫抱着抽奖券"/></section><div class="guide-page-grid"><section class="page-panel"><span class="guide-category">参与者 / 群聊</span><h2>先领一张抽奖券</h2><p class="muted">在本场活动允许的平台与群内操作，每个 QQ 号只计一次</p>${commands([["/抽奖", "查看图片帮助"], ["/抽奖 列表", "浏览可参加的活动"], ["/抽奖 详情 编号", "查看封面、分级奖品与时间"], ["/抽奖 参与 编号", "为实际发送者自己报名"], ["/抽奖 状态 编号", "查看自己的提交与审核进度"], ["/抽奖 退出 编号", "报名截止前退出，已中奖者不能退出"]])}</section><section class="page-panel"><span class="guide-category blue">参与者 / 私聊</span><h2>把资料悄悄告诉猫猫</h2><p class="muted">先在群内报名，再私聊报名时的同一个机器人，未添加好友时会提醒</p>${commands([["/抽奖 填写 编号", "开始或继续下一题"], ["/抽奖 回答 内容", "提交当前题，可带图片，保留空格与换行"], ["/抽奖 待办", "查看这个机器人上的待填写活动"], ["/抽奖 取消填写", "暂停填写，保留已提交的资料"]])}<div class="notice blue">最多 20 题，按题目发送文字、图片或图文消息<br/>必须当场答对：答错重答，全部完成即成功参与<br/>提交后审核：直接下一题，全部题目通过审核才有开奖资格</div></section><section class="page-panel full-width"><span class="guide-category">管理员 / 工作台</span><h2>从准备惊喜，到准时揭晓</h2><ol class="guide-steps"><li><strong>准备奖项与图片</strong><p>活动设置独立封面，每个奖项分别填写名称、奖品和名额，可上传一张奖品图片<br/>支持一等、二等、三等奖，也可以自定义；最多 10 个奖项，合计最多 100 个名额</p></li><li><strong>选择平台、群与时间</strong><p>每场活动单独选择机器人与群，设置报名截止与自动开奖时间<br/>不同群共享同一场参与名单，通知发往该场配置的所有群</p></li><li><strong>发布公告，审核资料</strong><p>创建后在详情发布群公告；后审模式请在自动开奖前完成逐题审核<br/>文字答案匹配不会覆盖已有标记，带图片的答案和资料题需人工审核</p></li><li><strong>按奖项接住好运</strong><p>可在活动详情提前抽出某个奖项，其余奖项继续按原时间开奖<br/>只抽取成功参与者，每个 QQ 最多中奖一次；名额不足优先分配靠前的未开奖奖项，空缺保留<br/>无人符合资格时保存空结果并通知所有群，已开奖奖项和中奖者资格锁定</p></li></ol></section><section class="page-panel full-width"><h2>管理员指令与 LLM 工具</h2><p class="muted">QQ 快速创建适用于单一奖品、不收集资料的活动，分奖项、上传图片与提前开奖请使用工作台</p>${commands([["/抽奖 创建 标题 | 奖品 | 人数 | 截止时间 | 开奖时间", "时间例如 2026-10-01 20:00，默认北京时间 UTC+8"], ["/抽奖 发布 编号", "将公告发送到全部配置群"], ["/抽奖 截止 编号 确认", "停止报名与填写，保留自动开奖时间"], ["/抽奖 开奖 编号 确认", "立即抽出全部剩余奖项"], ["/抽奖 取消 编号 确认", "仅取消尚无奖项开奖的活动"]])}<p class="field-hint">8 个 LLM 工具：查看列表、详情、参与、状态、退出、填写、快速创建与管理<br/>在插件设置统一开启或关闭，关闭后 QQ 指令与自动开奖照常运行</p></section></div></div>`;
}

function settings() {
  state.settingsDraft ??= { ...copy(state.settings), managerInput: "" };
  const draft = state.settingsDraft;
  return `<div class="subpage settings-page"><section class="page-intro compact"><div><span class="eyebrow">工作台 / 插件设置</span><h2>你的抽奖小屋，你来照顾</h2><p>管理 QQ 管理员与 LLM 工具调用<br/>每场活动的奖品、群与答题模式在各自活动中设置</p></div><div class="intro-stamp">${icon("settings")}<span>MEOW HOUSE</span></div></section><div class="settings-page-grid"><section class="page-panel"><div class="panel-heading"><div><span class="guide-category">智能助手</span><h2>LLM 工具调用</h2></div>${toggle("llm_tools_enabled", draft.llm_tools_enabled ?? true, "LLM 工具调用")}</div><p class="muted">统一开启或关闭本插件的 8 个工具，默认开启<br/>关闭后，QQ 指令和自动开奖照常运行</p><div class="settings-note">${icon("file")}<p>工具会校验真实 QQ 身份、管理员权限与活动范围<br/>资格审核、奖品上传和单个奖项提前开奖由工作台管理</p></div></section><section class="page-panel manager-panel"><span class="guide-category blue">管理权限</span><h2>抽奖管理员</h2><p class="muted">AstrBot 全局管理员始终具有管理权限<br/>这里添加的 QQ 号也可使用指令与已开启的工具管理抽奖</p><div id="manager-list">${draft.manager_ids.length ? draft.manager_ids.map(id => `<div class="list-row"><div class="avatar">${icon("paw")}</div><div><strong>QQ ${esc(id)}</strong><small>抽奖管理员</small></div><button class="icon-btn" data-remove-manager="${esc(id)}" aria-label="移除 QQ ${esc(id)}">${icon("close")}</button></div>`).join("") : '<div class="small-empty">尚未添加额外管理员，仅 AstrBot 全局管理员可以管理</div>'}</div><div class="inline-entry">${field("添加管理员 QQ", "manager", draft.managerInput, { placeholder: "输入 QQ 号", maxlength: 20 })}${button("添加", { action: "add-manager", glyph: "plus" })}</div><p class="field-hint">群成员只能管理自己的报名，不能替其他人报名或审核</p></section></div><div class="settings-save"><div><p class="settings-feedback" role="status" aria-live="polite"></p><p class="form-error" role="alert"></p><span class="field-hint">修改后点击保存，设置会在重启后保留</span></div>${button("保存设置", { action: "save-settings", glyph: "check", style: "primary" })}</div></div>`;
}

function editor(existing = null) {
  const frozen = !!existing?.entry_count || !!existing?.tier_draws?.length;
  const draft = existing ? copy(existing) : { title: "", prize_tiers: [{ name: "一等奖", prize: "", count: 1, image: "" }], cover: "", description: "", close_at: Date.now() / 1000 + 86400, draw_at: Date.now() / 1000 + 90000, targets: [], questions: [], require_correct: false };
  draft.prize_tiers ??= [{ name: "幸运奖", prize: draft.prize, count: draft.winner_count, image: "" }];
  draft.require_correct ??= true;
  const dialog = modal(existing ? "管理这一场好运" : "创建一场新的好运", existing ? `抽奖编号 ${existing.id} · 设置按活动独立保存` : "写下奖品，选好群，让猫猫准时开奖", `<div class="editor-sections"><section class="editor-section"><div class="editor-section-title"><span class="section-number">01</span><div><h3>准备这份惊喜</h3><p>封面属于本场活动，每个奖项有自己的奖品、图片与名额</p></div></div>${field("抽奖标题", "title", draft.title, { placeholder: "例如：午后的小小好运", maxlength: 80 })}${artwork("cover", draft.cover, "活动封面（可选）", { cover: true })}<div class="prizes-heading"><h4>奖项与奖品</h4><span class="prize-total" aria-live="polite"></span></div><div id="prize-tier-list"></div>${!frozen ? button("添加奖项", { action: "add-tier", glyph: "plus", style: "text" }) : ""}<p class="field-hint">从上到下为开奖顺序，人数不足时优先满足靠前的未开奖奖项<br/>每个 QQ 号最多中奖一次，最多 10 个奖项，名额合计不超过 100</p>${field("活动说明", "description", draft.description, { multiline: true, placeholder: "补充领取方式或参与说明（可选）", maxlength: 1500 })}</section>
    <section class="editor-section"><div class="editor-section-title"><span class="section-number">02</span><div><h3>约定揭晓的时间</h3><p>全部时间采用北京时间（UTC+8），不受浏览器时区影响</p></div></div><div class="form-grid"><div class="field"><span class="field-label">报名截止时间</span><meow-calendar name="close_at" value="${chinaDate(draft.close_at)}"></meow-calendar><span class="field-hint">截止后停止报名与私聊资料填写</span></div><div class="field"><span class="field-label">自动开奖时间</span><meow-calendar name="draw_at" value="${chinaDate(draft.draw_at)}"></meow-calendar><span class="field-hint">可以等于或晚于报名截止时间</span></div></div></section>
    <section class="editor-section"><div class="editor-section-title"><span class="section-number">03</span><div><h3>把好运送到哪些群？</h3><p>列表中的机器人平台和群，才可以参与本次抽奖，开奖发往所有这些群</p></div></div>${frozen ? '<div class="notice">已有报名或奖项开奖，平台、群、问题、答题模式、奖项顺序、奖品、图片与名额已锁定</div>' : '<div class="notice blue">平台自动读取自 AstrBot，可添加多个平台，每个平台再选择自己的群；不同抽奖互不影响</div>'}<div id="target-list"></div>${!frozen ? button("添加平台与群", { action: "add-target", glyph: "plus", style: "text" }) : ""}<p class="field-hint">同一 QQ 号跨平台、跨群只计一次，私聊资料必须发给报名时的那个机器人</p></section>
    <section class="editor-section"><div class="editor-section-title"><span class="section-number">04</span><div><h3>报名之前，想了解什么？</h3><p>最多 20 项，可混合添加答题、文本、图片与图文资料；按顺序逐题填写</p></div></div><section class="answer-mode"><div><h4>必须当场答对</h4><p class="answer-mode-text">${draft.require_correct ? "开启：答题当场核对，答错需重答；全部资料完成即成功参与" : "关闭：答案提交即进入下一题，不当场判断；全部资料提交后由管理员逐题审核，全部通过才有开奖资格"}</p></div>${toggle("require_correct", draft.require_correct, "必须当场答对", { disabled: frozen })}</section><div id="question-list"></div>${!frozen ? button("添加一个问题", { action: "add-question", glyph: "plus", style: "text" }) : ""}<p class="field-hint">不添加问题时，群聊报名直接成功；开启当场答对时，答题必须设置文字正确答案；关闭时可留空供人工审核；选择题参考答案填写完整选项文本，每行一个；仅管理员可见</p></section></div><p class="form-error" role="alert"></p>`, button("先返回", { action: "dismiss", style: "text" }) + button(existing ? "保存修改" : "创建抽奖", { action: "save-lottery", glyph: "check", style: "primary" }), { wide: true });

  function paintTiers() {
    dialog.holder.querySelector("#prize-tier-list").innerHTML = draft.prize_tiers.map((tier, index) => `<section class="prize-tier-card" data-tier="${index}"><div class="prize-tier-top"><span class="tier-order">${String(index + 1).padStart(2, "0")}</span><strong>第 ${index + 1} 个奖项</strong>${!frozen ? `<button class="icon-btn" data-action="move-tier" aria-label="向上移动第 ${index + 1} 个奖项" ${index === 0 ? "disabled" : ""}>↑</button><button class="icon-btn" data-action="remove-tier" aria-label="删除第 ${index + 1} 个奖项" ${draft.prize_tiers.length === 1 ? "disabled" : ""}>${icon("trash")}</button>` : ""}</div><div class="prize-tier-fields">${field("奖项名称", "tier_name", tier.name, { placeholder: "例如：一等奖", maxlength: 30, disabled: frozen })}<div class="field"><span class="field-label">本奖项名额</span><meow-stepper name="tier_count" label="第 ${index + 1} 个奖项名额" value="${tier.count}" min="1" max="100" ${frozen ? "disabled" : ""}></meow-stepper></div></div>${field("奖品内容", "tier_prize", tier.prize, { placeholder: "例如：猫猫玩偶一只，每位中奖者一份", maxlength: 300, disabled: frozen })}${artwork("tier_image", tier.image, `${tier.name || "本奖项"}奖品图片（可选）`, { disabled: frozen })}</section>`).join("");
    dialog.holder.querySelector(".prize-total").textContent = `共 ${draft.prize_tiers.length} 个奖项 · ${draft.prize_tiers.reduce((sum, tier) => sum + tier.count, 0)} 个名额`;
  }
  const readTiers = () => dialog.holder.querySelectorAll("[data-tier]").forEach(element => {
    const tier = draft.prize_tiers[Number(element.dataset.tier)];
    tier.name = element.querySelector('[name="tier_name"]').value.trim();
    tier.prize = element.querySelector('[name="tier_prize"]').value.trim();
    tier.count = element.querySelector('meow-stepper[name="tier_count"]').value;
    tier.image = element.querySelector('meow-upload[name="tier_image"]').value;
  });

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
    }).join("") : '<div class="small-empty">还没添加群；点击下方按钮，为这场抽奖选择参与范围</div>';
  }

  function paintQuestions() {
    dialog.holder.querySelector("#question-list").innerHTML = draft.questions.length ? draft.questions.map((question, index) => `<div class="question-card" data-question="${index}"><div class="question-header"><span class="question-number">第 ${index + 1} 项</span>${choice("kind", question.kind, [{ value: "quiz", label: "答题 · 选择或简答" }, { value: "text", label: "文本资料 · 可附图片" }, { value: "image", label: "图片资料 · 可附文字" }, { value: "mixed", label: "图文资料 · 文字图片都必填" }], { disabled: frozen })}${!frozen ? `<button class="icon-btn" data-action="move-question" aria-label="向上移动第 ${index + 1} 项" ${index === 0 ? "disabled" : ""}>↑</button><button class="icon-btn" data-action="remove-question" aria-label="删除第 ${index + 1} 项">${icon("trash")}</button>` : ""}</div>${field("问题内容", "prompt", question.prompt, { placeholder: "清晰地告诉参与者需要回答或提供什么", maxlength: 300, disabled: frozen })}${question.kind === "quiz" ? `<div class="form-grid">${field("选项（可选）", "options", question.options.join("\n"), { multiline: true, placeholder: "每行一个选项，最多 8 个\n留空则为简答题", disabled: frozen })}${field("文字正确答案（后审模式可留空）", "answers", question.answers.join("\n"), { multiline: true, placeholder: "每行一个答案，最多 20 个\n选择题请填完整的正确选项文本", disabled: frozen })}</div>` : `<p class="field-hint">${question.kind === "image" ? "参与者需要发送一张图片，最大 8 MB，可在同一条消息中附带 1000 字以内文字" : question.kind === "mixed" ? "参与者必须在同一条消息中发送 1–1000 字文字和一张图片，最大 8 MB" : "参与者发送 1–1000 字文本资料，可在同一条消息中附带一张图片"}</p>`}</div>`).join("") : '<div class="small-empty"><strong>轻轻松松参加</strong><span>没有私聊问题，群聊发送参与指令即可成功</span></div>';
  }

  const readQuestions = () => dialog.holder.querySelectorAll("[data-question]").forEach(element => {
    const question = draft.questions[Number(element.dataset.question)];
    question.prompt = element.querySelector('[name="prompt"]').value;
    if (question.kind === "quiz") {
      question.options = element.querySelector('[name="options"]').value.split("\n").map(text => text.trim()).filter(Boolean);
      question.answers = element.querySelector('[name="answers"]').value.split("\n").map(text => text.trim()).filter(Boolean);
    }
  });
  paintTiers(); paintTargets(); paintQuestions();
  dialog.holder.addEventListener("upload-state", () => {
    const busy = [...dialog.holder.querySelectorAll("meow-upload")].some(element => element.busy);
    dialog.holder.querySelector('[data-action="save-lottery"]').disabled = busy;
  });
  dialog.holder.addEventListener("change", event => {
    if (event.target.matches('meow-upload[name="cover"]')) draft.cover = event.target.value;
    if (event.target.closest("[data-tier]")) {
      readTiers();
      dialog.holder.querySelector(".prize-total").textContent = `共 ${draft.prize_tiers.length} 个奖项 · ${draft.prize_tiers.reduce((sum, tier) => sum + tier.count, 0)} 个名额`;
    }
    if (event.target.matches('meow-switch[name="require_correct"]')) {
      draft.require_correct = event.target.value;
      dialog.holder.querySelector(".answer-mode-text").textContent = draft.require_correct ? "开启：答题当场核对，答错需重答；全部资料完成即成功参与" : "关闭：答案提交即进入下一题，不当场判断；全部资料提交后由管理员逐题审核，全部通过才有开奖资格";
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
    if (["add-tier", "remove-tier", "move-tier"].includes(action)) {
      if ([...dialog.holder.querySelectorAll("meow-upload")].some(element => element.busy)) { toast("图片上传中，请完成后调整奖项", "error"); return; }
      readTiers();
      if (action === "add-tier") {
        if (draft.prize_tiers.length >= 10) { toast("最多设置 10 个奖项", "error"); return; }
        const names = ["一等奖", "二等奖", "三等奖", "四等奖", "五等奖", "六等奖", "七等奖", "八等奖", "九等奖", "十等奖"];
        draft.prize_tiers.push({ name: names.find(name => !draft.prize_tiers.some(tier => tier.name === name)) || `奖项 ${draft.prize_tiers.length + 1}`, prize: "", count: 1, image: "" });
      } else {
        const index = Number(event.target.closest("[data-tier]").dataset.tier);
        if (action === "remove-tier" && draft.prize_tiers.length > 1) draft.prize_tiers.splice(index, 1);
        else if (action === "move-tier" && index > 0) [draft.prize_tiers[index - 1], draft.prize_tiers[index]] = [draft.prize_tiers[index], draft.prize_tiers[index - 1]];
      }
      paintTiers();
    }
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
      if ([...dialog.holder.querySelectorAll("meow-upload")].some(element => element.busy)) { toast("请等待图片上传完成", "error"); return; }
      readTiers();
      readQuestions();
      for (const name of ["title", "description"]) draft[name] = dialog.holder.querySelector(`[name="${name}"]`).value.trim();
      draft.cover = dialog.holder.querySelector('meow-upload[name="cover"]').value;
      delete draft.prize; delete draft.winner_count;
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
      } catch (error) { errorField.textContent = cleanMessage(error.message) || "保存失败，请检查填写内容"; errorField.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
      finally { save.disabled = false; }
    }
  });
}

async function detail(id) {
  const item = state.lotteries.find(lottery => lottery.id === id);
  if (!item) { toast("记录已更新，请刷新后重试", "error"); return; }
  let data;
  try { data = await bridge.apiGet(`lotteries/${id}`); }
  catch (error) { toast(cleanMessage(error.message), "error"); return; }
  const laterReview = item.require_correct === false;
  const winnerIds = new Set(item.winners.map(winner => winner.user_id));
  const canReview = laterReview && item.status === "open" && state.refreshed < item.draw_at;
  const scope = item.targets.map(target => `<span class="scope-tag">${icon("group")}${esc(target.group_name || target.group_id)}<small>${esc(target.platform_id)} / QQ ${target.bot_id}</small></span>`).join("");
  const actions = item.status === "open" ? button("发布到所有群", { action: "publish", glyph: "send", style: "primary" }) + button("修改设置", { action: "detail-edit", glyph: "edit" }) + button("截止报名", { action: "close", style: "text" }) + button("立即开奖", { action: "draw", style: "text" }) + button("取消抽奖", { action: "cancel", style: "text", attrs: item.tier_draws?.length ? "disabled title=已有奖项开奖，不能取消" : "" }) : button("删除记录与资料", { action: "delete", style: "text", glyph: "trash" });
  const dialog = modal(item.title, `编号 ${id} · ${phaseLabel[item.phase]} · 仅管理页展示填写资料`, `
    ${item.cover ? `<div class="detail-cover"><img data-artwork="${esc(item.cover)}" alt="${esc(item.title)}封面"/></div>` : ""}<div class="detail-ticket"><span class="phase-badge phase-${item.phase}">${phaseLabel[item.phase]}</span><h3>${item.prize_tiers.length} 个奖项 · ${item.winner_count} 份好运</h3><div class="detail-dates"><span>截止 <b>${chinaDate(item.close_at)}</b></span><span>计划开奖 <b>${chinaDate(item.draw_at)}</b></span></div><p>${esc(item.description || "愿每一份期待都有回响")}</p><div class="scope-list">${scope}</div><p class="field-hint">答题模式：${laterReview ? "先提交，后审核资格" : "必须当场答对"}</p></div>
    <section class="detail-section"><div class="section-heading"><h3>${icon("gift")}奖项与开奖</h3><span class="muted">每个 QQ 号最多中奖一次</span></div><div class="award-results">${item.prize_tiers.map((tier, index) => {
      const record = item.tier_draws?.find(draw => draw.tier_index === index);
      const drawn = !!record || item.status === "drawn";
      const winners = item.winners.filter(winner => (winner.tier_index ?? 0) === index);
      return `<section class="award-result"><div class="award-result-heading"><div><span class="guide-category">${esc(tier.name)}</span><h4>${esc(tier.prize)}</h4><p>名额 ${tier.count} 位${drawn ? ` · 已中奖 ${winners.length} 位` : " · 尚未开奖"}</p></div>${!drawn && item.status === "open" ? button("提前开奖", { action: "draw-tier", style: "tonal", glyph: "gift", attrs: `data-tier-index="${index}" ${state.refreshed >= item.draw_at ? "disabled" : ""}` }) : `<span class="award-state">${drawn ? "已开奖" : "已取消"}</span>`}</div>${tier.image ? `<div class="award-picture"><img data-artwork="${esc(tier.image)}" alt="${esc(tier.name)}奖品图片"/></div>` : ""}${winners.map(winner => `<div class="winner-row"><span class="winner-paw">${icon("paw")}</span><strong>${esc(winner.nickname)}</strong><span>QQ ${esc(winner.user_id)}</span></div>`).join("")}${drawn && winners.length < tier.count ? `<p class="award-vacancy">${winners.length ? `本奖项空缺 ${tier.count - winners.length} 位` : "本奖项无人符合资格，名额全部空缺"} · 已保存，不能再次抽取</p>` : ""}${record ? `<p class="field-hint">开奖 ${chinaDate(record.drawn_at)} · 本次候选 ${record.eligible_count} 人<br/><code>${esc(record.pool_hash)}</code></p>` : ""}</section>`;
    }).join("")}</div><p class="field-hint">只抽取已完成资料且审核通过的成功参与者，已中奖者不进入剩余奖项<br/>人数不足时，按列表顺序优先分配未开奖奖项；提前开奖仅冻结当前奖项，剩余奖项按原时间开奖</p>${item.status === "drawn" ? `<p class="field-hint">全部奖项已开奖 · ${chinaDate(item.drawn_at)} · 有效参与 ${item.eligible_count} 人${item.winners.length ? "" : " · 本次无人中奖"}</p>` : ""}</section>
    <section class="detail-section"><div class="section-heading"><h3>${icon("group")}报名与私聊资料 <span class="count-pill">${data.entries.length}</span></h3>${canReview ? button("按文字答案匹配", { action: "match-answers", style: "tonal", glyph: "check" }) : ""}</div><p class="muted">资料仅此管理页面可见；群聊公告不会显示用户填写内容</p>${laterReview && item.questions.length ? `<div class="notice blue">${canReview ? "提交后可逐题标记；所有题目通过才算成功参与；文字匹配仅处理有参考答案、尚未标记的纯文字答题，不覆盖已有标记；带图片的答题与资料题请人工核对" : "审核已锁定，以保存的开奖资格与结果为准"}${canReview ? `<br/>请在 ${chinaDate(item.draw_at)} 开奖前完成审核；未填写完、待审核或未通过者均不进入开奖名单` : ""}</div>` : ""}<p class="review-summary muted" aria-live="polite"></p><div class="entry-list"></div></section>
    <section class="detail-section"><div class="section-heading"><h3>${icon("send")}通知发送记录</h3>${button("重试待发送通知", { action: "retry", glyph: "refresh", style: "text" })}</div><div class="delivery-list"></div><p class="field-hint">显示最近 200 条；群聊和私聊分别重试；开奖结果不会再次抽取</p></section><p class="form-error" role="alert"></p>`, `<div class="detail-actions">${actions}</div>`, { wide: true });

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
        return `<div class="answer-row" data-answer="${index}"><span>${index + 1}. ${esc(item.questions[index]?.prompt || "资料")}</span>${answer.kind === "image" ? (answer.text ? `<p>${esc(answer.text)}</p>` : "") : `<p>${esc(answer.value)}</p>`}${answer.kind === "image" || answer.image ? button("下载私聊图片", { action: "download-image", style: "text", glyph: "file", attrs: `data-file="${esc(answer.kind === "image" ? answer.value : answer.image)}"` }) : ""}${laterReview ? `<div class="answer-review"><span>审核标记</span>${choice("review", mark, [{ value: "pending", label: "待审核" }, { value: "correct", label: "正确 / 符合要求" }, { value: "wrong", label: "错误 / 不符合要求" }], { disabled: !canReview || entry.status !== "complete" || winnerIds.has(entry.user_id), label: `QQ ${entry.user_id} 第 ${index + 1} 题审核标记` })}</div>${answer.reviewed_at ? `<small class="field-hint">${answer.review_method === "match" ? "文字匹配" : "人工标记"} · ${chinaDate(answer.reviewed_at)} · ${esc(answer.reviewed_by)}</small>` : ""}` : ""}</div>`;
      }).join("") : '<p class="muted">尚未填写资料</p>'}</div></div>`;
    }).join("") : '<div class="small-empty">本场暂无报名记录</div>';
    dialog.holder.querySelector(".delivery-list").innerHTML = data.deliveries.length ? data.deliveries.map(delivery => `<div class="delivery-row"><div><strong>${({ success: "参与成功", submitted: "资料已提交", review: "资格审核结果", result: "开奖通知", tier_result: "单个奖项提前开奖", announcement: "群公告", cancelled: "取消通知", closed: "截止通知" })[delivery.kind] || "通知"}</strong><small>${delivery.target.channel === "group" ? "群" : "私聊 QQ"} ${delivery.target.recipient} · ${esc(delivery.target.platform_id)}</small></div><span class="${delivery.delivered_at ? "delivered" : "pending"}">${delivery.delivered_at ? "已发送" : delivery.attempts ? `待重试 · ${delivery.attempts} 次` : "等待发送"}</span></div>`).join("") : '<div class="small-empty">还没有发送记录</div>';
  }
  paintEntries(); loadArtwork(dialog.holder);
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
    } catch (error) { errorField.textContent = cleanMessage(error.message); paintEntries(); }
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
      catch (error) { toast(cleanMessage(error.message), "error"); }
      finally { target.disabled = false; }
    } else if (action === "match-answers") {
      dialog.holder.querySelectorAll('meow-choice[name="review"] button, [data-action="match-answers"]').forEach(button => { button.disabled = true; });
      dialog.holder.querySelector(".form-error").textContent = "";
      try {
        const result = await bridge.apiPost(`lotteries/${id}/review`, { action: "match" });
        data = await bridge.apiGet(`lotteries/${id}`);
        paintEntries(); await loadState({ quiet: true });
        toast(result.marked_answers ? `已匹配 ${result.marked_answers} 个文字答案；图片与未标记资料请人工审核` : "没有可匹配的未审核文字答案，请人工标记");
      } catch (error) { dialog.holder.querySelector(".form-error").textContent = cleanMessage(error.message); paintEntries(); }
      finally { target.disabled = false; }
    } else if (action === "draw-tier") {
      const tier_index = Number(target.dataset.tierIndex);
      const tier = item.prize_tiers[tier_index];
      const confirm = modal(`提前揭晓${tier.name}？`, `抽奖 ${item.title} · ${id}`, `<p class="confirm-copy">从当前成功参与且未中奖的人中抽取最多 ${tier.count} 位，通知将发送到全部配置群<br/>人数不足时保留空缺，无人符合资格也会保存空结果<br/>本奖项不能再次抽取，中奖者资格锁定；剩余奖项继续按原时间开奖</p>`, button("先返回", { action: "dismiss", style: "text" }) + button("确认开奖", { action: "confirm-tier", style: "tonal" }));
      confirm.holder.querySelector('[data-action="confirm-tier"]').addEventListener("click", async event => {
        event.currentTarget.disabled = true;
        try {
          await bridge.apiPost(`lotteries/${id}/action`, { action: "draw_tier", tier_index, confirmed: true });
          confirm.close(); dialog.close(); toast(`${tier.name}已开奖，通知已排队`);
          await loadState(); await detail(id);
        } catch (error) { confirm.close(); dialog.holder.querySelector(".form-error").textContent = cleanMessage(error.message); }
      });
    } else if (action === "detail-edit") { dialog.close(); editor(item); }
    else if (["publish", "retry", "close", "draw", "cancel", "delete"].includes(action)) {
      const execute = async () => {
        target.disabled = true;
        try {
          await bridge.apiPost(`lotteries/${id}/action`, { action, confirmed: true });
          dialog.close(); toast(action === "delete" ? "抽奖和资料已删除" : action === "retry" ? "待发送通知已重新排队" : "操作已保存，群通知已排队");
          await loadState();
        } catch (error) { dialog.holder.querySelector(".form-error").textContent = cleanMessage(error.message); }
        finally { target.disabled = false; }
      };
      if (["publish", "retry"].includes(action)) await execute();
      else {
        const info = { close: ["现在截止报名？", "群内报名与私聊填写会立即停止；后审活动仍可在开奖前审核；只抽取成功参与者，仍按原开奖时间自动开奖"], draw: ["现在揭晓中奖名单？", "报名和审核会立即锁定，只抽取成功参与者；未填写完、待审核或未通过者均不参与；没有合格者时保存空结果并通知所有群；开奖结果不能再次抽取"], cancel: ["取消这场抽奖？", "活动会结束，不再接受报名、审核或开奖，并向所有设置的群发送取消通知"], delete: ["删除抽奖与私聊资料？", "这会永久移除活动记录、报名资料、私聊图片及发送记录；此操作无法撤销"] }[action];
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
  if (["home", "settings", "guide"].includes(action)) {
    state.view = action;
    if (action === "home") { state.filter = "all"; state.search = ""; }
    history.replaceState(null, "", `#${action}`); render();
    app.querySelector("h1")?.setAttribute("tabindex", "-1"); app.querySelector("h1")?.focus({ preventScroll: true });
  }
  const removeManager = event.target.closest("[data-remove-manager]");
  if (removeManager) { state.settingsDraft.manager_ids = state.settingsDraft.manager_ids.filter(id => id !== removeManager.dataset.removeManager); render(); }
  if (action === "add-manager") {
    const input = app.querySelector('[name="manager"]');
    const id = input.value.trim();
    if (!/^[1-9]\d{4,19}$/.test(id)) { toast("请填写有效 QQ 号", "error"); return; }
    if (state.settingsDraft.manager_ids.length >= 100 && !state.settingsDraft.manager_ids.includes(id)) { toast("最多添加 100 个管理员", "error"); return; }
    if (!state.settingsDraft.manager_ids.includes(id)) state.settingsDraft.manager_ids.push(id);
    state.settingsDraft.managerInput = ""; render();
  }
  if (action === "save-settings") {
    const save = event.target.closest("button"); save.disabled = true;
    const draft = state.settingsDraft;
    app.querySelector(".form-error").textContent = "";
    try {
      await bridge.apiPost("settings", { manager_ids: draft.manager_ids, llm_tools_enabled: draft.llm_tools_enabled });
      state.settings = { manager_ids: [...draft.manager_ids], llm_tools_enabled: draft.llm_tools_enabled };
      toast("插件设置已保存");
      if (state.view === "settings") app.querySelector(".settings-feedback").textContent = "已保存，设置现已生效";
    } catch (error) { if (state.view === "settings") app.querySelector(".form-error").textContent = cleanMessage(error.message); }
    finally { if (save.isConnected) save.disabled = false; }
  }
  if (action === "create") editor();
  if (["detail", "edit"].includes(action)) {
    const id = event.target.closest("[data-id]").dataset.id;
    if (action === "detail") await detail(id);
    else editor(state.lotteries.find(item => item.id === id));
  }
});

app.addEventListener("input", event => {
  if (state.view === "settings" && event.target.matches('[name="manager"]')) state.settingsDraft.managerInput = event.target.value;
});
app.addEventListener("change", event => {
  if (state.view === "settings" && event.target.matches('meow-switch[name="llm_tools_enabled"]')) state.settingsDraft.llm_tools_enabled = event.target.value;
});
window.addEventListener("hashchange", () => {
  const view = location.hash.slice(1);
  if (["home", "settings", "guide"].includes(view)) { state.view = view; render(); }
});

if (!bridge) {
  app.setAttribute("aria-busy", "false");
  app.innerHTML = `<div class="boot"><img src="${esc(catURL)}" alt=""/><h1>请从 AstrBot 打开抽奖小屋</h1><p>登录 AstrBot → 插件 → 喵喵抽奖 → 管理工作台</p><p>这个页面通过 AstrBot 插件 Pages 安全访问管理数据</p></div>`;
} else {
  try {
    await bridge.ready();
    await loadState();
    await loadPlatforms();
    const timer = setInterval(() => { if (!document.hidden) loadState({ quiet: true }); }, 20000);
    window.addEventListener("beforeunload", () => clearInterval(timer));
  } catch (error) {
    app.setAttribute("aria-busy", "false");
    app.innerHTML = `<div class="boot"><img src="${esc(catURL)}" alt=""/><h1>工作台暂时没有连上</h1><p>${esc(cleanMessage(error.message) || "请在 AstrBot 中重新打开此页面")}</p></div>`;
  }
}
