/** Reusable, keyboard-accessible controls. Native pickers are never displayed. */
export const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));

export const icons = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  gift: '<path d="M3 9h18v4H3zM5 13v8h14v-8M12 9v12M12 9H7.5a2.5 2.5 0 1 1 2.4-3.2L12 9Zm0 0h4.5a2.5 2.5 0 1 0-2.4-3.2L12 9Z"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2"/>',
  chevron: '<path d="m8 10 4 4 4-4"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18"/>',
  group: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-3.9M16 3a4 4 0 0 1 0 8"/><circle cx="9" cy="7" r="4"/>',
  settings: '<path d="M12 3v3m0 12v3M3 12h3m12 0h3m-2.6-6.4-2.1 2.1m-8.6 8.6-2.1 2.1m12.8 0-2.1-2.1M7.7 7.7 5.6 5.6"/><circle cx="12" cy="12" r="5"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  edit: '<path d="m16 3 5 5-12 12H4v-5L16 3Z"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  send: '<path d="m22 2-7 20-4-9-9-4L22 2ZM22 2 11 13"/>',
  paw: '<ellipse cx="12" cy="16" rx="6" ry="4"/><ellipse cx="5" cy="8" rx="2" ry="3"/><ellipse cx="11" cy="5" rx="2" ry="3"/><ellipse cx="17" cy="6" rx="2" ry="3"/><ellipse cx="21" cy="11" rx="2" ry="3"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6ZM14 2v6h6M8 13h8m-8 4h6"/>',
};
export const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.gift}</svg>`;

export const button = (label, { action = "", style = "tonal", glyph = "", attrs = "" } = {}) => `<button class="btn btn-${style}" data-action="${escapeHTML(action)}" ${attrs}>${glyph ? icon(glyph) : ""}<span>${escapeHTML(label)}</span></button>`;
export const field = (label, name, value = "", { hint = "", placeholder = "", multiline = false, maxlength = 1000, disabled = false } = {}) => `<label class="field"><span class="field-label">${escapeHTML(label)}</span>${multiline ? `<textarea name="${name}" rows="3" maxlength="${maxlength}" ${disabled ? "disabled" : ""} placeholder="${escapeHTML(placeholder)}">${escapeHTML(value)}</textarea>` : `<input name="${name}" type="text" value="${escapeHTML(value)}" maxlength="${maxlength}" ${disabled ? "disabled" : ""} placeholder="${escapeHTML(placeholder)}" autocomplete="off" />`}${hint ? `<span class="field-hint">${escapeHTML(hint)}</span>` : ""}</label>`;

class Choice extends HTMLElement {
  connectedCallback() {
    if (this._ready) return;
    this._ready = true;
    this.items = JSON.parse(this.getAttribute("items") || "[]");
    this.value = this.getAttribute("value") || "";
    this.opened = false;
    this.render();
    this.addEventListener("click", event => {
      const option = event.target.closest("[data-value]");
      if (option) {
        this.value = option.dataset.value;
        this.opened = false;
        this.render();
        this.querySelector(".choice-trigger").focus();
        this.dispatchEvent(new Event("change", { bubbles: true }));
      } else if (event.target.closest(".choice-trigger")) {
        this.opened = !this.opened;
        this.render();
        if (this.opened) (this.querySelector('[role="option"][aria-selected="true"]') || this.querySelector('[role="option"]'))?.focus();
      }
    });
    this.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        this.opened = false;
        this.render();
        this.querySelector(".choice-trigger").focus();
      } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        this.opened = true;
        if (!this.querySelector('[role="listbox"]')) this.render();
        const options = [...this.querySelectorAll('[role="option"]')];
        if (!options.length) return;
        let index = options.indexOf(document.activeElement);
        index = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
        options[index].focus();
      }
    });
    this.outside = event => {
      if (this.opened && !event.composedPath().includes(this)) { this.opened = false; this.render(); }
    };
    document.addEventListener("click", this.outside);
  }
  disconnectedCallback() { document.removeEventListener("click", this.outside); }
  render() {
    const selected = this.items.find(item => String(item.value) === this.value);
    const disabled = this.hasAttribute("disabled");
    this.innerHTML = `<button type="button" class="choice-trigger" aria-haspopup="listbox" aria-expanded="${this.opened}" ${disabled ? "disabled" : ""}><span>${escapeHTML(selected?.label || this.getAttribute("placeholder") || "请选择")}</span>${icon("chevron")}</button>${this.opened && !disabled ? `<div class="choice-menu" role="listbox" aria-label="${escapeHTML(this.getAttribute("label") || "选项")}">${this.items.map(item => `<button type="button" role="option" aria-selected="${String(item.value) === this.value}" data-value="${escapeHTML(item.value)}">${escapeHTML(item.label)}${String(item.value) === this.value ? icon("check") : ""}</button>`).join("") || '<div class="choice-empty">没有可选项目</div>'}</div>` : ""}`;
  }
}
customElements.define("meow-choice", Choice);
export const choice = (name, value, items, { placeholder = "请选择", disabled = false, label = "" } = {}) => `<meow-choice name="${name}" value="${escapeHTML(value)}" items='${escapeHTML(JSON.stringify(items))}' placeholder="${escapeHTML(placeholder)}" label="${escapeHTML(label)}" ${disabled ? "disabled" : ""}></meow-choice>`;

class Switch extends HTMLElement {
  connectedCallback() {
    if (this._ready) return;
    this._ready = true;
    this.value = this.getAttribute("value") === "true";
    this.render();
    this.addEventListener("click", event => {
      if (this.hasAttribute("disabled") || !event.target.closest("button")) return;
      this.value = !this.value;
      this.setAttribute("value", String(this.value));
      this.render();
      this.querySelector("button").focus();
      this.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
  render() {
    this.innerHTML = `<button type="button" class="switch-control" role="switch" aria-checked="${this.value}" aria-label="${escapeHTML(this.getAttribute("label") || "开关")}" ${this.hasAttribute("disabled") ? "disabled" : ""}><span class="switch-track"><span class="switch-thumb">${icon(this.value ? "check" : "close")}</span></span><span>${this.value ? "已开启" : "已关闭"}</span></button>`;
  }
}
customElements.define("meow-switch", Switch);
export const toggle = (name, value, label, { disabled = false } = {}) => `<meow-switch name="${escapeHTML(name)}" value="${Boolean(value)}" label="${escapeHTML(label)}" ${disabled ? "disabled" : ""}></meow-switch>`;

class Stepper extends HTMLElement {
  connectedCallback() {
    if (this._ready) return;
    this._ready = true;
    this.value = Number(this.getAttribute("value") || 1);
    this.min = Number(this.getAttribute("min") || 1);
    this.max = Number(this.getAttribute("max") || 100);
    this.innerHTML = `<div class="stepper"><button type="button" aria-label="减少" data-step="-1">−</button><input type="text" inputmode="numeric" aria-label="${escapeHTML(this.getAttribute("label") || "数值")}" value="${this.value}" maxlength="3"/><button type="button" aria-label="增加" data-step="1">+</button></div>`;
    if (this.hasAttribute("disabled")) this.querySelectorAll("button,input").forEach(element => element.disabled = true);
    this.addEventListener("click", event => {
      const step = event.target.closest("[data-step]");
      if (step) { this.value = Math.min(this.max, Math.max(this.min, this.value + Number(step.dataset.step))); this.querySelector("input").value = this.value; this.dispatchEvent(new Event("change", { bubbles: true })); }
    });
    this.querySelector("input").addEventListener("change", event => {
      this.value = Math.min(this.max, Math.max(this.min, Number(event.target.value) || this.min));
      event.target.value = this.value;
    });
  }
}
customElements.define("meow-stepper", Stepper);

export const chinaDate = seconds => new Date(seconds * 1000 + 8 * 3600000).toISOString().slice(0, 16).replace("T", " ");
export const fullDate = seconds => chinaDate(seconds).slice(5);

class Calendar extends HTMLElement {
  connectedCallback() {
    if (this._ready) return;
    this._ready = true;
    this.value = this.getAttribute("value") || chinaDate(Date.now() / 1000 + 86400);
    this.month = this.value.slice(0, 7);
    this.opened = false;
    this.render();
    this.addEventListener("click", event => {
      if (event.target.closest(".date-trigger")) { this.opened = !this.opened; this.render(); }
      const nav = event.target.closest("[data-month]");
      if (nav) {
        const date = new Date(`${this.month}-01T00:00:00Z`);
        date.setUTCMonth(date.getUTCMonth() + Number(nav.dataset.month));
        this.month = date.toISOString().slice(0, 7);
        this.render();
      }
      const day = event.target.closest("[data-day]");
      if (day) { this.value = `${this.month}-${day.dataset.day.padStart(2, "0")} ${this.value.slice(11)}`; this.render(); this.dispatchEvent(new Event("change", { bubbles: true })); }
      if (event.target.closest(".date-done")) { this.opened = false; this.render(); this.querySelector(".date-trigger").focus(); }
    });
    this.addEventListener("change", event => {
      if (!event.target.matches("[data-time]")) return;
      const hour = Math.min(23, Math.max(0, Number(this.querySelector('[data-time="hour"]').value) || 0));
      const minute = Math.min(59, Math.max(0, Number(this.querySelector('[data-time="minute"]').value) || 0));
      this.value = `${this.value.slice(0, 10)} ${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
      event.target.value = String(event.target.dataset.time === "hour" ? hour : minute).padStart(2, "0");
      this.querySelector(".date-label").textContent = this.value;
    });
    this.addEventListener("keydown", event => {
      if (event.key === "Escape") { this.opened = false; this.render(); this.querySelector(".date-trigger").focus(); event.stopPropagation(); }
      if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key) && event.target.matches("[data-day]")) {
        event.preventDefault();
        const days = [...this.querySelectorAll("[data-day]")];
        const movement = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[event.key];
        days[Math.max(0, Math.min(days.length - 1, days.indexOf(event.target) + movement))]?.focus();
      }
    });
    this.outside = event => { if (this.opened && !event.composedPath().includes(this)) { this.opened = false; this.render(); } };
    document.addEventListener("click", this.outside);
  }
  disconnectedCallback() { document.removeEventListener("click", this.outside); }
  render() {
    const [year, month] = this.month.split("-").map(Number);
    const first = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
    const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
    this.innerHTML = `<button type="button" class="date-trigger" aria-haspopup="dialog" aria-expanded="${this.opened}">${icon("calendar")}<span class="date-label">${this.value}</span></button>${this.opened ? `<div class="calendar-popover" role="dialog" aria-label="选择北京时间"><div class="calendar-heading"><button type="button" data-month="-1" aria-label="上个月">‹</button><strong>${year} 年 ${month} 月</strong><button type="button" data-month="1" aria-label="下个月">›</button></div><div class="calendar-grid">${["日", "一", "二", "三", "四", "五", "六"].map(day => `<span>${day}</span>`).join("")}${'<span></span>'.repeat(first)}${Array.from({ length: days }, (_, index) => `<button type="button" data-day="${index + 1}" class="${this.value.slice(0, 10) === `${this.month}-${String(index + 1).padStart(2, "0")}` ? "selected" : ""}" aria-label="${month} 月 ${index + 1} 日">${index + 1}</button>`).join("")}</div><div class="time-editor"><span>北京时间</span><input type="text" inputmode="numeric" data-time="hour" aria-label="小时" maxlength="2" value="${this.value.slice(11, 13)}"/><span>:</span><input type="text" inputmode="numeric" data-time="minute" aria-label="分钟" maxlength="2" value="${this.value.slice(14, 16)}"/><button type="button" class="date-done btn btn-tonal">确定</button></div></div>` : ""}`;
  }
}
customElements.define("meow-calendar", Calendar);

