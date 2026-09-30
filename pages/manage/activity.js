import { escapeHTML as esc, icon, button, choice, avatar, pagination, loadArtwork, chinaDate, modal, toast, field, artwork, toggle } from "./components.js";

const labels = { approved: "参与成功", pending: "待审核", rejected: "未通过", incomplete: "待填写" };
const statusOf = entry => entry.status === "complete" ? (entry.review_status || "approved") : "incomplete";
const clean = value => String(value || "请求失败，请重试").replace(/[。.]+\s*$/, "");

export class ActivityView {
  constructor(root, bridge, navigate, updated, id) {
    this.root = root; this.bridge = bridge; this.navigate = navigate; this.updated = updated; this.id = id;
    this.tab = "overview"; this.filter = "all"; this.query = ""; this.page = 1; this.size = 20;
    this.selected = new Set(); this.selection = new Map(); this.deliveryPage = 1; this.sequence = 0; this.busy = false;
    root.addEventListener("click", event => this.click(event));
    root.addEventListener("input", event => {
      if (!event.target.matches('[name="participant-search"]')) return;
      this.query = event.target.value; this.page = 1; this.selected.clear();
      clearTimeout(this.searchTimer);
      this.searchTimer = setTimeout(() => this.loadEntries(), 250);
    });
    root.addEventListener("change", event => {
      if (event.target.matches('meow-choice[name="page_size"]')) { this.size = Number(event.target.value); this.page = 1; this.loadEntries(); }
    });
  }

  async open() {
    const sequence = ++this.sequence;
    this.root.innerHTML = '<div class="page-loading" role="status">正在读取活动详情</div>';
    try {
      const data = await this.bridge.apiGet(`lotteries/${this.id}`);
      if (!this.root.isConnected || sequence !== this.sequence) return;
      this.data = data; this.item = data.item; this.summary = data.summary;
      this.readonly = this.item.status !== "open" || data.server_time >= this.item.draw_at;
      this.render();
    } catch (error) {
      if (this.root.isConnected && sequence === this.sequence) this.root.innerHTML = `<div class="page-error"><h2>暂时无法打开活动</h2><p>${esc(clean(error.message))}</p>${button("返回抽奖小屋", { action: "activity-back", style: "text" })}${button("重新读取", { action: "activity-refresh" })}</div>`;
    }
  }

  render() {
    const item = this.item, summary = this.summary;
    const phase = item.status === "open" && this.data.server_time >= item.close_at ? "closed" : item.status;
    const phaseText = { open: "报名中", closed: "待开奖", drawn: "已开奖", cancelled: "已取消" }[phase];
    this.root.innerHTML = `<article class="activity-page"><div class="page-breadcrumb">${button("返回抽奖小屋", { action: "activity-back", style: "text", glyph: "arrow" })}<span>活动详情 / ${esc(this.id)}</span></div><header class="activity-header"><div><span class="phase-badge phase-${phase}">${phaseText}</span><h2>${esc(item.title)}</h2><p>编号 <code>${esc(this.id)}</code> · 北京时间 UTC+8</p></div><div class="activity-header-actions">${item.status === "open" ? button("修改设置", { action: "activity-edit", glyph: "edit" }) : ""}${button("刷新详情", { action: "activity-refresh", style: "text", glyph: "refresh" })}</div></header><div class="activity-metrics">${[["全部报名", summary.total, "all"], ["成功参与", summary.approved, "approved"], ["等待审核", summary.pending, "pending"], ["资料未齐", summary.incomplete, "incomplete"]].map(([name, count, filter]) => `<button class="summary-metric" data-summary="${filter}"><span>${name}</span><strong>${count}</strong>${icon("arrow")}</button>`).join("")}</div><nav class="activity-tabs" aria-label="活动管理分区">${[["overview", "活动与奖项"], ["participants", "报名审核"], ["deliveries", "通知记录"]].map(([tab, label]) => `<button data-tab="${tab}" class="${this.tab === tab ? "selected" : ""}" aria-current="${this.tab === tab ? "page" : "false"}">${label}${tab === "participants" && summary.pending ? `<span>${summary.pending}</span>` : ""}</button>`).join("")}</nav><div class="activity-content"></div></article>`;
    this.paintTab();
  }

