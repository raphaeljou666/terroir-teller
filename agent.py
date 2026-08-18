"""Agent orchestrator：透過 OpenAI function calling 自主蒐集資料並生成風味推測報告（T-12）。

流程分兩階段：本模組（階段一）是一個 tool-calling agentic loop，唯一職責是決定呼叫哪些
`src/tools.py` 定義的工具、蒐集氣候距平與知識庫資料，不產生任何面向使用者的文字；
`src/report.py`（階段二）拿蒐集到的資料另外呼叫一次 LLM，生成最終的風味推測報告。兩階段
分開是刻意設計：tool-routing 的可靠性跟報告寫作品質是不同的 prompt engineering 關注點，
混在一個 prompt 裡容易互相稀釋。

拒答清單第 8 項（超出 20 產區清單）在這裡用程式邏輯硬擋：`check_region_validity` 回傳
`valid: False` 時，迴圈立刻中止，報告生成（階段二）根本不會被呼叫，不是靠 prompt 指令
讓模型「有機會但選擇不」寫報告。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal

import openai
from dotenv import load_dotenv
from openai import OpenAI

from src import report, tools

logger = logging.getLogger(__name__)

# --- 常數設定 -----------------------------------------------------------------

AGENT_MODEL = "gpt-4o-mini"

# 涵蓋兩條路徑：產區+年份約 3 次工具呼叫，圖片路徑約 4 次，留一點餘裕給模型可能的
# 重試或非預期順序（對應 06_TechSetup.md §11「Agent 陷入無限 tool 呼叫迴圈」的踩雷點）。
MAX_ITERATIONS = 6

REQUEST_TIMEOUT_SECONDS = 60.0

USER_MESSAGE_NO_API_KEY = "系統尚未設定 OpenAI API Key，請聯絡開發者。"
USER_MESSAGE_AGENT_FAILED = "AI 服務暫時無法使用，請稍後再試一次。"
USER_MESSAGE_NO_REGION_FROM_LABEL = "這張酒標讀不出產區，請改用 --region 與 --year 手動輸入試試。"
USER_MESSAGE_REGION_NOT_COVERED_FALLBACK = "本系統目前不涵蓋這個產區。"

TerminalState = Literal["ok", "region_not_covered", "label_no_region", "max_iterations"]


# --- 例外 -------------------------------------------------------------------


class AgentError(Exception):
    """Agent orchestrator 執行失敗。

    訊息分兩層（條款 18），與 `climate.ClimateDataError`／`retrieval.RetrievalError`／
    `vision.VisionError`／`report.ReportGenerationError` 是同一種模式，但刻意獨立定義
    ——agent 迴圈是自己的關注點，不跨模組繼承。

    Attributes:
        user_message: 給使用者看的白話訊息。
        technical_detail: 給開發者除錯用的技術細節。
    """

    def __init__(self, user_message: str, technical_detail: str = "") -> None:
        super().__init__(technical_detail or user_message)
        self.user_message = user_message
        self.technical_detail = technical_detail


# --- 資料結構 ---------------------------------------------------------------


@dataclass
class GatheredData:
    """Agent 迴圈蒐集到的資料，交給 `report.generate_report()` 生成最終報告。"""

    region_canonical: str | None = None
    region_zh: str | None = None
    vintage_year: int | None = None
    label_info: dict[str, Any] | None = None
    region_validity: dict[str, Any] | None = None
    anomaly: dict[str, Any] | None = None
    knowledge_hits: list[dict[str, Any]] = field(default_factory=list)
    tool_call_log: list[dict[str, Any]] = field(default_factory=list)


# --- Orchestrator system prompt -------------------------------------------------

ORCHESTRATOR_SYSTEM_PROMPT = """\
你是 TerroirTeller 的資料蒐集 agent，只負責呼叫工具取得氣候與知識庫資料，不負責撰寫任何
面向使用者的文字說明。

標準流程：

1. 如果任務提到圖片，先呼叫 recognize_wine_label 取得產區、酒莊、年份、品種。
2. 一定要先呼叫 check_region_validity 確認產區在系統支援的20個產區清單內，才能繼續下
   一步。
3. 產區確認合法後，呼叫 query_climate_anomaly 取得氣候距平（需要產區與年份）。
4. 用氣候距平的重點（例如「偏暖偏乾」）組成一句查詢語句，呼叫 query_climate_knowledge，
   n_results 設為 3。
5. query_terroir_knowledge 只在明確需要風土或產區比較資訊時才呼叫，這個單一輪次的流程
   通常用不到，不用勉強呼叫它。

