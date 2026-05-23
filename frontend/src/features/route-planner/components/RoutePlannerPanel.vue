<script setup lang="ts">
import {
  BusFront,
  CalendarClock,
  CircleCheck,
  Clock,
  Footprints,
  LoaderCircle,
  MapPin,
  Navigation,
  RotateCcw,
  TriangleAlert,
} from "@lucide/vue";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DateTimePicker } from "@/components/ui/date-time-picker";
import { Separator } from "@/components/ui/separator";

import type {
  LngLat,
  PlaceCoordinate,
  RouteLeg,
  RouteOption,
  RoutePlan,
} from "../types";

defineProps<{
  kiosk: PlaceCoordinate;
  destination: LngLat | null;
  isDestinationConfirmed: boolean;
  isPlanningRoute: boolean;
  routePlan: RoutePlan | null;
  routePlanError: string;
  routePlanErrorKind: "no-service" | "generic";
  selectedRoute: RouteOption | null;
  departureMode: "now" | "scheduled";
  scheduledDateTime: string;
}>();

defineEmits<{
  confirm: [];
  reset: [];
  "select-route": [routeId: string];
  "update:departureMode": [mode: "now" | "scheduled"];
  "update:scheduledDateTime": [value: string];
}>();

const coordinateLabel = (coordinates: LngLat) =>
  `${coordinates[1].toFixed(6)}, ${coordinates[0].toFixed(6)}`;

const durationLabel = (durationSeconds: number) => {
  const total = Math.max(1, Math.round(durationSeconds / 60));
  if (total < 60) return `${total} 分鐘`;
  const h = Math.floor(total / 60);
  const m = total % 60;
  return m === 0 ? `${h} 小時` : `${h} 小時 ${m} 分`;
};

const distanceLabel = (distanceMeters: number) =>
  distanceMeters >= 1000
    ? `${(distanceMeters / 1000).toFixed(1)} km`
    : `${Math.round(distanceMeters)} m`;

const transferLabel = (route: RouteOption) =>
  route.transferCount === 0 ? "不用轉乘" : `轉乘 ${route.transferCount} 次`;

const busRouteLabel = (leg: RouteLeg) =>
  leg.route?.shortName || leg.route?.longName || "公車";

const busRouteSummary = (route: RouteOption) => {
  const labels = route.legs
    .filter((leg) => leg.mode === "BUS")
    .map(busRouteLabel)
    .filter((label, index, all) => all.indexOf(label) === index);
  return labels.length ? labels.join(" + ") : "步行接駁";
};

const scheduleLabel = (value: string) =>
  new Intl.DateTimeFormat("zh-TW", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(value));
</script>

