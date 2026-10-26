// Ход разбора: пока материал в очереди или идёт, опрашивает его статус и обновляет строку.
// Опросы идут цепочкой setTimeout: следующий - только после ответа предыдущего.
(function () {
  var rows = document.querySelectorAll("tr[data-live='1']");
  if (!rows.length) return;
  var PERIOD_MS = 6000;
  var reloading = false; // перезагрузка страницы - один раз на всю страницу

  function text(p) {
    if (!p) return "Разбирается: готовим порции";
    return "Разбирается: порция " + p.chunk + " из " + p.chunks + ", принято заданий " + p.accepted;
  }

  function watch(row) {
    var id = row.getAttribute("data-material-id");

    function next() {
      if (!reloading) setTimeout(tick, PERIOD_MS);
    }

    function tick() {
      fetch("/parent/materials/" + id, { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) return next();
          if (d.import_status === "queued" || d.import_status === "running") {
            row.querySelector(".js-status").textContent = d.import_status;
            var note = row.querySelector(".js-progress");
            if (note) note.textContent = d.progress || d.import_status === "running" ? text(d.progress) : "В очереди";
            row.querySelector(".js-tasks").textContent = d.tasks;
            return next();
          }
          if (!reloading) {
            reloading = true;
            location.reload(); // готово или упало: страница покажет итог и отчёт о пропусках
          }
        })
        .catch(next); // сеть моргнула: следующий опрос повторит
    }

    next();
  }

  rows.forEach(watch);
})();
