import type { ComputedRef, MaybeRefOrGetter } from "vue"
import { computed, onMounted, ref, toValue } from "vue"

import {
  createComponentFailureReporter,
  currentSynchronousScopeReleaseAttempt,
} from "@/lib/component-lifecycle"
import type {
  ResourceOwner,
  ResourceReleaseAttempt,
} from "@/lib/resource-owner"

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
  owner: ResourceOwner,
  themeProp?: MaybeRefOrGetter<Theme | undefined>,
): ComputedRef<Theme> {
  const docTheme = ref<Theme | null>(getDocumentTheme())
  const sysTheme = ref<Theme>(getSystemTheme())
  const reportCallbackFailure = createComponentFailureReporter(
    "resolved map theme callback",
  )

  const terminateUpdates = (failure: unknown) => {
    const failures = [failure]
    try {
      owner.dispose({})
    } catch (cleanupFailure) {
      failures.push(cleanupFailure)
    }
    reportCallbackFailure(
      failures.length === 1
        ? failure
        : new AggregateError(failures, "Map theme callback and cleanup failed"),
    )
  }

  const onDocumentThemeChange = () => {
    if (owner.disposed) return
    try {
      const theme = getDocumentTheme()
      if (!owner.disposed) docTheme.value = theme
    } catch (failure) {
      terminateUpdates(failure)
    }
  }
  const onSystemThemeChange = (event: MediaQueryListEvent) => {
    if (owner.disposed) return
    try {
      const theme = event.matches ? "dark" : "light"
      if (!owner.disposed) sysTheme.value = theme
    } catch (failure) {
      terminateUpdates(failure)
    }
  }

  onMounted(() => {
    if (owner.disposed) return
    const setupAttempt: ResourceReleaseAttempt =
      currentSynchronousScopeReleaseAttempt() ?? {}

    try {
      const observer = new MutationObserver(onDocumentThemeChange)
      owner.acquire(
        () => observer.observe(document.documentElement, {
          attributes: true,
          attributeFilter: ["class"],
        }),
        () => observer.disconnect(),
        "document theme observer",
        setupAttempt,
      )

      const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)")
      owner.acquire(
        () => mediaQuery.addEventListener("change", onSystemThemeChange),
        () => mediaQuery.removeEventListener("change", onSystemThemeChange),
        "system theme listener",
        setupAttempt,
      )
    } catch (setupError) {
      try {
        owner.dispose(setupAttempt)
      } catch (cleanupError) {
        throw new AggregateError(
          [setupError, cleanupError],
          "Failed to set up and roll back theme observers",
        )
      }
      throw setupError
    }
  })

  return computed(() => toValue(themeProp) ?? docTheme.value ?? sysTheme.value)
}
