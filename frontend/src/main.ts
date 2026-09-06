import { VueQueryPlugin } from "@tanstack/vue-query"
import { createApp } from "vue"

import App from "./App.vue"
import { createApplicationLifecycle } from "./lib/application-lifecycle"
import {
  reportClientEvent,
  shutdownClientEventReporting,
} from "./lib/report-client-event"
import router from "./router"
import "./style.css"

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

const app = createApp(App)

app.use(router)
app.use(VueQueryPlugin)

// Kiosk runs unattended — surface otherwise-invisible browser failures to the backend.
app.config.errorHandler = (err, _instance, info) => {
  reportClientEvent(
    "vue_error",
    errorMessage(err),
    `${info}\n${err instanceof Error ? err.stack : ""}`,
  )
}

const lifecycle = createApplicationLifecycle({
  app,
  mountTarget: "#app",
  reportClientEvent,
  shutdownClientEventReporting,
})

if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    lifecycle.requestObservedTeardown("application HMR teardown")
  })
}

try {
  await lifecycle.start()
} catch (startError) {
  let terminalError: unknown = startError
  try {
    await lifecycle.teardown()
  } catch (retryError) {
    terminalError = new AggregateError(
      [startError, retryError],
      "Application startup and retained rollback retry failed",
    )
  }
  app.config.errorHandler?.(terminalError, null, "application bootstrap")
  throw terminalError
}
