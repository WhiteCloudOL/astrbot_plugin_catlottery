"use strict";

const assert = require("node:assert/strict");
const { readFile } = require("node:fs/promises");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const { JSDOM } = require("jsdom");

const root = path.resolve(__dirname, "../pages/manage");
const flush = async () => { for (let index = 0; index < 3; index++) await new Promise(resolve => setImmediate(resolve)); };

async function setup(t, { lotteries = [], entries = [], hash = "" } = {}) {
  const html = await readFile(path.join(root, "index.html"), "utf8");
  const dom = new JSDOM(html, { url: "http://localhost/test" + hash, runScripts: "outside-only" });
  t.after(() => dom.window.close());
  const window = dom.window;
  const data = { lotteries, settings: { manager_ids: ["9999999"], llm_tools_enabled: true, avatar_cache_hours:24 }, server_time: Date.now() / 1000 };
  const calls = [];
  const polling = [];
  let rejectUpload = false, rejectReview = false;
  const forms = structuredClone(entries);
  const statusOf = entry => entry.status === "complete" ? entry.review_status || "approved" : "incomplete";
  const summary = () => Object.fromEntries(["total", "approved", "pending", "rejected", "incomplete"].map(key => [key, key === "total" ? forms.length : forms.filter(entry => statusOf(entry) === key).length]));
  const page = (params = {}) => { const rows = forms.filter(entry => (params.status === undefined || params.status === "all" || statusOf(entry) === params.status) && `${entry.nickname} ${entry.user_id} ${entry.group_id}`.includes(params.q || "")); const size = Number(params.page_size || 20), current = Math.min(Number(params.page || 1), Math.max(1, Math.ceil(rows.length / size))); return {entries: rows.slice((current - 1) * size, current * size).map(entry => {const result = structuredClone(entry); result.answer_count = result.answers.length; result.marked_count = result.answers.filter(answer => answer.correct != null).length; delete result.answers; return result;}),summary:summary(),total:rows.length,page:current,page_size:size}; };
  let uploads = 0;
  window.setInterval = callback => { polling.push(callback); return 1; };
  window.clearInterval = () => {};
  window.HTMLElement.prototype.scrollIntoView = () => {};
  window.scrollTo = () => {};
  window.AstrBotPluginPage = {
    ready: async () => ({}),
    apiGet: async (endpoint, params) => {
      if (endpoint === "state") return structuredClone(data);
      if (endpoint === "platforms") return { platforms: [{ id: "napcat", name: "NapCat", online: true, accounts: [{ bot_id: "1234567", nickname: "猫猫", groups: [{ group_id: "2222222", group_name: "测试群" }] }] }] };
      if (endpoint.startsWith("artwork/")) return { preview: "data:image/jpeg;base64,cHJldmlldw==" };
      if (endpoint.startsWith("avatars/")) return { preview:"", expires_at:Date.now()/1000 + 300 };
      if (endpoint.startsWith("images/")) return { preview:"data:image/jpeg;base64,cHJldmlldw==" };
      if (endpoint.startsWith("lotteries/")) {
        const parts = endpoint.split("/"); const item = data.lotteries.find(value => value.id === parts[1]);
        if (!item) throw Error("活动不存在");
        if (parts[2] === "entries" && parts[3]) return {entry:structuredClone(forms.find(entry => entry.user_id === parts[3])),item:structuredClone(item),server_time:data.server_time};
        if (parts[2] === "entries") return page(params);
        return {...page(),item:structuredClone(item),deliveries:[],server_time:data.server_time};
      }
      throw Error(`Unexpected endpoint: ${endpoint}`);
    },
    apiPost: async (endpoint, payload) => {
      calls.push({ endpoint, payload: structuredClone(payload) });
      if (endpoint === "avatars/clear") return {removed:1};
      if (endpoint.endsWith("/review")) {
        if (rejectReview) throw Error("审核保存失败");
        for (const entry of forms.filter(value => payload.user_ids ? payload.user_ids.includes(value.user_id) : value.user_id === payload.user_id)) { entry.answers.forEach((answer,index) => {if (payload.action === "bulk" || index === payload.question_index) answer.correct = payload.correct;}); entry.review_status = entry.answers.some(answer => answer.correct === false) ? "rejected" : entry.answers.every(answer => answer.correct === true) ? "approved" : "pending"; }
        return {marked_answers:1};
      }
      if (endpoint === "settings") { data.settings = structuredClone(payload); return data.settings; }
      if (endpoint === "lotteries") {
        const item = { ...structuredClone(payload), close_at: typeof payload.close_at === "number" ? payload.close_at : Date.parse(payload.close_at) / 1000, draw_at: typeof payload.draw_at === "number" ? payload.draw_at : Date.parse(payload.draw_at) / 1000, id: "a1234567", prize: "分级奖品", winner_count: payload.prize_tiers.reduce((sum, tier) => sum + tier.count, 0), phase: "open", status: "open", entry_count: 0, complete_count: 0, pending_deliveries: 0, winners: [], tier_draws: [] };
        data.lotteries = [item]; return item;
      }
      if (endpoint.endsWith("/action")) {
        const item = data.lotteries[0];
        if (payload.action === "draw_tier") item.tier_draws.push({ tier_index: payload.tier_index, drawn_at: data.server_time, eligible_count: 0, winner_count: 0, pool_hash: "0".repeat(64) });
        return item;
      }
      throw Error(`Unexpected mutation: ${endpoint}`);
    },
    upload: async (endpoint, file) => {
      assert.equal(endpoint, "artwork"); assert.ok(file.size > 0);
      if (rejectUpload) throw Error("上传失败。");
      return { image: String(++uploads).padStart(32, "0") + ".jpg" };
    },
  };
  const context = dom.getInternalVMContext();
  const modules = new Map();
  for (const name of ["components.js", "activity.js", "app.js"]) modules.set(name, new vm.SourceTextModule(await readFile(path.join(root, name), "utf8"), { context }));
  const app = modules.get("app.js");
  await app.link(specifier => modules.get(path.basename(specifier)));
  await app.evaluate();
  await flush();
  return { document: window.document, window, data, calls, polling, forms, rejectReviews: value => { rejectReview = value; }, rejectUploads: () => { rejectUpload = true; } };
}

