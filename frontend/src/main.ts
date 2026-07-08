import { VueQueryPlugin } from "@tanstack/vue-query";
import { createApp } from "vue";

import App from "./App.vue";
import { reportClientEvent } from "./lib/report-client-event";
import router from "./router";
import "./style.css";

const app = createApp(App);

app.use(router);

app.use(VueQueryPlugin);

// Kiosk runs unattended — surface otherwise-invisible browser failures to the backend.
app.config.errorHandler = (err, _instance, info) => {
  reportClientEvent(
    "vue_error",
    err instanceof Error ? err.message : String(err),
    `${info}\n${err instanceof Error ? err.stack : ""}`,
  );
};

window.addEventListener("error", (event) => {
  reportClientEvent(
    "window_error",
    event.message || String(event.error),
    `${event.filename}:${event.lineno}:${event.colno}\n${event.error?.stack ?? ""}`,
  );
});

window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  reportClientEvent(
    "unhandled_rejection",
    reason instanceof Error ? reason.message : String(reason),
    reason instanceof Error ? reason.stack : undefined,
  );
});

app.mount("#app");
