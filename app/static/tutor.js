/* Экран задания: ответы, подсказки, «объясни иначе», вопрос помощнику и голос. Сервер хранит
   состояние и строит HTML; здесь только отправка действий, запись голоса и замена фрагмента. */
(function () {
  "use strict";
  var root = document.getElementById("tutor");
  if (!root) return;
  var base = root.getAttribute("data-url");
  var POLL_MS = 2500;       // опрос сервера, пока шаг идёт: цепочка setTimeout, запросы не накладываются
  var LONG_WAIT_S = 60;
  var LONG_TEXT = "Модель отвечает долго, жду…";
  var MODEL_ACTS = { attempt: 1, hint: 1, explain: 1, repeat: 0, chat: 1 };
  var MAX_RECORD_MS = 30000;
  var MAX_BYTES = 2 * 1024 * 1024;
  var draft = "";
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

  var BUSY_SEL = ".helper-status[data-panel='thinking'], .helper-status[data-panel='second_channel']";
  var polling = false;
  var tickTimer = null;
  var tickBase = null;

  function busyNode() { return root.querySelector(BUSY_SEL); }

  function schedule() {
    clearTimeout(timer);
    var busy = busyNode();
    var wait = root.querySelector("[data-wait]");
    if (busy) timer = setTimeout(refresh, POLL_MS);
    else if (wait) timer = setTimeout(refresh, (parseInt(wait.getAttribute("data-wait"), 10) + 1) * 1000);
    startTick();
  }

  // Секунды ожидания идут по часам сервера: он отдаёт, сколько шаг уже идёт; здесь они только тикают.
  function renderTick() {
    var s = busyNode();
    if (!s || s.getAttribute("data-panel") !== "thinking" || tickBase === null) return;
    var n = tickBase.elapsed + Math.floor((Date.now() - tickBase.at) / 1000);
    s.textContent = n >= LONG_WAIT_S ? LONG_TEXT : "Думаю… " + n + " с";
  }

  function startTick() {
    clearTimeout(tickTimer);
    var s = busyNode();
    var e = s ? parseInt(s.getAttribute("data-elapsed"), 10) : NaN;
    if (!s || isNaN(e)) { tickBase = null; return; }
    tickBase = { elapsed: e, at: Date.now() };
    (function loop() {
      renderTick();
      tickTimer = setTimeout(loop, 1000);
    })();
  }

  function field() { return document.getElementById("chat-input"); }

  function swap(html) {
    var wasOpen = zoomDialog() && zoomDialog().open;
    if (field()) draft = field().value;  // недописанный вопрос не пропадает при обновлении панели
    root.innerHTML = html;
    if (field()) field().value = draft;
    if (wasOpen) openZoom({ preventDefault: function () {} });
    schedule();
  }

  function refresh() {
    if (polling) return;
    polling = true;
    var s = busyNode();
    var step = s ? s.getAttribute("data-step") : null;
    fetch(base + "/tutor" + (step ? "?step=" + encodeURIComponent(step) : ""), { headers: { "HX-Request": "true" } })
      .then(function (r) { return r.text(); })
      .then(function (html) { polling = false; swap(html); })
      .catch(function () { polling = false; schedule(); });  // сеть мигнула: опрос не обрывается
  }

  function setThinking() {
    var s = root.querySelector(".helper-status");
    if (s) { s.textContent = "Думаю…"; s.setAttribute("data-panel", "thinking"); }
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

  // ---------------------------------------------------------------- вопрос помощнику

  function sendQuestion(mode) {
    var box = field();
    var text = box ? box.value.trim() : "";
    if (!text) { if (box) box.focus(); return; }
    setThinking();
    var started = false;
    fetch(base + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "HX-Request": "true" },
      body: JSON.stringify({ message: text, mode: mode }),
    }).then(function (r) {
      started = r.headers.get("X-Step-Started") === "1";
      return r.text();
    }).then(function (html) {
      // вопрос ушёл, если сервер начал шаг или сразу ответил и панель не сообщает об отказе
      var sent = started || html.indexOf('data-panel="idle"') !== -1;
      if (sent && mode === "chat") draft = "";
      else if (field()) draft = field().value;
      swap(html);
      if (sent && mode === "chat" && field()) field().value = "";
    }).catch(function () {
      var s = root.querySelector(".helper-status");
      if (s) s.textContent = "Помощник сейчас не отвечает — проверка по ключу работает.";
    });
  }

  // ------------------------------------------------------------------------ голос

  var rec = { recorder: null, stream: null, chunks: [], timer: null, tooLong: false };

  function voiceState(name, text) {
    root.querySelectorAll("[data-voice-state], [data-voice-error]").forEach(function (p) { p.hidden = true; });
    var shown = name ? root.querySelector('[data-voice-state="' + name + '"]') : null;
    if (!shown && text) {
      shown = root.querySelector("[data-voice-error]");
      if (shown) shown.textContent = text;
    }
    if (shown) shown.hidden = false;
  }

  function recordButton() { return document.getElementById("voice-record"); }

  function setRecording(on) {
    var b = recordButton();
    if (!b) return;
    b.classList.toggle("recording", on);
    b.textContent = on ? "Остановить запись" : "Записать голосом";
  }

  function pickMime() {
    var list = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
    for (var i = 0; i < list.length; i++) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(list[i])) return list[i];
    }
    return "";  // браузер выберет сам
  }

  function releaseStream() {
    if (rec.stream) rec.stream.getTracks().forEach(function (t) { t.stop(); });
    rec.stream = null;
  }

  function sendAudio(blob) {
    if (blob.size > MAX_BYTES) return voiceState("too_long");
    voiceState("recognizing");
    var reader = new FileReader();
    reader.onload = function () {
      var b64 = String(reader.result).split(",")[1] || "";
      fetch(base + "/voice", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ audio: b64, mime: blob.type || "audio/webm" }),
      }).then(function (r) { return r.json(); }).then(function (data) {
        if (data && data.ok) {
          var box = field();
          if (box) { box.value = (box.value ? box.value + " " : "") + data.text; box.focus(); }
          voiceState(null);
        } else if (data && data.code === "too_long") {
          voiceState("too_long");
        } else {
          voiceState(null, (data && data.message) || (data && data.detail && data.detail.message) || "Распознавание сейчас недоступно. Напиши вопрос.");
        }
      }).catch(function () { voiceState(null, "Распознавание сейчас недоступно. Напиши вопрос."); });
    };
    reader.readAsDataURL(blob);
  }

  function stopRecording() {
    clearTimeout(rec.timer);
    if (rec.recorder && rec.recorder.state !== "inactive") rec.recorder.stop();
  }

  function startRecording() {
    if (!window.isSecureContext || !navigator.mediaDevices || !window.MediaRecorder) {
      return voiceState("insecure");
    }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      var mime = pickMime();
      rec.stream = stream;
      rec.chunks = [];
      rec.tooLong = false;
      rec.recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      rec.recorder.ondataavailable = function (e) { if (e.data && e.data.size) rec.chunks.push(e.data); };
      rec.recorder.onstop = function () {
        setRecording(false);
        releaseStream();
        if (rec.tooLong) return voiceState("too_long");  // длиннее 30 с не отправляем
        sendAudio(new Blob(rec.chunks, { type: rec.recorder.mimeType || mime || "audio/webm" }));
      };
      rec.recorder.start();
      voiceState(null);
      setRecording(true);
      rec.timer = setTimeout(function () { rec.tooLong = true; stopRecording(); }, MAX_RECORD_MS);
    }).catch(function () { voiceState("no_mic"); });
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
    if (t.hasAttribute("data-insert")) {  // готовая формулировка: в поле для правки, не в отправку
      var box = field();
      if (box) { box.value = t.getAttribute("data-insert"); box.focus(); }
      return;
    }
    if (t.hasAttribute("data-chat-mode")) return sendQuestion(t.getAttribute("data-chat-mode"));
    if (t.hasAttribute("data-voice")) {
      return rec.recorder && rec.recorder.state === "recording" ? stopRecording() : startRecording();
    }
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