  paintTab() {
    const item = this.item, outlet = this.root.querySelector(".activity-content");
    if (this.tab === "overview") {
      const scope = item.targets.map(target => `<span>QQ ${esc(target.bot_id)} · 群 ${esc(target.group_id)}</span>`).join("");
      const actions = item.status === "open" ? `${button("发布群公告", { action: "publish", glyph: "send", style: "primary", attrs: this.data.server_time >= item.close_at ? "disabled" : "" })}${button("截止报名", { action: "close", style: "text", attrs: this.data.server_time >= item.close_at ? "disabled" : "" })}${button("抽取全部剩余奖项", { action: "draw", style: "tonal" })}${button("取消活动", { action: "cancel", style: "text", attrs: item.tier_draws.length ? "disabled" : "" })}` : button("删除活动与资料", { action: "delete", style: "text", glyph: "trash" });
      outlet.innerHTML = `<div class="overview-grid"><section class="page-panel activity-facts">${item.cover ? `<div class="detail-cover"><img data-artwork="${esc(item.cover)}" alt="活动封面"/></div>` : ""}<h3>时间与参与范围</h3><dl><div><dt>报名截止</dt><dd>${chinaDate(item.close_at)}</dd></div><div><dt>自动开奖</dt><dd>${chinaDate(item.draw_at)}</dd></div><div><dt>报名资料</dt><dd>${item.questions.length ? `${item.questions.length} 题 · ${item.require_correct ? "必须当场答对" : "提交后审核"}` : "无需填写"}</dd></div><div><dt>群聊成功通知</dt><dd>${item.group_success_notify === false ? "已关闭" : "已开启"}</dd></div><div><dt>群聊待审核通知</dt><dd>${item.group_pending_notify === false ? "已关闭" : "已开启"}</dd></div><div><dt>群公告计划</dt><dd>${({off:"已关闭",once:"定时一次",repeat:"循环发送"})[item.announcement_schedule?.mode || "off"]}</dd></div>${item.announcement_schedule?.mode === "repeat" ? `<div><dt>循环间隔</dt><dd>${item.announcement_schedule.interval_minutes} 分钟</dd></div>` : ""}${item.announcement_next_at ? `<div><dt>下次公告</dt><dd>${chinaDate(item.announcement_next_at)}</dd></div>` : ""}${item.announcement_last_at ? `<div><dt>上次公告排队</dt><dd>${chinaDate(item.announcement_last_at)}</dd></div>` : ""}</dl><div class="scope-list">${scope}</div>${item.description ? `<div class="activity-description"><h3>活动说明</h3><p>${esc(item.description)}</p></div>` : ""}<div class="notice">每个 QQ 号最多中奖一次，仅成功参与者进入候选名单<br/>已中奖者不再参与剩余奖项，空缺保留，不会补抽</div><div class="activity-operations">${actions}</div><p class="page-action-error form-error" role="alert"></p></section><section class="page-panel"><div class="panel-heading"><h3>奖项与开奖</h3><span class="muted">已揭晓 ${item.tier_draws.length || (item.status === "drawn" ? item.prize_tiers.length : 0)} / ${item.prize_tiers.length} 项</span></div>${item.prize_tiers.map((tier, index) => {
        const record = item.tier_draws.find(draw => draw.tier_index === index), drawn = !!record || item.status === "drawn";
        const winners = item.winners.filter(winner => (winner.tier_index ?? 0) === index);
        return `<section class="award-result"><div class="award-result-heading"><div><span class="guide-category">${esc(tier.name)}</span><h4>${esc(tier.prize)}</h4><p>名额 ${tier.count} 位 · ${drawn ? `已中奖 ${winners.length} 位` : "尚未开奖"}</p></div>${!drawn && !this.readonly ? button("提前开奖", { action: "draw-tier", attrs: `data-tier-index="${index}"` }) : `<span class="award-state">${drawn ? "已开奖" : item.status === "cancelled" ? "已取消" : "等待自动开奖"}</span>`}</div>${tier.image ? `<div class="award-picture"><img data-artwork="${esc(tier.image)}" alt="${esc(tier.name)}奖品图片"/></div>` : ""}${winners.map(winner => `<div class="winner-row">${avatar(winner.user_id, winner.nickname)}<div><strong>${esc(winner.nickname)}</strong><small>QQ ${esc(winner.user_id)}</small></div></div>`).join("")}${drawn && winners.length < tier.count ? `<p class="award-vacancy">${winners.length ? `空缺 ${tier.count - winners.length} 位` : "无人符合资格，名额全部空缺"} · 已保存</p>` : ""}${record ? `<div class="draw-audit"><button class="btn btn-text" data-action="toggle-audit" aria-expanded="false">查看开奖记录</button><div hidden><p>开奖 ${chinaDate(record.drawn_at)} · 候选 ${record.eligible_count} 人</p><code>${esc(record.pool_hash)}</code></div></div>` : ""}</section>`;
      }).join("")}</section></div>`;
      loadArtwork(outlet);
    } else if (this.tab === "participants") {
      outlet.innerHTML = `<section class="page-panel participants-panel"><div class="panel-heading"><div><h3>报名与资格审核</h3><p class="muted">按人查找报名，查看和修改答案，审核整份资料</p></div>${!this.readonly && !item.require_correct && item.questions.length ? button("匹配文字答案", { action: "match", glyph: "check" }) : ""}</div>${item.questions.length && !item.require_correct ? `<div class="notice">${this.readonly ? "审核已锁定，仅可查看已保存的资料与标记" : `请在 ${chinaDate(item.draw_at)} 前完成审核；待审核、未通过或资料未齐者不进入开奖名单`}<br/>文字匹配只处理有参考答案且尚未标记的纯文字答题，图片与资料题需人工审核</div>` : ""}${!this.readonly && this.data.server_time < item.close_at && this.summary.incomplete ? `<div class="form-resume-banner"><div><strong>${this.summary.incomplete} 人尚未填写完整</strong><p>重新添加我为好友后可继续，也可由管理员重新发题</p></div>${button("重新通知未填写用户", {action:"restart-forms",glyph:"send"})}</div>` : ""}<div class="participant-toolbar"><div class="search participant-search">${icon("search")}<input name="participant-search" aria-label="搜索报名" placeholder="昵称、QQ 或来源群" maxlength="80" value="${esc(this.query)}"/></div>${choice("page_size", String(this.size), [{value:"10",label:"每页 10 人"},{value:"20",label:"每页 20 人"},{value:"50",label:"每页 50 人"}], {label:"每页人数"})}</div><div class="participant-filters" role="group" aria-label="报名资格筛选">${[["all", "全部", this.summary.total], ...Object.entries(labels).map(([key, text]) => [key, text, this.summary[key]])].map(([key, text, count]) => `<button data-entry-filter="${key}" aria-pressed="${this.filter === key}" class="${this.filter === key ? "selected" : ""}">${text}<span>${count}</span></button>`).join("")}</div><div class="selection-bar"></div><div class="participant-results" aria-live="polite"></div><p class="page-action-error form-error" role="alert"></p></section>`;
      this.loadEntries();
    } else {
      const rows = this.data.deliveries;
      this.deliveryPage = Math.min(this.deliveryPage, Math.max(1, Math.ceil(rows.length / 20)));
      outlet.innerHTML = `<section class="page-panel"><div class="panel-heading"><div><h3>通知发送记录</h3><p class="muted">图片和参与指令文本分别投递，发送失败只重试对应消息</p></div>${!this.readonly && this.data.server_time < item.close_at && this.summary.incomplete ? button("重新通知未填写用户", {action:"restart-forms",glyph:"send"}) : ""}${button("重试未发送通知", { action: "retry", glyph: "refresh" })}</div><div class="notification-table">${rows.slice((this.deliveryPage - 1) * 20, this.deliveryPage * 20).map(row => `<div class="notification-row"><div><strong>${esc(({success:"参与成功",submitted:"资料已提交",review:"整体审核结果",answer_changed:"填写资料更新",result:"开奖结果",tier_result:"奖项提前开奖",announcement:"公告图片",participation_guide:"参与指令文本",question:"私聊题目",cancelled:"活动取消",closed:"报名截止"})[row.kind] || "通知")}</strong><small>${row.target.channel === "group" ? "群" : "私聊 QQ"} ${esc(row.target.recipient)} · ${esc(row.target.platform_id)}</small></div><span class="${row.delivered_at ? "delivered" : "pending"}">${row.delivered_at ? "已发送" : row.attempts ? `待重试 ${row.attempts} 次` : "等待发送"}</span></div>`).join("") || '<div class="small-empty">还没有通知记录</div>'}</div>${pagination(this.deliveryPage, rows.length, 20, "delivery-page")}<p class="muted">最多显示最近 200 条记录</p><p class="page-action-error form-error" role="alert"></p></section>`;
    }
  }

