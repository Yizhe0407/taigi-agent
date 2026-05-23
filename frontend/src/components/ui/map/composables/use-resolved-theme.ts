import type { ComputedRef, MaybeRefOrGetter } from "vue"
import { computed, onBeforeUnmount, onMounted, ref, toValue } from "vue"

import type { Theme } from "../types"

function getDocumentTheme(): Theme | null {
  if (typeof document === "undefined") return null
  if (document.documentElement.classList.contains("dark")) return "dark"
  if (document.documentElement.classList.contains("light")) return "light"
  return null
}

function getSystemTheme(): Theme {
  if (typeof window === "undefined") return "light"
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light"
}

export function useResolvedTheme(
  themeProp?: MaybeRefOrGetter<Theme | undefined>,
): ComputedRef<Theme> {
  const docTheme = ref<Theme | null>(getDocumentTheme())
  const sysTheme = ref<Theme>(getSystemTheme())

  onMounted(() => {
    const observer = new MutationObserver(() => {
      docTheme.value = getDocumentTheme()
    })
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    })

    const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)")
    const onChange = (event: MediaQueryListEvent) => {
      sysTheme.value = event.matches ? "dark" : "light"
    }
    mediaQuery.addEventListener("change", onChange)

    onBeforeUnmount(() => {
      observer.disconnect()
      mediaQuery.removeEventListener("change", onChange)
    })
  })

  return computed(() => toValue(themeProp) ?? docTheme.value ?? sysTheme.value)
}
