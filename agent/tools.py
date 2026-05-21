from tools.bus import get_arrivals_here, get_route_stops, get_schedule
from tools.yunlin_ebus import get_routes_at_stop

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
            "name": "get_schedule",
            "description": "查詢公車路線的班次時間。公路客運（7126 等）回傳完整全日時刻表；縣府路線（201 等）只回傳接下來幾班，非完整全日時刻表",
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {
                        "type": "string",
                        "description": "路線號碼，例如 '7126'、'201'",
                    },
                },
                "required": ["route"],
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
            "description": "查詢某路線的所有站牌名稱（去程與回程）",
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
    "get_schedule": get_schedule,
    "get_route_stops": get_route_stops,
    "get_routes_at_stop": get_routes_at_stop,
}
