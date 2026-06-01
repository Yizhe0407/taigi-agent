from tools.kiosk_bus import (
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
            "name": "get_routes_at_stop",
            "description": (
                "查詢某站牌有哪些公車路線停靠，回傳路線清單。"
                "MUST 呼叫：使用者問某站有哪些路線、本站停靠幾路公車。"
                "NEVER：用來判斷哪條路線能到達目的地（此工具只列停靠路線，無法判斷目的地）。"
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
                "查詢本站有停靠的某路線去程與回程所有站牌名稱。"
                "MUST 呼叫：使用者問某路線的站牌、沿途停哪裡、去程或回程站序。"
                "NEVER：用來查即時到站時間（此工具只回傳站牌名稱，無即時資料）。"
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
}
