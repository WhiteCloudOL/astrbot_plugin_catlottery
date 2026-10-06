import { ActivityView } from "./activity.js";
import { escapeHTML as esc, icon, button, field, choice, toggle, artwork, loadArtwork, chinaDate, fullDate, modal, pageForm, clearAvatarCache, toast } from "./components.js";

const app = document.getElementById("app");
const bridge = window.AstrBotPluginPage;
window.addEventListener("error", () => console.error("CatLottery UI interaction failed unexpectedly."));
window.addEventListener("unhandledrejection", () => console.error("CatLottery UI request failed unexpectedly."));
const catURL = document.getElementById("cat-template").content.querySelector("img").getAttribute("src");
const initialRoute = location.hash.slice(1).split("/");
let activity = null;
const state = { lotteries: [], settings: { manager_ids: [], llm_tools_enabled: true }, settingsDraft: null, view: ["home", "settings", "guide", "create", "edit", "detail"].includes(initialRoute[0]) ? initialRoute[0] : "home", activityId: /^[a-f0-9]{8}$/.test(initialRoute[1] || "") ? initialRoute[1] : "", editorDirty: false, editorSaving: false, platforms: [], filter: "all", search: "", loadingPlatforms: false, error: "", refreshed: null };
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
    if (!typing && ["home", "guide", "settings"].includes(state.view) && !(quiet && state.view === "settings")) render();
  } catch (error) {
    state.error = cleanMessage(error.message) || "暂时无法读取抽奖，请刷新重试";
    if (!quiet) toast(state.error, "error");
    if (["home", "guide", "settings"].includes(state.view)) render();
  }
}

async function loadPlatforms() {
  state.loadingPlatforms = true;
  if (state.view === "home") render();
  try {
    const data = await bridge.apiGet("platforms");
    state.platforms = data.platforms;
  } catch (error) { toast(cleanMessage(error.message) || "平台读取失败", "error"); }
  finally { state.loadingPlatforms = false; if (state.view === "home") render(); }
}

