import { onBeforeUnmount, onMounted, shallowRef } from "vue"

export function useNow(intervalMs = 1_000) {
  const now = shallowRef(new Date())
  let timer: ReturnType<typeof setInterval> | null = null

  onMounted(() => {
    timer = setInterval(() => {
      now.value = new Date()
    }, intervalMs)
  })

  onBeforeUnmount(() => {
    if (timer) clearInterval(timer)
  })

  return { now }
}
