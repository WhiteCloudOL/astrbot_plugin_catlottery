"use strict";

const assert = require("node:assert/strict");
const { readFile } = require("node:fs/promises");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const { JSDOM } = require("jsdom");

const root = path.resolve(__dirname, "../pages/manage");
const flush = async () => { for (let index = 0; index < 3; index++) await new Promise(resolve => setImmediate(resolve)); };

async function setup(t, { lotteries = [] } = {}) {
  const html = await readFile(path.join(root, "index.html"), "utf8");
  const dom = new JSDOM(html, { url: "http://localhost/test", runScripts: "outside-only" });
  t.after(() => dom.window.close());
  const window = dom.window;
  const data = { lotteries, settings: { manager_ids: ["9999999"], llm_tools_enabled: true }, server_time: Date.now() / 1000 };
  const calls = [];
  const polling = [];
  let rejectUpload = false;
  let uploads = 0;
  window.setInterval = callback => { polling.push(callback); return 1; };
  window.clearInterval = () => {};
  window.HTMLElement.prototype.scrollIntoView = () => {};
  window.AstrBotPluginPage = {
    ready: async () => ({}),
    apiGet: async endpoint => {
      if (endpoint === "state") return structuredClone(data);
      if (endpoint === "platforms") return { platforms: [{ id: "napcat", name: "NapCat", online: true, accounts: [{ bot_id: "1234567", nickname: "猫猫", groups: [{ group_id: "2222222", group_name: "测试群" }] }] }] };
      if (endpoint.startsWith("artwork/")) return { preview: "data:image/jpeg;base64,cHJldmlldw==" };
      if (endpoint.startsWith("lotteries/")) return { entries: [], deliveries: [] };
      throw Error(`Unexpected endpoint: ${endpoint}`);
    },
    apiPost: async (endpoint, payload) => {
      calls.push({ endpoint, payload: structuredClone(payload) });
      if (endpoint === "settings") { data.settings = structuredClone(payload); return data.settings; }
      if (endpoint === "lotteries") {
        const item = { ...structuredClone(payload), close_at: Date.parse(payload.close_at) / 1000, draw_at: Date.parse(payload.draw_at) / 1000, id: "a1234567", prize: "分级奖品", winner_count: payload.prize_tiers.reduce((sum, tier) => sum + tier.count, 0), phase: "open", status: "open", entry_count: 0, complete_count: 0, pending_deliveries: 0, winners: [], tier_draws: [] };
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
  const components = new vm.SourceTextModule(await readFile(path.join(root, "components.js"), "utf8"), { context });
  const app = new vm.SourceTextModule(await readFile(path.join(root, "app.js"), "utf8"), { context });
  await components.link(() => { throw Error("Unexpected component import"); });
  await app.link(() => components);
  await app.evaluate();
  await flush();
  return { document: window.document, window, data, calls, polling, rejectUploads: () => { rejectUpload = true; } };
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
  assert.deepEqual(calls[0], { endpoint: "settings", payload: { manager_ids: ["9999999", "8888888"], llm_tools_enabled: false } });
  assert.ok(document.querySelector(".settings-page"));
  assert.match(document.querySelector(".settings-feedback").textContent, /已保存/);
});

test("per-award counts and independent images are saved together with the cover", async t => {
  const { document, window, calls, rejectUploads } = await setup(t);
  document.querySelector('[data-action="create"]').click();
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
  assert.equal(document.querySelectorAll(".modal-backdrop").length, 2);
  document.querySelector('[data-action="confirm-tier"]').click(); await flush();
  assert.deepEqual(calls[0], { endpoint: "lotteries/a1234567/action", payload: { action: "draw_tier", tier_index: 1, confirmed: true } });
  assert.equal(document.querySelectorAll('[data-action="draw-tier"]').length, 1);
  assert.match(document.querySelector(".award-vacancy").textContent, /无人符合资格/);
  assert.ok(document.querySelector('[data-action="cancel"]').disabled);
});
