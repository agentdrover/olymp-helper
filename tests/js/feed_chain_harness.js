// Цепочка key -> figure -> confirm из app/static/feed.js на минимальной модели DOM:
// node tests/js/feed_chain_harness.js <сценарий>. Проверяет, что она идёт по снимку карточки.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "..", "app", "static", "feed.js"), "utf8");
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

function build() {
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
    querySelector(sel) { return sel === ".card" ? cardEl : sel === ".key-fields" ? box : null; },
    querySelectorAll(sel) { return sel === "button" ? [figureBtn, skipBtn] : []; },
    addEventListener(t, fn) { (handlers[t] = handlers[t] || []).push(fn); },
  };
  const document = {
    getElementById(id) { return id === "feed" ? feed : id === "counter" ? { textContent: "" } : fields[id] || null; },
    querySelector(sel) { return sel.includes("leak") ? { value: "none" } : null; },
    addEventListener() {},
  };
  const fetch = (url, opts) => {
    calls.push(url);
    const reply = () => ({ ok: true, redirected: false, status: 200, json: async () => ({ status: "draft", next: null }), text: async () => "" });
    const gate = Object.keys(gates).find((g) => url.endsWith(g));
    return gate ? gates[gate].then(reply) : Promise.resolve(reply());
  };
  const ctx = { document, fetch, location: {}, Number, JSON, Array, URLSearchParams, Date, setTimeout, clearTimeout, console, Error, Promise };
  vm.runInNewContext(SRC, ctx);
  const click = (btn) => { if (!btn.disabled) return handlers.click[0]({ target: { closest: (s) => (s.startsWith("button") ? btn : null) } }); };
  return { calls, gates, cardEl, figureBtn, skipBtn, click };
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

const name = process.argv[2];
if (!scenarios[name]) fail("нет сценария " + name);
scenarios[name]().then(() => process.exit(0), (e) => fail(String(e)));
