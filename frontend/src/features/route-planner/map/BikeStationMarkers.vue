<script setup lang="ts">
import { Bike } from "@lucide/vue"
import { computed, ref } from "vue"

import { MapMarker, MarkerContent, useMap } from "@/components/ui/map"
import { useMapLayer } from "@/components/ui/map/composables/use-map-layer"

import type { BikeStation } from "../types"

const props = defineProps<{
  stations: BikeStation[]
}>()

const { map, isLoaded } = useMap()
const zoom = ref(13)

const MAJOR_STATION_KEYWORDS = [
  "火車站",
  "高鐵",
  "轉運站",
  "車站",
  "大學",
  "科技大學",
  "醫院",
  "縣政府",
  "市公所",
  "鎮公所",
  "鄉公所",
  "高中",
  "國中",
  "市場",
]

useMapLayer(
  map,
  isLoaded,
  "Bike station map listeners",
  (nextMap, owner, fail) => {
    const updateZoom = () => {
      if (owner.signal.aborted) return
      try {
        zoom.value = nextMap.getZoom()
      } catch (error) {
        fail(error)
      }
    }

    updateZoom()
    owner.acquire(
      () => nextMap.on("zoom", updateZoom),
      () => nextMap.off("zoom", updateZoom),
    )
    owner.acquire(
      () => nextMap.on("moveend", updateZoom),
      () => nextMap.off("moveend", updateZoom),
    )
  },
)

const serviceStatusLabel = (status: number | null) => {
  if (status === 1) return "正常"
  if (status === 0) return "停止營運"
  return "狀態未知"
}

const stationHasRentBikes = (station: BikeStation) =>
  station.serviceStatus !== 0 &&
  station.availableRentBikes !== null &&
  station.availableRentBikes > 0

const stationMarkerClass = (station: BikeStation) => {
  if (station.serviceStatus === 0) {
    return "border-white bg-slate-500 text-white shadow-slate-950/20"
  }
  if (station.availableRentBikes === null) {
    return "border-white bg-amber-500 text-white shadow-amber-950/20"
  }
  if (station.availableRentBikes > 0) {
    return "border-white bg-emerald-600 text-white shadow-emerald-950/25"
  }
  return "border-white bg-zinc-400 text-white shadow-zinc-950/20"
}

// Each feed reports a different kind of timestamp, so the popup has to say
// which one it is showing: TDX carries the operator's own update time, while
// the MOOVO website only tells us when we read the page.
const PROVIDER_LABELS: Record<string, string> = {
  tdx: "TDX 即時資料",
  moovo_web: "MOOVO 官網",
}

const providerLabel = (provider: string) =>
  PROVIDER_LABELS[provider] ?? "未知來源"

const stationUpdateLabel = (station: BikeStation) => {
  if (!station.updateTime) return "未提供更新時間"
  const formatted = new Intl.DateTimeFormat("zh-TW", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(station.updateTime))
  return station.provider === "moovo_web"
    ? `${formatted} 讀取`
    : `${formatted} 更新`
}

const stationImportanceScore = (station: BikeStation) => {
  const keywordBoost = MAJOR_STATION_KEYWORDS.some((keyword) =>
    station.name.includes(keyword),
  )
    ? 40
    : 0
  const serviceBoost = station.serviceStatus === 1 ? 12 : 0

  return (
    (station.bikeCapacity ?? 0) * 4 +
    (station.availableRentBikes ?? 0) * 2 +
    keywordBoost +
    serviceBoost
  )
}

const rankedStations = computed(() =>
  [...props.stations]
    .sort((a, b) => {
      const scoreDiff = stationImportanceScore(b) - stationImportanceScore(a)
      if (scoreDiff !== 0) return scoreDiff
      return a.name.localeCompare(b.name, "zh-Hant")
    })
    .map((station, index) => ({
      station,
      rank: index + 1,
    })),
)

const visibleStationLimit = computed(() => {
  if (zoom.value < 10.5) return 10
  if (zoom.value < 11.5) return 24
  if (zoom.value < 12.7) return 48
  if (zoom.value < 14) return 80
  if (zoom.value < 15.2) return 140
  return Number.POSITIVE_INFINITY
})

const visibleStations = computed(() =>
  rankedStations.value
    .filter(({ rank }) => rank <= visibleStationLimit.value)
    .map(({ station }) => station),
)
</script>

<template>
  <MapMarker
    v-for="station in visibleStations"
    :key="station.stationUid"
    :longitude="station.lng"
    :latitude="station.lat"
    :offset="[0, -8]"
  >
    <MarkerContent class="group">
      <div class="relative grid place-items-center">
        <div
          class="grid size-7 place-items-center rounded-full border-2 text-white shadow-lg transition group-hover:scale-110"
          :class="stationMarkerClass(station)"
          :title="`${station.name}：${station.availableRentBikes === null ? '可借數未知' : `可借 ${station.availableRentBikes} 輛`}`"
        >
          <Bike class="size-3.5" />
        </div>
        <div
          class="pointer-events-none absolute bottom-9 left-1/2 z-20 hidden min-w-44 -translate-x-1/2 rounded-md border border-border bg-background/95 px-3 py-2 text-left text-xs text-foreground shadow-lg backdrop-blur group-hover:block"
        >
          <p class="truncate font-semibold">{{ station.name }}</p>
          <p class="mt-1 text-muted-foreground">
            <template v-if="station.availableRentBikes === null">可借數未知</template>
            <template v-else>可借 {{ station.availableRentBikes }} 輛</template>
          </p>
          <p class="mt-0.5 text-muted-foreground">
            {{ serviceStatusLabel(station.serviceStatus) }} ·
            {{ stationUpdateLabel(station) }}
          </p>
          <p class="mt-0.5 text-muted-foreground">
            資料來源：{{ providerLabel(station.provider) }}
          </p>
        </div>
        <span
          v-if="stationHasRentBikes(station)"
          class="absolute -right-1 -top-1 grid min-w-4 place-items-center rounded-full bg-background px-1 text-[10px] font-semibold leading-4 text-emerald-700 shadow-sm ring-1 ring-emerald-600/30"
        >
          {{ Math.min(station.availableRentBikes ?? 0, 99) }}
        </span>
      </div>
    </MarkerContent>
  </MapMarker>
</template>
