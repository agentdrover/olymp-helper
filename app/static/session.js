/* Кнопки занятия: «Закончить» и «Показать родителю». Сервер хранит всё состояние; здесь только
   отправка запроса и переход на «Мои итоги». */
(function () {
  "use strict";
  function post(url) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); });
  }
  document.addEventListener("click", function (ev) {
    var finish = ev.target.closest("[data-finish]");
    if (finish) {
      finish.disabled = true;
      post("/session/finish").then(function (res) {
        if (res.body && res.body.session_id) location.href = "/my?session=" + res.body.session_id;
        else { finish.disabled = false; finish.textContent = "Идущего занятия нет"; }
      }).catch(function () { finish.disabled = false; });
      return;
    }
    var show = ev.target.closest("[data-post]");
    if (show) {
      show.disabled = true;
      post(show.getAttribute("data-post")).then(function () { location.reload(); })
        .catch(function () { show.disabled = false; });
    }
  });
})();
