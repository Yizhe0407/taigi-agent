<script setup lang="ts">
import type { MarkerOptions } from "maplibre-gl"
import MapLibreGL from "maplibre-gl"
import {
  onMounted,
  provide,
  shallowRef,
  useAttrs,
  watch,
  watchEffect,
} from "vue"

import {
  currentSynchronousScopeReleaseAttempt,
  observeSynchronousScopeTeardown,
} from "@/lib/component-lifecycle"
import {
  createResourceOwner,
  type ResourceClaim,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"

import { useMap } from "./composables/use-map"
import { MarkerContextKey } from "./context"

type Props = {
  longitude: number
  latitude: number
  draggable?: boolean
  offset?: MarkerOptions["offset"]
  rotation?: number
  rotationAlignment?: MarkerOptions["rotationAlignment"]
  pitchAlignment?: MarkerOptions["pitchAlignment"]
}

defineOptions({ inheritAttrs: false })

const props = withDefaults(defineProps<Props>(), {
  draggable: false,
})
const attrs = useAttrs()
const emit = defineEmits<{
  click: [event: MouseEvent]
  drag: [lngLat: { lng: number; lat: number }]
}>()
const { map } = useMap()
const markerRef = shallowRef<MapLibreGL.Marker | null>(null)

provide(MarkerContextKey, { marker: markerRef, map })

const collectMarkerOptions = (): Partial<MarkerOptions> => {
  const options: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined) continue
    if (/^on[A-Z]/.test(key)) continue
    if (key === "class" || key === "style" || key === "element") continue
    options[key] = value
  }
  return options as Partial<MarkerOptions>
}

let ownedMarker: MapLibreGL.Marker | null = null
const markerOwner = createResourceOwner("MapLibre marker")
let attachmentClaim: ResourceClaim | null = null

function cleanupMarker(
  attempt: ResourceReleaseAttempt,
  unresolvedFailureAlreadyRepresented = false,
): boolean {
  const failures: unknown[] = []

  // Gate callbacks and future attachments before publishing the null marker;
  // injected consumers may synchronously re-enter while that ref is changing.
  let closeFailed = false
  try {
    markerOwner.close(undefined, attempt)
  } catch (failure) {
    closeFailed = true
    failures.push(failure)
  }

  try {
    markerRef.value = null
  } catch (failure) {
    failures.push(failure)
  }

  let disposeFailed = false
  try {
    markerOwner.dispose(attempt)
  } catch (failure) {
    disposeFailed = true
    failures.push(failure)
  }

  if (markerOwner.settled) {
    ownedMarker = null
    attachmentClaim = null
  } else if (
    !unresolvedFailureAlreadyRepresented
    && !closeFailed
    && !disposeFailed
  ) {
    failures.push(new Error("MapLibre marker cleanup remains unresolved"))
  }

  if (failures.length === 1) throw failures[0]
  if (failures.length > 1) {
    throw new AggregateError(failures, "Failed to release MapLibre marker")
  }
  return markerOwner.settled
}

function disposeMarker(attempt: ResourceReleaseAttempt) {
  cleanupMarker(attempt)
}

function failMarker(error: unknown, attempt: ResourceReleaseAttempt): never {
  try {
    cleanupMarker(attempt, true)
  } catch (cleanupError) {
    throw new AggregateError(
      [error, cleanupError],
      "Map marker failed and could not be fully released",
    )
  }
  throw error
}

const onClick = (event: MouseEvent) => {
  if (markerOwner.disposed) return
  const attempt: ResourceReleaseAttempt = {}
  try {
    emit("click", event)
  } catch (error) {
    if (markerOwner.disposed) throw error
    failMarker(error, attempt)
  }
}

const onDrag = () => {
  if (markerOwner.disposed) return
  const marker = ownedMarker
  if (!marker) return
  const attempt: ResourceReleaseAttempt = {}
  try {
    const { lng, lat } = marker.getLngLat()
    emit("drag", { lng, lat })
  } catch (error) {
    if (markerOwner.disposed) throw error
    failMarker(error, attempt)
  }
}

