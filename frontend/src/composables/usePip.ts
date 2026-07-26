import { readonly, ref } from "vue"

const isOpen = ref(false)

export function usePip() {
  return {
    isOpen: readonly(isOpen),
    open() {
      isOpen.value = true
    },
    close() {
      isOpen.value = false
    },
  }
}
