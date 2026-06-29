<script setup lang="ts">
import { Check, MapPin, Search } from "@lucide/vue"
import { computed, onMounted, onUnmounted, ref } from "vue"

import { Map } from "@/components/ui/map"
import MapZoomControl from "@/features/route-planner/map/MapZoomControl.vue"
import YunlinServiceAreaLayer from "@/features/route-planner/map/YunlinServiceAreaLayer.vue"

import type { Direction, KioskConfig, StopEntry } from "./api/admin"
import { fetchAdminKiosk, fetchAdminStops, updateAdminKiosk } from "./api/admin"
import AdminStopsLayer from "./AdminStopsLayer.vue"

const VOYAGER_STYLE = "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json"

// ── State ─────────────────────────────────────────────────────────────────────

const current = ref<KioskConfig | null>(null)
const stops = ref<StopEntry[]>([])
const isLoading = ref(true)
const error = ref<string | null>(null)

// Pending selection (not yet applied)
const selectedStop = ref<StopEntry | null>(null)
const selectedDirection = ref<Direction>("回程")

// Search
const query = ref("")
const isSearchFocused = ref(false)

// Apply state
const isApplying = ref(false)
const applyError = ref<string | null>(null)
const applySuccess = ref(false)
let applySuccessTimer: ReturnType<typeof globalThis.setTimeout> | null = null

onUnmounted(() => {
  if (applySuccessTimer !== null) globalThis.clearTimeout(applySuccessTimer)
})

// Map ref for flyTo
const mapRef = ref<InstanceType<typeof Map> | null>(null)

function handleSearchBlur() {
  // Delay so mousedown on dropdown list fires before blur hides it
  globalThis.setTimeout(() => {
    isSearchFocused.value = false
  }, 150)
}

// ── Computed ──────────────────────────────────────────────────────────────────

/** Unique stop names filtered by search query. */
const filteredNames = computed<string[]>(() => {
  const q = query.value.trim()
  const allNames = [...new Set(stops.value.map((s) => s.name))].sort()
  if (!q) return allNames
  return allNames.filter((name) => name.includes(q))
})

/** Show dropdown when focused and query is non-empty. */
const showDropdown = computed(() => isSearchFocused.value && query.value.trim().length > 0)

const selectedStopName = computed(() => selectedStop.value?.name ?? null)

const canApply = computed(() => selectedStop.value !== null && !isApplying.value)

const directionLabel = computed(() => {
  if (selectedDirection.value === "去程") return "去程"
  if (selectedDirection.value === "回程") return "回程"
  return "兩方向"
})

// ── Init ──────────────────────────────────────────────────────────────────────