  async loadEntries() {
    const sequence = ++this.sequence;
    const outlet = this.root.querySelector(".participant-results");
    if (!outlet) return;
    outlet.setAttribute("aria-busy", "true");
    try {
      const data = await this.bridge.apiGet(`lotteries/${this.id}/entries`, {page:this.page,page_size:this.size,status:this.filter,q:this.query});
      if (!this.root.isConnected || sequence !== this.sequence || this.tab !== "participants") return;
      this.entries = data.entries; this.page = data.page; this.summary = data.summary;
      this.root.querySelectorAll("[data-entry-filter] span").forEach(element => { element.textContent = this.summary[element.parentElement.dataset.entryFilter === "all" ? "total" : element.parentElement.dataset.entryFilter]; });
      outlet.innerHTML = `<div class="participant-table" role="table" aria-label="报名列表"><div class="participant-table-head" role="row"><span>选择</span><span>参与者</span><span>填写进度</span><span>参与资格</span><span>操作</span></div>${data.entries.map(entry => {
        const status = statusOf(entry), winner = this.item.winners.some(value => value.user_id === entry.user_id);
        const canSelect = !this.readonly && this.item.questions.length && entry.answer_count > 0 && !winner;
        return `<div class="participant-row" role="row" data-user="${esc(entry.user_id)}"><button class="selection-toggle" role="checkbox" aria-label="选择 QQ ${esc(entry.user_id)}" aria-checked="${this.selected.has(entry.user_id)}" data-select="${esc(entry.user_id)}" ${canSelect ? "" : "disabled"}>${this.selected.has(entry.user_id) ? icon("check") : ""}</button><div class="participant-person">${avatar(entry.user_id, entry.nickname)}<div><strong>${esc(entry.nickname)}</strong><small>QQ ${esc(entry.user_id)}</small><small>来源群 ${esc(entry.group_id)}</small></div></div><div class="participant-progress"><strong>${entry.answer_count} / ${this.item.questions.length} 题</strong><small>${this.item.require_correct ? "当场答对" : "整份资料审核"}</small></div><div><span class="entry-status ${status}">${labels[status]}</span>${winner ? '<small class="winner-lock">已中奖 · 资格锁定</small>' : ""}</div>${button(this.readonly || winner ? "查看资料" : "管理资料", {action:"review-person",attrs:`data-user-id="${esc(entry.user_id)}"`,style:"tonal"})}</div>`;
      }).join("") || `<div class="table-empty"><h4>${this.query || this.filter !== "all" ? "没有符合条件的报名" : "还没有人报名"}</h4><p>${this.query || this.filter !== "all" ? "调整筛选或清空搜索后再试" : "先发布群公告，让群成员领取抽奖券"}</p>${this.query || this.filter !== "all" ? button("清空搜索与筛选", {action:"reset-entries",style:"text"}) : ""}</div>`}</div>${pagination(this.page, data.total, this.size, "entry-page")}`;
      this.paintSelection();
    } catch (error) { if (sequence === this.sequence && outlet.isConnected) outlet.innerHTML = `<div class="table-empty"><p>${esc(clean(error.message))}</p>${button("重试读取报名", {action:"reload-entries"})}</div>`; }
    finally { if (outlet.isConnected && sequence === this.sequence) outlet.setAttribute("aria-busy", "false"); }
  }