function render() {
  const openCount = state.lotteries.filter(item => item.phase === "open").length;
  const waitingCount = state.lotteries.filter(item => item.phase === "closed").length;
  const pendingCount = state.lotteries.reduce((sum, item) => sum + item.pending_deliveries, 0);
  const filtered = state.lotteries.filter(item => (state.filter === "all" || item.phase === state.filter) && `${item.title} ${item.id} ${item.prize}`.toLowerCase().includes(state.search.toLowerCase()));
  const accounts = accountOptions();
  const pageInfo = { home: ["抽奖小屋", "群里的抽奖，都在这里"], settings: ["插件设置", "管理员、工具与头像缓存"], guide: ["使用指南", "报名与开奖使用指南"], detail: ["活动详情", "查看奖项与报名"], edit: ["活动设置", "修改活动设置"], create: ["创建抽奖", "创建群抽奖"] }[state.view];
  app.setAttribute("aria-busy", "false");
  app.innerHTML = `<div class="workspace">
    <aside class="sidebar"><a class="brand" href="#home" data-action="home" aria-label="喵喵抽奖首页"><img src="${esc(catURL)}" alt=""/><span>喵喵抽奖<small>群聊报名 · 定时开奖</small></span></a><p class="sidebar-label">我的工作台</p><nav aria-label="主导航"><button class="nav-item ${["home", "detail", "edit", "create"].includes(state.view) ? "active" : ""}" aria-current="${["home", "detail", "edit", "create"].includes(state.view) ? "page" : "false"}" data-action="home" aria-label="抽奖小屋">${icon("gift")}<span>抽奖小屋</span><span class="nav-count">${state.lotteries.length}</span></button><button class="nav-item ${state.view === "settings" ? "active" : ""}" aria-current="${state.view === "settings" ? "page" : "false"}" data-action="settings" aria-label="插件设置">${icon("settings")}<span>插件设置</span></button><button class="nav-item ${state.view === "guide" ? "active" : ""}" aria-current="${state.view === "guide" ? "page" : "false"}" data-action="guide" aria-label="使用指南">${icon("file")}<span>使用指南</span></button></nav><div class="sidebar-note"><div class="tiny-paws">${icon("paw")}${icon("paw")}</div><strong>参与小提示</strong><p>从群聊报名，在私聊填写<br/>每个 QQ 号，一次机会</p><code>/抽奖</code></div><footer class="sidebar-footer"><span class="online-dot"></span>喵喵抽奖<small>清蒸云鸭 · v1.6.4</small></footer></aside>
    <main class="main"><header class="topbar"><div><span class="eyebrow">喵喵抽奖 / ${pageInfo[0]}</span><h1>${pageInfo[1]}</h1></div><div class="topbar-actions"><span class="time-label">${icon("clock")}北京时间 UTC+8</span>${button("刷新", { action: "refresh", style: "text", glyph: "refresh" })}</div></header>
    ${state.error ? `<div class="error-banner" role="alert">${icon("paw")}<span>${esc(state.error)} 已显示的记录可能不是最新状态</span>${button("重试", { action: "refresh", style: "text" })}</div>` : ""}
    ${state.view === "home" ? `<section class="hero"><div class="hero-copy"><span class="hero-stamp">喵喵抽奖 · 群活动</span><h2>选好奖品，<br/>猫猫来开奖</h2><p>选好奖品和参与群，发布公告<br/>猫猫帮你收报名，到点自动开奖</p>${button("创建一场抽奖", { action: "create", glyph: "plus", style: "primary" })}<span class="hero-caption">${openCount ? `${openCount} 场正在报名` : "创建活动后，记得发布群公告"}${waitingCount ? ` · ${waitingCount} 场等待开奖` : ""}</span></div><div class="hero-art" aria-hidden="true"><div class="floating-star star-one">✦</div><div class="floating-star star-two">✧</div><div class="raffle-ticket"><div class="ticket-top"><span>MEOW · LUCKY TICKET</span><span>♡</span></div><img src="${esc(catURL)}" alt=""/><strong>猫猫抽奖券</strong><p>今天的幸运，会是你吗</p><div class="ticket-stitch"></div><div class="ticket-bottom"><span>一人一次 · 到点开奖</span>${icon("paw")}</div></div><div class="ticket-label">奖品就位，等你报名</div></div></section>
    <div class="content-grid"><section class="lotteries"><div class="section-heading"><div><span class="eyebrow">抽奖清单</span><h2>全部抽奖 <span>${state.lotteries.length}</span></h2></div></div><div class="filterbar"><div class="segmented" role="group" aria-label="按活动状态筛选">${[["all", "全部"], ["open", "报名中"], ["closed", "待开奖"], ["drawn", "已开奖"], ["cancelled", "已取消"]].map(([value, label]) => `<button data-filter="${value}" aria-pressed="${state.filter === value}" class="${state.filter === value ? "selected" : ""}">${label}</button>`).join("")}</div><label class="search">${icon("search")}<input type="text" placeholder="搜索名称或编号" aria-label="搜索抽奖" value="${esc(state.search)}" maxlength="100"/></label></div><div class="lottery-grid">${filtered.length ? filtered.map(lotteryCard).join("") : `<div class="empty-state"><img src="${esc(catURL)}" alt=""/><h3>${state.search || state.filter !== "all" ? "没有找到匹配的活动" : "还没有抽奖活动"}</h3><p>${state.search || state.filter !== "all" ? "试试其他关键词，或切换活动状态" : "设置奖品、时间和参与群，就能开始报名"}</p>${button("创建抽奖", { action: "create", glyph: "plus" })}</div>`}</div></section>
    <aside class="right-rail"><section class="rail-panel"><div class="rail-title"><h3>${icon("group")}已开启的平台</h3><button class="icon-btn ${state.loadingPlatforms ? "spinning" : ""}" data-action="platforms" aria-label="刷新平台列表" ${state.loadingPlatforms ? "disabled" : ""}>${icon("refresh")}</button></div><p class="muted">自动读取 AstrBot 的 AioCqhttp 连接；每场抽奖单独选择允许的平台和群</p>${state.loadingPlatforms ? '<div class="loading-line">正在读取机器人与群列表…</div>' : state.platforms.length ? state.platforms.map(platform => `<div class="platform-card"><span class="status-dot ${platform.online ? "online" : "offline"}"></span><div><strong>${esc(platform.name)}</strong><small>${platform.online ? platform.accounts.map(account => `QQ ${esc(account.bot_id)} · ${account.groups.length} 个群`).join("<br/>") : esc(platform.error || "协议端未连接")}</small></div><span class="platform-tag">AIO</span></div>`).join("") : '<div class="rail-empty">还没有开启的 AioCqhttp 平台<br/>请先在 AstrBot 的消息平台中开启连接</div>'}<div class="rail-footnote">${accounts.length} 个在线机器人 · 支持 SnowLuma / NapCat</div></section>
    <section class="rail-panel pink-panel"><div class="rail-title"><h3>${icon("send")}群通知投递</h3><span class="count-pill">${pendingCount}</span></div><p>${pendingCount ? "有通知等待发送，离线或发送失败会自动重试" : "当前没有待发送的通知"}</p><span class="rail-footnote">查看抽奖详情，可检查每条通知的状态</span></section><section class="rail-panel quiet-panel"><span class="eyebrow">从这里开始</span><ol class="onboarding"><li><b>选好奖品与时间</b><span>截止报名与开奖可以是不同时间</span></li><li><b>选好平台与群列表</b><span>每场活动都有自己的参与范围</span></li><li><b>发布群公告</b><span>发布到群，发送 /抽奖 参与 编号</span></li></ol></section></aside></div>` : state.view === "settings" ? settings() : state.view === "guide" ? guide() : '<div class="activity-outlet"></div>'}<footer class="page-footer"><span>喵喵抽奖 · 群里的小惊喜</span><span>${state.refreshed ? `最近更新 ${fullDate(state.refreshed)}` : "正在连接"} · 北京时间</span></footer></main></div>`;
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
  return `<article class="lottery-card phase-${item.phase}" data-id="${esc(item.id)}">${item.cover ? `<div class="lottery-cover"><img data-artwork="${esc(item.cover)}" alt="${esc(item.title)}封面"/></div>` : ""}<div class="card-meta"><span class="phase-badge">${phaseLabel[item.phase]}</span><code>#${item.id}</code></div><h3>${esc(item.title)}</h3><div class="prize-line">${icon("gift")}<span>${esc(item.prize)}</span></div><div class="tier-chips">${item.prize_tiers.map(tier => `<span>${esc(tier.name)} <b>× ${tier.count}</b></span>`).join("")}${item.tier_draws?.length && item.status === "open" ? `<span class="early-chip">已揭晓 ${item.tier_draws.length} 项</span>` : ""}</div><div class="ticket-divider"></div><dl class="card-details"><div><dt>报名截止</dt><dd>${fullDate(item.close_at)}</dd></div><div><dt>自动开奖</dt><dd>${fullDate(item.draw_at)}</dd></div><div><dt>参与范围</dt><dd>${platforms.size} 个机器人 · ${targets.length} 个群</dd></div><div><dt>报名资料</dt><dd>${item.questions.length ? `${item.questions.length} 项私聊问题` : "无需填写资料"}</dd></div></dl><div class="participation"><span><strong>${item.complete_count}</strong> 份有效报名${pending ? `<small> · ${pending} 份待填写</small>` : ""}${item.review_pending_count ? `<small> · ${item.review_pending_count} 份待审核</small>` : ""}${item.rejected_count ? `<small> · ${item.rejected_count} 份未通过</small>` : ""}</span><span>${item.status === "drawn" ? `${item.winners.length} 人中奖` : `抽取 ${item.winner_count} 位`}</span></div>${item.pending_deliveries ? `<div class="delivery-hint">${icon("clock")}${item.pending_deliveries} 条通知待发送</div>` : ""}<footer class="card-actions">${button("查看详情", { action: "detail", style: "text", glyph: "arrow" })}${item.status === "open" ? button("修改设置", { action: "edit", style: "tonal", glyph: "edit" }) : ""}</footer></article>`;
}

