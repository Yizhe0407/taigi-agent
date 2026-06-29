<script setup lang="ts">
import {
  Bike,
  BusFront,
  LoaderCircle,
  MapPin,
  RefreshCw,
  TriangleAlert,
} from "@lucide/vue"
import { computed, ref, watch } from "vue"

import { Map, MapMarker, MapRoute, MarkerContent } from "@/components/ui/map"

import { isInYunlinCounty } from "../geo/yunlin-service-area"
import MapClickPicker from "../map/MapClickPicker.vue"
import MapZoomControl from "../map/MapZoomControl.vue"
import MoovoStationMarkers from "../map/MoovoStationMarkers.vue"
import RouteViewportFit from "../map/RouteViewportFit.vue"
import YunlinServiceAreaLayer from "../map/YunlinServiceAreaLayer.vue"
import type { KioskPlace, LngLat, MoovoStation, RouteOption } from "../types"
import { legDisplayCoordinates } from "../utils/route-display"

const props = defineProps<{
  kiosk: KioskPlace
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

const viewportCenter = computed(() => props.kiosk.coordinates)

// Fly to kiosk when the API resolves and replaces the fallback coordinates.
// We use flyTo (not a controlled :viewport) so user panning after destination
// selection doesn't get overridden on every parent re-render.
const mapRef = ref<InstanceType<typeof Map> | null>(null)
watch(viewportCenter, (coords) => {
  mapRef.value?.map?.flyTo({ center: coords, zoom: 13, duration: 600 })
})

const updateDestination = (coordinates: { lng: number; lat: number }) => {
  const nextDestination: LngLat = [coordinates.lng, coordinates.lat]
  if (!isInYunlinCounty(nextDestination)) {
    emit("reject-destination", nextDestination)
    return
  }
  emit("select-destination", nextDestination)
}

// ---------------------------------------------------------------------------
// Map style picker
// ---------------------------------------------------------------------------

const MAP_STYLES = [
  {
    id: "voyager",
    label: "彩色",
    url: "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
  },
  {
    id: "positron",
    label: "明亮",
    url: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
  },
] as const

type StyleId = (typeof MAP_STYLES)[number]["id"]

const activeStyleId = ref<StyleId>("voyager")
const showMoovoStations = ref(true)

const activeStyle = () =>
  MAP_STYLES.find((s) => s.id === activeStyleId.value)?.url ??
  MAP_STYLES[0].url

function selectStyle(id: StyleId) {
  activeStyleId.value = id
}
</script>

<template>
  <div class="relative min-h-[22rem] overflow-hidden bg-kiosk-dim">
    <Map
      ref="mapRef"
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
            v-if="legDisplayCoordinates(route, index).length > 1"
            :id="`${route.id}-leg-${index}`"
            :coordinates="legDisplayCoordinates(route, index)"
            :color="leg.mode === 'WALK' ? '#D86A1F' : '#1F5BBF'"
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
              class="grid size-10 place-items-center rounded-full border-2 border-white bg-kiosk-ink text-white shadow-xl shadow-kiosk-ink/30"
            >
              <BusFront class="size-5" />
            </div>
            <span
              class="absolute top-11 min-w-max rounded bg-white/95 px-2 py-1 text-xs font-medium text-kiosk-ink shadow-sm ring-1 ring-kiosk-line"
            >
              {{ kiosk.name }}
              <span v-if="kiosk.direction" class="ml-1 text-kiosk-muted">{{ kiosk.direction }}</span>
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
          <div class="-translate-y-3 text-kiosk-accent drop-shadow-lg">
            <MapPin class="size-9 fill-kiosk-accent stroke-white" />
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
      class="absolute left-3 top-3 z-10 flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-bold shadow-sm backdrop-blur transition-all duration-200 font-[inherit]"
      :class="
        isLoadingMoovoStations
          ? 'cursor-default border-kiosk-line bg-white/90 text-kiosk-faded'
          : moovoStationsError
            ? 'cursor-default border-kiosk-err/30 bg-kiosk-err-soft text-kiosk-err'
            : showMoovoStations
              ? 'border-kiosk-ok bg-kiosk-ok text-white hover:brightness-110'
              : 'border-kiosk-line bg-white/90 text-kiosk-faded hover:border-kiosk-line2 hover:text-kiosk-muted'
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
        class="size-3.5 animate-spin"
      />
      <TriangleAlert
        v-else-if="moovoStationsError"
        class="size-3.5"
      />
      <Bike
        v-else
        class="size-3.5 transition-transform duration-200"
        :class="showMoovoStations ? 'scale-110' : ''"
      />
      <span v-if="isLoadingMoovoStations">MOOVO 載入中</span>
      <span v-else-if="moovoStationsError">{{ moovoStationsError }}</span>
      <span v-else>MOOVO</span>
      <RefreshCw
        v-if="moovoStationsError"
        class="size-3.5 transition hover:opacity-70"
        @click.stop="$emit('refresh-moovo-stations')"
      />
    </button>

    <!-- Style picker -->
    <div class="absolute bottom-8 left-3 z-10 inline-flex items-center gap-0.5 rounded-full border border-kiosk-line bg-white/85 p-0.5 shadow-sm backdrop-blur">
      <button
        v-for="style in MAP_STYLES"
        :key="style.id"
        type="button"
        class="rounded-full px-3 py-1 text-xs font-bold transition-all duration-150 font-[inherit]"
        :class="
          activeStyleId === style.id
            ? 'bg-kiosk-ink text-white'
            : 'text-kiosk-faded hover:text-kiosk-muted'
        "
        @click="selectStyle(style.id)"
      >
        {{ style.label }}
      </button>
    </div>
  </div>
</template>
