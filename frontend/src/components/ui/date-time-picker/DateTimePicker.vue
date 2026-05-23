<script setup lang="ts">
/**
 * 24h Date & Time Picker — Vue port of rudrodip/shadcn-date-time-picker
 * (https://time.rdsx.dev)
 *
 * modelValue: YYYY-MM-DDTHH:mm string (Taiwan time, no tz suffix).
 * Emits the same format on any change.
 */
import { CalendarDate } from "@internationalized/date"
import { CalendarIcon } from "@lucide/vue"
import type { DateValue } from "reka-ui"
import { computed, nextTick, ref, watch } from "vue"

import { Button } from "@/components/ui/button"
import { Calendar } from "@/components/ui/calendar"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import {
  ScrollArea,
  ScrollBar,
} from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"

const props = defineProps<{
  modelValue: string // "YYYY-MM-DDTHH:mm" or ""
  disabled?: boolean
  class?: string
}>()

const emit = defineEmits<{
  "update:modelValue": [value: string]
}>()

// ---------------------------------------------------------------------------
// Open state
// ---------------------------------------------------------------------------

const open = ref(false)

// Scroll the active time button into view whenever the popover opens
watch(open, async (isOpen) => {
  if (!isOpen) return
  await nextTick()
  document
    .querySelector("[data-time-active]")
    ?.scrollIntoView({ block: "center" })
})

// ---------------------------------------------------------------------------
// Derived parts from modelValue
// ---------------------------------------------------------------------------

const datePart = computed(() => props.modelValue?.slice(0, 10) ?? "")

const selectedHour = computed(() =>
  props.modelValue ? parseInt(props.modelValue.slice(11, 13), 10) : -1,
)
const selectedMinute = computed(() =>
  props.modelValue ? parseInt(props.modelValue.slice(14, 16), 10) : -1,
)

// ---------------------------------------------------------------------------
// Calendar (reka-ui uses DateValue, not JS Date)
// ---------------------------------------------------------------------------

const calendarValue = computed<DateValue | undefined>(() => {
  if (!datePart.value) return undefined
  const [y, m, d] = datePart.value.split("-").map(Number)
  return new CalendarDate(y, m, d)
})

const todayCalendarDate = computed(() => {
  const now = new Date()
  return new CalendarDate(now.getFullYear(), now.getMonth() + 1, now.getDate())
})

// ---------------------------------------------------------------------------
// Emit helper
// ---------------------------------------------------------------------------

function buildValue(date: string, hour: number, minute: number): string {
  return `${date}T${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`
}

function onDateSelect(value: DateValue | undefined) {
  if (!value) return
  const y = value.year
  const m = String(value.month).padStart(2, "0")
  const d = String(value.day).padStart(2, "0")
  const dateStr = `${y}-${m}-${d}`
  const h = selectedHour.value >= 0 ? selectedHour.value : 8
  const min = selectedMinute.value >= 0 ? selectedMinute.value : 0
  emit("update:modelValue", buildValue(dateStr, h, min))
}

function onHourClick(hour: number) {
  if (!datePart.value) return
  const min = selectedMinute.value >= 0 ? selectedMinute.value : 0
  emit("update:modelValue", buildValue(datePart.value, hour, min))
}

function onMinuteClick(minute: number) {
  if (!datePart.value) return
  const h = selectedHour.value >= 0 ? selectedHour.value : 8
  emit("update:modelValue", buildValue(datePart.value, h, minute))
}

// ---------------------------------------------------------------------------
// Display label
// ---------------------------------------------------------------------------

const displayLabel = computed(() => {
  if (!props.modelValue) return "MM/DD/YYYY HH:mm"
  try {
    const date = new Date(`${props.modelValue}:00+08:00`)
    return new Intl.DateTimeFormat("zh-TW", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).format(date)
  } catch {
    return props.modelValue
  }
})

// ---------------------------------------------------------------------------
// Data arrays
// ---------------------------------------------------------------------------

const hours = Array.from({ length: 24 }, (_, i) => i)
const minutes = Array.from({ length: 12 }, (_, i) => i * 5)
</script>

<template>
  <Popover v-model:open="open">
    <PopoverTrigger as-child>
      <Button
        variant="outline"
        :disabled="disabled"
        :class="
          cn(
            'w-full justify-start text-left font-normal',
            !modelValue && 'text-muted-foreground',
            props.class,
          )
        "
      >
        <CalendarIcon class="mr-2 size-4" />
        {{ displayLabel }}
      </Button>
    </PopoverTrigger>

    <PopoverContent class="w-auto p-0">
      <div class="sm:flex">
        <!-- Calendar for date -->
        <Calendar
          :model-value="calendarValue"
          :min-value="todayCalendarDate"
          initial-focus
          @update:model-value="onDateSelect"
        />

        <!-- Hour & minute columns -->
        <div
          class="flex flex-col divide-y sm:h-[300px] sm:flex-row sm:divide-x sm:divide-y-0"
        >
          <!-- Hours 0–23 -->
          <ScrollArea class="w-64 sm:w-auto">
            <div class="flex p-2 sm:flex-col">
              <Button
                v-for="hour in hours"
                :key="hour"
                size="icon"
                :variant="selectedHour === hour ? 'default' : 'ghost'"
                class="aspect-square shrink-0 sm:w-full"
                :data-time-active="selectedHour === hour ? '' : undefined"
                @click="onHourClick(hour)"
              >
                {{ hour }}
              </Button>
            </div>
            <ScrollBar orientation="horizontal" class="sm:hidden" />
          </ScrollArea>

          <!-- Minutes in 5-minute steps -->
          <ScrollArea class="w-64 sm:w-auto">
            <div class="flex p-2 sm:flex-col">
              <Button
                v-for="minute in minutes"
                :key="minute"
                size="icon"
                :variant="selectedMinute === minute ? 'default' : 'ghost'"
                class="aspect-square shrink-0 sm:w-full"
                :data-time-active="selectedMinute === minute ? '' : undefined"
                @click="onMinuteClick(minute)"
              >
                {{ String(minute).padStart(2, "0") }}
              </Button>
            </div>
            <ScrollBar orientation="horizontal" class="sm:hidden" />
          </ScrollArea>
        </div>
      </div>
    </PopoverContent>
  </Popover>
</template>