function guide() {
  const commands = (rows) => rows.map(([command, help]) => `<div class="guide-command"><code>${esc(command)}</code><span>${help}</span></div>`).join("");
  return `<div class="subpage guide-page"><section class="page-intro"><div><span class="eyebrow">报名、资料、开奖</span><h2>参加抽奖，<br/>从群里报名开始</h2><p>从群聊报名，在私聊填写<br/>到约定的时间，把好运送回群里</p></div><img src="${esc(catURL)}" alt="猫猫抱着抽奖券"/></section><div class="guide-page-grid"><section class="page-panel"><span class="guide-category">参与者 / 群聊</span><h2>在群里报名</h2><p class="muted">在本场活动允许的平台与群内操作，每个 QQ 号只计一次</p>${commands([["/抽奖", "查看图片帮助"], ["/抽奖 列表", "浏览可参加的活动"], ["/抽奖 详情 编号", "查看封面、分级奖品与时间"], ["/抽奖 参与 编号", "为实际发送者自己报名"], ["/抽奖 状态 编号", "查看自己的报名、审核、中奖结果与估算中奖率"], ["/抽奖 退出 编号", "报名截止前退出，已中奖者不能退出"]])}</section><section class="page-panel"><span class="guide-category blue">参与者 / 私聊</span><h2>私聊填写资料</h2><p class="muted">群内报名后会私聊发题，直接按题回答；未添加好友时会提醒</p>${commands([["/抽奖 继续", "开启新一轮 30 分钟作答，保留答案并继续未完成的题目"], ["/抽奖 上一题", "返回上一道已回答题，重新发送可替换答案"], ["/抽奖 下一题", "查看已回答题的下一题，不能跳过未回答题"], ["/抽奖 切换 编号", "有多场待办时切换活动"], ["/抽奖 回答 我喜欢猫猫", "示例文字换成自己的答案，也可附图，等 5 秒自动提交"], ["/抽奖 待办", "查看我这里的待填写活动"], ["/抽奖 取消回答", "暂停填写，保留已提交的资料"]])}<div class="notice blue">最多 20 题，每轮作答模式持续 30 分钟，到期退出并提醒<br/>直接发送答案，首条开启 5 秒收集窗口，可分条补充文字和图片<br/>窗口结束自动提交，无需发送开始、确认或提交指令<br/>文字按顺序分段保存，最多 1000 字；图片分别保留，最多 9 张，每张最大 8 MB<br/>重复内容去重，我自动保存并发出下一题后，再回答下一题<br/>下一题会展示上次答案，可返回修改<br/>引用题目时忽略题目内容；引用自己的图片时保存图片，忽略引用文字<br/>命令示例：/抽奖 回答 我喜欢猫猫，请将示例答案换成你的答案<br/>只发图片也可使用 /抽奖 回答 或 /抽奖 并附图<br/>只剩一场待填写时，回答指令会直接恢复作答，无需先发送继续<br/>在原群再次报名会从第一题重新作答，已成功参与者保留资格<br/>必须当场答对：答错重答，全部完成即成功参与<br/>提交后审核：直接下一题，整份资料审核通过才有开奖资格<br/>审核未通过只私聊通知，截止前回原报名群发送 /抽奖 参与 编号 重新填写</div></section><section class="page-panel full-width"><span class="guide-category">管理员 / 工作台</span><h2>管理员操作流程</h2><ol class="guide-steps"><li><strong>准备奖项与图片</strong><p>活动设置独立封面，每个奖项分别填写名称、奖品和名额，可上传一张奖品图片<br/>支持一等、二等、三等奖，也可以自定义；最多 10 个奖项，合计最多 100 个名额</p></li><li><strong>选择平台、群与时间</strong><p>每场活动单独选择机器人与群，设置报名截止与自动开奖时间<br/>不同群共享同一场参与名单，通知发往该场配置的所有群</p></li><li><strong>发布公告，审核资料</strong><p>创建后在独立详情页发布群公告，图片与参与指令文本分别发送<br/>报名审核支持搜索、筛选、分页与明确选择后的批量标记；未填完整和尚未回答者也可人工通过或不通过<br/>打开单人资料弹窗查看、编辑或补填任意题目，统一审核整份，底部操作始终可见；请在自动开奖前完成<br/>文字匹配不会覆盖已完成的整体审核，图文资料需人工审核<br/>人工通过后直接获得资格，当前私聊作答会结束；修改资料后撤销原人工审核<br/>可编辑、补填、删除答案或批量删除已有资料；删除前选择是否私聊通知用户<br/>用户重新加好友后会恢复待办，也可重新通知全部未填写用户</p></li><li><strong>按奖项开奖</strong><p>可在活动详情提前抽出某个奖项，其余奖项继续按原时间开奖<br/>只抽取成功参与者，每个 QQ 最多中奖一次；名额不足优先分配靠前的未开奖奖项，空缺保留<br/>无人符合资格时保存空结果并通知所有群，已开奖奖项和中奖者资格锁定</p></li></ol></section><section class="page-panel full-width"><h2>管理员指令与 LLM 工具</h2><p class="muted">QQ 快速创建适用于单一奖品、不收集资料的活动，分奖项与上传图片请使用工作台；LLM 工具也可按明确的奖项序号提前开奖</p>${commands([["/抽奖 创建 标题 | 奖品 | 人数 | 截止时间 | 开奖时间", "时间例如 2026-10-01 20:00，默认北京时间 UTC+8"], ["/抽奖 发布 编号", "将公告发送到全部配置群"], ["/抽奖 截止 编号 确认", "停止报名与填写，保留自动开奖时间"], ["/抽奖 开奖 编号 确认", "立即抽出全部剩余奖项"], ["/抽奖 取消 编号 确认", "仅取消尚无奖项开奖的活动"]])}<p class="field-hint">9 个 LLM 工具：查看列表、详情、参与、状态、退出、填写、快速创建、活动管理与通知设置<br/>工具仅操作实际发送者的报名，管理操作校验权限与明确确认<br/>在插件设置统一开启或关闭，关闭后 QQ 指令、自动开奖与公告计划照常运行</p></section></div></div>`;
}

