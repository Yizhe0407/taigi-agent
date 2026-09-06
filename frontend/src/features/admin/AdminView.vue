<script setup lang="ts">
import { Check, MapPin, Search } from "@lucide/vue"
import { computed, onMounted, ref } from "vue"

import MapZoomControl from "@/components/map/MapZoomControl.vue"
import YunlinServiceAreaLayer from "@/components/map/YunlinServiceAreaLayer.vue"
import { Map } from "@/components/ui/map"
import { observeAsynchronousScopeTeardown } from "@/lib/component-lifecycle"
import { VOYAGER_STYLE_URL } from "@/lib/map-styles"
import {
  createResourceOwner,
  type ResourceClaim,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"
import { createOwnedTimeout, type TimerLease } from "@/lib/timer-owner"

import type { Direction, KioskConfig, StopEntry } from "./api/admin"
import { fetchAdminKiosk, fetchAdminStops, updateAdminKiosk } from "./api/admin"
import AdminStopsLayer from "./AdminStopsLayer.vue"

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

// Map ref for flyTo
const mapRef = ref<InstanceType<typeof Map> | null>(null)

const lifetimeOwner = createResourceOwner("admin view")
const RESOLVED_VOID = Promise.resolve()

let applySuccessTimer: TimerLease | null = null
let searchBlurTimer: TimerLease | null = null
let initialLoadOperation: RequestOperation | null = null
let applyOperation: RequestOperation | null = null
let teardownComplete = false
let teardownOperation: Promise<void> | null = null
let applySuccessGeneration: object | null = null
let searchBlurGeneration: object | null = null

class RequestOperation {
  readonly controller: AbortController
  readonly abortClaim: ResourceClaim
  abortRequested = false
  abortReason: unknown = undefined
  task: Promise<void> | null = null

  constructor(
    resource: string,
    rollbackAttempt: ResourceReleaseAttempt,
  ) {
    const acquisition = lifetimeOwner.acquire(
      () => new AbortController(),
      (controller) => {
        this.abortRequested = true
        controller?.abort(this.abortReason)
      },
      resource,
      rollbackAttempt,
    )
    this.controller = acquisition.value
    this.abortClaim = acquisition
  }
}

const isAbortError = (failure: unknown) =>
  failure instanceof DOMException && failure.name === "AbortError"

const failureMessage = (failure: unknown) =>
  failure instanceof Error ? failure.message : String(failure)

function requestOperationAbort(
  operation: RequestOperation,
  reason: unknown,
  attempt: ResourceReleaseAttempt,
): void {
  if (!operation.abortRequested) {
    operation.abortRequested = true
    operation.abortReason = reason
  }
  operation.abortClaim.release(attempt)
}

function releaseCompletedOperation(operation: RequestOperation): void {
  if (!operation.abortRequested && operation.abortClaim.active) {
    operation.abortClaim.transfer()
  }
}

function cancelApplySuccessTimer(attempt: ResourceReleaseAttempt): void {
  applySuccessGeneration = null
  const timer = applySuccessTimer
  if (!timer) return
  timer.cancel(attempt)
  if (applySuccessTimer === timer && !timer.active) applySuccessTimer = null
}

function cancelSearchBlurTimer(attempt: ResourceReleaseAttempt): void {
  searchBlurGeneration = null
  const timer = searchBlurTimer
  if (!timer) return
  timer.cancel(attempt)
  if (searchBlurTimer === timer && !timer.active) searchBlurTimer = null
}

function handleSearchFocus(): void {
  if (lifetimeOwner.disposed) return
  const attempt: ResourceReleaseAttempt = {}
  cancelSearchBlurTimer(attempt)
  isSearchFocused.value = true
}

function handleSearchBlur(): void {
  if (lifetimeOwner.disposed || !isSearchFocused.value) return
  const attempt: ResourceReleaseAttempt = {}
  // Delay so mousedown on dropdown list fires before blur hides it.
  cancelSearchBlurTimer(attempt)
  const generation = {}
  searchBlurGeneration = generation
  const timer = createOwnedTimeout(
    lifetimeOwner,
    "admin search blur",
    () => {
      if (searchBlurGeneration !== generation) return
      searchBlurGeneration = null
      searchBlurTimer = null
      isSearchFocused.value = false
    },
    150,
    attempt,
  )
  if (searchBlurGeneration === generation && timer.active) searchBlurTimer = timer
}

// ── Computed ──────────────────────────────────────────────────────────────────

/** Unique stop names filtered by search query. */
const filteredNames = computed<string[]>(() => {
  const q = query.value.trim()
  const allNames = [...new Set(stops.value.map((stop) => stop.name))].sort()
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

async function runInitialLoad(operation: RequestOperation): Promise<void> {
  if (
    lifetimeOwner.disposed
    || initialLoadOperation !== operation
    || operation.controller.signal.aborted
  ) return

  const cleanupFailures: unknown[] = []
  const primaryFailures = new Set<unknown>()

  const abortSibling = (requestFailure: unknown) => {
    if (!isAbortError(requestFailure)) primaryFailures.add(requestFailure)
    try {
      requestOperationAbort(operation, requestFailure, {})
    } catch (cleanupFailure) {
      cleanupFailures.push(cleanupFailure)
    }
  }

  const ownRequest = async <T,>(request: () => Promise<T>): Promise<T> => {
    try {
      return await request()
    } catch (requestFailure) {
      abortSibling(requestFailure)
      throw requestFailure
    }
  }

  const results = await Promise.allSettled([
    ownRequest(() => fetchAdminKiosk(operation.controller.signal)),
    ownRequest(() => fetchAdminStops(operation.controller.signal)),
  ])
  const failures = [...primaryFailures, ...cleanupFailures]

  if (
    failures.length > 0
    && !lifetimeOwner.disposed
    && initialLoadOperation === operation
  ) {
    error.value = failures.map(failureMessage).join("；") || "載入失敗"
    return
  }

  const [kioskResult, stopsResult] = results
  if (
    lifetimeOwner.disposed
    || operation.controller.signal.aborted
    || initialLoadOperation !== operation
    || kioskResult.status !== "fulfilled"
    || stopsResult.status !== "fulfilled"
  ) return

  const cfg = kioskResult.value
  const allStops = stopsResult.value
  current.value = cfg
  stops.value = allStops
  selectedDirection.value = cfg.direction
  const match = allStops.find((stop) => stop.name === cfg.stop_name)
  if (match) {
    selectedStop.value = match
    query.value = match.name
  }
}

function startInitialLoad(): Promise<void> {
  if (lifetimeOwner.disposed) return teardownOperation ?? RESOLVED_VOID
  if (initialLoadOperation?.task) return initialLoadOperation.task

  const setupAttempt: ResourceReleaseAttempt = {}
  const operation = new RequestOperation(
    "admin initial-load abort controller",
    setupAttempt,
  )
  let task!: Promise<void>
  const physicalTask = Promise.resolve().then(() => runInitialLoad(operation))
  task = physicalTask
    .catch((failure) => {
      if (!lifetimeOwner.disposed && initialLoadOperation === operation && !isAbortError(failure)) {
        error.value = failureMessage(failure) || "載入失敗"
      }
    })
    .finally(() => {
      releaseCompletedOperation(operation)
      if (initialLoadOperation === operation) {
        initialLoadOperation = null
        if (!lifetimeOwner.disposed) isLoading.value = false
      }
    })
  operation.task = task
  initialLoadOperation = operation
  return task
}

onMounted(() => {
  startInitialLoad()
})

// ── Handlers ──────────────────────────────────────────────────────────────────

function handleStopSelect(stop: StopEntry): void {
  if (lifetimeOwner.disposed) return
  cancelSearchBlurTimer({})
  selectedStop.value = stop
  query.value = stop.name
  isSearchFocused.value = false
}

function handleNameSelect(name: string): void {
  if (lifetimeOwner.disposed) return
  cancelSearchBlurTimer({})
  const stop = stops.value.find((candidate) => candidate.name === name)
  if (!stop) return
  selectedStop.value = stop
  query.value = name
  isSearchFocused.value = false

  mapRef.value?.map?.flyTo({ center: [stop.lng, stop.lat], zoom: 16, duration: 800 })
}

async function runApply(
  operation: RequestOperation,
  config: { stop_name: string; direction: Direction },
): Promise<void> {
  if (
    lifetimeOwner.disposed
    || applyOperation !== operation
    || operation.controller.signal.aborted
  ) return

  try {
    const cfg = await updateAdminKiosk(config, operation.controller.signal)
    if (
      lifetimeOwner.disposed
      || applyOperation !== operation
      || operation.controller.signal.aborted
    ) return

    const attempt: ResourceReleaseAttempt = {}
    const generation = {}
    applySuccessGeneration = generation
    const timer = createOwnedTimeout(
      lifetimeOwner,
      "admin apply success",
      () => {
        if (applySuccessGeneration !== generation) return
        applySuccessGeneration = null
        applySuccessTimer = null
        applySuccess.value = false
      },
      3000,
      attempt,
    )
    if (applySuccessGeneration === generation && timer.active) applySuccessTimer = timer
    current.value = cfg
    applySuccess.value = true
  } catch (failure) {
    if (
      lifetimeOwner.disposed
      || applyOperation !== operation
      || operation.controller.signal.aborted
      || isAbortError(failure)
    ) return
    applyError.value = failure instanceof Error ? failure.message : "套用失敗"
  }
}

function handleApply(): Promise<void> {
  if (lifetimeOwner.disposed) return applyOperation?.task ?? teardownOperation ?? RESOLVED_VOID
  if (applyOperation?.task) return applyOperation.task

  const stop = selectedStop.value
  if (!stop) return RESOLVED_VOID

  // A new apply generation is forbidden until the exact previous timer claim
  // has been released successfully.
  const attempt: ResourceReleaseAttempt = {}
  cancelApplySuccessTimer(attempt)

  const config = {
    stop_name: stop.name,
    direction: selectedDirection.value,
  }
  const operation = new RequestOperation(
    "admin apply abort controller",
    attempt,
  )
  isApplying.value = true
  applyError.value = null
  applySuccess.value = false

  let task!: Promise<void>
  const physicalTask = Promise.resolve().then(() => runApply(operation, config))
  task = physicalTask.finally(() => {
    releaseCompletedOperation(operation)
    if (applyOperation === operation) {
      applyOperation = null
      if (!lifetimeOwner.disposed) isApplying.value = false
    }
  })
  operation.task = task
  applyOperation = operation
  return task
}

// ── Teardown ──────────────────────────────────────────────────────────────────

type TeardownAttempt = {
  releaseAttempt: ResourceReleaseAttempt
  operations: RequestOperation[]
  failures: unknown[]
}

async function finishTeardownAttempt(attempt: TeardownAttempt): Promise<void> {
  const taskResults = await Promise.allSettled(
    attempt.operations
      .map((operation) => operation.task)
      .filter((task): task is Promise<void> => task !== null),
  )
  for (const result of taskResults) {
    if (result.status === "rejected") attempt.failures.push(result.reason)
  }

  if (!lifetimeOwner.settled && attempt.failures.length === 0) {
    attempt.failures.push(
      new Error("Admin view cleanup left unresolved resources"),
    )
  }

  if (attempt.failures.length > 0) {
    throw new AggregateError(
      attempt.failures,
      "Failed to release admin view resources",
    )
  }
  teardownComplete = true
}

function requestTeardown(releaseAttempt: ResourceReleaseAttempt): Promise<void> {
  if (teardownComplete) return RESOLVED_VOID
  if (teardownOperation) return teardownOperation

  let beginAttempt!: (attempt: TeardownAttempt) => void
  const attemptGate = new Promise<TeardownAttempt>((resolve) => {
    beginAttempt = resolve
  })
  let operation!: Promise<void>
  operation = attemptGate
    .then(finishTeardownAttempt)
    .finally(() => {
      if (teardownOperation === operation) teardownOperation = null
    })
  // Publish the joinable operation before abort()/clearTimeout() can re-enter
  // component code. The physical cancellation pass still begins synchronously.
  teardownOperation = operation

  const operations = [initialLoadOperation, applyOperation]
    .filter((ownedOperation): ownedOperation is RequestOperation => ownedOperation !== null)
  const cancellationReason = new DOMException("AdminView lifetimeOwner.disposed", "AbortError")
  for (const ownedOperation of operations) {
    if (!ownedOperation.abortRequested) {
      ownedOperation.abortRequested = true
      ownedOperation.abortReason = cancellationReason
    }
  }

  const failures: unknown[] = []
  try {
    lifetimeOwner.dispose(releaseAttempt)
  } catch (failure) {
    failures.push(failure)
  }
  beginAttempt({ releaseAttempt, operations, failures })
  return operation
}

const settleAdminViewTeardown = observeAsynchronousScopeTeardown(
  "AdminView teardown",
  requestTeardown,
)

defineExpose({ settleAdminViewTeardown })
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
              @focus="handleSearchFocus"
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
        :style-override="VOYAGER_STYLE_URL"
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
