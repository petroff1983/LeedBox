"use strict";
// An explicit click, never a timer: browsers require a user gesture for app launch.
const appLink = document.getElementById("app-link");
if (appLink && /Android/i.test(navigator.userAgent) && appLink.dataset.android) {
  appLink.href = appLink.dataset.android;
}
