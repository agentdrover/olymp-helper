// Прогон скрипта app/templates/child/exam_run.html на минимальной модели DOM: node exam_run_harness.js <сценарий>.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(path.join(__dirname, "..", "..", "app", "templates", "child", "exam_run.html"), "utf8");
const SRC = /<script>([\s\S]*?)<\/script>/.exec(html)[1] + "\n;globalThis.__exam = () => exam;";

function make() {
  const page = { clicks: [], posts: [], input: {value: ""}, els: {}, nav: ""};
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
  const state = n => ({
    id: 1, status: "running", title: "В", stage: "школьный", current: 1, server_now: "2026-01-01T00:00:00+00:00",
    deadline_at: "2099-01-01T00:00:00+00:00",
    items: [{position: 1, type: "numeric", number: "1", text: "Сколько?", options: null, figure: null, answer: n, marked: false}],
  });
  const deferred = [];
  const fetch = async (url, opts) => {
    if (!opts) return {ok: true, json: async () => state(null)};
    const body = JSON.parse(opts.body);
    if (url === "/exam/open") return {ok: true, json: async () => ({exam: state(null)})};
    const d = {url, body};
    d.promise = new Promise(res => { d.resolve = v => res({ok: true, json: async () => v}); });
    deferred.push(d);
    page.posts.push(d);
    return d.promise;
  };
  const ctx = {document, fetch, location: {}, confirm: () => true, setInterval() {}, setTimeout, Date, JSON, Promise, console, String, Object, Math};
  vm.runInNewContext(SRC, ctx);
  return {page, ctx, deferred};
}
const tick = () => new Promise(r => setTimeout(r, 5));
const click = (page, id) => page.click({target: {id, dataset: {}}});
const fail = msg => { console.error("FAIL: " + msg); process.exit(1); };

const scenarios = {
  async save_reply_keeps_newer_input() {
    const {page, ctx, deferred} = make();
    await tick();
    page.input.value = "28"; click(page, "exam-save"); await tick();
    page.input.value = "29";
    deferred[0].resolve({exam: {...ctx.__exam(), items: [{...ctx.__exam().items[0], answer: "28"}]}});
    await tick();
    if (page.input.value !== "29") fail("поле затёрто ответом сервера: " + page.input.value);
  },
  async out_of_order_replies_keep_the_last_request() {
    const {page, ctx, deferred} = make();
    await tick();
    const withAnswer = a => ({exam: {...ctx.__exam(), items: [{...ctx.__exam().items[0], answer: a}]}});
    page.input.value = "28"; click(page, "exam-save"); await tick();
    page.input.value = "29"; click(page, "exam-save"); await tick();
    deferred[1].resolve(withAnswer("29")); await tick();
    deferred[0].resolve(withAnswer("28")); await tick();
    if (page.input.value !== "29") fail("устаревший ответ перерисовал поле: " + page.input.value);
    if (ctx.__exam().items[0].answer !== "29") fail("устаревший ответ подменил состояние: " + ctx.__exam().items[0].answer);
  },
  async single_reply_still_renders() {
    const {page, ctx, deferred} = make();
    await tick();
    page.input.value = "28"; click(page, "exam-save"); await tick();
    deferred[0].resolve({exam: {...ctx.__exam(), items: [{...ctx.__exam().items[0], answer: "28"}]}});
    await tick();
    if (ctx.__exam().items[0].answer !== "28") fail("ответ сервера не принят");
  },
};
scenarios[process.argv[2]]().then(() => process.exit(0), e => fail(String(e && e.stack || e)));
