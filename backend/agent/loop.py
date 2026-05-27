import sys

from agent.error import summarize_error
from agent.session import InputEnricher
from config import Settings, make_agent_session


def run(input_enricher: InputEnricher | None = None) -> None:
    """CLI I/O layer around the product session runtime."""
    try:
        settings = Settings.from_env()
    except RuntimeError as error:
        print(f"錯誤：{error}")
        sys.exit(1)

    session = make_agent_session(settings, input_enricher=input_enricher)

    print("雲林公車助理啟動（輸入 'exit' 結束）\n")
    while True:
        user_input = input("你: ").strip()
        if user_input.lower() in ("exit", "quit", "再見", "掰掰"):
            print("掰掰！")
            break
        if not user_input:
            continue

        try:
            answer = session.respond(user_input)
        except Exception as e:
            print("\n助理: 系統暫時無法回應，請稍後再試。\n")
            print(f"[error] Agent 回應失敗：{summarize_error(e)}")
            continue

        print(f"\n助理: {answer}\n")
