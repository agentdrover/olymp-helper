// Прогон app/static/tutor.js на минимальной модели DOM (без jsdom и npm): node tests/js/tutor_harness.js <имя>.
// Модель умеет ровно то, что нужно скрипту: теги с атрибутами, поля формы, отметки, таймеры и fetch из очереди.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "..", "app", "static", "tutor.js"), "utf8");

function parse(html) {
  const els = [];
  let form = false, helper = false;
  const re = /<(\/?)(\w+)([^>]*)>/g;
  let m;
  while ((m = re.exec(html))) {
    if (m[1]) {
      if (m[2] === "form") form = false;
      if (m[2] === "aside") helper = false;
      continue;
    }
    const attrs = {};
    m[3].replace(/([\w-]+)(?:="([^"]*)")?/g, (_, k, v) => { attrs[k] = v === undefined ? "" : v; return ""; });
    if (m[2] === "form" && /answer-form/.test(attrs["class"] || "")) form = true;
    if (m[2] === "aside") helper = true;
    const el = {
      tag: m[2], attrs, inForm: form, inHelper: helper, textContent: "",
      value: attrs.value || "", checked: "checked" in attrs, disabled: "disabled" in attrs, hidden: "hidden" in attrs,
      classList: { toggle() {} },
      getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      hasAttribute(k) { return k in this.attrs; },
      focus() { page.focused = this; },
      closest() { return this; },
    };
    if (m[2] === "select") el.value = "";
    if (m[2] === "input" && attrs.type === "text") el.value = "";
    els.push(el);
  }
  return els;
}