test("settings and guide are independent pages and unsaved settings survive polling", async t => {
  const { document, window, calls, polling } = await setup(t);
  document.querySelector('[data-action="settings"]').click();
  assert.ok(document.querySelector(".settings-page"));
  assert.equal(document.querySelector(".modal-backdrop"), null);
  assert.equal(document.querySelector('[aria-current="page"]').dataset.action, "settings");
  const input = document.querySelector('[name="manager"]');
  input.value = "8888888"; input.dispatchEvent(new window.Event("input", { bubbles: true }));
  document.querySelector('[data-action="add-manager"]').click();
  document.querySelector('meow-switch[name="llm_tools_enabled"] button').click();
  polling[0](); await flush();
  assert.match(document.querySelector("#manager-list").textContent, /8888888/);
  assert.equal(document.querySelector('meow-switch[name="llm_tools_enabled"]').value, false);
  document.querySelector('[data-action="guide"]').click();
  assert.ok(document.querySelector(".guide-page"));
  assert.equal(document.querySelector(".modal-backdrop"), null);
  assert.match(document.querySelector(".guide-page").textContent, /提前抽出某个奖项/);
  assert.ok([...document.querySelectorAll("p,h1,h2,h3,.field-hint")].every(element => !/[。.]$/.test(element.textContent.trim())));
  document.querySelector('[data-action="settings"]').click();
  document.querySelector('[data-action="save-settings"]').click(); await flush();
  assert.deepEqual(calls[0], { endpoint: "settings", payload: { manager_ids: ["9999999", "8888888"], llm_tools_enabled: false, avatar_cache_hours:24 } });
  assert.ok(document.querySelector(".settings-page"));
  assert.match(document.querySelector(".settings-feedback").textContent, /已保存/);
});