function applyMarkerProps(marker: MapLibreGL.Marker) {
  marker.setLngLat([props.longitude, props.latitude])
  marker.setDraggable(props.draggable)
  marker.setOffset(props.offset ?? [0, 0])
  marker.setRotation(props.rotation ?? 0)
  marker.setRotationAlignment(props.rotationAlignment ?? "auto")
  marker.setPitchAlignment(props.pitchAlignment ?? "auto")
}

function attachMarker(
  marker: MapLibreGL.Marker,
  nextMap: MapLibreGL.Map,
  attempt: ResourceReleaseAttempt,
) {
  let attachmentMayExist = false
  attachmentClaim = markerOwner.acquire(
    () => {
      // addTo() may mutate before throwing, so publish conservative ownership
      // before entering MapLibre.
      attachmentMayExist = true
      return marker.addTo(nextMap)
    },
    () => {
      if (!attachmentMayExist) return
      marker.remove()
      attachmentMayExist = false
    },
    "MapLibre marker attachment",
    attempt,
  )
}

function releaseMarkerAttachment(attempt: ResourceReleaseAttempt) {
  const claim = attachmentClaim
  if (!claim) return
  claim.release(attempt)
  if (attachmentClaim === claim && !claim.active) attachmentClaim = null
}

function moveMarkerToMap(nextMap: MapLibreGL.Map | null) {
  const marker = ownedMarker
  if (!marker || markerOwner.disposed) return
  const attempt: ResourceReleaseAttempt = {}

  try {
    releaseMarkerAttachment(attempt)
    if (nextMap) attachMarker(marker, nextMap, attempt)
  } catch (error) {
    if (markerOwner.disposed) throw error
    failMarker(error, attempt)
  }
}

onMounted(() => {
  if (markerOwner.disposed) return
  const setupAttempt = currentSynchronousScopeReleaseAttempt() ?? {}

  try {
    const element = document.createElement("div")
    const marker = new MapLibreGL.Marker({
      ...collectMarkerOptions(),
      element,
    })
    ownedMarker = marker
    markerRef.value = marker

    applyMarkerProps(marker)

    const ownedElement = marker.getElement()
    markerOwner.acquire(
      () => ownedElement.addEventListener("click", onClick),
      () => ownedElement.removeEventListener("click", onClick),
      "MapLibre marker click listener",
      setupAttempt,
    )
    markerOwner.acquire(
      () => marker.on("drag", onDrag),
      () => marker.off("drag", onDrag),
      "MapLibre marker drag listener",
      setupAttempt,
    )

    if (map.value) attachMarker(marker, map.value, setupAttempt)
  } catch (error) {
    if (markerOwner.disposed) throw error
    failMarker(error, setupAttempt)
  }
})

watch(map, moveMarkerToMap)

watchEffect(() => {
  const longitude = props.longitude
  const latitude = props.latitude
  const draggable = props.draggable
  const offset = props.offset
  const rotation = props.rotation
  const rotationAlignment = props.rotationAlignment
  const pitchAlignment = props.pitchAlignment
  const marker = ownedMarker
  if (!marker || markerOwner.disposed) return
  const attempt: ResourceReleaseAttempt = {}

  try {
    marker.setLngLat([longitude, latitude])
    marker.setDraggable(draggable)
    marker.setOffset(offset ?? [0, 0])
    marker.setRotation(rotation ?? 0)
    marker.setRotationAlignment(rotationAlignment ?? "auto")
    marker.setPitchAlignment(pitchAlignment ?? "auto")
  } catch (error) {
    if (markerOwner.disposed) throw error
    failMarker(error, attempt)
  }
})

const settleMarkerTeardown = observeSynchronousScopeTeardown(
  "MapMarker teardown",
  disposeMarker,
)

defineExpose({ settleMarkerTeardown })
</script>

<template>
  <slot />
</template>
