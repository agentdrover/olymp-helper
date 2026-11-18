// «Снять задание» на странице опубликованных заданий родителя: запрос и удаление строки.
(function () {
  "use strict";
  const csrf = () => { const m = document.querySelector('meta[name="csrf-token"]'); return m ? m.content : ""; };
  const error = document.getElementById("error");
  document.addEventListener("click", async (e) => {
    const b = e.target.closest && e.target.closest("button[data-withdraw]");
    if (!b) return;
    b.disabled = true;
    try {
      const r = await fetch(`/parent/tasks/${b.dataset.withdraw}/withdraw`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
        body: "{}",
      });
      if (!r.ok) throw new Error(((await r.json()).error) || "Не получилось снять задание");
      b.closest("tr").remove();
      if (error) error.textContent = "";
    } catch (err) {
      b.disabled = false;
      if (error) error.textContent = err.message;
    }
  });
})();