test("per-award counts and independent images are saved together with the cover", async t => {
  const { document, window, calls, rejectUploads } = await setup(t);
  document.querySelector('[data-action="create"]').click();
  const successNotice = document.querySelector('meow-switch[name="group_success_notify"]');
  assert.equal(successNotice.value, true);
  successNotice.querySelector("button").click();
  assert.equal(successNotice.value, false);
  document.querySelector('[name="title"]').value = "三份好运";
  for (let index = 0; index < 2; index++) document.querySelector('[data-action="add-tier"]').click();
  const cards = [...document.querySelectorAll("[data-tier]")];
  assert.equal(cards.length, 3);
  for (let index = 0; index < cards.length; index++) {
    cards[index].querySelector('[name="tier_prize"]').value = ["玩偶", "杯垫", "贴纸"][index];
    const input = cards[index].querySelector('meow-stepper input');
    input.value = String(index + 1); input.dispatchEvent(new window.Event("change", { bubbles: true }));
    await cards[index].querySelector("meow-upload").upload(new window.File(["picture"], "cat.png", { type: "image/png" }));
  }
  const cover = document.querySelector('meow-upload[name="cover"]');
  await cover.upload(new window.File(["cover"], "cover.png", { type: "image/png" }));
  assert.match(cover.querySelector("img").src, /^data:image/);
  const previous = cover.value;
  rejectUploads();
  await cover.upload(new window.File(["another"], "other.png", { type: "image/png" }));
  assert.equal(cover.value, previous);
  assert.equal(document.querySelector(".toast-error span").textContent, "上传失败");
  document.querySelector('[data-action="save-lottery"]').click(); await flush();
  const saved = calls.find(call => call.endpoint === "lotteries").payload;
  assert.deepEqual(saved.prize_tiers.map(tier => [tier.name, tier.prize, tier.count]), [["一等奖", "玩偶", 1], ["二等奖", "杯垫", 2], ["三等奖", "贴纸", 3]]);
  assert.equal(new Set([saved.cover, ...saved.prize_tiers.map(tier => tier.image)]).size, 4);
  assert.equal(saved.winner_count, undefined);
  assert.equal(saved.prize, undefined);
  assert.equal(saved.group_success_notify, false);
  assert.ok(document.querySelector(".detail-cover img.loaded"));
  assert.equal(document.querySelectorAll(".award-picture img.loaded").length, 3);
  assert.equal(document.querySelectorAll('select,input[type="checkbox"],input[type="radio"],input[type="number"],input[type="date"],input[type="datetime-local"]').length, 0);
});

test("early drawing requires confirmation and only submits the selected award", async t => {
  const now = Date.now() / 1000;
  const item = { id: "a1234567", title: "活动", prize: "玩偶与贴纸", winner_count: 3, cover: "", prize_tiers: [{ name: "一等奖", prize: "玩偶", count: 1, image: "" }, { name: "二等奖", prize: "贴纸", count: 2, image: "" }], targets: [], questions: [], require_correct: true, description: "", close_at: now + 3600, draw_at: now + 7200, phase: "open", status: "open", entry_count: 0, complete_count: 0, pending_deliveries: 0, winners: [], tier_draws: [] };
  const { document, calls } = await setup(t, { lotteries: [item] });
  document.querySelector('[data-action="detail"]').click(); await flush();
  document.querySelector('[data-action="draw-tier"][data-tier-index="1"]').click();
  assert.equal(calls.length, 0);
  assert.equal(document.querySelectorAll(".modal-backdrop").length, 1);
  document.querySelector('[data-action="confirm-operation"]').click(); await flush();
  assert.deepEqual(calls[0], { endpoint: "lotteries/a1234567/action", payload: { action: "draw_tier", tier_index: 1, confirmed: true } });
  assert.equal(document.querySelectorAll('[data-action="draw-tier"]').length, 1);
  assert.match(document.querySelector(".award-vacancy").textContent, /无人符合资格/);
  assert.ok(document.querySelector('[data-action="cancel"]').disabled);
});

