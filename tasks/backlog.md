# Backlog

## 目標

收納目前不屬於 MVP，但可能在 demo、論文或後續版本有價值的項目。

## POI 與在地知識

- [ ] 雲科 + 斗六周邊 POI 資料集：餐廳、景點、醫療
- [ ] `search_poi`：附近店家 / 景點查詢
- [ ] 校園知識：雲科系所、行政、活動

## 公車資料延伸

- [ ] `get_nearby_stops`：附近站牌，需要 GPS 座標
- [ ] 外縣市業者 stop metadata 缺漏驗證
- [ ] ebus / GTFS route naming mapping

## UX 延伸

- [ ] 有限目的地候選確認：站名、醫院、車站、學校、廟宇、鄉鎮中心
- [ ] 最多一次轉乘的簡化抵達建議
- [ ] share mode 手機頁

## 技術債

- [ ] Compact 後完整內容 retrieval tool
- [ ] Chat session scale-out：從單機 SQLite 改外部 KV / Redis
- [ ] content-level observability 的資料保留與遮罩策略