function settings() {
  state.settingsDraft ??= { ...copy(state.settings), managerInput: "" };
  const draft = state.settingsDraft;
  return `<div class="subpage settings-page"><section class="page-intro compact"><div><span class="eyebrow">工作台 / 插件设置</span><h2>你的抽奖小屋，你来照顾</h2><p>管理 QQ 管理员与 LLM 工具调用<br/>每场活动的奖品、群与答题模式在各自活动中设置</p></div><div class="intro-stamp">${icon("settings")}<span>MEOW HOUSE</span></div></section><div class="settings-page-grid"><section class="page-panel"><div class="panel-heading"><div><span class="guide-category">智能助手</span><h2>LLM 工具调用</h2></div>${toggle("llm_tools_enabled", draft.llm_tools_enabled ?? true, "LLM 工具调用")}</div><p class="muted">统一开启或关闭本插件的 9 个工具，默认开启<br/>关闭后，QQ 指令和自动开奖照常运行</p><div class="settings-note">${icon("file")}<p>工具会校验真实 QQ 身份、管理员权限与活动范围<br/>资格审核与奖品上传在工作台管理<br/>工具支持单个奖项提前开奖、群聊通知开关与公告计划</p></div></section><section class="page-panel avatar-cache-panel"><span class="guide-category">报名头像</span><h2>QQ 头像缓存</h2><p class="muted">按真实 QQ 号获取头像，加载失败时显示昵称首字<br/>过期头像定时清理，缓存最多保留 1000 个头像</p><label class="field"><span>缓存有效期 · 小时</span><meow-stepper name="avatar_cache_hours" value="${draft.avatar_cache_hours ?? 24}" min="1" max="168" label="头像缓存小时数"></meow-stepper></label><p class="field-hint">支持 1–168 小时，修改后点击保存</p>${button("清空头像缓存", {action:"clear-avatars",style:"text",glyph:"refresh"})}<p class="avatar-feedback field-hint" role="status"></p></section><section class="page-panel manager-panel"><span class="guide-category blue">管理权限</span><h2>抽奖管理员</h2><p class="muted">AstrBot 全局管理员始终具有管理权限<br/>这里添加的 QQ 号也可使用指令与已开启的工具管理抽奖</p><div id="manager-list">${draft.manager_ids.length ? draft.manager_ids.map(id => `<div class="list-row"><div class="avatar">${icon("paw")}</div><div><strong>QQ ${esc(id)}</strong><small>抽奖管理员</small></div><button class="icon-btn" data-remove-manager="${esc(id)}" aria-label="移除 QQ ${esc(id)}">${icon("close")}</button></div>`).join("") : '<div class="small-empty">尚未添加额外管理员，仅 AstrBot 全局管理员可以管理</div>'}</div><div class="inline-entry">${field("添加管理员 QQ", "manager", draft.managerInput, { placeholder: "输入 QQ 号", maxlength: 20 })}${button("添加", { action: "add-manager", glyph: "plus" })}</div><p class="field-hint">群成员只能管理自己的报名，不能替其他人报名或审核</p></section></div><div class="settings-save"><div><p class="settings-feedback" role="status" aria-live="polite"></p><p class="form-error" role="alert"></p><span class="field-hint">修改后点击保存，设置会在重启后保留</span></div>${button("保存设置", { action: "save-settings", glyph: "check", style: "primary" })}</div></div>`;
}

