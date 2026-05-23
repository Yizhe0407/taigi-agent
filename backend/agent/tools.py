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
            "description": "查詢某路線下一班到本站（使用者所在站牌）的到站時間",
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {
                        "type": "string",
                        "description": "路線號碼，例如 '201'、'7126'、'7720'",
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
                "查詢本站目前所有停靠路線的到站狀態，適合回答現在還有哪些車、"
                "哪些還沒末班駛離、剩下路線是否還有車"
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
            "description": "查詢某站牌有哪些公車路線停靠",
            "parameters": {
                "type": "object",
                "properties": {
                    "stop_name": {
                        "type": "string",
                        "description": "站牌名稱，例如 '雲林科技大學'、'斗六火車站'",
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
            "description": "查詢本站有停靠的某路線所有站牌名稱（去程與回程）",
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {
                        "type": "string",
                        "description": "路線號碼，例如 '7126'",
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
