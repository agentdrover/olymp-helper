// Экран PIN: цифры с экрана и с клавиатуры, отправка сразу после 4-й цифры.
(function () {
  "use strict";
  const form = document.getElementById("pin-form");
  const input = document.getElementById("pin");
  if (!form || !input || input.disabled) return;

  function submitIfFull() {
    if (input.value.length === 4) {
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    }
  }
  function add(digit) {
    input.value = (input.value + digit).slice(0, 4);
    submitIfFull();
  }
  function back() { input.value = input.value.slice(0, -1); }

  form.querySelectorAll("[data-key]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.key === "back") back(); else add(btn.dataset.key);
      input.focus();
    });
  });
  input.addEventListener("input", () => {
    input.value = input.value.replace(/\D/g, "").slice(0, 4);
    submitIfFull();
  });
  // Цифры, набранные без фокуса в поле, тоже попадают в PIN.
  document.addEventListener("keydown", (e) => {
    if (e.target === input || e.ctrlKey || e.metaKey || e.altKey) return;
    if (/^[0-9]$/.test(e.key)) { add(e.key); e.preventDefault(); }
    else if (e.key === "Backspace") { back(); e.preventDefault(); }
  });
  input.focus();
})();
