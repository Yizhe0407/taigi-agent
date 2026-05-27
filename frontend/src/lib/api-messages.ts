export const API_NETWORK_MESSAGES = {
  agent: "目前無法連到公車助理服務",
  asr: "目前無法連到語音辨識服務",
  tts: "目前無法連到語音播報服務",
  departures: "目前無法連到公車資訊服務",
  routeDetail: "目前無法連到路線詳情服務",
  routePlans: "目前無法連到路線規劃服務",
  moovo: "目前無法連到 MOOVO 站點服務",
} as const

export const UI_FALLBACK_MESSAGES = {
  agentOffline: "公車助理暫時無法連線，請稍後再試。",
  agentNoReply: "目前無法取得助理回覆",
  departuresUnavailable: "公車資訊暫時無法載入",
  routeDetailUnavailable: "路線詳情暫時無法載入",
  moovoUnavailable: "MOOVO 站點暫時無法載入",
} as const
