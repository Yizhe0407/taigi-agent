import {
  computed,
  defineComponent,
  reactive,
  shallowRef,
  watchEffect,
  type Ref,
  type Slots,
} from "vue"

export function useVModel<
  Props extends Record<string, unknown>,
  Key extends keyof Props & string,
>(
  props: Props,
  key: Key,
  emit: (...args: never[]) => unknown,
  options: { defaultValue?: Props[Key]; passive?: boolean } = {},
) {
  const update = emit as unknown as (event: `update:${Key}`, value: unknown) => void

  return computed({
    get: () => props[key] ?? options.defaultValue,
    set: (value) => update(`update:${key}`, value),
  }) as Ref<Props[Key]>
}

export function reactiveOmit<
  Source extends Record<string, unknown>,
  Keys extends keyof Source,
>(source: Source, ...keys: Keys[]): Omit<Source, Keys> {
  const omitted = new Set<PropertyKey>(keys)
  const target = reactive({}) as Record<string, unknown>

  watchEffect(() => {
    for (const key of Object.keys(target)) {
      if (!(key in source) || omitted.has(key)) {
        delete target[key]
      }
    }

    for (const [key, value] of Object.entries(source)) {
      if (!omitted.has(key)) {
        target[key] = value
      }
    }
  })

  return target as Omit<Source, Keys>
}

export function createReusableTemplate<Props extends Record<string, unknown>>() {
  const renderSlot = shallowRef<Slots["default"]>()

  const DefineTemplate = defineComponent({
    name: "DefineReusableTemplate",
    setup(_, { slots }) {
      renderSlot.value = slots.default
      return () => null
    },
  })

  const ReuseTemplate = defineComponent({
    name: "ReuseReusableTemplate",
    inheritAttrs: false,
    setup(_, { attrs }) {
      return () => renderSlot.value?.(attrs as Props) ?? null
    },
  })

  return [DefineTemplate, ReuseTemplate] as const
}
