/* Экран задания: ответы, подсказки, «объясни иначе». Сервер хранит состояние и строит HTML;
   здесь только отправка действий и замена фрагмента. */
(function () {
  "use strict";
  var root = document.getElementById("tutor");
  if (!root) return;
  var base = root.getAttribute("data-url");
  var THINKING = "Думаю… обычно 5–15 с";
  var MODEL_ACTS = { attempt: 1, hint: 1, explain: 1, repeat: 0 };
  var timer = null;

  function zoomDialog() { return root.querySelector("dialog.zoom"); }
  function closeZoom() {
    var d = zoomDialog();
    if (d && d.open) d.close();
  }
  function openZoom(ev) {
    var d = zoomDialog();
    if (!d) return;
    ev.preventDefault();
    if (typeof d.showModal === "function") d.showModal(); else d.setAttribute("open", "");
  }

  function schedule() {
    clearTimeout(timer);
    var busy = root.querySelector(".helper-status[data-panel='thinking'], .helper-status[data-panel='second_channel']");
    var wait = root.querySelector("[data-wait]");
    if (busy) timer = setTimeout(refresh, 2000);
    else if (wait) timer = setTimeout(refresh, (parseInt(wait.getAttribute("data-wait"), 10) + 1) * 1000);
  }

  function swap(html) {
    var wasOpen = zoomDialog() && zoomDialog().open;
    root.innerHTML = html;
    if (wasOpen) openZoom({ preventDefault: function () {} });
    schedule();
  }

  function refresh() {
    fetch(base + "/tutor", { headers: { "HX-Request": "true" } })
      .then(function (r) { return r.text(); }).then(swap).catch(function () {});
  }

  function setThinking() {
    var s = root.querySelector(".helper-status");
    if (s) { s.textContent = THINKING; s.setAttribute("data-panel", "thinking"); }
    root.querySelectorAll(".helper button").forEach(function (b) { b.disabled = true; });
  }

  function send(act, body) {
    if (act in MODEL_ACTS && MODEL_ACTS[act]) setThinking();
    return fetch(base + "/" + act, {
      method: "POST",
      headers: { "Content-Type": "application/json", "HX-Request": "true" },
      body: JSON.stringify(body || {}),
    }).then(function (r) { return r.text(); }).then(swap).catch(function () {
      var s = root.querySelector(".helper-status");
      if (s) s.textContent = "Помощник сейчас не отвечает — проверка по ключу работает.";
    });
  }

  function collect(form) {
    var type = form.getAttribute("data-type");
    var answer;
    if (type === "single") {
      var one = form.querySelector("input[name=answer]:checked");
      answer = one ? one.value : null;
    } else if (type === "multiple") {
      answer = Array.prototype.map.call(form.querySelectorAll("input[name=answer]:checked"), function (i) { return i.value; });
      if (!answer.length) answer = null;
    } else if (type === "match") {
      answer = {};
      form.querySelectorAll("select").forEach(function (s) {
        if (s.value) answer[s.name.replace("pair-", "")] = s.value;
      });
    } else {
      answer = (form.querySelector("input[name=answer]") || {}).value;
    }
    var conf = form.querySelector("input[name=confidence]:checked");
    return { answer: answer, confidence: conf ? conf.value : null };
  }

  root.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (form.getAttribute("data-act") !== "attempt") return;
    ev.preventDefault();
    send("attempt", collect(form));
  });

  root.addEventListener("click", function (ev) {
    var t = ev.target.closest("button, a");
    if (!t) return;
    if (t.hasAttribute("data-zoom")) return openZoom(ev);
    if (t.hasAttribute("data-close-zoom")) return closeZoom();
    var open = t.getAttribute("data-open");
    if (open) { var box = document.getElementById(open); if (box) box.hidden = false; return; }
    var close = t.getAttribute("data-close-confirm");
    if (close) { var b2 = document.getElementById(close); if (b2) b2.hidden = true; return; }
    var act = t.getAttribute("data-act");
    if (!act || t.tagName === "FORM") return;
    var body = {};
    if (t.hasAttribute("data-level")) body.level = parseInt(t.getAttribute("data-level"), 10);
    if (t.hasAttribute("data-mode")) body.mode = t.getAttribute("data-mode");
    if (t.hasAttribute("data-confirm")) body.confirm = true;
    send(act, body);
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") closeZoom();
  });

  schedule();
})();