完成上述資料蒐集後，不需要輸出任何文字，直接停止，不要再呼叫工具。
"""


# --- OpenAI client ------------------------------------------------------------


def _get_client() -> OpenAI:
    """建立 OpenAI client，讀 `.env` 的 `OPENAI_API_KEY`。

    Raises:
        AgentError: 環境變數 `OPENAI_API_KEY` 未設定。
    """
    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise AgentError(USER_MESSAGE_NO_API_KEY, "環境變數 OPENAI_API_KEY 未設定")
    return OpenAI(timeout=REQUEST_TIMEOUT_SECONDS)


def _call_orchestrator(client: OpenAI, messages: list[dict[str, Any]]) -> Any:
    """呼叫一次 orchestrator LLM，回傳 assistant message 物件。

    Raises:
        AgentError: API 呼叫失敗（網路、額度等）。
    """
    try:
        response = client.chat.completions.create(
            model=AGENT_MODEL, messages=messages, tools=tools.ALL_TOOL_SCHEMAS,
        )
    except openai.OpenAIError as exc:
        logger.error("Agent orchestrator 呼叫失敗：%s", exc, exc_info=True)
        raise AgentError(
            USER_MESSAGE_AGENT_FAILED, f"chat.completions.create() 失敗：{exc!r}"
        ) from exc
    return response.choices[0].message


def _message_to_dict(message: Any) -> dict[str, Any]:
    """把 assistant message 序列化成下一輪 `messages` 需要的 dict 形狀。"""
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": call.type,
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in message.tool_calls
        ]
    return payload


# --- Tool dispatch 與資料蒐集 -------------------------------------------------------


def _dispatch_tool_call(tool_call: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """解析並執行單一 tool_call，回傳 `(工具名稱, 參數, 結果)`。"""
    name = tool_call.function.name
    try:
        arguments = json.loads(tool_call.function.arguments or "{}")
    except json.JSONDecodeError:
        logger.warning("工具呼叫參數無法解析為 JSON：%r", tool_call.function.arguments)
        arguments = {}

    dispatch_fn = tools.TOOL_DISPATCH.get(name)
    if dispatch_fn is None:
        logger.error("未知的工具名稱：%r", name)
        result: dict[str, Any] = {
            "error": True, "error_type": "unknown_tool", "user_message": "系統內部錯誤：未知的工具。",
        }
    else:
        result = dispatch_fn(**arguments)
    return name, arguments, result


def _update_gathered_data(
    gathered: GatheredData, tool_name: str, result: dict[str, Any]
) -> None:
    """依工具名稱把 dispatch 結果寫進 `gathered` 對應欄位（單一職責，方便測試）。"""
    if tool_name == "recognize_wine_label" and not result.get("error"):
        gathered.label_info = result
    elif tool_name == "check_region_validity":
        gathered.region_validity = result
        if result.get("valid"):
            gathered.region_canonical = result["region_canonical"]
            gathered.region_zh = result["region_zh"]
    elif tool_name == "query_climate_anomaly" and not result.get("error"):
        gathered.anomaly = result
        gathered.vintage_year = result.get("vintage_year")
    elif tool_name == "query_climate_knowledge" and not result.get("error"):
        gathered.knowledge_hits.extend(result.get("results", []))


def _process_tool_calls(
    tool_calls: list[Any], gathered: GatheredData, messages: list[dict[str, Any]]
) -> bool:
    """處理一輪所有 tool_calls，append 對應 `role: tool` 訊息。

    Returns:
        `True` 表示這輪偵測到產區不在支援清單內（拒答清單第 8 項）——這是硬擋點，呼叫端
        收到 `True` 要立刻中止迴圈，不再呼叫下一輪 LLM，更不會進到報告生成階段。
    """
    region_not_covered = False
    for tool_call in tool_calls:
        name, arguments, result = _dispatch_tool_call(tool_call)
        gathered.tool_call_log.append({"name": name, "arguments": arguments, "result": result})
        _update_gathered_data(gathered, name, result)
        messages.append(
            {"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(result, ensure_ascii=False)}
        )
        if name == "check_region_validity" and result.get("valid") is False:
            region_not_covered = True
    return region_not_covered


def _label_missing_region(gathered: GatheredData) -> bool:
    """酒標辨識完全讀不到產區時回傳 `True`，讓迴圈提早中止。

    只有 `region` 缺才中止；`vintage` 缺仍允許繼續，`report.py` 會誠實說明沒有年份專屬
    距平，不會因此讓整個流程卡住。
    """
    return gathered.label_info is not None and not gathered.label_info.get("region")


def run_agent_loop(
    client: OpenAI, user_task: str, max_iterations: int = MAX_ITERATIONS
) -> tuple[GatheredData, TerminalState]:
    """執行 tool-calling agentic loop，回傳 `(蒐集到的資料, 終止狀態)`。

    Args:
        client: 已建立的 OpenAI client。
        user_task: 描述任務的第一則使用者訊息（見 `build_user_task_message()`）。
        max_iterations: 最大輪數上限，超過就優雅收尾（用已蒐集的資料生成報告，而不是
            掛掉或無限迴圈，對應 06_TechSetup.md §11 的踩雷點）。

    Returns:
        `(GatheredData, TerminalState)`。`"max_iterations"` 不是錯誤，呼叫端應該跟
        `"ok"` 一樣繼續嘗試生成報告。

    Raises:
        AgentError: LLM API 呼叫失敗。
    """
    gathered = GatheredData()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": ORCHESTRATOR_SYSTEM_PROMPT},
        {"role": "user", "content": user_task},
    ]

    for _ in range(max_iterations):
        message = _call_orchestrator(client, messages)
        messages.append(_message_to_dict(message))

        if not message.tool_calls:
            return gathered, "ok"
        if _process_tool_calls(message.tool_calls, gathered, messages):
            return gathered, "region_not_covered"
        if _label_missing_region(gathered):
            return gathered, "label_no_region"

    logger.warning(
        "Orchestrator 達到 max_iterations=%d 上限，改用目前已蒐集的資料產生報告", max_iterations
    )
    return gathered, "max_iterations"


# --- CLI ------------------------------------------------------------------------


def build_user_task_message(args: argparse.Namespace) -> str:
    """依 CLI 輸入方式（`--image` 或 `--region`/`--year`）組出 orchestrator 的任務描述。"""
    if args.image:
        return (
            f"使用者上傳了一張酒標照片，路徑是「{args.image}」。"
            "請先辨識酒標，再依標準流程確認產區合法性、查詢氣候距平與相關知識庫片段。"
        )
    return (
        f"使用者想了解 {args.region} 產區 {args.year} 年份的葡萄酒。"
        "請依標準流程確認產區合法性、查詢氣候距平與相關知識庫片段。"
    )


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器。"""
    parser = argparse.ArgumentParser(
        prog="python agent.py",
        description="TerroirTeller Agent orchestrator：蒐集資料並生成風味推測報告（T-12、T-13）",
    )
    parser.add_argument("--region", help="產區名稱或別名，例：Bordeaux")
    parser.add_argument("--year", type=int, help="酒標年份，例：2019")
    parser.add_argument("--image", help="酒標照片的本機路徑")
    parser.add_argument(
        "--max-iterations", type=int, default=MAX_ITERATIONS,
        help=f"Agent tool-calling 迴圈的最大輪數上限，預設 {MAX_ITERATIONS}",
    )
    parser.add_argument("--verbose", action="store_true", help="顯示 DEBUG 等級的開發者 log")
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """檢查 CLI 參數的互斥與成對規則，違反時呼叫 `parser.error()`（退出碼 2）。"""
    if args.image and (args.region or args.year):
        parser.error("--image 不可與 --region／--year 同時使用")
    if bool(args.region) != bool(args.year):
        parser.error("--region 與 --year 必須一起提供")


