"use strict";
document.querySelectorAll("[data-copy-target]").forEach(button => {
  button.addEventListener("click", async () => {
    const field = document.getElementById(button.dataset.copyTarget);
    const status = button.closest(".filter-actions").nextElementSibling;
    try {
      await navigator.clipboard.writeText(field.value);
      status.textContent = "Текст скопирован.";
    } catch {
      field.focus(); field.select();
      status.textContent = "Автоматическое копирование недоступно. Текст выделен: скопируйте его вручную.";
    }
  });
});
