export type RouteColorToken = {
  hex: string
  bgClass: string
  borderClass: string
  textClass: string
  oklab: {
    l: number
    a: number
    b: number
  }
}

function hexToOklab(hex: string) {
  const normalized = hex.replace("#", "")
  const channels = [0, 2, 4].map((offset) =>
    Number.parseInt(normalized.slice(offset, offset + 2), 16) / 255,
  )

  const linear = channels.map((channel) =>
    channel <= 0.04045
      ? channel / 12.92
      : ((channel + 0.055) / 1.055) ** 2.4,
  )

  const [r, g, b] = linear
  const l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
  const m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
  const s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b

  const lRoot = Math.cbrt(l)
  const mRoot = Math.cbrt(m)
  const sRoot = Math.cbrt(s)

  return {
    l: 0.2104542553 * lRoot + 0.793617785 * mRoot - 0.0040720468 * sRoot,
    a: 1.9779984951 * lRoot - 2.428592205 * mRoot + 0.4505937099 * sRoot,
    b: 0.0259040371 * lRoot + 0.7827717662 * mRoot - 0.808675766 * sRoot,
  }
}

const ROUTE_COLOR_POOL = [
  { hex: "#2F6FDB", bgClass: "bg-[#2F6FDB]", borderClass: "border-[#2F6FDB]", textClass: "text-[#2F6FDB]" },
  { hex: "#3C8FD8", bgClass: "bg-[#3C8FD8]", borderClass: "border-[#3C8FD8]", textClass: "text-[#3C8FD8]" },
  { hex: "#2D9C95", bgClass: "bg-[#2D9C95]", borderClass: "border-[#2D9C95]", textClass: "text-[#2D9C95]" },
  { hex: "#38A76F", bgClass: "bg-[#38A76F]", borderClass: "border-[#38A76F]", textClass: "text-[#38A76F]" },
  { hex: "#73AB3E", bgClass: "bg-[#73AB3E]", borderClass: "border-[#73AB3E]", textClass: "text-[#73AB3E]" },
  { hex: "#97B338", bgClass: "bg-[#97B338]", borderClass: "border-[#97B338]", textClass: "text-[#97B338]" },
  { hex: "#C3A136", bgClass: "bg-[#C3A136]", borderClass: "border-[#C3A136]", textClass: "text-[#C3A136]" },
  { hex: "#D58A38", bgClass: "bg-[#D58A38]", borderClass: "border-[#D58A38]", textClass: "text-[#D58A38]" },
  { hex: "#D86B4A", bgClass: "bg-[#D86B4A]", borderClass: "border-[#D86B4A]", textClass: "text-[#D86B4A]" },
  { hex: "#D85A77", bgClass: "bg-[#D85A77]", borderClass: "border-[#D85A77]", textClass: "text-[#D85A77]" },
  { hex: "#B764C8", bgClass: "bg-[#B764C8]", borderClass: "border-[#B764C8]", textClass: "text-[#B764C8]" },
  { hex: "#8469DB", bgClass: "bg-[#8469DB]", borderClass: "border-[#8469DB]", textClass: "text-[#8469DB]" },
  { hex: "#557ED1", bgClass: "bg-[#557ED1]", borderClass: "border-[#557ED1]", textClass: "text-[#557ED1]" },
  { hex: "#4E9CCF", bgClass: "bg-[#4E9CCF]", borderClass: "border-[#4E9CCF]", textClass: "text-[#4E9CCF]" },
  { hex: "#43ACA2", bgClass: "bg-[#43ACA2]", borderClass: "border-[#43ACA2]", textClass: "text-[#43ACA2]" },
  { hex: "#4CB985", bgClass: "bg-[#4CB985]", borderClass: "border-[#4CB985]", textClass: "text-[#4CB985]" },
  { hex: "#5B74E2", bgClass: "bg-[#5B74E2]", borderClass: "border-[#5B74E2]", textClass: "text-[#5B74E2]" },
  { hex: "#5A95E0", bgClass: "bg-[#5A95E0]", borderClass: "border-[#5A95E0]", textClass: "text-[#5A95E0]" },
  { hex: "#59B4D9", bgClass: "bg-[#59B4D9]", borderClass: "border-[#59B4D9]", textClass: "text-[#59B4D9]" },
  { hex: "#61C39E", bgClass: "bg-[#61C39E]", borderClass: "border-[#61C39E]", textClass: "text-[#61C39E]" },
  { hex: "#8FC357", bgClass: "bg-[#8FC357]", borderClass: "border-[#8FC357]", textClass: "text-[#8FC357]" },
  { hex: "#D0B14A", bgClass: "bg-[#D0B14A]", borderClass: "border-[#D0B14A]", textClass: "text-[#D0B14A]" },
  { hex: "#E08C5A", bgClass: "bg-[#E08C5A]", borderClass: "border-[#E08C5A]", textClass: "text-[#E08C5A]" },
  { hex: "#D97792", bgClass: "bg-[#D97792]", borderClass: "border-[#D97792]", textClass: "text-[#D97792]" },
] as const satisfies readonly Omit<RouteColorToken, "oklab">[]

