# 路線規劃

路線規劃是**次要功能**：使用者在地圖上點目的地，系統畫出從這個站牌過去的候選路線。聊天不做這件事，被問「怎麼去某地」時，助理只引導使用者去用地圖。

## 為什麼用地圖選點

讓語言模型或文字去猜目的地座標會猜錯。由前端地圖給出確切座標，後端就只做規劃，不做猜測。

## 流程

```
地圖點選目的地 → POST /api/route-plans → OTP 規劃 → 轉成地圖可畫的路線 → 畫在 MapCN
```

- 起點固定是 Kiosk 站牌。
- 找不到方案、座標不可用、OTP 沒開，API 直接回明確錯誤，不叫 LLM 補答案。
- 找不到方案時，畫面保留選點，讓使用者重選。

## OTP 和 TDX 各管什麼

| 問題 | 誰回答 |
|---|---|
| 該搭哪條、在哪上下車、要不要轉乘 | OTP（吃 GTFS 時刻表與 OSM 路網） |
| 本站下一班現在到哪了 | TDX |

兩邊不要在 provider 層混在一起；要整合就在 service 層組合。

## 程式在哪

| 檔案 | 職責 |
|---|---|
| `backend/providers/otp.py` | 呼叫 OTP、處理逾時與錯誤 |
| `backend/services/route_plans.py` | 起點、雲林邊界、轉成前端用的格式 |
| `backend/api/route_plans.py` | 驗證輸入、對應 HTTP 狀態 |
| `backend/otp/README.md` | GTFS / OSM 下載與 graph 建置 |

## 會踩到的資料問題

- 路線名稱對不上：GTFS 有 `7000D`，Kiosk 可能顯示 `7000B`。
- 目的地選在河對岸或離站牌太遠，OSM 步行路網會讓結果失真。
- 班次少的路線在「現在出發」常常無方案，所以保留指定出發時間。

參考：[OTP 文件](https://docs.opentripplanner.org/en/latest/)
