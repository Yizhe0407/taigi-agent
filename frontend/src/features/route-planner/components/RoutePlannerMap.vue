<script setup lang="ts">
import {
  Bike,
  BusFront,
  Layers,
  LoaderCircle,
  MapPin,
  RefreshCw,
  TriangleAlert,
} from "@lucide/vue"
import { computed, ref } from "vue"

import { Map, MapMarker, MapRoute, MarkerContent } from "@/components/ui/map"

import { isInYunlinCounty } from "../geo/yunlin-service-area"
import MapClickPicker from "../map/MapClickPicker.vue"
import MapZoomControl from "../map/MapZoomControl.vue"
import MoovoStationMarkers from "../map/MoovoStationMarkers.vue"
import RouteViewportFit from "../map/RouteViewportFit.vue"
import YunlinServiceAreaLayer from "../map/YunlinServiceAreaLayer.vue"
import type { LngLat, MoovoStation, PlaceCoordinate, RouteOption } from "../types"

const props = defineProps<{
  kiosk: PlaceCoordinate
  destination: LngLat | null
  route: RouteOption | null
  moovoStations: MoovoStation[]
  isLoadingMoovoStations: boolean
  moovoStationsError: string
}>()

const emit = defineEmits<{
  "select-destination": [coordinates: LngLat]
  "reject-destination": [coordinates: LngLat]
  "refresh-moovo-stations": []
}>()

// Use a computed so the map recenter if kiosk coordinates load after mount.
// In practice the fallback is already the catalog value, but this is safer.
const viewportCenter = computed(() => props.kiosk.coordinates)

const updateDestination = (coordinates: { lng: number; lat: number }) => {
  const nextDestination: LngLat = [coordinates.lng, coordinates.lat]
  if (!isInYunlinCounty(nextDestination)) {
    emit("reject-destination", nextDestination)
    return
  }
  emit("select-destination", nextDestination)
}

// ---------------------------------------------------------------------------
// Per-leg coordinate resolution
// ---------------------------------------------------------------------------

/**
 * Return display coordinates for a leg.
 * WALK legs from OTP sometimes have empty geometry (0 m walk at the kiosk stop).
 * Fall back to a straight connector between adjacent leg endpoints so the
 * dashed walk segment still renders.
 */
function legCoords(route: RouteOption, index: number): LngLat[] {
  const leg = route.legs[index]
  if (leg.coordinates.length > 1) return leg.coordinates
  if (leg.mode !== "WALK") return leg.coordinates

  const prev = index > 0 ? route.legs[index - 1] : null
  const next = index < route.legs.length - 1 ? route.legs[index + 1] : null
  const start = prev?.coordinates.at(-1)
  const end = next?.coordinates[0]
  if (start && end && (start[0] !== end[0] || start[1] !== end[1])) {
    return [start, end]
  }
  return leg.coordinates
}

// ---------------------------------------------------------------------------
// Map style picker
// ---------------------------------------------------------------------------

const MAP_STYLES = [
  {
    id: "positron",
    label: "明亮",
    url: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
  },
  {
    id: "voyager",
    label: "彩色",
    url: "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
  },
  {
    id: "dark-matter",
    label: "暗色",
    url: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
  },
] as const

type StyleId = (typeof MAP_STYLES)[number]["id"]

const activeStyleId = ref<StyleId>("positron")
const showStylePicker = ref(false)
const showMoovoStations = ref(true)

const activeStyle = () =>
  MAP_STYLES.find((s) => s.id === activeStyleId.value)?.url ??
  MAP_STYLES[0].url

function selectStyle(id: StyleId) {
  activeStyleId.value = id
  showStylePicker.value = false
}
</script>