const ROUTE_COLOR_TOKENS: RouteColorToken[] = ROUTE_COLOR_POOL.map((token) => ({
  ...token,
  oklab: hexToOklab(token.hex),
}))
const DEFAULT_ROUTE_COLOR = ROUTE_COLOR_TOKENS[0]

// This is a screen-level assignment strategy, not a county-wide route identity
// registry. Kiosk views normally show a small route set, so we keep colors
// deterministic per visible set and maximize perceptual separation before
// accepting reuse when the 24-token pool is exhausted.
const PREFERENCE_STEP = 5
const REUSE_PENALTY = 0.12

export function normalizeRouteCode(routeCode: string): string {
  return routeCode.normalize("NFKC").trim().toUpperCase()
}

function fnv1a32(input: string): number {
  let hash = 0x811c9dc5
  for (const char of input) {
    hash ^= char.codePointAt(0) ?? 0
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash >>> 0
}

function mixHash(hash: number): number {
  let mixed = hash >>> 0
  mixed ^= mixed >>> 16
  mixed = Math.imul(mixed, 0x7feb352d) >>> 0
  mixed ^= mixed >>> 15
  mixed = Math.imul(mixed, 0x846ca68b) >>> 0
  mixed ^= mixed >>> 16
  return mixed >>> 0
}

function colorDistance(a: RouteColorToken, b: RouteColorToken): number {
  return Math.hypot(
    a.oklab.l - b.oklab.l,
    a.oklab.a - b.oklab.a,
    a.oklab.b - b.oklab.b,
  )
}

function candidateOrder(routeCode: string): RouteColorToken[] {
  const normalized = normalizeRouteCode(routeCode)
  if (!normalized) return [DEFAULT_ROUTE_COLOR]

  const baseHash = mixHash(fnv1a32(normalized) ^ normalized.length)
  const baseIndex = baseHash % ROUTE_COLOR_TOKENS.length
  const ordered: RouteColorToken[] = []

  for (let i = 0; i < ROUTE_COLOR_TOKENS.length; i += 1) {
    ordered.push(
      ROUTE_COLOR_TOKENS[
        (baseIndex + i * PREFERENCE_STEP) % ROUTE_COLOR_TOKENS.length
      ],
    )
  }

  return ordered
}

function routeBaseColorToken(routeCode: string): RouteColorToken {
  return candidateOrder(routeCode)[0] ?? DEFAULT_ROUTE_COLOR
}

export function buildRouteColorAssignments(routeCodes: string[]): Record<string, RouteColorToken> {
  const normalizedCodes = Array.from(
    new Set(routeCodes.map(normalizeRouteCode).filter(Boolean)),
  )
  const assignments: Record<string, RouteColorToken> = {}
  const usedCounts = new Map<string, number>()

  for (const routeCode of normalizedCodes) {
    const candidates = candidateOrder(routeCode)
    let bestCandidate = candidates[0] ?? DEFAULT_ROUTE_COLOR
    let bestScore = Number.NEGATIVE_INFINITY

    for (let rank = 0; rank < candidates.length; rank += 1) {
      const candidate = candidates[rank]
      const assignedTokens = Object.values(assignments)
      const minDistance = assignedTokens.length === 0
        ? 1
        : Math.min(...assignedTokens.map((token) => colorDistance(candidate, token)))
      const reusePenalty = (usedCounts.get(candidate.hex) ?? 0) * REUSE_PENALTY
      const preferenceBonus = (candidates.length - rank) / candidates.length / 100
      const score = minDistance - reusePenalty + preferenceBonus

      if (score > bestScore) {
        bestScore = score
        bestCandidate = candidate
      }
    }

    assignments[routeCode] = bestCandidate
    usedCounts.set(bestCandidate.hex, (usedCounts.get(bestCandidate.hex) ?? 0) + 1)
  }

  return assignments
}

export function routeColor(routeCode: string): string {
  return routeBaseColorToken(routeCode).hex
}

export function routeColorBgClass(routeCode: string): string {
  return routeBaseColorToken(routeCode).bgClass
}

export function routeColorBorderClass(routeCode: string): string {
  return routeBaseColorToken(routeCode).borderClass
}

export function routeColorTextClass(routeCode: string): string {
  return routeBaseColorToken(routeCode).textClass
}
