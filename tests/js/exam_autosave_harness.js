// Скрипт бланка из app/templates/child/exam_run.html на минимальной модели DOM:
// node tests/js/exam_autosave_harness.js <сценарий> [путь к шаблону]. Исполняет скрипт, а не ищет в нём подстроки.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const TEMPLATE = process.argv[3] || path.join(__dirname, "..", "..", "app", "templates", "child", "exam_run.html");
const html = fs.readFileSync(TEMPLATE, "utf8");
const SRC = html.slice(html.indexOf("<script>") + 8, html.indexOf("</script>")).split("{{ v.id }}").join("7");
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

const DEADLINE = Date.parse("2026-01-01T10:10:00Z");

function build(reply, opts = {}) {
  const clock = { t: DEADLINE - (opts.left || 600000) };
  class FakeDate extends Date { static now() { return clock.t; } }
  const mk = (extra) => Object.assign({ disabled: false, id: "", dataset: {}, tag: "button", type: "submit" }, extra);
  const hidden = (name, value) => mk({ tag: "input", type: "hidden", name, value });
  const controls = {
    open: mk({ name: "action", value: "open:2" }), save: mk({ name: "action", value: "save" }),
    mark: mk({ name: "action", value: "mark" }), submit: mk({ name: "action", value: "submit", id: "exam-submit" }),
    text: mk({ tag: "input", type: "text", name: "n", value: "28" }),
    pick: mk({ tag: "select", type: "select-one", name: "m.1", value: "B" }),
  };
  const fields = [hidden("attempt_id", "7"), hidden("rev", "5"), hidden("shown", "1"), hidden("action_id", "orig")];
  const all = [...fields, ...Object.values(controls)];
  // Как в браузере: new FormData(form) берёт поля ответа с именем и без disabled; кнопки без submitter не входят.
  class FakeFormData {
    constructor(form) { this.map = new Map(form.all.filter((f) => f.name && !f.disabled && f.tag !== "button").map((f) => [f.name, f.value])); }
    set(k, v) { this.map.set(k, v); }
    get(k) { return this.map.get(k); }
  }
  const SELECTOR = "button, input:not([type=hidden]), select";
  const handlers = {};
  const notes = [];
  const form = {
    all, elements: {},
    addEventListener(t, fn) { (handlers[t] = handlers[t] || []).push(fn); },
    querySelectorAll(sel) {
      if (sel !== SELECTOR) throw new Error("селектор вне модели: " + sel);
      return all.filter((f) => f.tag === "button" || f.tag === "select" || (f.tag === "input" && f.type !== "hidden"));
    },
    before(n) { notes.push(n); },
  };
  for (const f of fields) form.elements[f.name] = f;
  const left = { dataset: { deadline: new Date(DEADLINE).toISOString(), now: new Date(clock.t).toISOString() }, textContent: "" };
  const document = {
    getElementById: (id) => (id === "exam-left" ? left : id === "exam-form" ? form : null),
    createElement: () => ({ attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } }),
  };
  const posts = [];
  const fetch = (url, opts) => { posts.push({ url, data: opts.body }); return reply(); };
  let tick = null;
  const loc = { href: "" };
  let confirms = 0;
  const ctx = {
    document, fetch, URL, FormData: FakeFormData, Date: FakeDate, Math, String, Promise, console,
    setInterval: (fn) => { tick = fn; }, setTimeout: (fn) => fn(), location: loc,
    confirm: () => { confirms++; return true; }, window: { addEventListener() {} },
  };
  vm.runInNewContext(SRC, ctx);
  const at = (msLeft) => { clock.t = DEADLINE - msLeft; tick(); };
  const run = (type, ev) => {
    ev.defaultPrevented = false; ev.preventDefault = () => { ev.defaultPrevented = true; }; ev.stopImmediatePropagation = () => { ev.stop = true; };
    for (const h of handlers[type] || []) { if (ev.stop) break; h(ev); }
    return ev;
  };
  // Нажатие кнопки: выключенную браузер не нажимает; click, затем submit; true - форма ушла бы на сервер.
  const press = (btn) => {
    if (btn.disabled) return false;
    if (run("click", { target: btn }).defaultPrevented) return false;
    return !run("submit", { submitter: btn }).defaultPrevented;
  };
  // Обход выключенной кнопки (Enter, requestSubmit): только submit.
  const forceSubmit = (btn) => !run("submit", { submitter: btn }).defaultPrevented;
  return { clock, controls, form, notes, posts, loc, at, press, forceSubmit, confirms: () => confirms };
}

const ok = (body) => () => Promise.resolve({
  ok: true, status: 200, redirected: true, url: "http://x/exam/run",
  text: async () => '<input name="attempt_id" value="7"><input name="rev" value="99">',
});
const toResults = () => Promise.resolve({ ok: true, status: 200, redirected: true, url: "http://x/exam/results/7", text: async () => "" });
const conflict = () => Promise.resolve({ ok: false, status: 409, redirected: false, text: () => Promise.reject(new Error("body lost")) });
const flush = () => new Promise((r) => setTimeout(r, 5));
const alerts = (p) => p.notes.filter((n) => n.attrs.role === "alert");