function editor(existing = null) {
  const frozen = !!existing?.entry_count || !!existing?.tier_draws?.length;
  const draft = existing ? copy(existing) : { title: "", prize_tiers: [{ name: "一等奖", prize: "", count: 1, image: "" }], cover: "", description: "", close_at: Date.now() / 1000 + 86400, draw_at: Date.now() / 1000 + 90000, targets: [], questions: [], require_correct: false };
  draft.prize_tiers ??= [{ name: "幸运奖", prize: draft.prize, count: draft.winner_count, image: "" }];
  draft.require_correct ??= true;
  draft.group_success_notify ??= true;
  draft.group_pending_notify ??= true;
  draft.announcement_schedule ??= { mode: "off", start_at: null, interval_minutes: 60 };
  const dialog = pageForm(existing ? "修改抽奖" : "创建抽奖", existing ? `抽奖编号 ${existing.id} · 设置按活动独立保存` : "填写奖品、参与群和时间，保存后即可发布公告", `<div class="editor-sections"><section class="editor-section"><div class="editor-section-title"><span class="section-number">01</span><div><h3>奖品与封面</h3><p>封面属于本场活动，每个奖项有自己的奖品、图片与名额</p></div></div>${field("抽奖标题", "title", draft.title, { placeholder: "例如：午后的小小好运", maxlength: 80 })}${artwork("cover", draft.cover, "活动封面（可选）", { cover: true })}<div class="prizes-heading"><h4>奖项与奖品</h4><span class="prize-total" aria-live="polite"></span></div><div id="prize-tier-list"></div>${!frozen ? button("添加奖项", { action: "add-tier", glyph: "plus", style: "text" }) : ""}<p class="field-hint">从上到下为开奖顺序，人数不足时优先满足靠前的未开奖奖项<br/>每个 QQ 号最多中奖一次，最多 10 个奖项，名额合计不超过 100</p>${field("活动说明", "description", draft.description, { multiline: true, placeholder: "补充领取方式或参与说明（可选）", maxlength: 1500 })}</section>
    <section class="editor-section"><div class="editor-section-title"><span class="section-number">02</span><div><h3>报名与开奖时间</h3><p>全部时间采用北京时间（UTC+8），不受浏览器时区影响</p></div></div><div class="form-grid"><div class="field"><span class="field-label">报名截止时间</span><meow-calendar label="报名截止时间，北京时间" name="close_at" value="${chinaDate(draft.close_at)}"></meow-calendar><span class="field-hint">截止后停止报名与私聊资料填写</span></div><div class="field"><span class="field-label">自动开奖时间</span><meow-calendar label="自动开奖时间，北京时间" name="draw_at" value="${chinaDate(draft.draw_at)}"></meow-calendar><span class="field-hint">可以等于或晚于报名截止时间</span></div></div></section>
    <section class="editor-section"><div class="editor-section-title"><span class="section-number">03</span><div><h3>参与平台与群</h3><p>列表中的机器人平台和群，才可以参与本次抽奖，开奖发往所有这些群</p></div></div>${frozen ? '<div class="notice">已有报名或奖项开奖，平台、群、问题、答题模式、奖项顺序、奖品、图片与名额已锁定</div>' : '<div class="notice blue">平台自动读取自 AstrBot，可添加多个平台，每个平台再选择自己的群；不同抽奖互不影响</div>'}<div id="target-list"></div>${!frozen ? button("添加平台与群", { action: "add-target", glyph: "plus", style: "text" }) : ""}<p class="field-hint">同一 QQ 号跨平台、跨群只计一次，私聊资料必须发给报名时的那个机器人</p><section class="answer-mode"><div><h4>群聊参与成功通知</h4><p>成功参与时向报名群发送确认图片，默认开启<br/>私聊确认始终发送，开奖公告照常发送<br/>每条通知自动显示发送时的成功参与与待审核人数</p></div>${toggle("group_success_notify", draft.group_success_notify, "群聊参与成功通知")}</section><section class="answer-mode"><div><h4>群聊待审核通知</h4><p>提交资料或审核标记变为待审核时，向报名群发送提醒，默认开启<br/>关闭仅影响群聊，私聊仍确认资料已提交<br/>审核未通过仅私聊提醒，截止前引导回原群重新填写</p></div>${toggle("group_pending_notify", draft.group_pending_notify, "群聊待审核通知")}</section></section>
    <section class="editor-section notification-plan"><div class="editor-section-title"><span class="section-number">04</span><div><h3>群公告计划</h3><p>群内抽奖公告按本场配置的群发送，图片与参与指令文本分别投递</p></div></div><label class="field"><span class="field-label">群公告通知计划</span>${choice("announcement_mode", draft.announcement_schedule.mode, [{value:"off",label:"关闭自动通知"},{value:"once",label:"定时发送一次"},{value:"repeat",label:"循环发送"}], {label:"群公告通知计划"})}</label><div class="form-grid schedule-fields" ${draft.announcement_schedule.mode === "off" ? "hidden" : ""}><div class="field"><span class="field-label">首次通知时间 · 北京时间</span><meow-calendar label="首次通知时间，北京时间" name="announcement_start_at" value="${chinaDate(draft.announcement_schedule.start_at || Math.min(draft.close_at - 60, Date.now() / 1000 + 3600))}"></meow-calendar><span class="field-hint">必须早于报名截止，修改已有计划时选择未来时间</span></div><div class="field schedule-interval" ${draft.announcement_schedule.mode !== "repeat" ? "hidden" : ""}><span class="field-label">循环间隔 · 分钟</span><meow-stepper name="announcement_interval_minutes" value="${draft.announcement_schedule.interval_minutes || 60}" min="1" max="10080" label="循环通知间隔分钟数"></meow-stepper><span class="field-hint">每次按最新成功参与与待审核人数发送</span></div></div><p class="field-hint">报名截止、活动取消或全部开奖后自动停发<br/>重启后最多补发一轮，未送达公告不会逐轮积压<br/>关闭自动通知后仍可手动发布群公告</p></section>
    <section class="editor-section"><div class="editor-section-title"><span class="section-number">05</span><div><h3>私聊报名问题</h3><p>最多 20 项，可混合添加答题、文本、图片与图文资料；按顺序逐题填写</p></div></div><section class="answer-mode"><div><h4>必须当场答对</h4><p class="answer-mode-text">${draft.require_correct ? "开启：答题当场核对，答错需重答；全部资料完成即成功参与" : "关闭：答案提交即进入下一题，不当场判断；全部资料提交后由管理员审核整份资料，通过后才有开奖资格"}</p></div>${toggle("require_correct", draft.require_correct, "必须当场答对", { disabled: frozen })}</section><div id="question-list"></div>${!frozen ? button("添加一个问题", { action: "add-question", glyph: "plus", style: "text" }) : ""}<p class="field-hint">不添加问题时，群聊报名直接成功；开启当场答对时，答题必须设置文字正确答案；关闭时可留空供人工审核；选择题参考答案填写完整选项文本，每行一个；仅管理员可见</p></section></div><p class="form-error" role="alert"></p>`, button("先返回", { action: "dismiss", style: "text" }) + button(existing ? "保存修改" : "创建抽奖", { action: "save-lottery", glyph: "check", style: "primary" }), { wide: true });

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
    dialog.holder.querySelector("#question-list").innerHTML = draft.questions.length ? draft.questions.map((question, index) => `<div class="question-card" data-question="${index}"><div class="question-header"><span class="question-number">第 ${index + 1} 项</span>${choice("kind", question.kind, [{ value: "quiz", label: "答题 · 选择或简答" }, { value: "text", label: "文本资料 · 可附图片" }, { value: "image", label: "图片资料 · 可附文字" }, { value: "mixed", label: "图文资料 · 文字图片都必填" }], { disabled: frozen, label: `第 ${index + 1} 题类型` })}${!frozen ? `<button class="icon-btn" data-action="move-question" aria-label="向上移动第 ${index + 1} 项" ${index === 0 ? "disabled" : ""}>↑</button><button class="icon-btn" data-action="remove-question" aria-label="删除第 ${index + 1} 项">${icon("trash")}</button>` : ""}</div>${field("问题内容", "prompt", question.prompt, { placeholder: "清晰地告诉参与者需要回答或提供什么", maxlength: 300, disabled: frozen })}${question.kind === "quiz" ? `<div class="form-grid">${field("选项（可选）", "options", question.options.join("\n"), { multiline: true, placeholder: "每行一个选项，最多 8 个\n留空则为简答题", disabled: frozen })}${field("文字正确答案（后审模式可留空）", "answers", question.answers.join("\n"), { multiline: true, placeholder: "每行一个答案，最多 20 个\n选择题请填完整的正确选项文本", disabled: frozen })}</div>` : `<p class="field-hint">${question.kind === "image" ? "5 秒收集窗口内发送 1–9 张图片，每张最大 8 MB，可附带合计 1000 字以内文字" : question.kind === "mixed" ? "5 秒收集窗口内发送 1–1000 字文字和 1–9 张图片，每张最大 8 MB" : "5 秒收集窗口内发送合计 1–1000 字文本资料，可附带最多 9 张图片"}</p>`}</div>`).join("") : '<div class="small-empty"><strong>无需填写资料</strong><span>没有私聊问题，群聊发送参与指令即可成功</span></div>';
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
    if (event.target.matches('meow-choice[name="announcement_mode"]')) {
      dialog.holder.querySelector(".schedule-fields").hidden = event.target.value === "off";
      dialog.holder.querySelector(".schedule-interval").hidden = event.target.value !== "repeat";
    }
    if (event.target.matches('meow-switch[name="require_correct"]')) {
      draft.require_correct = event.target.value;
      dialog.holder.querySelector(".answer-mode-text").textContent = draft.require_correct ? "开启：答题当场核对，答错需重答；全部资料完成即成功参与" : "关闭：答案提交即进入下一题，不当场判断；全部资料提交后由管理员审核整份资料，通过后才有开奖资格";
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
    if (["add-tier", "remove-tier", "move-tier", "add-question", "remove-question", "move-question", "add-target", "remove-target"].includes(action)) state.editorDirty = true;
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
    if (action === "dismiss" || action === "form-back") { navigate(existing ? "detail" : "home", existing?.id || ""); return; }
    if (action === "save-lottery") {
      if (state.editorSaving) return;
      if ([...dialog.holder.querySelectorAll("meow-upload")].some(element => element.busy)) { toast("请等待图片上传完成", "error"); return; }
      readTiers();
      readQuestions();
      for (const name of ["title", "description"]) draft[name] = dialog.holder.querySelector(`[name="${name}"]`).value.trim();
      draft.cover = dialog.holder.querySelector('meow-upload[name="cover"]').value;
      delete draft.prize; delete draft.winner_count;
      draft.group_success_notify = dialog.holder.querySelector('meow-switch[name="group_success_notify"]').value;
      draft.group_pending_notify = dialog.holder.querySelector('meow-switch[name="group_pending_notify"]').value;
      const scheduleMode = dialog.holder.querySelector('meow-choice[name="announcement_mode"]').value;
      const scheduleDate = dialog.holder.querySelector('[name="announcement_start_at"]').value;
      const announcementSchedule = { mode: scheduleMode, start_at: scheduleMode === "off" ? null : draft.announcement_schedule.start_at && scheduleDate === chinaDate(draft.announcement_schedule.start_at) ? draft.announcement_schedule.start_at : `${scheduleDate.replace(" ", "T")}:00+08:00`, interval_minutes: scheduleMode === "repeat" ? dialog.holder.querySelector('[name="announcement_interval_minutes"]').value : 0 };
      draft.require_correct = dialog.holder.querySelector('meow-switch[name="require_correct"]').value;
      const payload = { ...draft, announcement_schedule: announcementSchedule };
      for (const name of ["close_at", "draw_at"]) {
        const value = dialog.holder.querySelector(`[name="${name}"]`).value;
        payload[name] = value === chinaDate(draft[name]) ? draft[name] : `${value.replace(" ", "T")}:00+08:00`;
      }
      const save = event.target.closest("button");
      save.disabled = true; state.editorSaving = true;
      const errorField = dialog.holder.querySelector(".form-error");
      errorField.textContent = "";
      try {
        const item = await bridge.apiPost("lotteries", payload);
        state.editorDirty = false; dialog.close(); toast(existing ? "修改已保存" : "抽奖已创建，接下来发布到群吧");
        await loadState({quiet:true}); await navigate("detail", item.id, {force:true});
      } catch (error) { errorField.textContent = cleanMessage(error.message) || "保存失败，请检查填写内容"; errorField.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
      finally { state.editorSaving = false; if (save.isConnected) save.disabled = false; }
    }
  });
}

