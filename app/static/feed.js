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

  async function post(path, body) {
    const r = await fetch(`/parent/tasks/${taskId()}/${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
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
    feed.innerHTML = await r.text();
    counter.textContent = count || "";
    frame = null;
    bindPage();
  }

  async function advance(data) { await show(data.next ? data.next.task_id : null, data.counter); }

  function fail(e) { const el = document.getElementById("error"); if (el) el.textContent = e.message; }

  const actions = {
    confirm: () => post("confirm", { answer: val("answer") || null }),
    "confirm-no-key": () => post("confirm", {}),
    skip: () => post("skip"),
    exclude: () => post("exclude"),
    retry: async () => { await post("retry-model"); return { next: { task_id: Number(taskId()) }, counter: card().dataset.counter }; },
    resolve: (b) => post("resolve", { choice: b.dataset.choice, answer: val("answer") || null }),
    figure: () => post("figure", {
      rect_px: frame ? [frame.x0, frame.y0, frame.x1, frame.y1] : null, scale: PAGE_SCALE,
      page: Number(card().dataset.figurePage) || null,
      alt: val("alt"), teacher_note: val("teacher_note"),
      no_answer_ack: checked("no_answer_ack"), teacher_note_ack: checked("teacher_note_ack"),
      leak_ack: checked("leak_ack"),
    }).then((d) => ({ next: { task_id: Number(taskId()) }, counter: card().dataset.counter, reason: d.reason })),
    "save-edit": () => post("edit", { text: val("edit-text"), manual_condition: checked("manual") })
      .then(() => ({ next: { task_id: Number(taskId()) }, counter: card().dataset.counter })),
  };

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
    // Фокус на поле, кнопке или ссылке: Enter принадлежит им (например, «Выйти»), не ленте.
    if (t && t.closest && t.closest("input, textarea, select, button, a, [contenteditable]")) return;
    if (e.key === "Enter") {
      const b = feed.querySelector('button[data-action="confirm"], button[data-action="resolve"][data-choice="key"]');
      if (b) { e.preventDefault(); b.click(); }
    } else if (e.key === "ArrowRight") {
      const b = feed.querySelector('button[data-action="skip"]');
      if (b) { e.preventDefault(); b.click(); }
    } else if (e.code === "KeyE") {
      e.preventDefault(); toggleEdit();
    }
  });

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
    });
  }
  bindPage();
})();
