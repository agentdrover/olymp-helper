// Лента проверки: клавиши Enter (верно и подтвердить), ArrowRight (пропустить), E (поправить).
// Текст карточки приходит с сервера уже экранированным; здесь он вставляется как готовый фрагмент.
(function () {
  "use strict";
  const feed = document.getElementById("feed");
  const counter = document.getElementById("counter");
  if (!feed) return;

  const card = () => feed.querySelector(".card");
  const taskId = () => (card() ? card().dataset.taskId : null);
  const val = (id) => { const el = document.getElementById(id); return el ? el.value : ""; };
  const checked = (id) => { const el = document.getElementById(id); return !!(el && el.checked); };
  let frame = null; // {x0,y0,x1,y1} в пикселях рендера страницы
  const PAGE_SCALE = 1.5;

  // Токен защиты от подделки запросов: сервер отдаёт его в <meta> страницы родителя.
  const csrf = () => { const m = document.querySelector('meta[name="csrf-token"]'); return m ? m.content : ""; };

  async function post(path, body, id) {
    const r = await fetch(`/parent/tasks/${id || taskId()}/${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Не получилось");
    return data;
  }

  async function show(id, count) {
    if (!id) {
      feed.innerHTML = "<p>Всё проверено: в ленте больше нет заданий.</p>";
      counter.textContent = "";
      return;
    }
    const r = await fetch(`/parent/review/${id}?fragment=1`);
    if (r.redirected || r.status === 403) { location.href = "/parent/login"; return; } // сессия кончилась
    feed.innerHTML = await r.text();
    counter.textContent = count || "";
    frame = null;
    bindPage();
    refreshReady();
  }

  async function advance(data) { await show(data.next ? data.next.task_id : null, data.counter); }

  function fail(e) {
    const el = document.getElementById("error");
    if (el) el.textContent = e.message;
    // Сервер нашёл в вырезке текст ответа: даём явно решить, что он допустим.
    const row = document.getElementById("leak-ack-row");
    if (row && /напечатан текст ответа/.test(e.message)) { row.hidden = false; row.dataset.need = "1"; }
    refreshReady();
  }

  // ---- карточка рисунка: шаги 1-4 и одна основная кнопка
  function teacherNote() {
    const rows = Array.from(feed.querySelectorAll("[data-note]"));
    if (!rows.length) return val("note_free");
    const lines = rows.filter((i) => i.value.trim()).map((i) => `${i.dataset.note} — ${i.value.trim().replace(/[;\n]+/g, ",")}`);
    const rest = val("note_rest");
    if (rest) lines.push(rest);
    return lines.join("\n");
  }

  // Ответ строкой для действия key; "!" — для match выбраны не все рисунки. Поля ответа стоят
  // под плитками или в списке .key-fields: ищем по всей карточке.
  function keyAnswer() {
    const box = feed.querySelector(".key-fields");
    if (!box) return { text: "", box: null, total: 0, chosen: 0 };
    const lefts = Array.from(feed.querySelectorAll("[data-key-left]"));
    if (!lefts.length) {
      const text = val("key_text").trim();
      return { text, box, total: 1, chosen: text ? 1 : 0 };
    }
    const chosen = lefts.filter((x) => x.value);
    const base = { box, total: lefts.length, chosen: chosen.length };
    if (chosen.length && chosen.length < lefts.length) return { ...base, text: "!" };
    return { ...base, text: chosen.map((x) => `${x.dataset.keyLeft}-${x.value}`).join(", ") };
  }

  // Плитки в порядке показа и их привязка к номерам: уходят в запрос подтверждения как есть.
  function tilesState() {
    return Array.from(feed.querySelectorAll("[data-tile-id]")).map((li) => ({
      id: li.dataset.tileId, label: li.dataset.label || null,
    }));
  }

  // Единственное состояние готовности: от него зависят и кнопка, и Enter. Сервер проверяет то же
  // самое заново, поэтому список здесь только объясняет, чего не хватает.
  function problems() {
    const out = [];
    const leak = document.querySelector('input[name="leak"]:checked');
    if (!leak) out.push("Ответьте на вопрос: есть ли ответ на картинках.");
    else if (leak.value !== "none") out.push("Поправьте рамку на странице слева, затем ответьте «Ответа нет».");
    const key = keyAnswer();
    if (key.text === "!") out.push("Выберите ответ для каждого рисунка.");
    else if (!key.text) out.push("Выберите правильный ответ: без ключа задание не публикуется.");
    if (!val("alt").trim()) out.push("Напишите подпись к картинке.");
    const row = document.getElementById("leak-ack-row");
    const known = feed.querySelector("[data-tile-leak]") || (row && row.dataset.need === "1");
    if (row && known && !checked("leak_ack")) out.push("В рисунке напечатан текст ответа: поправьте рамку или отметьте, что так и надо.");
    const tiles = tilesState();
    const labels = tiles.map((t) => t.label).filter(Boolean);
    if (new Set(labels).size !== labels.length) out.push("Два рисунка привязаны к одному номеру.");
    const box = key.box;
    if (box && box.dataset.bound === "1" && labels.length !== tiles.length) out.push("Привяжите каждый рисунок к номеру задания.");
    return out;
  }

  function setSum(name, ok, text) {
    const el = feed.querySelector(`[data-sum="${name}"]`);
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("bad", ok === false);
  }

  function refreshReady() {
    const list = problems();
    const btn = feed.querySelector('button[data-action="figure"]');
    if (btn) btn.disabled = list.length > 0;
    const leak = document.querySelector('input[name="leak"]:checked');
    const key = keyAnswer();
    setSum("leak", leak && leak.value === "none", leak && leak.value === "none" ? "✓ Ответа на картинках нет" : "✗ Не отмечено, что ответа на картинках нет");
    setSum("key", key.total > 0 && key.chosen === key.total, key.total > 0 && key.chosen === key.total
      ? `✓ Ответ задан для ${key.chosen} из ${key.total}` : `✗ Ответ задан для ${key.chosen} из ${key.total || 1} — выберите`);
    setSum("alt", !!val("alt").trim(), val("alt").trim() ? "✓ Подпись есть" : "✗ Подписи нет");
    const rows = Array.from(feed.querySelectorAll("[data-note]"));
    const filled = rows.filter((i) => i.value.trim()).length;
    const head = document.getElementById("notes-summary");
    if (head && rows.length) head.textContent = `Описать рисунки для подсказок · ${filled} из ${rows.length}`;
    const pill = document.getElementById("mode-pill");
    const hints = !!(pill && /С подсказками/.test(pill.textContent || ""));
    setSum("hints", null, hints ? "Подсказки: включены" : "Подсказки: выключены — откройте «Описать рисунки», если нужны");
    return list;
  }

  async function publishFigure() {
    // Снимок на момент нажатия: id карточки и все поля. Цепочка идёт только по нему, поэтому
    // «Пропустить» или смена карточки во время запроса не переносят шаги на другое задание.
    const id = taskId();
    const counter = card().dataset.counter;
    const list = problems();
    if (list.length) throw new Error(list[0]);
    const leak = document.querySelector('input[name="leak"]:checked');
    const key = keyAnswer();
    const tiles = tilesState();
    const snap = {
      key: key.text,
      body: {
        rect_px: frame ? [frame.x0, frame.y0, frame.x1, frame.y1] : null, scale: PAGE_SCALE,
        page: Number(card().dataset.figurePage) || null,
        alt: val("alt"), teacher_note: teacherNote(),
        no_answer_ack: leak.value === "none", teacher_note_ack: checked("note_ack"),
        leak_ack: checked("leak_ack"),
        tiles: tiles.length ? tiles : null,
      },
    };
    const buttons = Array.from(feed.querySelectorAll("button"));
    buttons.forEach((x) => { x.disabled = true; });
    const saved = [];
    const step = async (name, path, body) => {
      try {
        const r = await post(path, body, id);
        saved.push(name);
        return r;
      } catch (err) {
        const kept = saved.length ? ` Уже сохранено: ${saved.join(", ")}.` : " Ничего не сохранено.";
        throw new Error(`Шаг «${name}»: ${err.message}.${kept}`);
      }
    };
    try {
      // Неизменённый ключ из файла сервер оставляет ключом файла: сравнивает он, не лента.
      await step("ключ", "key", { answer: snap.key, keep_candidate: true });
      const done = await step("рисунок", "figure", snap.body);
      if (done.status === "draft") return await step("публикация", "confirm", {}); // следующая карточка из ответа
      return { next: { task_id: Number(id) }, counter, reason: done.reason };
    } finally {
      buttons.forEach((x) => { x.disabled = false; });
      refreshReady();
    }
  }

  // Вид ребёнка и режим подсказок обновляются по мере правок; ничего не сохраняется.
  let modeTimer = null;
  function refreshMode() {
    clearTimeout(modeTimer);
    modeTimer = setTimeout(async () => {
      const pill = document.getElementById("mode-pill");
      if (!pill) return;
      try {
        const r = await fetch(`/parent/tasks/${taskId()}/figure/mode`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
          body: JSON.stringify({ teacher_note: teacherNote(), teacher_note_ack: checked("note_ack") }),
        });
        const m = await r.json();
        if (!r.ok) return;
        pill.textContent = m.label;
        document.getElementById("mode-hint").textContent = m.hint;
        const rows = Array.from(feed.querySelectorAll("[data-note]"));
        const filled = rows.filter((i) => i.value.trim()).length;
        const status = document.getElementById("note-status");
        if (status && rows.length) status.textContent = `Описано ${filled} из ${rows.length}.`;
      } catch (e) { /* режим обновится при следующей правке */ }
    }, 250);
  }

  function refreshPreview() {
    const img = feed.querySelector("[data-figure-img]");
    if (!img || !frame) return;
    const slot = img.closest("figure");
    if (slot) slot.hidden = false;  // карточка без областей: место под вырезку скрыто до первой рамки
    img.hidden = false;
    const q = new URLSearchParams({
      page: card().dataset.figurePage || "", rect_px: [frame.x0, frame.y0, frame.x1, frame.y1].join(","),
      scale: PAGE_SCALE, t: Date.now(),
    });
    img.src = `/parent/tasks/${taskId()}/figure-preview.png?${q}`;
  }

  feed.addEventListener("input", (e) => {
    if (e.target.id === "alt") {
      const img = feed.querySelector("[data-figure-img]");
      if (img) img.alt = e.target.value;
    }
    if (e.target.matches("[data-note], #note_free, #note_ack")) refreshMode();
    refreshReady();
  });
  feed.addEventListener("change", (e) => {
    if (e.target.id === "note_ack") refreshMode();
    if (e.target.matches && e.target.matches("[data-tile-bind]")) {
      const li = e.target.closest("[data-tile-id]");
      if (li) li.dataset.label = e.target.value;
      refreshTiles();  // ключ идёт за номером: поля под плитками собираются заново
    }
    refreshReady();
  });

  // Плитки и ввод ответа по текущей рамке: сервер отдаёт тот же фрагмент, что и карточка.
  // Выбранные ответы переживают замену по номеру плитки, а не по месту.
  async function refreshTiles() {
    const area = document.getElementById("figure-area");
    if (!area) return;
    const id = taskId();
    const kept = {};
    feed.querySelectorAll("[data-key-left]").forEach((x) => { kept[x.dataset.keyLeft] = x.value; });
    const single = document.getElementById("key_text");
    const keptSingle = single ? single.value : null;
    const q = new URLSearchParams({
      state: tilesState().map((t) => `${t.id}:${t.label || ""}`).join(","),
    });
    if (frame) {
      q.set("page", card().dataset.figurePage || "");
      q.set("rect_px", [frame.x0, frame.y0, frame.x1, frame.y1].join(","));
      q.set("scale", PAGE_SCALE);
    }
    const r = await fetch(`/parent/tasks/${id}/figure-tiles?${q}`);
    if (!r.ok || taskId() !== id) return;  // карточка успела смениться
    area.innerHTML = await r.text();
    feed.querySelectorAll("[data-key-left]").forEach((x) => { if (kept[x.dataset.keyLeft]) x.value = kept[x.dataset.keyLeft]; });
    const again = document.getElementById("key_text");
    if (again && keptSingle) again.value = keptSingle;
    const row = document.getElementById("leak-ack-row");
    if (row && feed.querySelector("[data-tile-leak]")) row.hidden = false;
    refreshReady();
  }

  function moveTile(li, dir) {
    const other = dir < 0 ? li.previousElementSibling : li.nextElementSibling;
    if (!other) return;
    if (dir < 0) li.parentNode.insertBefore(li, other); else li.parentNode.insertBefore(other, li);
    Array.from(feed.querySelectorAll("[data-tile-id]")).forEach((x, i) => {
      const num = x.querySelector("[data-tile-num]");
      if (num && !x.dataset.label) num.textContent = String(i + 1);
    });
  }

  const actions = {
    confirm: () => post("confirm", { answer: val("answer") || null }),
    "confirm-no-key": () => post("confirm", {}),
    skip: () => post("skip"),
    exclude: () => post("exclude"),
    retry: async () => { await post("retry-model"); return { next: { task_id: Number(taskId()) }, counter: card().dataset.counter }; },
    resolve: (b) => post("resolve", { choice: b.dataset.choice, answer: val("answer") || null }),
    figure: publishFigure,
    "save-edit": () => post("edit", changedEdit())
      .then(() => ({ next: { task_id: Number(taskId()) }, counter: card().dataset.counter })),
  };

  // Только изменённые поля: неизменённое условие не должно создавать ревизию.
  function changedEdit() {
    const body = { manual_condition: checked("manual") };
    const t = document.getElementById("edit-text");
    if (t && t.value !== t.defaultValue) body.text = t.value;
    const s = document.getElementById("edit-topic");
    if (s && s.value !== (Array.from(s.options).find((o) => o.defaultSelected) || s.options[0]).value) body.topic = s.value;
    return body;
  }

  function toggleEdit() {
    const p = document.getElementById("edit-panel");
    if (p) { p.hidden = !p.hidden; if (!p.hidden) document.getElementById("edit-text").focus(); }
  }

  feed.addEventListener("click", async (e) => {
    const link = e.target.closest("a.page-link");
    if (link) {  // другая страница диапазона в той же панели
      e.preventDefault();
      const shown = document.getElementById("page-img");
      if (shown) { shown.src = link.dataset.pageUrl; frame = null; }
      return;
    }
    const mover = e.target.closest("[data-tile-move]");
    if (mover) {
      moveTile(mover.closest("[data-tile-id]"), Number(mover.dataset.tileMove));
      return refreshReady();
    }
    const b = e.target.closest("button[data-action]");
    if (!b) return;
    if (b.dataset.action === "edit") return toggleEdit();
    try { await advance(await actions[b.dataset.action](b)); } catch (err) { fail(err); }
  });

  document.addEventListener("keydown", (e) => {
    // Enter на переключателе или флажке своего действия не имеет: он подтверждает карточку.
    const choice = e.target && e.target.closest && e.target.closest("input[type=radio], input[type=checkbox]");
    const t = choice && e.key === "Enter" ? null : e.target;
    // В поле ввода все клавиши принадлежат полю.
    if (t && t.closest && t.closest("input, textarea, select, [contenteditable]")) return;
    if (e.key === "Enter") {
      // Enter на кнопке или ссылке (например, «Выйти») нажимает её, а не подтверждает задание.
      if (t && t.closest && t.closest("button, a")) return;
      const fig = feed.querySelector('button[data-action="figure"]');
      if (fig) {  // карточка рисунка: Enter подчиняется тому же состоянию, что и кнопка
        e.preventDefault();
        if (!fig.disabled) fig.click();
        return;
      }
      const b = feed.querySelector('button[data-action="confirm"], button[data-action="resolve"][data-choice="key"]');
      if (b) { e.preventDefault(); b.click(); }
    } else if (e.key === "ArrowRight") {
      const b = feed.querySelector('button[data-action="skip"]');
      if (b) { e.preventDefault(); b.click(); }
    } else if (e.code === "KeyE") {
      e.preventDefault(); toggleEdit();
    }
  });

  function drawFrame(img) {
    const wrap = img.parentElement;
    let box = document.getElementById("draft-frame");
    if (!box) { box = document.createElement("span"); box.id = "draft-frame"; box.className = "region-frame draft"; wrap.appendChild(box); }
    const w = img.naturalWidth, h = img.naturalHeight;
    box.style.left = `${frame.x0 / w * 100}%`; box.style.top = `${frame.y0 / h * 100}%`;
    box.style.width = `${(frame.x1 - frame.x0) / w * 100}%`; box.style.height = `${(frame.y1 - frame.y0) / h * 100}%`;
  }

  // Ручная рамка: тянуть мышью по изображению страницы; координаты — пиксели рендера.
  function bindPage() {
    const img = document.getElementById("page-img");
    if (!img) return;
    let start = null;
    const natural = (ev) => {
      const r = img.getBoundingClientRect();
      return [(ev.clientX - r.left) * img.naturalWidth / r.width, (ev.clientY - r.top) * img.naturalHeight / r.height];
    };
    img.addEventListener("mousedown", (ev) => { ev.preventDefault(); start = natural(ev); });
    img.addEventListener("mouseup", (ev) => {
      if (!start) return;
      const end = natural(ev);
      frame = { x0: Math.min(start[0], end[0]), y0: Math.min(start[1], end[1]), x1: Math.max(start[0], end[0]), y1: Math.max(start[1], end[1]) };
      start = null;
      drawFrame(img);
      const row = document.getElementById("leak-ack-row");
      if (row) row.dataset.need = "";  // прежняя находка относилась к прежней рамке
      refreshPreview();
      refreshTiles();
    });
  }
  bindPage();
  refreshReady();
})();
