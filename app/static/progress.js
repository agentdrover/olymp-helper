// Ход разбора: пока материал в очереди или идёт, опрашивает его статус и обновляет строку.
(function () {
  var rows = document.querySelectorAll("tr[data-live='1']");
  if (!rows.length) return;
  var PERIOD_MS = 6000;

  function text(p) {
    if (!p) return "Разбирается: готовим порции";
    return "Разбирается: порция " + p.chunk + " из " + p.chunks + ", принято заданий " + p.accepted;
  }

  function watch(row) {
    var id = row.getAttribute("data-material-id");
    var timer = setInterval(tick, PERIOD_MS);
    function stop() { clearInterval(timer); }
    function tick() {
      fetch("/parent/materials/" + id, { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) return;
          if (d.import_status === "queued" || d.import_status === "running") {
            row.querySelector(".js-status").textContent = d.import_status;
            var note = row.querySelector(".js-progress");
            if (note) note.textContent = d.progress || d.import_status === "running" ? text(d.progress) : "В очереди";
            row.querySelector(".js-tasks").textContent = d.tasks;
          } else {
            stop();
            location.reload(); // готово или упало: страница покажет итог и отчёт о пропусках
          }
        })
        .catch(function () {}); // сеть моргнула: следующий опрос повторит
    }
  }

  rows.forEach(watch);
})();