  paintSelection() {
    const outlet = this.root.querySelector(".selection-bar");
    if (!outlet) return;
    const selectable = (this.entries || []).filter(entry => entry.answer_count > 0 && !this.item.winners.some(winner => winner.user_id === entry.user_id));
    const canReview = !this.item.require_correct && [...this.selected].every(id => this.selection.get(id)?.status === "complete");
    outlet.innerHTML = !this.readonly && this.item.questions.length ? `<div>${button("选择本页有资料报名", {action:"select-page",style:"text",attrs:selectable.length ? "" : "disabled"})}<span>已选 ${this.selected.size} 人 / 最多 100 人</span>${this.selected.size ? button("清空选择", {action:"clear-selection",style:"text"}) : ""}</div><div>${button("批量通过", {action:"bulk-approve",attrs:this.selected.size && canReview ? "" : "disabled"})}${button("批量不通过", {action:"bulk-reject",style:"text",attrs:this.selected.size && canReview ? "" : "disabled"})}${button("批量删除资料", {action:"bulk-delete-entries",style:"text",glyph:"trash",attrs:this.selected.size ? "" : "disabled"})}</div>` : "";
    this.root.querySelectorAll("[data-select]").forEach(element => { element.setAttribute("aria-checked", String(this.selected.has(element.dataset.select))); element.innerHTML = this.selected.has(element.dataset.select) ? icon("check") : ""; });
  }

