import type { Map as MapLibreMap } from "maplibre-gl"
import { onBeforeUnmount, watch, type Ref } from "vue"

export type LayerSetup = (map: MapLibreMap) => void | (() => void)

export function useMapLayer(
  map: Ref<MapLibreMap | null>,
  isLoaded: Ref<boolean>,
  setup: LayerSetup,
) {
  let cleanup: (() => void) | null = null

  const teardown = () => {
    cleanup?.()
    cleanup = null
  }

  watch(
    [map, isLoaded],
    ([mapInstance, loaded]) => {
      if (!mapInstance) {
        teardown()
        return
      }
      if (loaded) {
        if (cleanup) return
        const nextCleanup = setup(mapInstance)
        cleanup = typeof nextCleanup === "function" ? nextCleanup : null
        return
      }
      teardown()
    },
    { immediate: true },
  )

  onBeforeUnmount(teardown)
}
