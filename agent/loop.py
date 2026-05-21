import os
import sys

from openai import OpenAI

from agent.prompt import build_system_prompt
from agent.session import AgentSession, InputEnricher, summarize_error
from agent.telemetry import configure_telemetry
from agent.tools import TOOL_HANDLERS, TOOL_SCHEMAS


def run(input_enricher: InputEnricher | None = None) -> None:
    """CLI I/O layer around the product session runtime."""
    base_url = os.getenv("LLM_BASE_URL")
    model = os.getenv("LLM_MODEL")
    api_key = os.getenv("LLM_API_KEY", "ollama")

    if not base_url or not model:
        print("錯誤：請在 .env 設定 LLM_BASE_URL 和 LLM_MODEL")
        sys.exit(1)

    session = AgentSession(
        client=OpenAI(base_url=base_url, api_key=api_key),
        model=model,
        system_prompt=build_system_prompt(),
        tool_schemas=TOOL_SCHEMAS,
        tool_handlers=TOOL_HANDLERS,
        input_enricher=input_enricher,
        telemetry=configure_telemetry(),
    )

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
