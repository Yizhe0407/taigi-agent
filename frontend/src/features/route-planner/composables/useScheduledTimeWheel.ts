import { nextTick, ref, type Ref, useTemplateRef, watch } from "vue"

import { todayTaipeiDateInputValue } from "@/lib/time"

export type DepartureMode = "now" | "scheduled"

export const HOURS = Array.from({ length: 24 }, (_, index) => index)
export const MINUTES = Array.from({ length: 12 }, (_, index) => index * 5)

const WHEEL_ITEM_HEIGHT = 56

function snapToFiveMinutes(minutes: number): number {
  const snapped = Math.round(minutes / 5) * 5
  return snapped >= 60 ? 55 : snapped
}

function selectedTimeFromInput(value: string): { hour: number; minute: number } {
  const [, time = ""] = value.split("T")
  const [hourText, minuteText] = time.split(":")
  const parsedHour = Number.parseInt(hourText, 10)
  const parsedMinute = Number.parseInt(minuteText, 10)

  if (!Number.isNaN(parsedHour) && !Number.isNaN(parsedMinute)) {
    const snappedMinute = snapToFiveMinutes(parsedMinute)
    return {
      hour: Math.max(0, Math.min(23, parsedHour)),
      minute: MINUTES.includes(snappedMinute) ? snappedMinute : 0,
    }
  }

  const now = new Date()
  return {
    hour: now.getHours(),
    minute: snapToFiveMinutes(now.getMinutes()),
  }
}

export function useScheduledTimeWheel(
  scheduledDateTime: Ref<string>,
  departureMode: Ref<DepartureMode>,
) {
  const sheetOpen = ref(false)
  const pendingHour = ref(0)
  const pendingMinute = ref(0)
  const hourScrollEl = useTemplateRef<HTMLElement>("hour-wheel")
  const minuteScrollEl = useTemplateRef<HTMLElement>("minute-wheel")

  function openSheet() {
    const selected = selectedTimeFromInput(scheduledDateTime.value)
    pendingHour.value = selected.hour
    pendingMinute.value = selected.minute
    sheetOpen.value = true
  }

  watch(sheetOpen, async (open) => {
    if (!open) return
    await nextTick()

    if (hourScrollEl.value) {
      hourScrollEl.value.scrollTop = pendingHour.value * WHEEL_ITEM_HEIGHT
    }
    if (minuteScrollEl.value) {
      const minuteIndex = MINUTES.indexOf(pendingMinute.value)
      minuteScrollEl.value.scrollTop =
        Math.max(0, minuteIndex) * WHEEL_ITEM_HEIGHT
    }
  })

  function handleHourScroll(event: Event) {
    const el = event.target as HTMLElement
    const index = Math.round(el.scrollTop / WHEEL_ITEM_HEIGHT)
    pendingHour.value = HOURS[Math.max(0, Math.min(HOURS.length - 1, index))]
  }

  function handleMinuteScroll(event: Event) {
    const el = event.target as HTMLElement
    const index = Math.round(el.scrollTop / WHEEL_ITEM_HEIGHT)
    pendingMinute.value = MINUTES[Math.max(0, Math.min(MINUTES.length - 1, index))]
  }

  function confirmSheet() {
    const hour = String(pendingHour.value).padStart(2, "0")
    const minute = String(pendingMinute.value).padStart(2, "0")
    scheduledDateTime.value = `${todayTaipeiDateInputValue()}T${hour}:${minute}`
    departureMode.value = "scheduled"
    sheetOpen.value = false
  }

  return {
    sheetOpen,
    pendingHour,
    pendingMinute,
    openSheet,
    handleHourScroll,
    handleMinuteScroll,
    confirmSheet,
  }
}