const scenarios = {
  async closed_after_window_sends_nothing() {
    const p = build(ok());
    p.at(5000);
    if (p.posts.length) fail("POST раньше окна");
    p.at(1400); await flush();
    if (p.posts.length !== 1) fail("автосохранений " + p.posts.length);
    if (!Object.values(p.controls).every((c) => c.disabled)) fail("бланк не закрыт");
    if (p.press(p.controls.submit) || p.press(p.controls.save)) fail("кнопка отправила форму");
    if (p.forceSubmit(p.controls.submit) || p.forceSubmit(p.controls.mark) || p.forceSubmit(p.controls.save)) fail("обход кнопок отправил форму");
    p.controls.submit.disabled = false;
    if (p.press(p.controls.submit)) fail("«Сдать» отправила форму после окна");
    if (p.confirms()) fail("спросили подтверждение у закрытого бланка");
    p.at(1000); p.at(500); await flush();
    if (p.posts.length !== 1) fail("повторный POST: " + p.posts.length);
  },
  async stale_409_with_broken_body_shows_alert_and_sends_nothing() {
    const p = build(conflict);
    p.at(1400); await flush();
    if (alerts(p).length !== 1 || !alerts(p)[0].textContent.includes("могли не сохраниться")) fail("нет сообщения: " + JSON.stringify(p.notes));
    if (p.forceSubmit(p.controls.submit) || p.press(p.controls.submit)) fail("«Сдать» ушла после 409");
    await flush();
    if (p.posts.length !== 1) fail("POST после 409: " + p.posts.length);
  },
  async network_error_shows_alert() {
    const p = build(() => Promise.reject(new Error("offline")));
    p.at(1400); await flush();
    if (alerts(p).length !== 1) fail("нет сообщения о сбое сети");
    if (p.forceSubmit(p.controls.submit)) fail("«Сдать» ушла после сбоя");
    if (p.posts.length !== 1) fail("POST " + p.posts.length);
  },
  async success_one_save_and_form_unchanged() {
    const p = build(ok());
    p.at(1400); await flush();
    if (p.posts.length !== 1) fail("POST " + p.posts.length);
    const d = p.posts[0];
    if (d.url !== "/exam/act" || d.data.get("action") !== "save") fail("не save: " + d.data.get("action"));
    if (!String(d.data.get("action_id")).startsWith("auto-")) fail("action_id " + d.data.get("action_id"));
    if (d.data.get("rev") !== "5") fail("rev в запросе " + d.data.get("rev"));
    if (d.data.get("n") !== "28" || d.data.get("m.1") !== "B") fail("в POST нет ответа ребёнка: " + JSON.stringify([...d.data.map]));
    if (p.form.elements.rev.value !== "5") fail("rev в форме изменился: " + p.form.elements.rev.value);
    if (alerts(p).length || !p.notes.some((n) => n.textContent === "Ответы сохранены")) fail("сообщение об успехе: " + JSON.stringify(p.notes));
    if (p.loc.href) fail("ушли со страницы до срока");
  },
  async redirect_to_results_is_not_a_saved_answer() {
    const p = build(toResults);
    p.at(1400); await flush();
    if (alerts(p).length !== 1) fail("ответ после срока выдан за сохранённый: " + JSON.stringify(p.notes));
    if (p.notes.some((n) => n.textContent === "Ответы сохранены")) fail("ложное «Ответы сохранены»");
  },
  async page_opened_inside_the_window_is_closed_at_once() {
    const p = build(ok(), { left: 1000 });  // ни одного тика ещё не было
    if (!Object.values(p.controls).every((c) => c.disabled)) fail("бланк открыт до первого тика");
    if (p.forceSubmit(p.controls.submit) || p.forceSubmit(p.controls.save)) fail("отправка до первого тика");
    p.at(900); await flush();
    if (p.posts.length > 1) fail("POST " + p.posts.length);
    p.at(500); await flush();
    if (p.posts.length > 1) fail("POST " + p.posts.length);
  },
  async handlers_compare_the_deadline_not_only_the_flag() {
    const p = build(ok());
    p.clock.t = DEADLINE - 1000;  // окно наступило, тик ещё не был
    if (p.press(p.controls.submit)) fail("клик прошёл в окне без тика");
    if (p.forceSubmit(p.controls.mark)) fail("submit прошёл в окне без тика");
    if (p.posts.length) fail("POST " + p.posts.length);
  },
  async deadline_goes_to_results() {
    const p = build(ok());
    p.at(1400); await flush();
    p.at(250);
    if (p.loc.href) fail("ушли до срока");
    p.at(0);
    if (p.loc.href !== "/exam/results/7") fail("переход " + p.loc.href);
    if (p.posts.length !== 1) fail("POST " + p.posts.length);
  },
};

const name = process.argv[2];
if (!scenarios[name]) fail("нет сценария " + name);
scenarios[name]().then(() => process.exit(0), (e) => fail(String(e && e.stack || e)));
