from dotenv import load_dotenv

# 為什麼放在最前面？
# load_dotenv() 把 .env 檔的內容寫進 shell 環境變數
# 之後所有 os.getenv() 才讀得到
# 一定要在 import agent.loop 之前執行，否則 tools/bus.py 載入時已經讀不到值
load_dotenv()

from agent.loop import run

if __name__ == "__main__":
    run()
