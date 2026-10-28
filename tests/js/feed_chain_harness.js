// Цепочка key -> figure -> confirm из app/static/feed.js на минимальной модели DOM:
// node tests/js/feed_chain_harness.js <сценарий>. Проверяет, что она идёт по снимку карточки.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "..", "app", "static", "feed.js"), "utf8");
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

function build(extra, opts) {
  opts = Object.assign({ leak: "none", tiles: [], keySelects: [], lefts: "", area: null, fetch: null }, opts || {});
  const calls = [];
  const docHandlers = {};
  const gates = {};
  const handlers = {};
  const cardEl = { dataset: { taskId: "1", counter: "1 из 2", figurePage: "1" } };
  const mk = (extra) => Object.assign({ disabled: false, dataset: {}, value: "", checked: false }, extra);
  const figureBtn = mk({ dataset: { action: "figure" } });
  const skipBtn = mk({ dataset: { action: "skip" } });
  figureBtn.click = () => handlers.click[0]({ target: { closest: (s) => (s.startsWith("button") ? figureBtn : null) } });
  const fields = {
    alt: mk({ value: "Подпись" }), key_text: mk({ value: "Б" }), note_free: mk({ value: "" }),
    note_ack: mk({ checked: false }), leak_ack: mk({ checked: false }), error: mk({}),
  };
  const box = { dataset: { candidate: "0", current: "", lefts: opts.lefts }, querySelectorAll: () => [] };
  const feed = {
    set innerHTML(h) { feed.html = h; }, get innerHTML() { return feed.html; },
    querySelector(sel) {
      if (sel === ".card") return cardEl;
      if (sel === ".key-fields") return box;
      if (sel === 'button[data-action="figure"]') return figureBtn;
      if (sel === "[data-figure-img]" && extra) return extra.img;
      return null;
    },
    querySelectorAll(sel) {
      if (sel === "button") return [figureBtn, skipBtn];
      if (sel === "[data-tile-id]") return opts.tiles;
      if (sel === "[data-key-left]") return opts.keySelects;
      return [];
    },
    addEventListener(t, fn) { (handlers[t] = handlers[t] || []).push(fn); },
  };
  const document = {
    getElementById(id) { return id === "feed" ? feed : id === "counter" ? { textContent: "" } : id === "page-img" && extra ? extra.page : id === "draft-frame" ? null : id === "figure-area" ? opts.area : fields[id] || null; },
    createElement() { return { style: {} }; },
    querySelector(sel) { return sel.includes("leak") && opts.leak ? { value: opts.leak } : null; },
    addEventListener(t, fn) { (docHandlers[t] = docHandlers[t] || []).push(fn); },
  };
  const replies = {};
  const bodies = [];
  const fetch = opts.fetch ? (url) => { calls.push(url); return opts.fetch(url); } : (url, opts) => {
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
  return { calls, gates, replies, bodies, cardEl, figureBtn, skipBtn, click, feed, document, handlers, docHandlers, fields };
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

const enter = (p) => p.docHandlers.keydown[0]({ key: "Enter", code: "Enter", target: { closest: () => null }, preventDefault() {} });

scenarios.ready_state_gates_button_and_enter = async function () {
  const p = build(null, { leak: null });
  p.fields.key_text.value = "";
  // загрузка: не отвечено на шаг 1 и нет ключа - кнопка закрыта, Enter не делает ничего
  if (!p.figureBtn.disabled) fail("кнопка открыта без готовности");
  enter(p); await tick();
  if (p.calls.length) fail("Enter ушёл в запрос при закрытой кнопке: " + JSON.stringify(p.calls));
  // всё готово: тот же сигнал открывает и кнопку, и Enter
  const ready = build(null, { leak: "none" });
  ready.fields.key_text.value = "Б";
  ready.handlers.input[0]({ target: { id: "alt", matches: () => false } });
  if (ready.figureBtn.disabled) fail("кнопка закрыта при готовой карточке");
  enter(ready); await tick(); await tick(); await tick();
  if (!ready.calls.some((u) => u.endsWith("/1/key"))) fail("Enter не запустил цепочку: " + JSON.stringify(ready.calls));
  // подпись стёрли: закрыты оба сразу
  ready.fields.alt.value = "";
  ready.handlers.input[0]({ target: { id: "alt", matches: () => false } });
  if (!ready.figureBtn.disabled) fail("кнопка осталась открытой без подписи");
  const before = ready.calls.length;
  enter(ready); await tick();
  if (ready.calls.length !== before) fail("Enter сработал при закрытой кнопке");
};

scenarios.chain_carries_tiles = async function () {
  const tile = (id, label) => ({ dataset: { tileId: id, label }, querySelector: () => null });
  const p = build(null, { tiles: [tile("ta", "2"), tile("tb", "1")] });
  p.click(p.figureBtn);
  await tick(); await tick(); await tick();
  const figureBody = JSON.parse(p.bodies[1]);
  const want = [{ id: "ta", label: "2" }, { id: "tb", label: "1" }];
  if (JSON.stringify(figureBody.tiles) !== JSON.stringify(want)) fail("плитки в снимке: " + p.bodies[1]);
};

scenarios.tile_move_changes_order_only = async function () {
  const list = [];
  const mkTile = (id, label) => {
    const num = { textContent: label || "" };
    const li = {
      dataset: { tileId: id, label }, querySelector: (s) => (s === "[data-tile-num]" ? num : null),
      get previousElementSibling() { return list[list.indexOf(li) - 1] || null; },
      get nextElementSibling() { return list[list.indexOf(li) + 1] || null; },
      parentNode: {
        insertBefore(node, ref) { list.splice(list.indexOf(node), 1); list.splice(list.indexOf(ref), 0, node); },
      },
    };
    list.push(li);
    return li;
  };
  const a = mkTile("ta", "1"), b = mkTile("tb", "2");
  const select = (label, value) => ({ dataset: { keyLeft: label }, value });
  const p = build(null, { tiles: list, keySelects: [select("1", "А"), select("2", "Б")] });
  const mover = { dataset: { tileMove: "-1" }, closest: () => b };
  p.handlers.click[0]({ target: { closest: (s) => (s === "[data-tile-move]" ? mover : null) } });
  if (list[0] !== b || list[1] !== a) fail("порядок не изменился");
  if (a.dataset.label !== "1" || b.dataset.label !== "2") fail("перестановка сдвинула привязку");
  p.click(p.figureBtn);
  await tick(); await tick(); await tick();
  const sent = JSON.parse(p.bodies[0]);
  if (sent.answer !== "1-А, 2-Б") fail("ключ следует за номером, а не за местом: " + p.bodies[0]);
  const tilesSent = JSON.parse(p.bodies[1]).tiles;
  if (JSON.stringify(tilesSent.map((t) => t.id)) !== JSON.stringify(["tb", "ta"])) fail("порядок в запросе: " + p.bodies[1]);
};

scenarios.unbound_tile_blocks_publish = async function () {
  const tile = (id, label) => ({ dataset: { tileId: id, label }, querySelector: () => null });
  const tiles = ["1", "2", "3", "4", "5", ""].map((l, i) => tile("t" + i, l));
  const select = (label) => ({ dataset: { keyLeft: label }, value: "А" });
  const p = build(null, { tiles, keySelects: ["1", "2", "3", "4", "5"].map(select), lefts: "1,2,3,4,5,6" });
  p.handlers.input[0]({ target: { id: "alt", matches: () => false } });
  if (!p.figureBtn.disabled) fail("кнопка открыта при шести левых и пяти ответах");
  const before = p.calls.length;
  enter(p); await tick();
  if (p.calls.length !== before) fail("Enter опубликовал неполный ключ");
  const all = build(null, { tiles, keySelects: ["1", "2", "3", "4", "5", "6"].map(select), lefts: "1,2,3,4,5,6" });
  all.handlers.input[0]({ target: { id: "alt", matches: () => false } });
  if (all.figureBtn.disabled) fail("полный ключ закрыл кнопку");
};

scenarios.frame_change_clears_leak_ack = async function () {
  const listeners = {};
  const img = { src: "", hidden: true, alt: "", dataset: {}, parentElement: null };
  img.closest = () => null;
  const page = {
    naturalWidth: 900, naturalHeight: 1200, parentElement: { appendChild() {} },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 900, height: 1200 }),
    addEventListener(t, fn) { listeners[t] = fn; },
  };
  const p = build({ page, img });
  p.fields.leak_ack.checked = true;
  listeners.mousedown({ preventDefault() {}, clientX: 100, clientY: 100 });
  listeners.mouseup({ clientX: 400, clientY: 500 });
  if (p.fields.leak_ack.checked) fail("отметка leak_ack пережила новую рамку");
};

