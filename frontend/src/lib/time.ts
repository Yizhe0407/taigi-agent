export const TAIPEI_TIME_ZONE = "Asia/Taipei"

const taipeiHourMinuteFormatter = new Intl.DateTimeFormat("zh-TW", {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
  timeZone: TAIPEI_TIME_ZONE,
})

export function formatTaipeiHourMinute(date: Date): string {
  return taipeiHourMinuteFormatter.format(date)
}

export function todayTaipeiDateInputValue(date = new Date()): string {
  return new Intl.DateTimeFormat("sv-SE", {
    timeZone: TAIPEI_TIME_ZONE,
  }).format(date)
}

export function parseTaipeiDateTimeInput(value: string): Date | undefined {
  if (!value) return undefined
  return new Date(`${value}:00+08:00`)
}
