import { type HTMLAttributes, defineComponent, h } from "vue"
import {
  DrawerClose,
  DrawerContent,
  DrawerDescription,
  DrawerOverlay,
  DrawerPortal,
  DrawerRoot,
  DrawerTitle,
  DrawerTrigger,
} from "vaul-vue"

import { cn } from "@/lib/utils"

const Drawer = DrawerRoot
const DrawerTriggerComponent = DrawerTrigger
const DrawerCloseComponent = DrawerClose
const DrawerPortalComponent = DrawerPortal

const DrawerOverlayComponent = defineComponent({
  props: { class: String as () => HTMLAttributes["class"] },
  setup(props, { attrs }) {
    return () =>
      h(DrawerOverlay, {
        class: cn("fixed inset-0 z-50 bg-kiosk-ink/45", props.class),
        ...attrs,
      })
  },
})

// DrawerContent wraps portal + overlay. Does NOT inject a handle —
// callers provide their own so custom handle styles are preserved.
const DrawerContentComponent = defineComponent({
  props: { class: String as () => HTMLAttributes["class"] },
  setup(props, { attrs, slots }) {
    return () =>
      h(DrawerPortal, {}, () => [
        h(DrawerOverlayComponent),
        h(
          DrawerContent,
          {
            class: cn(
              "fixed inset-x-0 bottom-0 z-50 flex h-auto flex-col rounded-t-[10px] border bg-background",
              props.class,
            ),
            ...attrs,
          },
          slots.default,
        ),
      ])
  },
})

const DrawerHeaderComponent = defineComponent({
  props: { class: String as () => HTMLAttributes["class"] },
  setup(props, { attrs, slots }) {
    return () =>
      h("div", { class: cn("grid gap-1.5 p-4 text-center sm:text-left", props.class), ...attrs }, slots.default?.())
  },
})

const DrawerFooterComponent = defineComponent({
  props: { class: String as () => HTMLAttributes["class"] },
  setup(props, { attrs, slots }) {
    return () =>
      h("div", { class: cn("mt-auto flex flex-col gap-2 p-4", props.class), ...attrs }, slots.default?.())
  },
})

const DrawerTitleComponent = defineComponent({
  props: { class: String as () => HTMLAttributes["class"] },
  setup(props, { attrs, slots }) {
    return () =>
      h(DrawerTitle, { class: cn("text-lg font-semibold leading-none tracking-tight", props.class), ...attrs }, slots.default?.())
  },
})

const DrawerDescriptionComponent = defineComponent({
  props: { class: String as () => HTMLAttributes["class"] },
  setup(props, { attrs, slots }) {
    return () =>
      h(DrawerDescription, { class: cn("text-sm text-muted-foreground", props.class), ...attrs }, slots.default?.())
  },
})

export {
  Drawer,
  DrawerTriggerComponent as DrawerTrigger,
  DrawerCloseComponent as DrawerClose,
  DrawerPortalComponent as DrawerPortal,
  DrawerOverlayComponent as DrawerOverlay,
  DrawerContentComponent as DrawerContent,
  DrawerHeaderComponent as DrawerHeader,
  DrawerFooterComponent as DrawerFooter,
  DrawerTitleComponent as DrawerTitle,
  DrawerDescriptionComponent as DrawerDescription,
}