function reviewActivity() {
  const now = Date.now() / 1000;
  return {id:"a1234567",title:"猫猫报名审核",prize:"玩偶",winner_count:1,cover:"",prize_tiers:[{name:"一等奖",prize:"玩偶",count:1,image:""}],targets:[{platform_id:"napcat",bot_id:"1234567",group_id:"2222222"}],questions:[{kind:"quiz",prompt:"第一题",options:[],answers:["reference"]},{kind:"mixed",prompt:"图文资料",options:[],answers:[]}],require_correct:false,description:"",close_at:now+3600,draw_at:now+7200,phase:"open",status:"open",entry_count:65,complete_count:0,pending_deliveries:0,winners:[],tier_draws:[]};
}

function participants(count = 65) {
  return Array.from({length:count}, (_,index) => ({user_id:String(4444444+index),nickname:`猫猫 ${index}`,group_id:"2222222",bot_id:"1234567",platform_id:"napcat",status:index === count-1 ? "pending" : "complete",review_status:index === count-1 ? "incomplete" : "pending",answers:index === count-1 ? [] : [{kind:"text",value:"  a  b\nline  two  ",correct:null},{kind:"mixed",value:"图片与文字",image:String(index+1).padStart(32,"0")+".jpg",correct:null}]}));
}

test("detail and edit use independent pages, Back protects edits and polling preserves the form", async t => {
  const ui = await setup(t, {lotteries:[reviewActivity()],entries:participants(3),hash:"#detail/a1234567"});
  const {document,window,polling} = ui;
  assert.ok(document.querySelector(".activity-page"));
  assert.equal(document.querySelector(".modal-backdrop"),null);
  document.querySelector('[data-action="activity-edit"]').click(); await flush();
  assert.ok(document.querySelector(".editor-page"));
  const title = document.querySelector('[name="title"]');
  title.value = "尚未保存的编辑"; title.dispatchEvent(new window.Event("input",{bubbles:true}));
  polling[0](); await flush();
  assert.equal(document.querySelector('[name="title"]').value,"尚未保存的编辑");
  document.querySelector('[data-action="form-back"]').click();
  assert.equal(document.querySelectorAll(".modal-backdrop").length,1);
  document.querySelector('[data-action="dismiss"]').click();
  assert.ok(document.querySelector(".editor-page"));
  document.querySelector('[data-action="form-back"]').click();
  document.querySelector('[data-action="discard-edit"]').click(); await flush();
  assert.ok(document.querySelector(".activity-page"));
  document.querySelector('[data-action="activity-back"]').click(); await flush();
  assert.equal(window.location.hash,"#home");
});

test("group success notices remain editable after enrollment and survive polling and saving", async t => {
  const {document,calls,polling} = await setup(t, {lotteries:[reviewActivity()],entries:participants(3),hash:"#detail/a1234567"});
  assert.match(document.querySelector(".activity-facts").textContent,/群聊成功通知已开启/);
  document.querySelector('[data-action="activity-edit"]').click(); await flush();
  assert.ok(document.querySelector('meow-switch[name="require_correct"] button').disabled);
  const successNotice = document.querySelector('meow-switch[name="group_success_notify"]');
  assert.equal(successNotice.value,true);
  assert.equal(successNotice.querySelector("button").disabled,false);
  successNotice.querySelector("button").click();
  polling[0](); await flush();
  assert.equal(document.querySelector('meow-switch[name="group_success_notify"]').value,false);
  document.querySelector('[data-action="save-lottery"]').click(); await flush();
  assert.equal(calls.find(call => call.endpoint === "lotteries").payload.group_success_notify,false);
  assert.match(document.querySelector(".activity-facts").textContent,/群聊成功通知已关闭/);
  assert.equal(document.querySelectorAll('select,input[type="checkbox"],input[type="radio"]').length,0);
});