scenarios.leak_ack_digest_is_sent = async function () {
  const p = build();
  p.fields.leak_ack.checked = true;
  p.fields.leak_ack.dataset.digest = "abc123";
  p.click(p.figureBtn);
  await tick(); await tick(); await tick();
  const sent = JSON.parse(p.bodies[1]);
  if (sent.leak_digest !== "abc123") fail("слепок рамки не ушёл с подтверждением: " + p.bodies[1]);
};

scenarios.stale_tiles_response_is_dropped = async function () {
  const li = { dataset: { tileId: "ta", label: "" }, querySelector: () => null };
  const area = { innerHTML: "" };
  const release = {};
  const answer = (html) => ({ ok: true, status: 200, text: async () => html, json: async () => ({}) });
  const fetch = (url) => new Promise((resolve) => {
    const n = decodeURIComponent(url).match(/ta:(\d*)/)[1];
    release[n] = () => resolve(answer("R" + n));
  });
  const p = build(null, { tiles: [li], area, fetch });
  const pick = (value) => p.handlers.change[0]({ target: { id: "", value, matches: (s) => s === "[data-tile-bind]", closest: () => li } });
  pick("1"); pick("2");   // два выбора подряд: оба запроса в полёте
  release["2"](); await tick(); await tick();
  if (area.innerHTML !== "R2") fail("свежий ответ не применён: " + area.innerHTML);
  release["1"](); await tick(); await tick();
  if (area.innerHTML !== "R2") fail("устаревший ответ затёр новую привязку: " + area.innerHTML);
  if (li.dataset.label !== "2") fail("привязка плитки изменилась: " + li.dataset.label);
};