<template>
  <aside
    class="flex min-h-0 flex-col overflow-y-auto border-t border-border bg-background/96 backdrop-blur lg:border-l lg:border-t-0"
  >
    <div class="space-y-5 px-5 py-5 sm:px-6 lg:px-7 lg:py-7">
      <!-- Header -->
      <h1 class="text-xl font-semibold text-foreground">路線規劃</h1>

      <!-- Origin / destination -->
      <div class="space-y-3">
        <div class="flex gap-3">
          <div
            class="mt-1 grid size-8 shrink-0 place-items-center rounded-md bg-teal-600/12 text-teal-700"
          >
            <Navigation class="size-4" />
          </div>
          <div class="min-w-0">
            <p class="text-xs font-medium text-muted-foreground">起點</p>
            <p class="truncate text-sm font-semibold text-foreground">
              {{ kiosk.name }}
            </p>
          </div>
        </div>

        <Separator />

        <div class="flex gap-3">
          <div
            class="mt-1 grid size-8 shrink-0 place-items-center rounded-md bg-rose-600/12 text-rose-700"
          >
            <MapPin class="size-4" />
          </div>
          <div class="min-w-0">
            <p class="text-xs font-medium text-muted-foreground">目的地</p>
            <p v-if="destination" class="font-mono text-sm text-foreground">
              {{ coordinateLabel(destination) }}
            </p>
            <p v-else class="text-sm font-medium text-muted-foreground">
              尚未選點
            </p>
          </div>
        </div>
      </div>

      <!-- Departure time control -->
      <div class="space-y-2">
        <p class="text-xs font-medium text-muted-foreground">出發時間</p>
        <div class="grid grid-cols-2 gap-2">
          <button
            type="button"
            class="flex items-center justify-center gap-1.5 rounded-lg border py-2.5 text-sm font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :class="
              departureMode === 'now'
                ? 'border-primary bg-primary text-primary-foreground'
                : 'border-border bg-background text-foreground hover:bg-accent'
            "
            @click="$emit('update:departureMode', 'now')"
          >
            <Clock class="size-3.5" />
            現在出發
          </button>
          <button
            type="button"
            class="flex items-center justify-center gap-1.5 rounded-lg border py-2.5 text-sm font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :class="
              departureMode === 'scheduled'
                ? 'border-primary bg-primary text-primary-foreground'
                : 'border-border bg-background text-foreground hover:bg-accent'
            "
            @click="$emit('update:departureMode', 'scheduled')"
          >
            <CalendarClock class="size-3.5" />
            指定時間
          </button>
        </div>
        <DateTimePicker
          v-if="departureMode === 'scheduled'"
          :model-value="scheduledDateTime"
          :disabled="isPlanningRoute"
          @update:model-value="$emit('update:scheduledDateTime', $event)"
        />
      </div>

      <!-- Action buttons -->
      <div class="flex flex-col gap-2 sm:flex-row">
        <Button
          class="h-11 flex-1 gap-2"
          :disabled="
            !destination ||
            isPlanningRoute ||
            isDestinationConfirmed ||
            (departureMode === 'scheduled' && !scheduledDateTime)
          "
          @click="$emit('confirm')"
        >
          <LoaderCircle v-if="isPlanningRoute" class="size-4 animate-spin" />
          <CircleCheck v-else class="size-4" />
          {{ isPlanningRoute ? "規劃中" : "確認目的地" }}
        </Button>
        <Button
          variant="outline"
          class="h-11 gap-2"
          :disabled="!destination || isPlanningRoute"
          @click="$emit('reset')"
        >
          <RotateCcw class="size-4" />
          重選
        </Button>
      </div>

      <!-- Error / loading feedback -->
      <!-- No-service error (404) -->
      <div
        v-if="routePlanError && routePlanErrorKind === 'no-service'"
        class="space-y-2 rounded-lg border border-amber-500/30 bg-amber-50/60 px-4 py-3 dark:bg-amber-950/20"
      >
        <div
          class="flex items-center gap-2 text-sm font-semibold text-amber-700 dark:text-amber-400"
        >
          <CalendarClock class="size-4 shrink-0" />
          此時段無班次
        </div>
        <p class="text-xs text-amber-700/80 dark:text-amber-400/80">
          {{ routePlanError }}。請嘗試調整出發時間或選擇其他目的地。
        </p>
      </div>

      <!-- Generic error -->
      <div
        v-else-if="routePlanError"
        class="flex gap-3 rounded-lg border border-destructive/30 bg-destructive/8 px-4 py-3 text-sm text-destructive"
      >
        <TriangleAlert class="mt-0.5 size-4 shrink-0" />
        <p class="min-w-0 break-words">{{ routePlanError }}</p>
      </div>

      <!-- Planning in progress -->
      <div
        v-else-if="isPlanningRoute"
        class="flex items-center gap-3 rounded-lg border border-border bg-muted/35 px-4 py-3 text-sm text-muted-foreground"
      >
        <LoaderCircle class="size-4 shrink-0 animate-spin" />
        <p>正在向 OTP 規劃公車路線。</p>
      </div>

      <!-- Empty destination prompt -->
      <div
        v-else-if="!destination && !routePlan"
        class="rounded-lg border border-dashed border-border bg-muted/20 px-4 py-5 text-center text-sm text-muted-foreground"
      >
        <MapPin class="mx-auto mb-2 size-6 opacity-40" />
        點選地圖選擇目的地，再按「確認目的地」開始規劃。
      </div>

      <!-- Route results -->
      <template v-if="routePlan && selectedRoute">
        <Separator />

        <section class="space-y-3">
          <div class="flex items-center justify-between gap-3">
            <h2 class="text-sm font-semibold text-foreground">候選路線</h2>
            <Badge variant="outline"
              >{{ routePlan.routes.length }} 個方案</Badge
            >
          </div>

          <div class="grid gap-2">
            <button
              v-for="(route, index) in routePlan.routes"
              :key="route.id"
              type="button"
              class="rounded-lg border px-3 py-3 text-left transition hover:border-primary/50 hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              :class="
                route.id === selectedRoute.id
                  ? 'border-primary bg-accent'
                  : 'border-border bg-background'
              "
              @click="$emit('select-route', route.id)"
            >
              <span class="flex items-start justify-between gap-3">
                <span class="min-w-0">
                  <span class="block text-xs font-medium text-muted-foreground">
                    方案 {{ index + 1 }}
                  </span>
                  <span
                    class="block truncate text-sm font-semibold text-foreground"
                  >
                    {{ busRouteSummary(route) }}
                  </span>
                </span>
                <span class="shrink-0 text-sm font-semibold text-foreground">
                  {{ durationLabel(route.duration) }}
                </span>
              </span>
              <span
                class="mt-2 flex flex-wrap gap-2 text-xs text-muted-foreground"
              >
                <span>{{ transferLabel(route) }}</span>
                <span>{{ distanceLabel(route.distance) }}</span>
              </span>
            </button>
          </div>
        </section>

        <section class="space-y-3">
          <div class="flex items-center justify-between gap-3">
            <h2 class="text-sm font-semibold text-foreground">行程</h2>
            <Badge variant="secondary">
              {{ durationLabel(selectedRoute.duration) }}
            </Badge>
          </div>

          <ol class="space-y-2">
            <li
              v-for="(leg, index) in selectedRoute.legs"
              :key="`${leg.mode}-${leg.start}-${index}`"
              class="flex gap-3 rounded-lg border border-border bg-background px-3 py-3"
            >
              <div
                class="mt-0.5 grid size-7 shrink-0 place-items-center rounded-md"
                :class="
                  leg.mode === 'BUS'
                    ? 'bg-blue-600/12 text-blue-700'
                    : 'bg-amber-500/15 text-amber-700'
                "
              >
                <BusFront v-if="leg.mode === 'BUS'" class="size-4" />
                <Footprints v-else class="size-4" />
              </div>
              <div class="min-w-0 space-y-1">
                <p class="text-xs font-medium text-muted-foreground">
                  <span v-if="leg.mode === 'BUS'">
                    搭 {{ busRouteLabel(leg) }}
                  </span>
                  <span v-else>步行</span>
                  · {{ scheduleLabel(leg.start) }} -
                  {{ scheduleLabel(leg.end) }}
                </p>
                <p class="break-words text-sm font-medium text-foreground">
                  {{ leg.fromName }} → {{ leg.toName }}
                </p>
                <p class="text-xs text-muted-foreground">
                  {{ durationLabel(leg.duration) }} ·
                  {{ distanceLabel(leg.distance) }}
                </p>
              </div>
            </li>
          </ol>
        </section>
      </template>
    </div>

    <!-- Footer hint -->
    <div
      class="mt-auto border-t border-border bg-muted/30 px-5 py-4 text-sm text-muted-foreground sm:px-6 lg:px-7"
    >
      <p v-if="routePlan && selectedRoute" class="font-medium text-foreground">
        地圖顯示目前選取的候選路線。
      </p>
      <p v-else-if="isDestinationConfirmed" class="font-medium text-foreground">
        目的地已確認
      </p>
      <p v-else-if="destination">拖曳圖釘可微調座標。</p>
      <p v-else>點選地圖後可確認目的地。</p>
    </div>
  </aside>
</template>
