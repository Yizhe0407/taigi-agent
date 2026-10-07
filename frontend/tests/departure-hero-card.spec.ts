import { mount } from "@vue/test-utils"
import { describe, expect, it, vi } from "vitest"

import DepartureHeroCard from "@/features/departures/components/DepartureHeroCard.vue"
import type { DepartureRouteStatus } from "@/features/departures/types"
import { splitDirectionLabel } from "@/features/departures/utils/departure-status"

vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }))

function route(overrides: Partial<DepartureRouteStatus> = {}): DepartureRouteStatus {
  return {
    id: "201-0",
    route: "201",
    routeId: "YUN201",
    direction: "往高鐵雲林站",
    goBack: 0,
    section: "available",
    decision: "can_wait",
    statusText: "約 7 分",
    decisionText: "可以等",
    minutes: 7,
    scheduledTime: null,
    carId: null,
    ...overrides,
  }
}

describe("splitDirectionLabel", () => {
  it("moves the backend 往 into a separate prefix", () => {
    expect(splitDirectionLabel("往高鐵雲林站")).toEqual({
      prefix: "往",
      destination: "高鐵雲林站",
    })
  })

  it("leaves 去程/回程 fallbacks without a prefix", () => {
    expect(splitDirectionLabel("去程")).toEqual({ prefix: null, destination: "去程" })
    expect(splitDirectionLabel("回程")).toEqual({ prefix: null, destination: "回程" })
  })
})

describe("DepartureHeroCard", () => {
  it("renders 往 once before the destination", () => {
    const wrapper = mount(DepartureHeroCard, { props: { nextBest: route() } })
    expect(wrapper.text()).toContain("往高鐵雲林站")
    expect(wrapper.text()).not.toContain("往往")
  })

  it("labels the minutes as a live arrival estimate, not a scheduled departure", () => {
    const wrapper = mount(DepartureHeroCard, { props: { nextBest: route() } })
    expect(wrapper.text()).toContain("預計到站")
    expect(wrapper.text()).not.toContain("預定發車")
  })

  it("does not prefix the empty state with 往", () => {
    const wrapper = mount(DepartureHeroCard, { props: { nextBest: null } })
    expect(wrapper.text()).toContain("無可搭班次")
    expect(wrapper.text()).not.toContain("往無可搭班次")
  })
})