function matchOne(el, part) {
  part = part.trim();
  let rest = part;
  const anc = /^\.(answer-form|helper) /.exec(rest);
  if (anc) {
    if (anc[1] === "answer-form" && !el.inForm) return false;
    if (anc[1] === "helper" && !el.inHelper) return false;
    rest = rest.slice(anc[0].length);
  }
  const tag = /^\w+/.exec(rest);
  if (tag) { if (el.tag !== tag[0]) return false; rest = rest.slice(tag[0].length); }
  const re = /\.([\w-]+)|\[([\w-]+)(?:=(?:'([^']*)'|([^\]]*)))?\]/g;
  let t;
  while ((t = re.exec(rest))) {
    if (t[1]) { if (!(el.attrs["class"] || "").split(/\s+/).includes(t[1])) return false; }
    else if (t[3] !== undefined || t[4] !== undefined) { if (el.attrs[t[2]] !== (t[3] !== undefined ? t[3] : t[4])) return false; }
    else if (!(t[2] in el.attrs)) return false;
  }
  return true;
}

const page = { els: [], focused: null, timers: [], now: 0, fetches: [], handlers: {}, posts: [] };

const root = {
  getAttribute(k) { return k === "data-url" ? "/tasks/1" : null; },
  set innerHTML(h) { page.els = parse(h); page.focused = null; },
  get innerHTML() { return ""; },
  querySelectorAll(sel) { return page.els.filter((e) => sel.split(",").some((p) => matchOne(e, p))); },
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
  addEventListener(type, fn) { (page.handlers[type] = page.handlers[type] || []).push(fn); },
};
root.querySelectorAll.bind(root);
const document = {
  getElementById(id) { return id === "tutor" ? root : page.els.find((e) => e.attrs.id === id) || null; },
  addEventListener() {},
  get activeElement() { return page.focused; },
};

let seq = 0;
const sandbox = {
  document, window: { isSecureContext: false }, console, parseInt, String, Array, Promise, JSON, Math, Date: { now: () => page.now },
  setTimeout(fn, ms) { const id = ++seq; page.timers.push({ id, fn, at: page.now + ms }); return id; },
  clearTimeout(id) { page.timers = page.timers.filter((t) => t.id !== id); },
  fetch(url, opts) { page.posts.push({ url, method: (opts && opts.method) || "GET" }); return page.fetches.shift()(url, opts); },
  navigator: {}, encodeURIComponent,
};

const tick = () => new Promise((r) => setImmediate(r));
async function settle() { for (let i = 0; i < 5; i++) await tick(); }
async function advance(ms) {
  page.now += ms;
  const due = page.timers.filter((t) => t.at <= page.now).sort((a, b) => a.at - b.at);
  page.timers = page.timers.filter((t) => t.at > page.now);
  due.forEach((t) => t.fn());
  await settle();
}
const ok = (html) => () => Promise.resolve({ text: () => Promise.resolve(html), headers: { get: () => null } });
const fail = () => Promise.reject(new TypeError("network"));
const deferred = () => { let res; const p = new Promise((r) => { res = r; }); return { p, res }; };

const FORM = '<form class="answer-form" data-act="attempt" data-type="single"><input type="radio" name="answer" value="А"><input type="radio" name="answer" value="Б">' +
  '<input type="text" name="num"><select name="pair-1"><option value="">выбери</option></select>' +
  '<input type="radio" name="confidence" value="sure"><input type="radio" name="confidence" value="doubt"></form>';
const panel = (state, extra) => '<aside class="helper"><div class="helper-status" data-panel="' + state + '"' + (extra || "") + '>t</div>' +
  '<button type="button" data-act="hint" data-level="1">Подсказка 1</button></aside>';
const screen = (state, extra) => FORM + panel(state, extra);
const get = (sel) => root.querySelector(sel);
const checkedOf = (name) => page.els.filter((e) => e.attrs.name === name && e.checked).map((e) => e.value);

function boot(html) {
  root.innerHTML = html;
  vm.runInContext(SRC, vm.createContext(sandbox));
}

const scenarios = {
  // Опрос во время «думаю» не стирает несохранённый ответ ребёнка.
  async poll_keeps_unsent_answer() {
    boot(screen("thinking", ' data-step="3"'));
    page.els.find((e) => e.attrs.value === "Б").checked = true;
    page.els.find((e) => e.attrs.value === "doubt").checked = true;
    page.els.find((e) => e.attrs.name === "num").value = "42";
    page.els.find((e) => e.tag === "select").value = "7";
    page.fetches.push(ok(screen("thinking", ' data-step="3"')));
    await advance(2600);
    const sel = page.els.find((e) => e.tag === "select");
    if (page.posts.length !== 1) throw new Error("poll did not run: " + page.posts.length);
    if (checkedOf("answer").join() !== "Б") throw new Error("option lost: " + checkedOf("answer"));
    if (checkedOf("confidence").join() !== "doubt") throw new Error("confidence lost");
    if (page.els.find((e) => e.attrs.name === "num").value !== "42") throw new Error("number lost");
    if (sel.value !== "7") throw new Error("select lost");
  },
  // Последний опрос (шаг готов) тоже не стирает то, что ребёнок успел выбрать.
  async final_poll_keeps_unsent_answer() {
    boot(screen("thinking", ' data-step="3"'));
    page.els.find((e) => e.attrs.value === "А").checked = true;
    page.fetches.push(ok(screen("idle")));
    await advance(2600);
    if (get(".helper-status").attrs["data-panel"] !== "idle") throw new Error("not swapped");
    if (checkedOf("answer").join() !== "А") throw new Error("option lost on final poll");
  },
  // После обрыва POST кнопки включены, опрос идёт; шаг, который сервер начал, показывается.
  async post_failure_polls_and_shows_started_step() {
    boot(screen("idle"));
    page.fetches.push(fail, ok(screen("thinking", ' data-step="4"')), ok(screen("idle")));
    page.handlers.click[0]({ target: { closest: () => get("button") } });
    await settle();
    if (get("button").disabled) throw new Error("buttons stay disabled after failed POST");
    await advance(2600);
    if (page.posts.length < 2 || page.posts[1].method !== "GET") throw new Error("no poll after failed POST: " + JSON.stringify(page.posts));
    if (get(".helper-status").attrs["data-panel"] !== "thinking") throw new Error("started step not shown");
    await advance(2600);
    if (get(".helper-status").attrs["data-panel"] !== "idle" || get("button").disabled) throw new Error("did not return to idle");
  },
  // Сервер ничего не начал: опрос возвращает панель в покой с включёнными кнопками.
  async post_failure_without_step_returns_to_idle() {
    boot(screen("idle"));
    page.fetches.push(fail, ok(screen("idle")));
    page.handlers.click[0]({ target: { closest: () => get("button") } });
    await settle();
    await advance(2600);
    if (get(".helper-status").attrs["data-panel"] !== "idle" || get("button").disabled) throw new Error("not idle/enabled");
  },
  // Опоздавший ответ опроса не затирает экран после новой отправки (поколение запросов).
  async stale_poll_does_not_overwrite_newer_step() {
    boot(screen("thinking", ' data-step="3"'));
    const slow = deferred();
    page.fetches.push(() => slow.p);
    await advance(2600);  // опрос ушёл и висит
    page.fetches.push(ok(screen("thinking", ' data-step="5"')));
    page.handlers.click[0]({ target: { closest: () => get("button") } });
    await settle();
    if (get(".helper-status").attrs["data-step"] !== "5") throw new Error("new step not shown");
    slow.res({ text: () => Promise.resolve(screen("idle")), headers: { get: () => null } });
    await settle();
    if (get(".helper-status").attrs["data-step"] !== "5") throw new Error("stale poll overwrote the newer step");
  },
};

const name = process.argv[2];
if (!scenarios[name]) { console.error("unknown scenario " + name + "; have " + Object.keys(scenarios)); process.exit(2); }
scenarios[name]().then(() => { console.log("ok " + name); process.exit(0); }, (e) => { console.error("FAIL " + name + ": " + e.message); process.exit(1); });
