// Зона загрузки: показывает имя выбранного файла, принимает перетаскивание.
(function () {
  var input = document.getElementById("upload-file");
  var name = document.getElementById("upload-name");
  if (!input || !name) return;
  var zone = input.closest(".dropzone");
  function show() {
    name.textContent = input.files && input.files.length ? input.files[0].name : "Файл не выбран";
  }
  input.addEventListener("change", show);
  ["dragenter", "dragover"].forEach(function (ev) {
    zone.addEventListener(ev, function () { zone.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    zone.addEventListener(ev, function () { zone.classList.remove("over"); });
  });
})();