<template>
  <div class="relative min-h-[22rem] overflow-hidden bg-muted">
    <Map
      :center="viewportCenter"
      :zoom="13"
      :pitch="0"
      :style-override="activeStyle()"
      class="h-full min-h-[22rem]"
    >
      <MapClickPicker
        @select="$emit('select-destination', $event)"
        @reject="$emit('reject-destination', $event)"
      />
      <MapZoomControl position="bottom-right" />
      <YunlinServiceAreaLayer />
      <!-- Render each leg: BUS = solid blue, WALK = dashed gray -->
      <template v-if="route">
        <template
          v-for="(leg, index) in route.legs"
          :key="`${route.id}-leg-${index}`"
        >
          <MapRoute
            v-if="legCoords(route, index).length > 1"
            :id="`${route.id}-leg-${index}`"
            :coordinates="legCoords(route, index)"
            :color="leg.mode === 'WALK' ? '#f97316' : '#2563eb'"
            :width="leg.mode === 'WALK' ? 3 : 5"
            :opacity="leg.mode === 'WALK' ? 0.8 : 0.85"
            :dash-array="leg.mode === 'WALK' ? [3, 4] : undefined"
            :line-cap="leg.mode === 'WALK' ? 'butt' : 'round'"
            :line-join="leg.mode === 'WALK' ? 'miter' : 'round'"
            :interactive="false"
          />
        </template>
        <RouteViewportFit
          v-if="route.coordinates.length > 1"
          :coordinates="route.coordinates"
        />
      </template>

      <MapMarker
        :longitude="kiosk.coordinates[0]"
        :latitude="kiosk.coordinates[1]"
      >
        <MarkerContent>
          <div class="relative grid place-items-center">
            <div
              class="grid size-10 place-items-center rounded-full border-2 border-white bg-teal-600 text-white shadow-xl shadow-teal-950/30"
            >
              <BusFront class="size-5" />
            </div>
            <span
              class="absolute top-11 min-w-max rounded bg-background/95 px-2 py-1 text-xs font-medium text-foreground shadow-sm ring-1 ring-border"
            >
              {{ kiosk.name }}
            </span>
          </div>
        </MarkerContent>
      </MapMarker>

      <MoovoStationMarkers
        v-if="showMoovoStations"
        :stations="moovoStations"
      />

      <MapMarker
        v-if="destination"
        draggable
        :longitude="destination[0]"
        :latitude="destination[1]"
        @drag="updateDestination"
      >
        <MarkerContent>
          <div class="-translate-y-3 text-rose-600 drop-shadow-lg">
            <MapPin class="size-9 fill-rose-600 stroke-white" />
          </div>
        </MarkerContent>
      </MapMarker>
    </Map>

    <!-- Top gradient overlay -->
    <div
      class="pointer-events-none absolute inset-x-0 top-0 h-24 bg-gradient-to-b from-black/15 to-transparent"
    />

    <button
      type="button"
      class="absolute left-3 top-3 z-10 flex items-center gap-2 rounded-md border border-border bg-background/90 px-3 py-2 text-xs font-medium shadow-sm backdrop-blur transition"
      :class="
        isLoadingMoovoStations || moovoStationsError
          ? 'cursor-default text-foreground'
          : showMoovoStations
            ? 'text-foreground hover:bg-accent'
            : 'text-muted-foreground hover:bg-accent'
      "
      :title="
        isLoadingMoovoStations ? 'MOOVO 載入中'
        : moovoStationsError ? moovoStationsError
        : showMoovoStations ? '隱藏 MOOVO 站點'
        : '顯示 MOOVO 站點'
      "
      :disabled="isLoadingMoovoStations"
      @click="!isLoadingMoovoStations && !moovoStationsError && (showMoovoStations = !showMoovoStations)"
    >
      <LoaderCircle
        v-if="isLoadingMoovoStations"
        class="size-3.5 animate-spin text-muted-foreground"
      />
      <TriangleAlert
        v-else-if="moovoStationsError"
        class="size-3.5 text-destructive"
      />
      <Bike
        v-else
        class="size-3.5 transition"
        :class="showMoovoStations ? 'text-emerald-600' : 'text-muted-foreground'"
      />
      <span v-if="isLoadingMoovoStations" class="text-muted-foreground">MOOVO 載入中</span>
      <span v-else-if="moovoStationsError">{{ moovoStationsError }}</span>
      <span v-else>MOOVO</span>
      <RefreshCw
        v-if="moovoStationsError"
        class="size-3.5 text-muted-foreground transition hover:text-foreground"
        @click.stop="$emit('refresh-moovo-stations')"
      />
    </button>

    <!-- Style picker toggle -->
    <div class="absolute bottom-8 left-3 z-10 flex flex-col items-start gap-1">
      <!-- Style buttons (shown when open) -->
      <transition
        enter-active-class="transition-all duration-150"
        enter-from-class="opacity-0 translate-y-1"
        enter-to-class="opacity-100 translate-y-0"
        leave-active-class="transition-all duration-100"
        leave-from-class="opacity-100 translate-y-0"
        leave-to-class="opacity-0 translate-y-1"
      >
        <div
          v-if="showStylePicker"
          class="mb-1 flex flex-col gap-1"
        >
          <button
            v-for="style in MAP_STYLES"
            :key="style.id"
            type="button"
            class="flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs font-medium shadow-sm backdrop-blur transition"
            :class="
              activeStyleId === style.id
                ? 'border-primary bg-primary text-primary-foreground'
                : 'border-border bg-background/90 text-foreground hover:bg-accent'
            "
            @click="selectStyle(style.id)"
          >
            {{ style.label }}
          </button>
        </div>
      </transition>

      <!-- Toggle button -->
      <button
        type="button"
        class="grid size-8 place-items-center rounded-md border border-border bg-background/90 text-foreground shadow-sm backdrop-blur transition hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        :class="showStylePicker && 'bg-accent'"
        :title="showStylePicker ? '關閉圖層選擇' : '切換地圖樣式'"
        @click="showStylePicker = !showStylePicker"
      >
        <Layers class="size-4" />
      </button>
    </div>
  </div>
</template>
