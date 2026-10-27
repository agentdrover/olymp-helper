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

  async function post(path, body) {
    const r = await fetch(`/parent/tasks/${taskId()}/${path}`, {
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
  }

  async function advance(data) { await show(data.next ? data.next.task_id : null, data.counter); }

  function fail(e) {
    const el = document.getElementById("error");
    if (el) el.textContent = e.message;
    // Сервер нашёл в вырезке текст ответа: даём явно решить, что он допустим.
    const row = document.getElementById("leak-ack-row");
    if (row && /напечатан текст ответа/.test(e.message)) row.hidden = false;
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

  // Ответ шага 2 строкой для действия key; "!" — для match выбраны не все рисунки.
  function keyAnswer() {
    const box = feed.querySelector(".key-fields");
    if (!box) return { text: "", box: null };
    const lefts = Array.from(box.querySelectorAll("[data-key-left]"));
    if (!lefts.length) return { text: val("key_text").trim(), box };
    const chosen = lefts.filter((x) => x.value);
    if (chosen.length && chosen.length < lefts.length) return { text: "!", box };
    return { text: chosen.map((x) => `${x.dataset.keyLeft}-${x.value}`).join(", "), box };
  }

  async function publishFigure() {
    const leak = document.querySelector('input[name="leak"]:checked');
    if (!leak) throw new Error("Ответьте на шаг 1: есть ли ответ на картинках.");
    if (leak.value !== "none") throw new Error("Поправьте рамку на странице слева, затем ответьте «Да, ответа нет».");
    const key = keyAnswer();
    if (key.text === "!") throw new Error("Выберите ответ для каждого рисунка.");
    if (!key.text) throw new Error("Выберите правильный ответ: без ключа задание не публикуется.");
    // Ключ из загруженного файла без правки подтверждает сервер; всё остальное — ключ родителя.
    if (!(key.box.dataset.candidate === "1" && key.text === key.box.dataset.current)) await post("key", { answer: key.text });
    const done = await post("figure", {
      rect_px: frame ? [frame.x0, frame.y0, frame.x1, frame.y1] : null, scale: PAGE_SCALE,
      page: Number(card().dataset.figurePage) || null,
      alt: val("alt"), teacher_note: teacherNote(),
      no_answer_ack: true, teacher_note_ack: checked("note_ack"),
      leak_ack: checked("leak_ack"),
    });
    if (done.status === "draft") await post("confirm", {});
    return { next: { task_id: Number(taskId()) }, counter: card().dataset.counter, reason: done.reason };
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
  });
  feed.addEventListener("change", (e) => { if (e.target.id === "note_ack") refreshMode(); });

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
    const b = e.target.closest("button[data-action]");
    if (!b) return;
    if (b.dataset.action === "edit") return toggleEdit();
    try { await advance(await actions[b.dataset.action](b)); } catch (err) { fail(err); }
  });

  document.addEventListener("keydown", (e) => {
    const t = e.target;
    // В поле ввода все клавиши принадлежат полю.
    if (t && t.closest && t.closest("input, textarea, select, [contenteditable]")) return;
    if (e.key === "Enter") {
      // Enter на кнопке или ссылке (например, «Выйти») нажимает её, а не подтверждает задание.
      if (t && t.closest && t.closest("button, a")) return;
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
      refreshPreview();
    });
  }
  bindPage();
})();
