import { apiFetch, ApiError } from "@/lib/api"
import { API_NETWORK_MESSAGES } from "@/lib/api-messages"

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
    networkMessage: API_NETWORK_MESSAGES.admin,
  })
  return (await res.json()) as KioskConfig
}

const ADMIN_TOKEN_KEY = "admin_token"

function adminTokenHeaders(): Record<string, string> {
  const token = sessionStorage.getItem(ADMIN_TOKEN_KEY)
  return token ? { "X-Admin-Token": token } : {}
}

/** Prompt once for the admin token and remember it; null if the user cancels. */
function promptForAdminToken(): string | null {
  const token = window.prompt("請輸入管理員權杖")
  if (token) sessionStorage.setItem(ADMIN_TOKEN_KEY, token)
  return token
}

export async function updateAdminKiosk(config: {
  stop_name: string
  direction: Direction
}): Promise<KioskConfig> {
  try {
    const res = await apiFetch("/api/admin/kiosk", {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...adminTokenHeaders() },
      body: JSON.stringify(config),
      errorClass: AdminApiError,
      networkMessage: API_NETWORK_MESSAGES.adminUpdate,
    })
    return (await res.json()) as KioskConfig
  } catch (error) {
    // 401 = ADMIN_TOKEN configured server-side but missing/wrong locally.
    // Prompt once and retry so a fresh deployment doesn't need a separate login page.
    if (error instanceof AdminApiError && error.status === 401 && promptForAdminToken()) {
      const res = await apiFetch("/api/admin/kiosk", {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...adminTokenHeaders() },
        body: JSON.stringify(config),
        errorClass: AdminApiError,
        networkMessage: API_NETWORK_MESSAGES.adminUpdate,
      })
      return (await res.json()) as KioskConfig
    }
    throw error
  }
}

export async function fetchAdminStops(): Promise<StopEntry[]> {
  const res = await apiFetch("/api/admin/stops", {
    errorClass: AdminApiError,
    networkMessage: API_NETWORK_MESSAGES.adminStops,
  })
  return (await res.json()) as StopEntry[]
}