  async click(event) {
    const target = event.target.closest("button"), action = target?.dataset.action;
    if (!target || target.disabled || this.busy) return;
    if (action === "toggle-audit") { const panel = target.nextElementSibling; panel.hidden = !panel.hidden; target.setAttribute("aria-expanded", String(!panel.hidden)); return; }
    if (action === "activity-back") { this.navigate("home"); return; }
    if (action === "activity-edit") { this.navigate("edit", this.id); return; }
    if (action === "activity-refresh") { await this.open(); return; }
    if (target.dataset.tab || target.dataset.summary) {
      this.tab = target.dataset.tab || "participants";
      if (target.dataset.summary) { this.filter = target.dataset.summary; this.page = 1; this.selected.clear(); }
      this.render(); return;
    }
    if (target.dataset.entryFilter) { this.filter = target.dataset.entryFilter; this.page = 1; this.selected.clear(); this.paintTab(); return; }
    if (target.dataset.select) {
      const id = target.dataset.select;
      if (this.selected.has(id)) this.selected.delete(id);
      else if (this.selected.size < 100) { this.selected.add(id); this.selection.set(id, this.entries.find(entry => entry.user_id === id)); }
      else toast("一次最多选择 100 份报名", "error");
      this.paintSelection(); return;
    }
    if (action === "select-page") { for (const entry of this.entries) if (entry.answer_count > 0 && !this.item.winners.some(winner => winner.user_id === entry.user_id) && this.selected.size < 100) { this.selected.add(entry.user_id); this.selection.set(entry.user_id, entry); } this.paintSelection(); return; }
    if (action === "clear-selection") { this.selected.clear(); this.paintSelection(); return; }
    if (action === "reset-entries") { this.filter = "all"; this.query = ""; this.page = 1; this.selected.clear(); this.paintTab(); return; }
    if (action === "entry-page" || action === "reload-entries") { if (target.dataset.page) this.page = Number(target.dataset.page); await this.loadEntries(); return; }
    if (action === "delivery-page") { this.deliveryPage = Number(target.dataset.page); this.paintTab(); return; }
    if (action === "review-person") { await this.review(target.dataset.userId); return; }
    if (["bulk-approve", "bulk-reject"].includes(action)) {
      const ids = [...this.selected], correct = action === "bulk-approve";
      await this.confirm(`将 ${ids.length} 份报名${correct ? "全部通过" : "标记不通过"}？`, `这会将所选报名的整份资料审核为${correct ? "符合" : "不符合"}要求，并更新开奖资格<br/>仅影响当前明确选择的 ${ids.length} 个 QQ 号，已中奖者与未提交者不能操作`, async () => {
        await this.bridge.apiPost(`lotteries/${this.id}/review`, {action:"bulk",user_ids:ids,correct,revisions:Object.fromEntries(ids.map(id => [id,this.selection.get(id)?.answer_revision || 0]))});
        this.selected.clear(); await this.reloadAfterReview(); toast(`已处理 ${ids.length} 份报名`);
      }); return;
    }
    if (action === "restart-forms") {
      await this.confirm("重新通知全部未填写完的用户？", "只发送到原报名 QQ 的私聊，保留已有答案，从第一道缺失题继续<br/>会切换这些用户的当前答题活动，发送失败可在通知记录中重试", async () => {
        const result = await this.bridge.apiPost(`lotteries/${this.id}/review`, {action:"restart_forms",confirmed:true});
        await this.reloadAfterReview(); toast(`已为 ${result.restarted_users} 人重新排队私聊题目`);
      }); return;
    }
    if (action === "bulk-delete-entries") {
      const ids = [...this.selected];
      await this.confirm(`删除 ${ids.length} 人的填写资料？`, "会移除所选用户的全部答案和图片，保留群报名，取消当前开奖资格<br/>删除不可撤销，报名截止前可重新回答，已中奖者不能删除", async notify => {
        await this.bridge.apiPost(`lotteries/${this.id}/review`, {action:"delete_entries",user_ids:ids,revisions:Object.fromEntries(ids.map(id => [id,this.selection.get(id)?.answer_revision || 0])),confirmed:true,notify});
        this.selected.clear(); await this.reloadAfterReview(); toast("所选用户的填写资料已删除");
      }, true); return;
    }
    if (action === "match") { target.disabled = true; try { const result = await this.bridge.apiPost(`lotteries/${this.id}/review`, {action:"match"}); await this.reloadAfterReview(); toast(`已匹配 ${result.marked_answers} 个未标记文字答案`); } catch (error) { this.showError(error); } finally { if (target.isConnected) target.disabled = false; } return; }
    if (["publish", "close", "draw", "cancel", "delete", "retry", "draw-tier"].includes(action)) {
      const tier = action === "draw-tier" ? this.item.prize_tiers[Number(target.dataset.tierIndex)] : null;
      const messages = {publish:["发布本场群公告？","将横版公告图片和独立的参与指令文本发送到所有配置群"],close:["现在截止报名？","停止群报名与私聊填写，剩余奖项仍按原时间开奖"],draw:["抽取全部剩余奖项？","仅抽取成功参与且未中奖者，审核与报名立即锁定，已保存的奖项不会重复抽取"],cancel:["取消本场活动？","停止报名与审核，并向所有配置群发送取消通知"],delete:["删除活动与私聊资料？","永久移除本场活动、报名资料及通知记录，此操作无法撤销"],retry:["重试未发送通知？","只重新排队未送达的消息，已发送图片不会因为文本失败而重发"],"draw-tier":[`提前揭晓${tier?.name}？`,`仅抽取当前成功参与且未中奖者，人数不足保留空缺<br/>本奖项结果立即锁定，剩余奖项继续按原时间开奖`]};
      await this.confirm(...messages[action], async () => {
        await this.bridge.apiPost(`lotteries/${this.id}/action`, {action:action === "draw-tier" ? "draw_tier" : action, confirmed:true, ...(tier ? {tier_index:Number(target.dataset.tierIndex)} : {})});
        toast(action === "delete" ? "活动与资料已删除" : "操作已保存，通知已排队");
        if (action === "delete") this.navigate("home", "", {force:true}); else await this.open();
        this.updated();
      });
    }
  }

