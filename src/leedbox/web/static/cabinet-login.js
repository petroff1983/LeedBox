"use strict";
const ticket = new URLSearchParams(window.location.hash.slice(1)).get("token");
window.history.replaceState(null, "", window.location.pathname);
if (ticket && /^[A-Za-z0-9_-]{43}$/.test(ticket)) {
  document.getElementById("ticket-token").value = ticket;
  document.getElementById("ticket-form").hidden = false;
  document.getElementById("login-hint").textContent = "Подтвердите вход в кабинет по личной ссылке из бота.";
}
