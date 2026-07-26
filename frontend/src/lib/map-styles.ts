/** Basemap style URLs shared by the route planner map and the admin kiosk map. */
export const MAP_STYLES = [
  {
    id: "voyager",
    label: "彩色",
    url: "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
  },
  {
    id: "positron",
    label: "明亮",
    url: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
  },
] as const

export type MapStyleId = (typeof MAP_STYLES)[number]["id"]

/** Admin kiosk map always uses the voyager style — same look as the route planner default. */
export const VOYAGER_STYLE_URL = MAP_STYLES[0].url
