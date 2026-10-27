// Ручной запуск автопубликации: кнопка запускает фоновую работу и показывает ход.
(function () {
  "use strict";
  const buttons = document.querySelectorAll("#autorun, .autorun");
  const out = document.getElementById("autorun-status");
  if (!buttons.length || !out) return;
  const csrf = () => { const m = document.querySelector('meta[name="csrf-token"]'); return m ? m.content : ""; };
  let timer = null;

  async function poll() {
    const r = await fetch("/parent/review/auto/status");
    const s = await r.json();
    out.textContent = s.text || "";
    if (s.state === "running") timer = setTimeout(poll, 1500);
  }

  buttons.forEach((b) => b.addEventListener("click", async () => {
    const id = b.dataset.materialId;
    const r = await fetch("/parent/review/auto/run", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify({ material_id: id ? Number(id) : null }),
    });
    if (!r.ok) { out.textContent = "Не получилось запустить."; return; }
    clearTimeout(timer);
    poll();
  }));
})();