async function navigate(view, id = "", {force = false, replace = false} = {}) {
  if (!force && state.editorSaving) { toast("正在保存，请稍候", "error"); return; }
  if (!force && state.editorDirty && ["create", "edit"].includes(state.view)) {
    const confirm = modal("离开当前编辑？", "修改尚未保存", '<p class="confirm-copy">返回后将丢弃尚未保存的表单内容，已保存活动不会受到影响</p>', button("继续编辑", {action:"dismiss",style:"text"}) + button("放弃修改并返回", {action:"discard-edit"}));
    confirm.holder.querySelector('[data-action="discard-edit"]').addEventListener("click", () => { confirm.close(); navigate(view, id, {force:true,replace}); });
    return;
  }
  const overlay = document.querySelector(".modal-backdrop");
  if (!force && overlay?.querySelector(".modal-close")?.disabled) { history.replaceState(null, "", `#${state.view}${state.activityId ? "/" + state.activityId : ""}`); toast("正在保存，请稍候", "error"); return; }
  if (!force) overlay?.querySelector(".modal-close")?.click();
  clearTimeout(activity?.searchTimer);
  state.view = view; state.activityId = id; state.editorDirty = false;
  activity = null;
  const route = `#${view}${id ? "/" + id : ""}`;
  history[replace ? "replaceState" : "pushState"](null, "", route);
  render();
  if (view === "detail") {
    activity = new ActivityView(app.querySelector(".activity-outlet"), bridge, navigate, () => loadState({quiet:true}), id);
    await activity.open();
  } else if (view === "create") editor();
  else if (view === "edit") {
    try {
      const result = await bridge.apiGet(`lotteries/${id}`);
      if (state.view !== view || state.activityId !== id) return;
      const item = {...result.item, entry_count:result.summary.total};
      if (item.status !== "open") throw Error("本场活动已结束，不能再编辑");
      editor(item);
    } catch (error) { if (state.view !== view || state.activityId !== id) return; app.querySelector(".activity-outlet").innerHTML = `<div class="page-error"><h2>无法编辑活动</h2><p>${esc(cleanMessage(error.message))}</p>${button("返回抽奖小屋", {action:"home",style:"text"})}</div>`; }
  }
  window.scrollTo?.({top:0,behavior:"instant"});
}

