# 站牌前，用台語問「還有車無？」

![Kiosk 畫面：左邊是下一班車，右邊是本站所有路線；右下角是數位站務員「小芸」](docs/images/kiosk-with-assistant.png)

雲林鄉下的站牌，阿嬤不用滑手機、不用看地圖。站牌旁的 **Kiosk**（立式觸控螢幕）直接顯示這站每條路線的狀態：**7 分後到、可以等、未發車、末班已過**。她也可以對著它說台語，站務員小芸會用台語回答：

> 201 往高鐵雲林站，七分鐘後到。

這是大學專題：**台語友善的固定站牌離站決策系統**。它不教你從 A 走到 B，只回答站在這裡的人最想知道的事：現在還有車可以搭嗎？

## 為什麼可靠

語言模型很會說話，也很會編數字。所以這裡**數字不讓模型說**：到站時間、方向、末班狀態由程式向交通部 TDX 查好、算好，模型只負責聽懂問題，再把結果改寫成一句台語。

## 跑起來

```bash
cd backend
uv sync
cp .env.example .env     # 填 LLM_BASE_URL、LLM_MODEL、TDX_CLIENT_ID、TDX_CLIENT_SECRET、ADMIN_TOKEN
uv run uvicorn api:app --reload --port 8000
```

```bash
cd frontend
pnpm install
pnpm dev
```

開 Vite 印出的網址，會看到上面那個畫面。要接模型主機、路線規劃、觀測，看 [本地開發](docs/deployment/local-development.md)；要上正式主機，看 [正式部署](docs/deployment/production.md)。

## 往下讀

| 想知道 | 看 |
|---|---|
| 一句話怎麼變成答案、程式怎麼分工 | [架構](docs/architecture.md) |
| 為什麼不做成「台語版 Google Maps」 | [產品定位](docs/product-positioning.md) |
| 免費的 TDX 方案兩天就用完 | [TDX 方案與限流](docs/tdx-tiers.md) |
| 出事時怎麼查 | [觀測](docs/observability.md) |
| 做了什麼、沒做什麼 | [開發紀錄](docs/changelog.md) |
| 要改某個模組的細節 | [架構參考](docs/architecture-reference.md) |
| AI agent 作業規範 | [CLAUDE.md](CLAUDE.md) |
