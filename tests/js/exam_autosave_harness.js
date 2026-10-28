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
    // multiple-ответ: три флажка с одним именем, выбраны А и В; радио-группа с невыбранным вариантом
    cbA: mk({ tag: "input", type: "checkbox", name: "a", value: "A", checked: true }),
    cbB: mk({ tag: "input", type: "checkbox", name: "a", value: "B", checked: false }),
    cbC: mk({ tag: "input", type: "checkbox", name: "a", value: "C", checked: true }),
    rdX: mk({ tag: "input", type: "radio", name: "r", value: "X", checked: false }),
    rdY: mk({ tag: "input", type: "radio", name: "r", value: "Y", checked: true }),
    // метка «ответ в форме есть»: скрытое поле, которое тоже выключается при закрытии бланка
    marker: mk({ tag: "input", type: "hidden", name: "answer_present", value: "1" }),
  };
  const fields = [hidden("attempt_id", "7"), hidden("rev", "5"), hidden("shown", "1"), hidden("action_id", "orig")];
  const all = [...fields, ...Object.values(controls)];
  // Как в браузере: new FormData(form) берёт поля ответа с именем и без disabled; кнопки без submitter не входят.
  class FakeFormData {
    constructor(form) {
      this.pairs = form.all
        .filter((f) => f.name && !f.disabled && f.tag !== "button" && (f.type !== "checkbox" && f.type !== "radio" || f.checked))
        .map((f) => [f.name, f.value]);
    }
    set(k, v) { const i = this.pairs.findIndex((x) => x[0] === k); this.pairs = this.pairs.filter((x) => x[0] !== k); this.pairs.splice(Math.max(i, 0), 0, [k, v]); }
    get(k) { const x = this.pairs.find((p) => p[0] === k); return x ? x[1] : null; }
    getAll(k) { return this.pairs.filter((p) => p[0] === k).map((p) => p[1]); }
  }
  const handlers = {};
  const notes = [];
  const form = {
    all, elements: {},
    addEventListener(t, fn) { (handlers[t] = handlers[t] || []).push(fn); },
    querySelectorAll(sel) {
      const base = "button, input:not([type=hidden]), select";
      if (sel !== base && sel !== base + ", input[name=answer_present]") throw new Error("селектор вне модели: " + sel);
      const withMarker = sel !== base;
      return all.filter((f) => f.tag === "button" || f.tag === "select" || (f.tag === "input" && f.type !== "hidden") || (withMarker && f.name === "answer_present"));
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
  let confirms = 0, reloads = 0;
  const timers = [], win = {};
  loc.reload = () => { reloads++; };
  const ctx = {
    document, fetch, URL, FormData: FakeFormData, Date: FakeDate, Math, String, Promise, console,
    setInterval: (fn) => { tick = fn; }, setTimeout: (fn) => { timers.push(fn); }, location: loc,
    confirm: () => { confirms++; return !opts.decline; }, window: { addEventListener(t, fn) { win[t] = fn; } },
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
  const clickOnly = (btn) => run("click", { target: btn }).defaultPrevented;
  const runTimers = () => { while (timers.length) timers.shift()(); };
  const pageshow = (persisted) => win.pageshow({ persisted });
  return { clickOnly, runTimers, pageshow, reloads: () => reloads, clock, controls, form, notes, posts, loc, at, press, forceSubmit, confirms: () => confirms };
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
    if (JSON.stringify(p.posts[0].data.getAll("answer_present")) !== '["1"]') fail("метка потеряна: " + JSON.stringify(p.posts[0].data.pairs));
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
    if (d.data.get("n") !== "28" || d.data.get("m.1") !== "B") fail("в POST нет ответа ребёнка: " + JSON.stringify(d.data.pairs));
    if (JSON.stringify(d.data.getAll("a")) !== '["A","C"]') fail("multiple-ответ в POST: " + JSON.stringify(d.data.pairs));
    if (JSON.stringify(d.data.getAll("r")) !== '["Y"]') fail("радио в POST: " + JSON.stringify(d.data.pairs));
    if (JSON.stringify(d.data.getAll("answer_present")) !== '["1"]') fail("метка «ответ в форме есть» в POST: " + JSON.stringify(d.data.pairs));
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
    if (p.posts.length !== 0) fail("POST " + p.posts.length);
    p.at(500); await flush();
    if (p.posts.length !== 0) fail("POST " + p.posts.length);
  },
  async handlers_compare_the_deadline_not_only_the_flag() {
    const p = build(ok());
    p.clock.t = DEADLINE - 1000;  // окно наступило, тик ещё не был
    if (p.press(p.controls.submit)) fail("клик прошёл в окне без тика");
    if (p.forceSubmit(p.controls.mark)) fail("submit прошёл в окне без тика");
    if (p.posts.length !== 1 || p.posts[0].data.get("action") !== "save") fail("POST " + p.posts.length);
    if (JSON.stringify(p.posts[0].data.getAll("answer_present")) !== '["1"]') fail("метка потеряна: " + JSON.stringify(p.posts[0].data.pairs));
  },
  async save_click_in_window_before_the_tick_keeps_the_answer() {
    const p = build(ok());
    p.clock.t = DEADLINE - 1450;  // окно наступило, тика ещё не было
    if (p.press(p.controls.save)) fail("«Сохранить» ушла как обычная отправка");
    p.at(1200); p.at(950); await flush();
    if (p.posts.length !== 1) fail("POST " + p.posts.length);
    const d = p.posts[0].data;
    if (d.get("n") !== "28" || JSON.stringify(d.getAll("a")) !== '["A","C"]') fail("ответ потерян: " + JSON.stringify(d.pairs));
    if (d.get("action") !== "save" || !String(d.get("action_id")).startsWith("auto-")) fail("не автосохранение");
    if (JSON.stringify(d.getAll("answer_present")) !== '["1"]') fail("метка потеряна у автосохранения: " + JSON.stringify(d.pairs));
    if (!Object.values(p.controls).every((c) => c.disabled)) fail("бланк не закрыт");
  },
  async normal_submit_then_window_tick_sends_no_autosave() {
    const p = build(ok());
    p.clock.t = DEADLINE - 1700;
    if (!p.press(p.controls.save)) fail("обычная отправка вне окна не прошла");
    p.runTimers();  // поля выключены после сбора формы браузером
    if (!Object.values(p.controls).every((c) => c.disabled)) fail("после отправки бланк не выключен");
    p.at(1400); p.at(1150); await flush();
    if (p.posts.length !== 0) fail("пустое автосохранение после обычной отправки: " + JSON.stringify(p.posts[0] && p.posts[0].data.pairs));
  },
  async normal_submit_disables_the_fields_after_collecting() {
    const p = build(ok());
    if (!p.press(p.controls.save)) fail("обычная отправка не прошла");
    if (p.controls.text.disabled) fail("поля выключены до сбора формы");
    p.runTimers();
    if (!Object.values(p.controls).every((c) => c.disabled)) fail("после отправки бланк не выключен");
  },
  async tick_after_the_deadline_sends_nothing() {
    const p = build(ok());
    p.at(5000);
    p.at(-100); await flush();  // вкладка «спала» и проснулась после срока
    if (p.posts.length !== 0) fail("POST после срока: " + p.posts.length);
    if (p.loc.href !== "/exam/results/7") fail("переход " + p.loc.href);
  },
  async submit_handler_alone_checks_the_deadline() {
    const p = build(ok());
    p.clock.t = DEADLINE - 1000;  // ни тика, ни клика: только submit (Enter, requestSubmit)
    if (p.forceSubmit(p.controls.mark)) fail("submit прошёл в окне");
  },
  async click_handler_alone_checks_the_deadline() {
    const p = build(ok());
    p.clock.t = DEADLINE - 1000;  // только click, без тика
    if (!p.clickOnly(p.controls.submit)) fail("click прошёл в окне");
    if (p.confirms()) fail("спросили подтверждение в окне");
  },
  async pageshow_from_cache_reloads() {
    const p = build(ok());
    p.pageshow(false);
    if (p.reloads()) fail("перезагрузка без bfcache");
    p.pageshow(true);
    if (p.reloads() !== 1) fail("страница из bfcache не перезагружена");
  },
  async submit_button_asks_for_confirmation() {
    const p = build(ok());
    if (!p.press(p.controls.submit)) fail("«Сдать» вне окна не прошла");
    if (p.confirms() !== 1) fail("подтверждений " + p.confirms());
    const q = build(ok(), { decline: true });
    if (q.press(q.controls.submit)) fail("«Сдать» ушла без согласия");
    if (q.confirms() !== 1) fail("подтверждений " + q.confirms());
    if (!q.press(q.controls.mark) || q.confirms() !== 1) fail("лишнее подтверждение у другой кнопки");
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
