from tools.kiosk_bus import (
    check_stop_on_route,
    find_routes_to_destination,
    get_arrivals_here,
    get_route_stops,
    get_routes_at_stop,
    get_stop_arrival_statuses_here,
)

TOOL_SCHEMAS: list = [
    {
        "type": "function",
        "function": {
            "name": "get_arrivals_here",
            "description": (
                "查詢某路線下一班抵達本站的即時時間。"
                "MUST 呼叫：使用者提供路線號碼且明確問到站時間、等多久、幾分鐘後到、下一班何時來。"
                "NEVER：用訓練資料推斷時刻，即使你知道該路線；"
                "NEVER：使用者訊息只有路線號碼本身（無動詞、無問句）時絕對不呼叫；"
                "NEVER：使用者未提供路線號碼時不呼叫。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {
                        "type": "string",
                        "description": "路線號碼字串，例如 '201'、'7126'、'7720'。只填號碼，不加「路」字。",
                    },
                },
                "required": ["route"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stop_arrival_statuses_here",
            "description": (
                "查詢本站目前所有停靠路線的到站狀態（含末班、尚未到站、已過末班）。"
                "MUST 呼叫：使用者問本站現在還有哪些車、還有幾路、末班車走了嗎、哪些路線還在跑。"
                "NEVER：使用者只問單一路線的到站時間時，改用 get_arrivals_here。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_routes_to_destination",
            "description": (
                "查詢本站有哪些路線能到達指定目的地，回傳路線與方向。"
                "MUST 呼叫：使用者問「怎麼去某地」「要搭哪台去某地」「到某地搭什麼車」（尚未知路線號碼）。"
                "NEVER：目的地是遠距城市（台北、台中、高雄、嘉義）時不呼叫。"
                "NEVER：使用者已知路線號碼時不呼叫（改用 check_stop_on_route）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "destination": {
                        "type": "string",
                        "description": "目的地名稱，例如 '斗六火車站'、'北港朝天宮'。",
                    },
                },
                "required": ["destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_stop_on_route",
            "description": (
                "查詢某路線是否停靠指定站牌，回傳「有」或「沒有」。"
                "MUST 呼叫：使用者已說出路線號碼，且問有沒有停某站、能不能到某地。"
                "NEVER：用來查到站時間或完整站牌清單。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {
                        "type": "string",
                        "description": "路線號碼，例如 '201'、'Y01'。",
                    },
                    "stop_name": {
                        "type": "string",
                        "description": "要查詢的站牌或地點名稱，例如 '斗六火車站'。",
                    },
                },
                "required": ["route", "stop_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_routes_at_stop",
            "description": (
                "查詢某站牌有哪些公車路線停靠，回傳路線清單。"
                "MUST 呼叫：使用者問本站停靠幾路公車、這裡有哪些路線。"
                "NEVER：用來判斷哪條路線能到達目的地（改用 find_routes_to_destination）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "stop_name": {
                        "type": "string",
                        "description": "站牌名稱，例如 '雲林科技大學'、'斗六火車站'。問本站路線時填本站站名。",
                    },
                },
                "required": ["stop_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_route_stops",
            "description": (
                "查詢本站有停靠的某路線去程與回程所有站牌名稱（完整清單）。"
                "MUST 呼叫：使用者明確要求列出去程或回程全部站牌。"
                "NEVER：用來查即時到站時間（此工具只回傳站牌名稱，無即時資料）。"
                "NEVER：查某站有沒有停某站（改用 check_stop_on_route）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {
                        "type": "string",
                        "description": "路線號碼字串，例如 '7126'、'201'。只填號碼，不加「路」字。",
                    },
                },
                "required": ["route"],
            },
        },
    },
]

TOOL_HANDLERS: dict = {
    "get_arrivals_here": get_arrivals_here,
    "get_stop_arrival_statuses_here": get_stop_arrival_statuses_here,
    "get_route_stops": get_route_stops,
    "get_routes_at_stop": get_routes_at_stop,
    "check_stop_on_route": check_stop_on_route,
    "find_routes_to_destination": find_routes_to_destination,
}
