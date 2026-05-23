from dotenv import load_dotenv

# 為什麼放在最前面？
# load_dotenv() 把 .env 檔的內容寫進 shell 環境變數
# 之後所有 os.getenv() 才讀得到
# 一定要在 import agent.loop 之前執行，否則 Kiosk tools 載入時已經讀不到值
load_dotenv()

from agent.loop import run  # noqa: E402
from tools.kiosk_bus import prefetch_route_arrival_context  # noqa: E402

if __name__ == "__main__":
    run(input_enricher=prefetch_route_arrival_context)