test("pending notices and recurring announcements use reusable controls and preserve saved choices", async t => {
  const {document,window,calls,polling} = await setup(t, {lotteries:[reviewActivity()],entries:participants(3),hash:"#detail/a1234567"});
  document.querySelector('[data-action="activity-edit"]').click(); await flush();
  const pending = document.querySelector('meow-switch[name="group_pending_notify"]');
  assert.equal(pending.value,true);
  assert.equal(pending.querySelector("button").disabled,false);
  pending.querySelector("button").click();
  const mode = document.querySelector('meow-choice[name="announcement_mode"]');
  assert.equal(document.querySelector(".schedule-fields").hidden,true);
  mode.querySelector(".choice-trigger").click();
  mode.querySelector('[data-value="repeat"]').click();
  assert.equal(document.querySelector(".schedule-fields").hidden,false);
  assert.equal(document.querySelector(".schedule-interval").hidden,false);
  const interval = document.querySelector('meow-stepper[name="announcement_interval_minutes"] input');
  assert.equal(interval.maxLength,5);
  interval.value = "10080"; interval.dispatchEvent(new window.Event("input",{bubbles:true}));
  polling[0](); await flush();
  assert.equal(document.querySelector('meow-switch[name="group_pending_notify"]').value,false);
  document.querySelector('[data-action="save-lottery"]').click(); await flush();
  const saved = calls.find(call => call.endpoint === "lotteries").payload;
  assert.equal(saved.group_pending_notify,false);
  assert.equal(saved.group_success_notify,true);
  assert.equal(saved.announcement_schedule.mode,"repeat");
  assert.equal(saved.announcement_schedule.interval_minutes,10080);
  assert.match(saved.announcement_schedule.start_at,/\+08:00$/);
  assert.match(document.querySelector(".activity-facts").textContent,/群聊待审核通知已关闭/);
  assert.match(document.querySelector(".activity-facts").textContent,/群公告计划循环发送/);
  assert.equal(document.querySelectorAll('select,input[type="checkbox"],input[type="radio"],input[type="number"],input[type="date"],input[type="datetime-local"]').length,0);
});

test("editing a running schedule preserves its original seconds and once mode hides only the interval", async t => {
  const item = reviewActivity();
  item.announcement_schedule = {mode:"repeat",start_at:Date.now()/1000 - 1807,interval_minutes:15};
  item.announcement_next_at = Date.now()/1000 + 600;
  const {document,calls} = await setup(t, {lotteries:[item],hash:"#detail/a1234567"});
  assert.match(document.querySelector(".activity-facts").textContent,/下次公告/);
  document.querySelector('[data-action="activity-edit"]').click(); await flush();
  document.querySelector('[name="title"]').value = "新的标题";
  document.querySelector('[data-action="save-lottery"]').click(); await flush();
  assert.equal(calls.find(call => call.endpoint === "lotteries").payload.announcement_schedule.start_at,item.announcement_schedule.start_at);
  document.querySelector('[data-action="activity-edit"]').click(); await flush();
  const mode = document.querySelector('meow-choice[name="announcement_mode"]');
  mode.querySelector(".choice-trigger").click(); mode.querySelector('[data-value="once"]').click();
  assert.equal(document.querySelector(".schedule-fields").hidden,false);
  assert.equal(document.querySelector(".schedule-interval").hidden,true);
  mode.querySelector(".choice-trigger").click(); mode.querySelector('[data-value="off"]').click();
  assert.equal(document.querySelector(".schedule-fields").hidden,true);
});

test("large participant lists are paginated, searchable, filtered and bulk review only changes selected QQ IDs", async t => {
  const ui = await setup(t, {lotteries:[reviewActivity()],entries:participants(),hash:"#detail/a1234567"});
  const {document,window,calls,forms} = ui;
  document.querySelector('[data-tab="participants"]').click(); await flush();
  assert.equal(document.querySelectorAll(".participant-row").length,20);
  document.querySelector('[data-action="entry-page"][data-page="2"]').click(); await flush();
  assert.match(document.querySelector(".participant-row").textContent,/4444464/);
  const search = document.querySelector('[name="participant-search"]');
  search.value = "4444465"; search.dispatchEvent(new window.Event("input",{bubbles:true}));
  await new Promise(resolve => setTimeout(resolve,300)); await flush();
  assert.equal(document.querySelectorAll(".participant-row").length,1);
  document.querySelector('[data-select="4444465"]').click();
  document.querySelector('[data-action="bulk-approve"]').click();
  assert.equal(calls.length,0);
  document.querySelector('[data-action="confirm-operation"]').click(); await flush();
  assert.deepEqual(calls[0].payload,{action:"bulk",user_ids:["4444465"],correct:true});
  assert.equal(forms.filter(entry => entry.review_status === "approved").length,1);
  document.querySelector('[data-entry-filter="approved"]').click(); await flush();
  assert.equal(document.querySelectorAll(".participant-row").length,1);
  assert.equal(document.querySelectorAll('select,input[type="checkbox"],input[type="radio"],details').length,0);
});

