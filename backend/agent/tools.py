from tools.kiosk_bus import (
    check_stop_on_route,
    get_arrivals_here,
    get_arrivals_to_destination,
    get_route_stops,
    get_routes_at_stop,
    get_routes_at_stop_here,
    get_stop_arrival_statuses_here,
)


async def respond_directly(message: str) -> str:
    return message


TOOL_SCHEMAS: list = [
    {
        "type": "function",
        "function": {
            "name": "respond_directly",
            "description": (
                "直接回傳訊息給使用者。"
                "以下情況使用此工具：非公車查詢、語意不清需追問、使用者表示不需要、"
                "以及公車工具查詢完成後輸出最終答案。"
                "公車相關查詢（到站時間、目的地路線等）必須先呼叫對應的公車工具，不得直接用此工具回答。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "要回傳給使用者的文字。",
                    },
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_arrivals_here",
            "description": "查詢指定路線下一班抵達本站的即時到站時間。必須提供路線號碼。",
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
            "description": "查詢本站所有路線目前是否還有班次（含末班、尚未到站、已過末班）。適用於「還有車嗎」「末班走了沒」「現在幾路在跑」等查詢。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_arrivals_to_destination",
            "description": (
                "查詢本站哪些路線能到達目的地，並列出各路線下一班到站時間，依到站時間排序。"
                "使用者問「想去X」「有車去X嗎」「哪台比較快到X」時使用此工具；"
                "結果含時間，不需再呼叫 get_arrivals_here。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "destination": {
                        "type": "string",
                        "description": "目的地名稱，例如 '斗六火車站'、'北港朝天宮'。常見縮寫：雲科大=雲林科技大學、斗火=斗六火車站、北港廟=北港朝天宮。",
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
            "description": "查詢某路線是否停靠指定站牌，回傳「有」或「沒有」。",
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {
                        "type": "string",
                        "description": "路線號碼，例如 '201'、'Y01'。",
                    },
                    "stop_name": {
                        "type": "string",
                        "description": "要查詢的站牌或地點名稱，例如 '斗六火車站'。常見縮寫：雲科大=雲林科技大學、斗火=斗六火車站、北港廟=北港朝天宮。",
                    },
                },
                "required": ["route", "stop_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_routes_at_stop_here",
            "description": "查詢本站有哪些公車路線停靠，回傳路線清單。",
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
            "description": "查詢指定站牌有哪些公車路線停靠，回傳路線清單。用於查詢非本站的其他站牌。",
            "parameters": {
                "type": "object",
                "properties": {
                    "stop_name": {
                        "type": "string",
                        "description": "站牌名稱，例如 '斗六火車站'、'北港朝天宮'。常見縮寫：雲科大=雲林科技大學、斗火=斗六火車站、北港廟=北港朝天宮。",
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
            "description": "查詢本站有停靠的某路線去程與回程所有站牌名稱（完整清單）。",
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
    "respond_directly": respond_directly,
    "get_arrivals_here": get_arrivals_here,
    "get_arrivals_to_destination": get_arrivals_to_destination,
    "get_stop_arrival_statuses_here": get_stop_arrival_statuses_here,
    "get_route_stops": get_route_stops,
    "get_routes_at_stop": get_routes_at_stop,
    "get_routes_at_stop_here": get_routes_at_stop_here,
    "check_stop_on_route": check_stop_on_route,
}