app.addEventListener("click", async event => {
  const filter = event.target.closest("[data-filter]");
  if (filter) { state.filter = filter.dataset.filter; render(); return; }
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (action === "refresh") { if (activity) await activity.open(); else if (!["create", "edit"].includes(state.view)) await loadState(); }
  if (action === "platforms" && !state.loadingPlatforms) await loadPlatforms();
  if (["home", "settings", "guide"].includes(action)) {
    event.preventDefault(); await navigate(action);
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
  if (action === "clear-avatars") {
    let clearing = false;
    const dialog = modal("清空头像缓存？", "已保存的报名和资料不受影响", '<p>下次查看报名时重新获取头像</p><p class="form-error" role="alert"></p>', button("返回", {action:"dismiss",style:"text"}) + button("确认清空", {action:"confirm-avatar-clear"}), {canClose:() => !clearing});
    dialog.holder.querySelector('[data-action="confirm-avatar-clear"]').addEventListener("click", async () => {
      if (clearing) return;
      clearing = true;
      const controls = [...dialog.holder.querySelectorAll("button")]; controls.forEach(element => element.disabled = true);
      try { await bridge.apiPost("avatars/clear", {confirmed:true}); clearAvatarCache(); clearing = false; dialog.close(); toast("头像缓存已清空"); }
      catch (error) { dialog.holder.querySelector(".form-error").textContent = cleanMessage(error.message); }
      finally { clearing = false; controls.forEach(element => { if (element.isConnected) element.disabled = false; }); }
    });
  }
  if (action === "save-settings") {
    if (state.settingsSaving) return;
    state.settingsSaving = true;
    const save = event.target.closest("button"); save.disabled = true;
    const draft = copy(state.settingsDraft);
    app.querySelector(".form-error").textContent = "";
    try {
      await bridge.apiPost("settings", { manager_ids: draft.manager_ids, llm_tools_enabled: draft.llm_tools_enabled, avatar_cache_hours: draft.avatar_cache_hours ?? 24 });
      if (state.settings.avatar_cache_hours !== draft.avatar_cache_hours) clearAvatarCache();
      state.settings = { manager_ids: [...draft.manager_ids], llm_tools_enabled: draft.llm_tools_enabled, avatar_cache_hours: draft.avatar_cache_hours ?? 24 };
      toast("插件设置已保存");
      if (state.view === "settings") app.querySelector(".settings-feedback").textContent = "已保存，设置现已生效";
    } catch (error) { if (state.view === "settings") app.querySelector(".form-error").textContent = cleanMessage(error.message); }
    finally { state.settingsSaving = false; if (save.isConnected) save.disabled = false; }
  }
  if (action === "create") await navigate("create");
  if (["detail", "edit"].includes(action)) {
    const id = event.target.closest("[data-id]").dataset.id;
    await navigate(action === "detail" ? "detail" : "edit", id);
  }
});

app.addEventListener("input", event => {
  if (["create", "edit"].includes(state.view)) state.editorDirty = true;
  if (state.view === "settings" && event.target.matches('[name="manager"]')) state.settingsDraft.managerInput = event.target.value;
});
app.addEventListener("change", event => {
  if (["create", "edit"].includes(state.view)) state.editorDirty = true;
  if (state.view === "settings" && event.target.matches('meow-switch[name="llm_tools_enabled"]')) state.settingsDraft.llm_tools_enabled = event.target.value;
  if (state.view === "settings" && event.target.matches('meow-stepper[name="avatar_cache_hours"]')) state.settingsDraft.avatar_cache_hours = Number(event.target.value);
});
window.addEventListener("hashchange", () => {
  const [view, id = ""] = location.hash.slice(1).split("/");
  if (!["home", "settings", "guide", "create", "detail", "edit"].includes(view)) return;
  if (state.editorDirty || state.editorSaving) history.replaceState(null, "", `#${state.view}${state.activityId ? "/" + state.activityId : ""}`);
  navigate(view, id, {replace:true});
});

if (!bridge) {
  app.setAttribute("aria-busy", "false");
  app.innerHTML = `<div class="boot"><img src="${esc(catURL)}" alt=""/><h1>请从 AstrBot 打开抽奖小屋</h1><p>登录 AstrBot → 插件 → 喵喵抽奖 → 管理工作台</p><p>管理页需要 AstrBot 的登录状态，请从插件详情打开</p></div>`;
} else {
  try {
    await bridge.ready();
    await loadState();
    await loadPlatforms();
    await navigate(state.view, state.activityId, {force:true,replace:true});
    const timer = setInterval(() => { if (!document.hidden) loadState({ quiet: true }); }, 20000);
    window.addEventListener("beforeunload", event => {
      const draft = state.settingsDraft;
      const settingsDirty = draft && (JSON.stringify(draft.manager_ids) !== JSON.stringify(state.settings.manager_ids) || draft.llm_tools_enabled !== state.settings.llm_tools_enabled || (draft.avatar_cache_hours ?? 24) !== (state.settings.avatar_cache_hours ?? 24));
      if (state.editorDirty || state.editorSaving || state.settingsSaving || settingsDirty) { event.preventDefault(); event.returnValue = ""; }
    });
    window.addEventListener("pagehide", () => clearInterval(timer));
  } catch (error) {
    app.setAttribute("aria-busy", "false");
    app.innerHTML = `<div class="boot"><img src="${esc(catURL)}" alt=""/><h1>工作台暂时没有连上</h1><p>${esc(cleanMessage(error.message) || "请在 AstrBot 中重新打开此页面")}</p></div>`;
  }
}
