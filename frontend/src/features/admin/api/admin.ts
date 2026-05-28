import { apiFetch, ApiError } from "@/lib/api"

export class AdminApiError extends ApiError {
  constructor(message: string, status: number | null = null) {
    super(message, status)
    this.name = "AdminApiError"
  }
}

export type Direction = "去程" | "回程" | null

export interface KioskConfig {
  stop_name: string
  direction: Direction
  lat: number | null
  lng: number | null
}

export interface StopEntry {
  name: string
  lat: number
  lng: number
}

export async function fetchAdminKiosk(): Promise<KioskConfig> {
  const res = await apiFetch("/api/admin/kiosk", {
    errorClass: AdminApiError,
    networkMessage: "無法連接後台",
  })
  return (await res.json()) as KioskConfig
}

export async function updateAdminKiosk(config: {
  stop_name: string
  direction: Direction
  lat: number
  lng: number
}): Promise<KioskConfig> {
  const res = await apiFetch("/api/admin/kiosk", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
    errorClass: AdminApiError,
    networkMessage: "無法更新站牌設定",
  })
  return (await res.json()) as KioskConfig
}

export async function fetchAdminStops(): Promise<StopEntry[]> {
  const res = await apiFetch("/api/admin/stops", {
    errorClass: AdminApiError,
    networkMessage: "無法載入站牌清單",
  })
  return (await res.json()) as StopEntry[]
}
