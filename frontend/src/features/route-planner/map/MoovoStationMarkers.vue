<script setup lang="ts">
import { Bike } from "@lucide/vue"
import { computed, ref, watch } from "vue"

import { MapMarker, MarkerContent, useMap } from "@/components/ui/map"

import type { MoovoStation } from "../types"

const props = defineProps<{
  stations: MoovoStation[]
}>()

const { map } = useMap()
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

const updateZoom = () => {
  if (map.value) zoom.value = map.value.getZoom()
}

watch(
  map,
  (nextMap, _previousMap, onCleanup) => {
    if (!nextMap) return

    updateZoom()
    nextMap.on("zoom", updateZoom)
    nextMap.on("moveend", updateZoom)

    onCleanup(() => {
      nextMap.off("zoom", updateZoom)
      nextMap.off("moveend", updateZoom)
    })
  },
  { immediate: true },
)

const serviceStatusLabel = (status: number) => {
  if (status === 1) return "正常"
  if (status === 0) return "停止營運"
  return "暫停服務"
}

const stationHasRentBikes = (station: MoovoStation) =>
  station.serviceStatus === 1 && station.availableRentBikes > 0

const stationMarkerClass = (station: MoovoStation) => {
  if (station.serviceStatus !== 1) {
    return "border-white bg-slate-500 text-white shadow-slate-950/20"
  }
  if (station.availableRentBikes > 0) {
    return "border-white bg-emerald-600 text-white shadow-emerald-950/25"
  }
  return "border-white bg-zinc-400 text-white shadow-zinc-950/20"
}

const stationUpdateLabel = (value: string | null) => {
  if (!value) return "未提供更新時間"
  return new Intl.DateTimeFormat("zh-TW", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(value))
}

const stationImportanceScore = (station: MoovoStation) => {
  const keywordBoost = MAJOR_STATION_KEYWORDS.some((keyword) =>
    station.name.includes(keyword),
  )
    ? 40
    : 0
  const serviceBoost = station.serviceStatus === 1 ? 12 : 0

  return (
    station.bikeCapacity * 4 +
    station.availableRentBikes * 2 +
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
          :title="`${station.name}：可借 ${station.availableRentBikes} 輛`"
        >
          <Bike class="size-3.5" />
        </div>
        <div
          class="pointer-events-none absolute bottom-9 left-1/2 z-20 hidden min-w-44 -translate-x-1/2 rounded-md border border-border bg-background/95 px-3 py-2 text-left text-xs text-foreground shadow-lg backdrop-blur group-hover:block"
        >
          <p class="truncate font-semibold">{{ station.name }}</p>
          <p class="mt-1 text-muted-foreground">
            可借 {{ station.availableRentBikes }} 輛
          </p>
          <p class="mt-0.5 text-muted-foreground">
            {{ serviceStatusLabel(station.serviceStatus) }} ·
            {{ stationUpdateLabel(station.updateTime) }}
          </p>
        </div>
        <span
          v-if="stationHasRentBikes(station)"
          class="absolute -right-1 -top-1 grid min-w-4 place-items-center rounded-full bg-background px-1 text-[10px] font-semibold leading-4 text-emerald-700 shadow-sm ring-1 ring-emerald-600/30"
        >
          {{ Math.min(station.availableRentBikes, 99) }}
        </span>
      </div>
    </MarkerContent>
  </MapMarker>
</template>