  showError(error) { const outlet = this.root.querySelector(".page-action-error"); if (outlet) outlet.textContent = clean(error.message); else toast(clean(error.message), "error"); }

  async confirm(title, text, execute, notifyOption = false) {
    let busy = false;
    const dialog = modal(title, `${this.item.title} · ${this.id}`, `<p class="confirm-copy">${text}</p>${notifyOption ? `<div class="delete-notify-choice"><div><strong>是否私聊通知所选用户</strong><p>只说明整体作答状态，不包含题号、错误原因或答案</p></div>${toggle("delete_notify",true,"通知用户")}</div>` : ""}<p class="form-error" role="alert"></p>`, button("先返回", {action:"dismiss",style:"text"}) + button("确认操作", {action:"confirm-operation",style:"tonal"}), {canClose:() => !busy});
    dialog.holder.querySelector('[data-action="confirm-operation"]').addEventListener("click", async () => {
      if (busy) return; busy = true;
      const controls = [...dialog.holder.querySelectorAll("button")].map(element => [element, element.disabled]); controls.forEach(([element]) => element.disabled = true);
      try { await execute(dialog.holder.querySelector('meow-switch[name="delete_notify"]')?.value); busy = false; dialog.close(); }
      catch (error) { dialog.holder.querySelector(".form-error").textContent = clean(error.message); }
      finally { busy = false; controls.forEach(([element, disabled]) => { if (element.isConnected) element.disabled = disabled; }); }
    });
  }

  async reloadAfterReview() {
    const data = await this.bridge.apiGet(`lotteries/${this.id}`);
    if (!this.root.isConnected) return;
    this.data = data; this.item = data.item; this.summary = data.summary;
    this.readonly = data.item.status !== "open" || data.server_time >= data.item.draw_at;
    this.render(); this.updated();
  }