def _print_terminal_state_message(gathered: GatheredData, state: TerminalState) -> None:
    """印出 `region_not_covered`／`label_no_region` 終止狀態的白話說明。"""
    if state == "region_not_covered":
        reason = (
            gathered.region_validity.get("reason")
            if gathered.region_validity else USER_MESSAGE_REGION_NOT_COVERED_FALLBACK
        )
        print(f"\n{reason}")
    elif state == "label_no_region":
        print(f"\n{USER_MESSAGE_NO_REGION_FROM_LABEL}")


def main(argv: list[str] | None = None) -> int:
    """CLI 進入點。

    Args:
        argv: 命令列參數；`None` 表示直接讀 `sys.argv`。

    Returns:
        行程結束碼，0 為成功、1 為可預期的資料／覆蓋範圍問題、2 為參數用法錯誤。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    _validate_args(parser, args)
    if not (args.image or (args.region and args.year)):
        parser.print_help()
        return 2

    try:
        client = _get_client()
        gathered, state = run_agent_loop(client, build_user_task_message(args), args.max_iterations)
    except AgentError as exc:
        # 條款 18：使用者只看到白話訊息，技術細節寫進 log。
        logger.error("技術細節：%s", exc.technical_detail)
        print(f"\n{exc.user_message}")
        return 1

    if state in ("region_not_covered", "label_no_region"):
        _print_terminal_state_message(gathered, state)
        return 1

    if gathered.region_canonical is None:
        logger.error("Agent 迴圈結束但未能確認產區，終止狀態：%s", state)
        print(f"\n{USER_MESSAGE_AGENT_FAILED}")
        return 1

    try:
        markdown = report.generate_report(
            region_canonical=gathered.region_canonical,
            region_zh=gathered.region_zh,
            vintage_year=gathered.vintage_year,
            anomaly=gathered.anomaly,
            knowledge_hits=gathered.knowledge_hits,
            label_info=gathered.label_info,
        )
    except report.ReportGenerationError as exc:
        logger.error("技術細節：%s", exc.technical_detail)
        print(f"\n{exc.user_message}")
        return 1

    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
