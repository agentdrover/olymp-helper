// Цепочка key -> figure -> confirm из app/static/feed.js на минимальной модели DOM:
// node tests/js/feed_chain_harness.js <сценарий>. Проверяет, что она идёт по снимку карточки.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "..", "app", "static", "feed.js"), "utf8");
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

function build(extra) {
  const calls = [];
  const gates = {};
  const handlers = {};
  const cardEl = { dataset: { taskId: "1", counter: "1 из 2", figurePage: "1" } };
  const mk = (extra) => Object.assign({ disabled: false, dataset: {}, value: "", checked: false }, extra);
  const figureBtn = mk({ dataset: { action: "figure" } });
  const skipBtn = mk({ dataset: { action: "skip" } });
  const fields = {
    alt: mk({ value: "Подпись" }), key_text: mk({ value: "Б" }), note_free: mk({ value: "" }),
    note_ack: mk({ checked: false }), leak_ack: mk({ checked: false }), error: mk({}),
  };
  const box = { dataset: { candidate: "0", current: "" }, querySelectorAll: () => [] };
  const feed = {
    set innerHTML(h) { feed.html = h; }, get innerHTML() { return feed.html; },
    querySelector(sel) { return sel === ".card" ? cardEl : sel === ".key-fields" ? box : sel === "[data-figure-img]" && extra ? extra.img : null; },
    querySelectorAll(sel) { return sel === "button" ? [figureBtn, skipBtn] : []; },
    addEventListener(t, fn) { (handlers[t] = handlers[t] || []).push(fn); },
  };
  const document = {
    getElementById(id) { return id === "feed" ? feed : id === "counter" ? { textContent: "" } : id === "page-img" && extra ? extra.page : id === "draft-frame" ? null : fields[id] || null; },
    createElement() { return { style: {} }; },
    querySelector(sel) { return sel.includes("leak") ? { value: "none" } : null; },
    addEventListener() {},
  };
  const replies = {};
  const bodies = [];
  const fetch = (url, opts) => {
    calls.push(url);
    const hit = Object.keys(replies).find((g) => url.endsWith(g));
    const body = hit ? replies[hit] : { status: "draft", next: null };
    bodies.push(opts && opts.body);
    const reply = () => ({ ok: true, redirected: false, status: 200, json: async () => body, text: async () => "" });
    const gate = Object.keys(gates).find((g) => url.endsWith(g));
    return gate ? gates[gate].then(reply) : Promise.resolve(reply());
  };
  const ctx = { document, fetch, location: {}, Number, JSON, Array, URLSearchParams, Date, setTimeout, clearTimeout, console, Error, Promise };
  vm.runInNewContext(SRC, ctx);
  const click = (btn) => { if (!btn.disabled) return handlers.click[0]({ target: { closest: (s) => (s.startsWith("button") ? btn : null) } }); };
  return { calls, gates, replies, bodies, cardEl, figureBtn, skipBtn, click, feed, document, handlers };
}

const tick = () => new Promise((r) => setTimeout(r, 5));

const scenarios = {
  async skip_during_delayed_key() {
    const p = build();
    let release;
    p.gates["/1/key"] = new Promise((r) => { release = r; });
    p.click(p.figureBtn);
    await tick();
    if (!p.skipBtn.disabled) fail("кнопка «Пропустить» не отключена на время цепочки");
    p.click(p.skipBtn);  // отключённую кнопку браузер не нажимает
    p.cardEl.dataset.taskId = "2";  // карточка успела смениться на B
    release();
    await tick(); await tick();
    const posts = p.calls.filter((u) => u.includes("/parent/tasks/"));
    const want = ["/parent/tasks/1/key", "/parent/tasks/1/figure", "/parent/tasks/1/confirm"];
    if (JSON.stringify(posts) !== JSON.stringify(want)) fail("вызовы " + JSON.stringify(posts));
  },
};

scenarios.publish_moves_to_next_card = async function () {
  const p = build();
  p.replies["/1/confirm"] = { next: { task_id: 2 }, counter: "2 из 2" };
  p.click(p.figureBtn);
  await tick(); await tick(); await tick();
  if (!String(p.bodies[0]).includes('"keep_candidate":true')) fail("ключ без keep_candidate: " + p.bodies[0]);
  const shown = p.calls.filter((u) => u.includes("/parent/review/"));
  if (JSON.stringify(shown) !== JSON.stringify(["/parent/review/2?fragment=1"])) fail("показано " + JSON.stringify(shown));
};

scenarios.frame_fills_empty_preview = async function () {
  const listeners = {};
  const img = { src: "", hidden: true, alt: "", dataset: {}, parentElement: null };
  const figure = { hidden: true, querySelector: () => img };
  img.closest = (s) => (s === "figure" ? figure : null);
  const page = {
    naturalWidth: 900, naturalHeight: 1200, parentElement: { appendChild() {} },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 900, height: 1200 }),
    addEventListener(t, fn) { listeners[t] = fn; },
  };
  const p = build({ page, img });
  listeners.mousedown({ preventDefault() {}, clientX: 100, clientY: 100 });
  listeners.mouseup({ clientX: 400, clientY: 500 });
  if (!img.src.includes("figure-preview.png") || !img.src.includes("rect_px=")) fail("предпросмотр не запрошен: " + img.src);
  if (figure.hidden || img.hidden) fail("место под вырезку осталось скрытым");
};

const name = process.argv[2];
if (!scenarios[name]) fail("нет сценария " + name);
scenarios[name]().then(() => process.exit(0), (e) => fail(String(e)));
