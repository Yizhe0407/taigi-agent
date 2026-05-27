# QR Share

## 目標

讓 Kiosk 產出的路線可被帶走，解決上車、轉乘、走路時資訊不能持續查看的問題。

## 狀態

Deferred。新版主軸下 QR share 不是 MVP；只有 demo 明確需要時才做。

## 待辦

- [ ] Route view model share serialization：先考慮 URL hash + base64
- [ ] Kiosk QR overlay：路線結果頁加「帶走路線」按鈕
- [ ] 手機端 `/share?plan=...` read-only route
- [ ] `RoutePlannerPanel` read-only mode
- [ ] Service worker offline cache
- [ ] URL 長度量測與 QR 掃描率測試
- [ ] 隱私揭露：share 連結含座標與行程
- [ ] 量化指標：轉乘站行程完成率 vs 控制組

## 決策

- 先用 URL hash，避免後端 storage；實測 QR 掃描率不夠再退 short link。
- 手機與 Kiosk 先共用 Vue app，加 share-mode flag；bundle 太大再切。
- 本流程預設服務觀光客或陪同者，不服務獨自長輩主場景。

## 相關文件

- `docs/product-positioning.md`
- `tasks/route-planning.md`