onMounted(async () => {
  try {
    const [cfg, allStops] = await Promise.all([fetchAdminKiosk(), fetchAdminStops()])
    current.value = cfg
    stops.value = allStops
    // Pre-select the currently active stop
    selectedDirection.value = cfg.direction
    // Pre-select the currently active stop by name
    const match = allStops.find((s) => s.name === cfg.stop_name)
    if (match) {
      selectedStop.value = match
      query.value = match.name
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : "載入失敗"
  } finally {
    isLoading.value = false
  }
})

// ── Handlers ──────────────────────────────────────────────────────────────────

function handleStopSelect(stop: StopEntry) {
  selectedStop.value = stop
  query.value = stop.name
  isSearchFocused.value = false
}

function handleNameSelect(name: string) {
  const stop = stops.value.find((s) => s.name === name)
  if (!stop) return
  selectedStop.value = stop
  query.value = name
  isSearchFocused.value = false

  // Fly map to the selected stop
  mapRef.value?.map?.flyTo({ center: [stop.lng, stop.lat], zoom: 16, duration: 800 })
}

async function handleApply() {
  if (!selectedStop.value) return
  isApplying.value = true
  applyError.value = null
  applySuccess.value = false
  try {
    const cfg = await updateAdminKiosk({
      stop_name: selectedStop.value.name,
      direction: selectedDirection.value,
      lat: selectedStop.value.lat,
      lng: selectedStop.value.lng,
    })
    current.value = cfg
    applySuccess.value = true
    applySuccessTimer = globalThis.setTimeout(() => {
      applySuccess.value = false
      applySuccessTimer = null
    }, 3000)
  } catch (e) {
    applyError.value = e instanceof Error ? e.message : "套用失敗"
  } finally {
    isApplying.value = false
  }
}
</script>

<template>
  <div class="w-full h-full bg-kiosk-bg font-tc flex flex-col overflow-hidden">
    <!-- Top bar -->
    <div
      class="relative z-10 bg-kiosk-bg grid grid-cols-[1fr_auto] items-center gap-5 pt-[18px] px-7 pb-4 border-b-2 border-kiosk-line shrink-0"
    >
      <div>
        <div class="text-[13px] text-kiosk-muted font-medium mb-0.5">系統管理</div>
        <div class="text-[30px] font-extrabold tracking-[-0.02em] leading-none text-kiosk-ink">
          站牌切換
        </div>
      </div>

      <div v-if="current" class="text-right">
        <div class="text-[13px] text-kiosk-muted font-medium mb-0.5">目前站牌</div>
        <div class="text-[20px] font-bold text-kiosk-ink leading-tight">
          {{ current.stop_name }}
          <span class="text-[14px] font-medium text-kiosk-muted ml-1">
            {{ current.direction ?? "兩方向" }}
          </span>
        </div>
      </div>
    </div>

    <!-- Body: panel + map -->
    <section class="flex-1 min-h-0 grid grid-cols-[22rem_minmax(0,1fr)] gap-5 pt-5 px-7 pb-6">
      <!-- Left panel -->
      <div class="flex flex-col gap-4 min-h-0">
        <!-- Error / loading -->
        <div v-if="isLoading" class="text-kiosk-muted text-sm">載入站牌清單…</div>
        <div v-else-if="error" class="text-red-500 text-sm">{{ error }}</div>

        <!-- Search -->
        <div v-else class="relative">
          <div
            class="flex items-center gap-2 bg-white border-2 border-kiosk-line rounded-2xl px-4 h-[52px] focus-within:border-kiosk-ink transition-colors"
          >
            <Search class="size-5 text-kiosk-muted shrink-0" />
            <input
              v-model="query"
              type="text"
              placeholder="搜尋站牌名稱…"
              class="flex-1 bg-transparent text-[16px] text-kiosk-ink placeholder:text-kiosk-muted outline-none font-[inherit]"
              @focus="isSearchFocused = true"
              @blur="handleSearchBlur"
            />
          </div>

          <!-- Dropdown -->
          <div
            v-if="showDropdown && filteredNames.length > 0"
            class="absolute top-full mt-1 left-0 right-0 bg-white border-2 border-kiosk-line rounded-2xl shadow-lg z-20 max-h-64 overflow-y-auto"
          >
            <button
              v-for="name in filteredNames.slice(0, 20)"
              :key="name"
              class="w-full text-left px-4 py-3 text-[15px] text-kiosk-ink hover:bg-kiosk-bg transition-colors first:rounded-t-2xl last:rounded-b-2xl font-[inherit]"
              @mousedown.prevent="handleNameSelect(name)"
            >
              <MapPin class="inline size-4 text-kiosk-muted mr-1.5 -mt-0.5" />
              {{ name }}
            </button>
            <div
              v-if="filteredNames.length > 20"
              class="px-4 py-2 text-[13px] text-kiosk-muted border-t border-kiosk-line"
            >
              還有 {{ filteredNames.length - 20 }} 筆，請繼續輸入縮小範圍
            </div>
          </div>
        </div>

        <!-- Selected stop info -->
        <div
          v-if="selectedStop"
          class="bg-white border-2 border-kiosk-line rounded-2xl p-4 flex flex-col gap-3"
        >
          <div>
            <div class="text-[12px] text-kiosk-muted font-medium mb-1">選取站牌</div>
            <div class="text-[18px] font-bold text-kiosk-ink leading-tight">
              {{ selectedStop.name }}
            </div>
            <div class="text-[12px] text-kiosk-muted mt-0.5 font-mono">
              {{ selectedStop.lat.toFixed(5) }}, {{ selectedStop.lng.toFixed(5) }}
            </div>
          </div>

          <!-- Direction selector -->
          <div>
            <div class="text-[12px] text-kiosk-muted font-medium mb-2">方向篩選</div>
            <div class="flex gap-2">
              <button
                v-for="opt in [
                  { value: '去程' as Direction, label: '去程' },
                  { value: '回程' as Direction, label: '回程' },
                  { value: null as Direction, label: '兩方向' },
                ]"
                :key="opt.label"
                class="flex-1 h-[40px] rounded-xl border-2 text-[14px] font-bold font-[inherit] cursor-pointer transition-colors"
                :class="
                  selectedDirection === opt.value
                    ? 'border-kiosk-ink bg-kiosk-ink text-white'
                    : 'border-kiosk-line bg-white text-kiosk-ink hover:border-kiosk-ink'
                "
                @click="selectedDirection = opt.value"
              >
                {{ opt.label }}
              </button>
            </div>
          </div>
        </div>

        <!-- Apply button -->
        <div class="mt-auto flex flex-col gap-2">
          <div v-if="applyError" class="text-red-500 text-sm">{{ applyError }}</div>
          <div
            v-if="applySuccess"
            class="text-green-600 text-sm font-medium flex items-center gap-1"
          >
            <Check class="size-4" />
            已套用，重整頁面即生效
          </div>
          <button
            :disabled="!canApply"
            class="w-full h-[56px] rounded-2xl text-[18px] font-bold font-[inherit] cursor-pointer transition-colors"
            :class="
              canApply
                ? 'bg-kiosk-ink text-white hover:opacity-90'
                : 'bg-kiosk-line text-kiosk-muted cursor-not-allowed'
            "
            @click="handleApply"
          >
            <span v-if="isApplying">套用中…</span>
            <span v-else-if="selectedStop">
              套用「{{ selectedStop.name }}」{{ directionLabel }}
            </span>
            <span v-else>先在地圖選取站牌</span>
          </button>
        </div>
      </div>

      <!-- Map — same voyager style as route planner, no Moovo -->
      <Map
        ref="mapRef"
        class="rounded-[28px] border-2 border-kiosk-line overflow-hidden min-h-0"
        :style-override="VOYAGER_STYLE"
        :viewport="{
          center: [120.5385, 23.697],
          zoom: 11,
        }"
      >
        <YunlinServiceAreaLayer />
        <MapZoomControl position="bottom-right" />
        <AdminStopsLayer
          :stops="stops"
          :selected-stop-name="selectedStopName"
          @select="handleStopSelect"
        />
      </Map>
    </section>
  </div>
</template>