test("answer dialog advances one question, keeps original whitespace and previews the correct user's image", async t => {
  const {document,calls} = await setup(t, {lotteries:[reviewActivity()],entries:participants(3),hash:"#detail/a1234567"});
  document.querySelector('[data-tab="participants"]').click(); await flush();
  document.querySelector('[data-action="review-person"][data-user-id="4444444"]').click(); await flush();
  assert.equal(document.querySelectorAll(".modal-backdrop").length,1);
  assert.equal(document.querySelector(".review-answer-text").textContent,"  a  b\nline  two  ");
  assert.match(document.querySelector(".review-reference").textContent,/reference/);
  assert.equal(document.querySelector('[data-action="review-previous"]'),null);
  document.querySelector('[data-review-mark="correct"]').click(); await flush();
  assert.deepEqual(calls[0].payload,{action:"mark",user_id:"4444444",question_index:0,correct:true});
  assert.match(document.querySelector(".review-prompt").textContent,/图文资料/);
  assert.equal(document.querySelector("img[data-review-image]").dataset.reviewImage,"00000000000000000000000000000001.jpg");
  assert.match(document.querySelector("img[data-review-image]").src,/^data:image\/jpeg/);
  assert.ok(document.querySelector('[data-action="review-previous"]'));
  assert.equal(document.querySelector('[data-action="review-next"]'),null);
  document.querySelector('[data-review-mark="correct"]').click(); await flush();
  document.querySelector('[data-action="review-next-person"]').click(); await flush();
  assert.match(document.querySelector(".review-person").textContent,/4444445/);
  assert.match(document.querySelector(".review-prompt").textContent,/第一题/);
});

test("failed review retains question and saved marks; an expired form stays locked after a request", async t => {
  const ui = await setup(t, {lotteries:[reviewActivity()],entries:participants(3),hash:"#detail/a1234567"});
  const {document,calls,data,forms,rejectReviews} = ui;
  document.querySelector('[data-tab="participants"]').click(); await flush();
  document.querySelector('[data-action="review-person"]').click(); await flush();
  rejectReviews(true);
  document.querySelector('[data-review-mark="correct"]').click(); await flush();
  assert.match(document.querySelector(".review-error").textContent,/审核保存失败/);
  assert.match(document.querySelector(".review-prompt").textContent,/第一题/);
  assert.equal(forms[0].answers[0].correct,null);
  rejectReviews(false); data.server_time = data.lotteries[0].draw_at + 1;
  document.querySelector('[data-review-mark="correct"]').click(); await flush();
  assert.ok([...document.querySelectorAll("[data-review-mark]")].every(button => button.disabled));
  document.querySelector('[data-action="review-previous"]').click(); await flush();
  assert.ok([...document.querySelectorAll("[data-review-mark]")].every(button => button.disabled));
  assert.equal(calls.length,2);
});

test("incomplete and winning participants cannot be selected or reviewed", async t => {
  const item = reviewActivity(), entries = participants(3);
  item.winners = [{user_id:entries[0].user_id,nickname:entries[0].nickname,tier_index:0}];
  const {document,calls} = await setup(t, {lotteries:[item],entries,hash:"#detail/a1234567"});
  document.querySelector('[data-tab="participants"]').click(); await flush();
  assert.ok(document.querySelector('[data-select="4444444"]').disabled);
  assert.ok(document.querySelector('[data-select="4444446"]').disabled);
  document.querySelector('[data-action="select-page"]').click();
  document.querySelector('[data-action="bulk-approve"]').click();
  document.querySelector('[data-action="confirm-operation"]').click(); await flush();
  assert.deepEqual(calls[0].payload.user_ids,["4444445"]);
  document.querySelector('[data-action="review-person"][data-user-id="4444444"]').click(); await flush();
  assert.ok([...document.querySelectorAll("[data-review-mark]")].every(button => button.disabled));
  assert.match(document.querySelector(".review-mark-panel").textContent,/已中奖/);
});

