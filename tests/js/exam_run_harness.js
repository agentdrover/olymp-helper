// Прогон скрипта app/templates/child/exam_run.html на минимальной модели DOM: node exam_run_harness.js <сценарий>.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(path.join(__dirname, "..", "..", "app", "templates", "child", "exam_run.html"), "utf8");
const SRC = /<script>([\s\S]*?)<\/script>/.exec(html)[1] + "\n;globalThis.__exam = () => exam;";

function make() {
  const page = {posts: [], input: {value: ""}, els: {}};
  const el = id => page.els[id] || (page.els[id] = {
    id, textContent: "", dataset: {},
    set innerHTML(h) {
      this._h = h;
      if (id === "exam-task") { const m = /name="n"[^>]*value="([^"]*)"/.exec(h); page.input.value = m ? m[1] : ""; }
    },
    get innerHTML() { return this._h || ""; },
    querySelector(sel) { return sel === "input[name=n]" ? page.input : null; },
    querySelectorAll() { return []; },
  });
  const document = {getElementById: el, addEventListener(t, fn) { if (t === "click") page.click = fn; }};
  const view = (answers, marked = [false, false]) => ({
    id: 1, status: "running", title: "В", stage: "школьный", current: 1, server_now: "2026-01-01T00:00:00+00:00",
    deadline_at: "2099-01-01T00:00:00+00:00",
    items: answers.map((a, i) => ({position: i + 1, type: "numeric", number: String(i + 1), text: "Сколько?", options: null, figure: null, answer: a, marked: marked[i]})),
  });
  let startup = true;
  const fetch = async (url, opts) => {
    if (!opts) return {ok: true, json: async () => view([null, null])};
    if (startup && url === "/exam/open") { startup = false; return {ok: true, json: async () => ({exam: view([null, null])})}; }
    const d = {url, body: JSON.parse(opts.body)};
    d.promise = new Promise(res => { d.resolve = v => res({ok: true, json: async () => v}); });
    page.posts.push(d);
    return d.promise;
  };
  const ctx = {document, fetch, location: {}, confirm: () => true, setInterval() {}, setTimeout, Date, JSON, Promise, console, String, Object, Math};
  vm.runInNewContext(SRC, ctx);
  return {page, ctx, view, d: i => page.posts[i]};
}
const tick = () => new Promise(r => setTimeout(r, 5));
const click = (page, id, p) => page.click({target: {id: id || "", dataset: p ? {p: String(p)} : {}}});
const fail = msg => { console.error("FAIL: " + msg); process.exit(1); };
const ok = (v, want, what) => { if (v !== want) fail(what + ": " + JSON.stringify(v) + " вместо " + JSON.stringify(want)); };
const R = (exam) => ({exam});

const scenarios = {
  async save_reply_keeps_newer_input() {
    const {page, view, d} = make();
    await tick();
    page.input.value = "28"; click(page, "exam-save"); await tick();
    page.input.value = "29";
    d(0).resolve(R(view(["28", null])));
    await tick();
    ok(page.input.value, "29", "поле затёрто ответом сохранения");
  },
  async out_of_order_replies_keep_the_last_request() {
    const {page, ctx, view, d} = make();
    await tick();
    page.input.value = "28"; click(page, "exam-save"); await tick();
    page.input.value = "29"; click(page, "exam-save"); await tick();
    d(1).resolve(R(view(["29", null]))); await tick();
    d(0).resolve(R(view(["28", null]))); await tick();
    ok(page.input.value, "29", "устаревший ответ перерисовал поле");
    ok(ctx.__exam().items[0].answer, "29", "устаревший ответ подменил состояние");
  },
  async single_reply_still_renders() {
    const {page, ctx, view, d} = make();
    await tick();
    page.input.value = "28"; click(page, "exam-save"); await tick();
    d(0).resolve(R(view(["28", null])));
    await tick();
    ok(ctx.__exam().items[0].answer, "28", "ответ сервера не принят");
  },
  async mark_reply_keeps_newer_input() {
    // 28 сохранено; набрали 30, «Вернусь»; пока сохранение в пути - 31; затем пока в пути отметка - 32.
    const {page, ctx, view, d} = make();
    await tick();
    page.input.value = "28"; click(page, "exam-save"); await tick();
    d(0).resolve(R(view(["28", null]))); await tick();
    page.input.value = "30"; click(page, "exam-mark"); await tick();
    ok(d(1).url, "/exam/answer", "перед отметкой уходит сохранение");
    page.input.value = "31";
    d(1).resolve(R(view(["30", null]))); await tick();
    ok(page.input.value, "31", "ответ сохранения затёр ввод");
    ok(d(2).url + d(2).body.answer, "/exam/answer31", "правка во время сохранения не ушла на сервер");
    d(2).resolve(R(view(["31", null]))); await tick();
    ok(d(3).url, "/exam/mark", "отметка не отправлена");
    page.input.value = "32";
    d(3).resolve(R(view(["31", null], [true, false]))); await tick();
    ok(page.input.value, "32", "ответ отметки затёр ввод");
    ok(ctx.__exam().items[0].marked, true, "отметка не применена");
    ok(ctx.__exam().items[0].answer, "31", "состояние сервера не принято");
  },
  async navigation_reply_keeps_newer_input() {
    // набрали 30, перешли на задание 2; пока «открыть» в пути - 31: остаёмся на задании 1 с 31.
    const {page, ctx, view, d} = make();
    await tick();
    page.input.value = "30"; click(page, "", 2); await tick();
    d(0).resolve(R(view(["30", null]))); await tick();
    ok(d(1).url, "/exam/open", "переход не отправлен");
    page.input.value = "31";
    d(1).resolve(R(view(["30", "7"]))); await tick();
    ok(page.input.value, "31", "ответ перехода затёр ввод");
  },
  async navigation_without_edit_switches() {
    const {page, view, d} = make();
    await tick();
    page.input.value = "30"; click(page, "", 2); await tick();
    d(0).resolve(R(view(["30", "7"]))); await tick();
    d(1).resolve(R(view(["30", "7"]))); await tick();
    ok(page.input.value, "7", "переход без правки не показал задание 2");
  },
  async mark_without_edit_renders() {
    const {page, ctx, view, d} = make();
    await tick();
    click(page, "exam-mark"); await tick();
    d(0).resolve(R(view([null, null], [true, false]))); await tick();
    ok(ctx.__exam().items[0].marked, true, "отметка не применена");
  },
};
scenarios[process.argv[2]]().then(() => process.exit(0), e => fail(String(e && e.stack || e)));