scenarios.reorder_keeps_the_frame_update = async function () {
  const list = [];
  const mkTile = (id, label) => {
    const li = {
      dataset: { tileId: id, label }, querySelector: () => null,
      get previousElementSibling() { return list[list.indexOf(li) - 1] || null; },
      get nextElementSibling() { return list[list.indexOf(li) + 1] || null; },
      parentNode: { insertBefore(node, ref) { list.splice(list.indexOf(node), 1); list.splice(list.indexOf(ref), 0, node); } },
    };
    list.push(li);
    return li;
  };
  const a = mkTile("ta", "1"), b = mkTile("tb", "2");
  const area = { innerHTML: "" };
  const waiting = [];
  const fetch = (url) => new Promise((resolve) => {
    const q = decodeURIComponent(url);
    const html = `frame:${q.includes("rect_px=")} order:${(q.match(/state=([^&]*)/) || [])[1]}`;
    waiting.push(() => resolve({ ok: true, status: 200, text: async () => html, json: async () => ({}) }));
  });
  const listeners = {};
  const img = { src: "", hidden: true, alt: "", dataset: {}, parentElement: null };
  img.closest = () => null;
  const page = {
    naturalWidth: 900, naturalHeight: 1200, parentElement: { appendChild() {} },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 900, height: 1200 }),
    addEventListener(t, fn) { listeners[t] = fn; },
  };
  const p = build({ page, img }, { tiles: list, area, fetch });
  listeners.mousedown({ preventDefault() {}, clientX: 100, clientY: 100 });
  listeners.mouseup({ clientX: 400, clientY: 500 });   // новая рамка: запрос плиток ушёл
  const mover = { dataset: { tileMove: "-1" }, closest: () => b };
  p.handlers.click[0]({ target: { closest: (s) => (s === "[data-tile-move]" ? mover : null) } });  // порядок изменён до ответа
  waiting.splice(0).forEach((release) => release());
  await tick(); await tick(); await tick();
  if (area.innerHTML !== "frame:true order:tb:2,ta:1") fail("рамка или порядок потеряны: " + area.innerHTML);
};

const name = process.argv[2];
if (!scenarios[name]) fail("нет сценария " + name);
scenarios[name]().then(() => process.exit(0), (e) => fail(String(e)));