  async review(user) {
    if (this.reviewLoading || this.reviewOpen) return;
    this.reviewLoading = true;
    let response;
    try { response = await this.bridge.apiGet(`lotteries/${this.id}/entries/${user}`); }
    catch (error) { this.showError(error); return; }
    finally { this.reviewLoading = false; }
    if (!this.root.isConnected) return;
    let entry = response.entry, item = response.item, questionIndex = 0, mode = "view", busy = false, closed = false;
    this.reviewOpen = true;
    const assets = new Map();
    const dialog = modal("管理填写资料", "逐题查看或编辑答案，整份资料统一审核", "", '<div class="review-actions-footer"></div>', {
      wide:true,
      canClose:() => { if (busy || dialog.holder.querySelector("meow-upload")?.busy) return false; if (mode !== "view") { toast("请先保存或取消当前操作"); return false; } return true; },
      onClose:() => { closed = true; this.reviewOpen = false; assets.clear(); }
    });
    dialog.holder.classList.add("review-dialog");
    const paint = () => {
      const winner = item.winners.some(value => value.user_id === entry.user_id);
      const canChange = item.status === "open" && response.server_time < item.draw_at && !winner;
      const canReview = canChange && !item.require_correct && entry.status === "complete";
      const question = item.questions[questionIndex], slot = entry.answers[questionIndex], answer = slot?.kind === "deleted" ? null : slot;
      const image = answer?.kind === "image" ? answer.value : answer?.image;
      const text = answer?.kind === "image" ? (answer.text || "") : (answer?.value || "");
      let content = `<div class="review-person">${avatar(entry.user_id, entry.nickname)}<div><h3>${esc(entry.nickname)}</h3><p>QQ ${esc(entry.user_id)} · 来源群 ${esc(entry.group_id)}</p></div><span class="entry-status ${statusOf(entry)}">${labels[statusOf(entry)]}</span></div>`;
      if (mode === "view") content += `<div class="review-question-nav" aria-label="查看填写题目">${item.questions.map((value,index) => `<button data-review-question="${index}" class="${index === questionIndex ? "selected" : ""}" aria-label="第 ${index + 1} 题${entry.answers[index] && entry.answers[index].kind !== "deleted" ? "已填写" : "未填写"}">${index + 1}</button>`).join("")}</div>`;
      if (question) {
        content += `<div class="review-answer-layout"><section><span class="guide-category">第 ${questionIndex + 1} / ${item.questions.length} 题</span><h3 class="review-prompt">${esc(question.prompt)}</h3>${question.options?.length ? `<div class="review-options">${question.options.map((value,index) => `<p>${String.fromCharCode(65+index)} · ${esc(value)}</p>`).join("")}</div>` : ""}`;
        content += mode === "edit" ? field("答案文字", "answer_text", text, {multiline:true,hint:question.kind === "image" ? "文字可留空，图片必填" : "保留空格与换行，最多 1000 字"}) : `<span class="answer-caption">用户回答</span><div class="review-answer-text">${answer ? esc(text || "本题仅提交图片") : slot ? "本题答案已删除" : "尚未回答本题"}</div>`;
        content += question.answers?.length ? `<div class="review-reference"><strong>管理员参考答案</strong><p>${question.answers.map(value => esc(value)).join("<br/>")}</p></div>` : "";
        content += `</section><section class="review-image-panel">${mode === "edit" ? artwork("answer_image",image || "","答案图片",{privateImage:true}) : image ? `<div class="review-image-loading" role="status">正在读取提交图片</div><img data-review-image="${esc(image)}" alt="当前用户本题提交的图片"/><div class="review-image-actions">${button("下载图片",{action:"review-download",attrs:`data-file="${esc(image)}"`,style:"text",glyph:"file"})}</div>` : `<div class="review-no-image">${icon("image")}<span>本题没有附图</span></div>`}</section></div>`;
        if (mode === "edit") content += `<div class="notice">${item.require_correct ? "当场答对模式仍需符合题目要求和参考答案" : "保存后整份资料需要重新审核，原通过结果会取消"}</div>`;
        if (mode === "delete") content += `<div class="answer-delete-confirm"><strong>确认删除本题答案？</strong><p>文字和图片会永久删除，其他题答案保留<br/>该用户恢复为资料未齐，补齐并审核通过后才有开奖资格</p></div>`;
        if (mode !== "view") content += `<div class="delete-notify-choice"><div><strong>是否私聊通知该用户</strong><p>只说明整体作答状态，不包含题号、错误原因或答案</p></div>${toggle("answer_notify",true,"通知用户")}</div>`;
        if (slot?.edited_at || slot?.deleted_at) content += `<p class="field-hint">${slot.deleted_at ? "资料删除" : "答案编辑"} · ${chinaDate(slot.deleted_at || slot.edited_at)} · ${esc(slot.deleted_by || slot.edited_by)}</p>`;
      } else content += '<div class="small-empty">本场不需要提交资料</div>';
      dialog.holder.querySelector(".modal-body").innerHTML = content;
      let footer = '<p class="review-error form-error" role="alert"></p>';
      if (mode === "view") {
        footer += `<div class="review-mark-panel"><div><strong>整份资料审核</strong><p>${winner ? "已中奖，资料与资格锁定" : !canChange ? "开奖时间已到或活动已结束，仅可查看" : canReview ? "查看全部回答后统一通过或不通过，用户只收到整体结果" : item.require_correct ? "当场答对模式自动核对，可修改符合要求的答案" : "资料尚未齐全，补齐后才能审核整份"}</p></div><div class="review-mark-buttons" role="group" aria-label="整份资料审核"><button class="review-mark correct ${statusOf(entry) === "approved" ? "selected" : ""}" data-review-decision="approved" ${canReview ? "" : "disabled"}>${icon("check")}整份通过</button><button class="review-mark wrong ${statusOf(entry) === "rejected" ? "selected" : ""}" data-review-decision="rejected" ${canReview ? "" : "disabled"}>${icon("close")}整份不通过</button></div></div>`;
        footer += `<div class="answer-management-tools">${slot && canChange ? button(slot.kind === "deleted" ? "重新填写本题" : "编辑本题答案",{action:"answer-edit",glyph:"edit",style:"text"}) : ""}${answer && canChange ? button("删除本题答案",{action:"answer-delete",glyph:"trash",style:"text"}) : ""}</div><div class="review-navigation">${questionIndex > 0 ? button("上一题",{action:"review-previous",style:"text"}) : ""}${questionIndex+1 < item.questions.length ? button("下一题",{action:"review-next"}) : ""}${!this.readonly && !item.require_correct && this.summary.pending ? button("下一份待审核",{action:"review-next-person",glyph:"arrow"}) : ""}${button("刷新资料",{action:"review-refresh",style:"text"})}${button("返回报名列表",{action:"dismiss",style:"text"})}</div>`;
      } else footer += `<div class="review-navigation">${button("取消操作",{action:"answer-cancel",style:"text"})}${button(mode === "edit" ? "保存答案" : "确认删除本题答案",{action:mode === "edit" ? "answer-save" : "answer-delete-confirm",glyph:mode === "edit" ? "check" : "trash"})}</div>`;
      dialog.holder.querySelector(".review-actions-footer").innerHTML = footer;
      dialog.holder.querySelector(".modal-body").scrollTop = 0;
      if (image && mode === "view") {
        if (!assets.has(image)) assets.set(image,this.bridge.apiGet(`images/${image}`,{preview:"1"}));
        assets.get(image).then(data => {
          const element = dialog.holder.querySelector(`[data-review-image="${image}"]`);
          if (!closed && element && /^data:image\/jpeg;base64,/.test(data.preview || "")) { element.src = data.preview; element.classList.add("loaded"); dialog.holder.querySelector(".review-image-loading")?.remove(); }
        }).catch(() => { if (!closed && dialog.holder.querySelector(`[data-review-image="${image}"]`)) (dialog.holder.querySelector(".review-image-loading") || {}).textContent = "图片无法读取，可下载或稍后重试"; });
      }
      if (mode === "edit") dialog.holder.querySelector('textarea[name="answer_text"]')?.focus();
    };
    paint();
    dialog.holder.addEventListener("click",async event => {
      const target = event.target.closest("button"), action = target?.dataset.action;
      if (!target || target.disabled || busy || closed) return;
      if (dialog.holder.querySelector("meow-upload")?.busy && action?.startsWith("answer-")) { toast("请等待图片上传完成"); return; }
      if (target.dataset.reviewQuestion !== undefined && mode === "view") { questionIndex = Number(target.dataset.reviewQuestion); paint(); return; }
      if (action === "review-previous" || action === "review-next") { questionIndex += action === "review-previous" ? -1 : 1; paint(); return; }
      if (action === "answer-edit" || action === "answer-delete") { mode = action === "answer-edit" ? "edit" : "delete"; paint(); return; }
      if (action === "answer-cancel") { mode = "view"; paint(); return; }
      if (action === "review-download") {
        target.disabled = true;
        try { await this.bridge.download(`images/${target.dataset.file}`,{},`喵喵抽奖-${entry.user_id}-${target.dataset.file}`); }
        catch (error) { dialog.holder.querySelector(".review-error").textContent = clean(error.message); }
        finally { if (target.isConnected) target.disabled = false; } return;
      }
      if (!(target.dataset.reviewDecision || ["review-next-person","review-refresh","answer-save","answer-delete-confirm"].includes(action))) return;
      const controls = [...dialog.holder.querySelectorAll("button,input,textarea")].map(element => [element,element.disabled]);
      busy = true; controls.forEach(([element]) => element.disabled = true);
      try {
        if (action === "review-next-person") {
          const pending = await this.bridge.apiGet(`lotteries/${this.id}/entries`,{page:1,page_size:20,status:"pending",q:this.query});
          const next = pending.entries.find(value => value.user_id !== entry.user_id);
          if (!next) { toast("当前筛选范围内没有其他待审核报名"); busy = false; dialog.close(); await this.reloadAfterReview(); return; }
          user = next.user_id; questionIndex = 0;
        } else if (action !== "review-refresh") {
          let payload;
          if (target.dataset.reviewDecision) payload = {action:"decision",user_id:entry.user_id,correct:target.dataset.reviewDecision === "approved",revision:entry.answer_revision || 0};
          else {
            const notify = dialog.holder.querySelector('meow-switch[name="answer_notify"]').value;
            payload = {action:action === "answer-save" ? "edit_answer" : "delete_answer",user_id:entry.user_id,question_index:questionIndex,revision:entry.answer_revision || 0,notify};
            if (action === "answer-save") {
              const question = item.questions[questionIndex], text = dialog.holder.querySelector('[name="answer_text"]').value, image = dialog.holder.querySelector('meow-upload[name="answer_image"]').value;
              const kind = ["image","mixed"].includes(question.kind) ? question.kind : "text";
              if (text.length > 1000 || kind !== "image" && !text.trim()) throw Error("本题需要 1–1000 字非空文字，空格与换行也计入长度");
              if (["image","mixed"].includes(kind) && !image) throw Error("本题需要一张图片，请上传后保存");
              payload.answer = kind === "image" ? {kind,value:image,text} : {kind,value:text,...(image ? {image} : {})};
            } else payload.confirmed = true;
          }
          await this.bridge.apiPost(`lotteries/${this.id}/review`,payload);
          mode = "view";
        }
        response = await this.bridge.apiGet(`lotteries/${this.id}/entries/${user}`);
        entry = response.entry; item = response.item; assets.clear();
        await this.reloadAfterReview(); paint();
      } catch (error) { dialog.holder.querySelector(".review-error").textContent = clean(error.message); }
      finally { busy = false; controls.forEach(([element,disabled]) => { if (element.isConnected) element.disabled = disabled; }); }
    });
  }

}
