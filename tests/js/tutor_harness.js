// Прогон app/static/tutor.js на минимальной модели DOM (без jsdom и npm): node tests/js/tutor_harness.js <имя>.
// Модель умеет ровно то, что нужно скрипту: теги с атрибутами, поля формы, отметки, таймеры и fetch из очереди.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(process.env.TUTOR_JS || path.join(__dirname, "..", "..", "app", "static", "tutor.js"), "utf8");

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

const page = { els: [], focused: null, timers: [], now: 0, fetches: [], handlers: {}, posts: [], postq: [], getq: [], getFn: null };

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
  fetch(url, opts) { page.posts.push({ url, method: (opts && opts.method) || "GET" }); const post = opts && opts.method === "POST";
    if (post && page.postq.length) return page.postq.shift()(url, opts);
    if (!post && page.getq.length) return page.getq.shift()(url, opts);
    if (!post && page.getFn) return page.getFn(url, opts);
    return page.fetches.shift()(url, opts);
  },
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
const ok_ = (fn) => () => Promise.resolve({ text: () => Promise.resolve(fn()), headers: { get: () => null } });
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
  // Версия сервера - единственное правило устаревания: опоздавший ответ (опрос) старее применённого не рисуется.
  async stale_poll_does_not_overwrite_newer_step() {
    boot(screen("thinking", ' data-step="3" data-version="3"'));
    const slow = deferred();
    page.fetches.push(() => slow.p);
    await advance(2600);  // опрос ушёл и висит
    page.fetches.push(ok(screen("thinking", ' data-step="5" data-version="5"')));
    page.handlers.click[0]({ target: { closest: () => get("button") } });
    await settle();
    if (get(".helper-status").attrs["data-step"] !== "5") throw new Error("new step not shown");
    slow.res({ text: () => Promise.resolve(screen("idle", ' data-version="4"')), headers: { get: () => null } });
    await settle();
    if (get(".helper-status").attrs["data-step"] !== "5") throw new Error("stale poll overwrote the newer step");
  },
  // Повтор после обрыва: опрос не перекрывает идущий POST, а потерянный ответ повтора не обрывает опрос.
  async lost_retry_reply_keeps_recovery_polling() {
    boot(screen("idle"));
    let server = screen("idle");
    page.getFn = ok_(() => server);
    const retry = deferred();
    page.postq.push(fail, () => retry.p);
    const click = () => page.handlers.click[0]({ target: { closest: () => get("button") } });
    click();  // первый POST обрывается до сервера
    await settle();
    click();  // ребёнок жмёт включённую кнопку до опроса: повтор идёт медленно
    await settle();
    await advance(2600);  // опрос в этот момент не должен перекрасить панель
    server = screen("thinking", ' data-step="4"');  // сервер принял повтор, но ответ потерян
    retry.res(Promise.reject(new TypeError("network")));
    retry.p.catch(() => {});
    await settle();
    for (let i = 0; i < 3; i++) await advance(2600);
    if (get(".helper-status").attrs["data-panel"] !== "thinking") throw new Error("started step never shown: " + get(".helper-status").attrs["data-panel"]);
  },
  // Codex 1: оба POST оборваны, сервер принял шаг позже срока от старта: опрос продлевается от сбоя.
  async failed_posts_extend_polling_deadline() {
    boot(screen("idle", ' data-version="0"'));
    let server = screen("idle", ' data-version="0"');
    page.getFn = ok_(() => server);
    const retry = deferred();
    page.postq.push(fail, () => retry.p);
    const click = () => page.handlers.click[0]({ target: { closest: () => get("button") } });
    click();
    await settle();
    click();  // кнопка включена после первого сбоя
    await settle();
    for (let i = 0; i < 19; i++) await advance(2600);  // ~49 с, опрос возвращает покой
    retry.res(Promise.reject(new TypeError("network")));
    retry.p.catch(() => {});
    await settle();
    for (let i = 0; i < 6; i++) await advance(2600);  // ~65 с: срок от старта вышел, от сбоя - нет
    server = screen("thinking", ' data-step="1" data-version="1"');  // сервер начал шаг позже
    for (let i = 0; i < 3; i++) await advance(2600);
    if (get(".helper-status").attrs["data-panel"] !== "thinking") throw new Error("late step never shown: " + get(".helper-status").attrs["data-panel"]);
  },
  // Codex 2: ранний POST ещё в пути, поздний уже ответил; сбой и опоздавший ответ опроса цикл не рвут.
  async loop_survives_failed_and_stale_polls_while_older_post_pends() {
    boot(screen("idle", ' data-version="4"'));
    const older = deferred();
    page.postq.push(() => older.p, ok(screen("thinking", ' data-step="6" data-version="6"')));
    page.getq.push(fail, ok(screen("thinking", ' data-step="5" data-version="5"')), ok(screen("idle", ' data-version="7"')));
    const click = () => page.handlers.click[0]({ target: { closest: () => get("button") } });
    click();  // старый POST: ответ задержан
    await settle();
    click();  // новый POST отвечает "думаю"
    await settle();
    if (get(".helper-status").attrs["data-version"] !== "6") throw new Error("newer POST not applied");
    for (let i = 0; i < 3; i++) await advance(2600);
    if (get(".helper-status").attrs["data-version"] !== "7") throw new Error("loop died: " + get(".helper-status").attrs["data-version"]);
  },
  // Codex 3: ответ на отметку ключа (старый прогон) опоздал к новому прогону того же шага: форму не затирает.
  async older_run_response_does_not_replace_new_run_form() {
    boot(screen("idle", ' data-version="9"'));
    const dispute = deferred();
    page.postq.push(() => dispute.p, ok(screen("idle", ' data-version="11"')));
    const click = () => page.handlers.click[0]({ target: { closest: () => get("button") } });
    click();  // «Кажется, ключ неверный»: ответ задержан
    await settle();
    click();  // «Разобрать ещё раз»: новый прогон
    await settle();
    if (get(".helper-status").attrs["data-version"] !== "11") throw new Error("new run not applied");
    page.els.find((e) => e.attrs.value === "Б").checked = true;  // ребёнок выбрал ответ
    dispute.res({ text: () => Promise.resolve(screen("idle", ' data-version="10"')), headers: { get: () => null } });
    await settle();
    if (get(".helper-status").attrs["data-version"] !== "11") throw new Error("old run response replaced the screen");
    if (checkedOf("answer").join() !== "Б") throw new Error("selected answer lost");
  },
  // Ответ на подсказку/вопрос (равная версия) не стирает ответ, который ребёнок ещё не отправил.
  async hint_response_keeps_unsent_answer() {
    boot(screen("idle", ' data-version="5"'));
    page.els.find((e) => e.attrs.value === "Б").checked = true;
    page.els.find((e) => e.attrs.name === "num").value = "42";
    page.postq.push(ok(screen("idle", ' data-version="5"')));
    page.handlers.click[0]({ target: { closest: () => get("button") } });  // data-act="hint"
    await settle();
    if (page.posts.length !== 1 || page.posts[0].method !== "POST") throw new Error("hint POST not sent");
    if (checkedOf("answer").join() !== "Б") throw new Error("option wiped by the hint response");
    if (page.els.find((e) => e.attrs.name === "num").value !== "42") throw new Error("number wiped");
  },
  // Ответ на отправку самой формы (attempt) форму сбрасывает, как раньше.
  async attempt_response_resets_the_form() {
    boot(screen("idle", ' data-version="5"'));
    page.els.find((e) => e.attrs.value === "Б").checked = true;
    page.postq.push(ok(screen("idle", ' data-version="6"')));
    const form = { getAttribute: (k) => (k === "data-act" ? "attempt" : "single"), querySelector: () => ({ value: "Б" }), querySelectorAll: () => [], preventDefault() {} };
    page.handlers.submit[0]({ target: form, preventDefault() {} });
    await settle();
    if (checkedOf("answer").length) throw new Error("form not reset after attempt");
  },
};

const name = process.argv[2];
if (!scenarios[name]) { console.error("unknown scenario " + name + "; have " + Object.keys(scenarios)); process.exit(2); }
scenarios[name]().then(() => { console.log("ok " + name); process.exit(0); }, (e) => { console.error("FAIL " + name + ": " + e.message); process.exit(1); });
