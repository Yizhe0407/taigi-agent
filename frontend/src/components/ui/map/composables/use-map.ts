import { inject } from "vue"

import { MapContextKey, type MapContextValue } from "../context"

export function useMap(): MapContextValue {
  const context = inject(MapContextKey, null)
  if (!context) {
    throw new Error("useMap must be used within a Map component")
  }
  return context
}