test("avatar cache duration is retained during polling and clearing requires an explicit action", async t => {
  const {document,window,calls,polling} = await setup(t);
  document.querySelector('[data-action="settings"]').click();
  const input = document.querySelector('meow-stepper[name="avatar_cache_hours"] input');
  input.value = "48"; input.dispatchEvent(new window.Event("change",{bubbles:true}));
  polling[0](); await flush();
  assert.equal(document.querySelector('meow-stepper[name="avatar_cache_hours"]').value,48);
  document.querySelector('[data-action="save-settings"]').click(); await flush();
  assert.equal(calls[0].payload.avatar_cache_hours,48);
  document.querySelector('[data-action="clear-avatars"]').click();
  assert.equal(calls.length,1);
  document.querySelector('[data-action="confirm-avatar-clear"]').click(); await flush();
  assert.deepEqual(calls[1],{endpoint:"avatars/clear",payload:{confirmed:true}});
});

test("failed scheduled form save can be retried without losing dates or input", async t => {
  const item = reviewActivity();
  item.announcement_schedule = {mode:"repeat",start_at:Date.now()/1000+600,interval_minutes:30};
  const {document,window,calls} = await setup(t,{lotteries:[item],hash:"#edit/a1234567"});
  const originalPost = window.AstrBotPluginPage.apiPost;
  let attempts = 0;
  window.AstrBotPluginPage.apiPost = async (...args) => {
    if (args[0] === "lotteries" && attempts++ === 0) throw Error("暂时无法保存");
    return originalPost(...args);
  };
  document.querySelector('[name="title"]').value = "修改后保留";
  const calendar = document.querySelector('[name="announcement_start_at"]');
  calendar.value = calendar.value.slice(0,14) + "59";
  document.querySelector('[data-action="save-lottery"]').click(); await flush();
  assert.match(document.querySelector(".form-error").textContent,/暂时无法保存/);
  assert.equal(document.querySelector('[name="title"]').value,"修改后保留");
  document.querySelector('[data-action="save-lottery"]').click(); await flush();
  assert.equal(attempts,2);
  assert.ok(document.querySelector(".activity-page"));
  const saved = calls.find(call=>call.endpoint === "lotteries").payload;
  assert.equal(saved.close_at,item.close_at);
  assert.equal(saved.draw_at,item.draw_at);
  assert.equal(saved.title,"修改后保留");
});

test("page unload protects unsaved forms and does not cancel future polling", async t => {
  const {document,window,polling} = await setup(t);
  document.querySelector('[data-action="create"]').click(); await flush();
  const title = document.querySelector('[name="title"]');
  title.value = "未保存";
  title.dispatchEvent(new window.Event("input",{bubbles:true}));
  const unload = new window.Event("beforeunload",{cancelable:true});
  window.dispatchEvent(unload);
  assert.equal(unload.defaultPrevented,true);
  polling[0](); await flush();
  assert.equal(document.querySelector('[name="title"]').value,"未保存");
});

test("calendar keeps keyboard focus after picking a day and fields have labels", async t => {
  const {document} = await setup(t);
  document.querySelector('[data-action="create"]').click(); await flush();
  const calendar = document.querySelector('[name="close_at"]');
  assert.match(calendar.querySelector(".date-trigger").getAttribute("aria-label"),/报名截止/);
  calendar.querySelector(".date-trigger").click();
  assert.ok(document.activeElement.hasAttribute("data-day"));
  calendar.querySelector('[data-day="10"]').click();
  assert.equal(document.activeElement.dataset.day,"10");
  document.querySelectorAll(".choice-trigger").forEach(element=>assert.ok(element.getAttribute("aria-label")));
});

test("confirmation isolates the background and restores it on close", async t => {
  const {document} = await setup(t,{lotteries:[reviewActivity()],hash:"#detail/a1234567"});
  document.querySelector('[data-action="close"]').click();
  assert.equal(document.getElementById("app").inert,true);
  document.querySelector('.modal-close').click();
  assert.equal(document.getElementById("app").inert,false);
});
