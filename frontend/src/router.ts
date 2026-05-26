import { createRouter, createWebHistory } from "vue-router"

import DepartureDashboardView from "@/features/departures/components/DepartureDashboardView.vue"

const RoutePlannerView = () =>
  import("@/features/route-planner/components/RoutePlannerView.vue")

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/",
      component: DepartureDashboardView,
    },
    {
      path: "/plan",
      component: RoutePlannerView,
    },
  ],
})

export default router