let modalSequence = 0;
export function modal(title, subtitle, body, footer, { wide = false, onClose } = {}) {
  const root = document.getElementById("overlay-root");
  const previous = document.activeElement;
  const holder = document.createElement("div");
  const titleId = `meow-modal-title-${++modalSequence}`;
  holder.className = "modal-backdrop";
  holder.innerHTML = `<section class="modal ${wide ? "modal-wide" : ""}" role="dialog" aria-modal="true" aria-labelledby="${titleId}"><header class="modal-header"><div><p class="eyebrow">喵喵抽奖 / 管理工作台</p><h2 id="${titleId}" class="text-h3 pa-4 pb-0 pl-6">${escapeHTML(title)}</h2><p>${escapeHTML(subtitle)}</p></div><button class="icon-btn modal-close" aria-label="关闭">${icon("close")}</button></header><div class="modal-body">${body}</div>${footer ? `<footer class="modal-footer">${footer}</footer>` : ""}</section>`;
  root.append(holder);
  document.body.classList.add("has-modal");
  const close = () => { holder.remove(); if (!root.children.length) document.body.classList.remove("has-modal"); previous?.focus(); onClose?.(); };
  holder.querySelector(".modal-close").addEventListener("click", close);
  holder.addEventListener("click", event => { if (event.target === holder || event.target.closest('[data-action="dismiss"]')) close(); });
  holder.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
    if (event.key === "Tab") {
      const focusable = [...holder.querySelectorAll('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), [tabindex="0"]')].filter(element => element.offsetParent !== null);
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  holder.querySelector("input,button,textarea")?.focus();
  return { holder, close };
}

export function toast(message, type = "ok") {
  const root = document.getElementById("toast-root");
  const element = document.createElement("div");
  element.className = `toast toast-${type}`;
  element.innerHTML = `${icon(type === "ok" ? "check" : "paw")}<span>${escapeHTML(message)}</span>`;
  root.append(element);
  setTimeout(() => element.remove(), type === "error" ? 8000 : 4000);
}
