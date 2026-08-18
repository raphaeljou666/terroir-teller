"""GPT-4o-mini Vision 酒標辨識。

對應 Backlog 任務：

* **T-10**：輸入本機圖片路徑，呼叫 GPT-4o-mini Vision，輸出結構化 JSON
  （產區／酒莊／年份／品種）。不確定的欄位一律 `null`，絕不用猜測值填補（呼應條款 15
  「不編造資料」的精神延伸到影像辨識場景）。

格式支援範圍（條款 27 使用者覆寫）：本階段僅支援 JPG／JPEG／PNG，不支援 HEIC——HEIC 需要
額外解碼依賴（`pillow-heif`），且非主流格式支援對 MVP 非必要，使用者已決定本階段排除，
避免範圍蔓延（呼應條款 10）。`docs/03_UserStory.md` US-1.1 與 `CLAUDE.md` 條款 27 已同步
註明此覆寫。

圖片格式與大小驗證（≤5MB）在本模組內完成；辨識出的產區字串**不**在本模組內比對 20 個
產區的合法性清單，那是下游 T-12／T-13 的責任（見 `src/tools.py` 的產區合法性檢查工具，
對應 T-13 拒答清單第 8 項）。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import openai
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

# --- 常數設定 ---------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

VISION_MODEL = "gpt-4o-mini"
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 條款 27：上傳圖片大小上限 5MB
ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png")
ALLOWED_PIL_FORMATS = ("JPEG", "PNG")

# 送進 Vision API 前的縮圖上限。GPT-4o-mini Vision 本身就會把圖縮到 2048px 內、短邊 768px
# 再切 512px tile，送超過 2048 的部分模型根本看不到，純屬浪費上傳時間與 image token。
# 1600 留有安全邊界，確定不會被 API 自己的縮放吃掉任何細節（見 T-18 實作備註的實測數字）。
ENCODE_MAX_LONG_EDGE_PX = 1600
ENCODE_JPEG_QUALITY = 85

# 給 UI 顯示縮圖用的尺寸，跟送進 API 的版本是兩個獨立設定——顯示只需要 240px 寬，
# 這裡抓 2 倍給高解析度螢幕，遠比 API 用的 1600px 版本小很多。
DISPLAY_THUMBNAIL_MAX_LONG_EDGE_PX = 480
DISPLAY_THUMBNAIL_JPEG_QUALITY = 80

# HTTP client 逾時設寬鬆一點，避免把正常的網路波動誤判成辨識失敗；US-1.2 的「5 秒」是
# 效能目標，不是硬性逾時門檻，超過只記 log warning（見 `_call_vision_api()`）。
REQUEST_TIMEOUT_SECONDS = 20.0
RECOGNITION_TARGET_SECONDS = 5.0

# 給使用者看的統一錯誤訊息（條款 18：使用者看白話，開發者看 log）。
USER_MESSAGE_NO_API_KEY = "系統尚未設定 OpenAI API Key，請聯絡開發者。"
USER_MESSAGE_FILE_NOT_FOUND = "找不到這張圖片，請重新上傳。"
USER_MESSAGE_BAD_FORMAT = "這張圖片的格式看起來不是 JPG 或 PNG，請重新拍照或選擇其他檔案。"
USER_MESSAGE_TOO_LARGE = "這張圖片檔案太大了（超過 5MB），請重新拍照或壓縮後再試。"
USER_MESSAGE_API_FAILED = "這張酒標我讀不太清楚，可以手動輸入嗎？"
USER_MESSAGE_BAD_RESPONSE = "酒標辨識結果格式異常，請聯絡開發者。"


# --- 例外 -------------------------------------------------------------------


class VisionError(Exception):
    """酒標辨識失敗。

    訊息分兩層（條款 18），與 `climate.ClimateDataError`／`retrieval.RetrievalError` 是
    同一種模式，但刻意獨立定義——酒標辨識是影像輸入領域，跟氣候 API、知識檢索是不同關注點。

    Attributes:
        user_message: 給使用者看的白話訊息。
        technical_detail: 給開發者除錯用的技術細節。
    """

    def __init__(self, user_message: str, technical_detail: str = "") -> None:
        super().__init__(technical_detail or user_message)
        self.user_message = user_message
        self.technical_detail = technical_detail


# --- 資料結構 ---------------------------------------------------------------


@dataclass(frozen=True)
class LabelInfo:
    """酒標辨識出的四個關鍵欄位（US-1.2）。

    不確定的欄位一律為 `None`，絕不用推測值填補（條款 15）。`vintage` 用 `int` 而非
    `str`，方便下游 T-12 直接餵給 `climate.fetch_season_climate()`。
    """

    region: str | None
    winery: str | None
    vintage: int | None
    grape: str | None

    def to_dict(self) -> dict[str, Any]:
        """轉成可序列化的 dict，供 `src/tools.py` 的 tool 回傳使用。"""
        return asdict(self)


# --- Structured Outputs schema -----------------------------------------------

_LABEL_INFO_JSON_SCHEMA = {
    "name": "label_info",
    "schema": {
        "type": "object",
        "properties": {
            "region": {
                "type": ["string", "null"],
                "description": "酒標上的產區名稱，看不清楚或無法判斷時為 null",
            },
            "winery": {
                "type": ["string", "null"],
                "description": "酒莊名稱，看不清楚或無法判斷時為 null",
            },
            "vintage": {
                "type": ["integer", "null"],
                "description": "年份（西元年四位數），看不清楚或無法判斷時為 null",
            },
            "grape": {
                "type": ["string", "null"],
                "description": "品種名稱，看不清楚或無法判斷時為 null",
            },
        },
        "required": ["region", "winery", "vintage", "grape"],
        "additionalProperties": False,
    },
    "strict": True,
}

_SYSTEM_PROMPT = (
    "你是專業的葡萄酒酒標辨識助手。請仔細判讀圖片中的酒標文字，"
    "只回傳看得清楚、有把握的資訊；不確定的欄位一律使用 JSON 的 null 值，"
    "絕對不要用常識猜測或推論，也不要輸出字串 \"null\" 或 \"N/A\" 來代替。"
)

# 即使 system prompt 已要求，GPT-4o-mini 在 Structured Outputs 的 nullable 欄位
# （`type: ["string", "null"]`）偶爾還是會選到字串分支、填入字面上的 "null" 文字而不是
# 真正的 JSON null（實測 12 張酒標中就出現過幾次）。這裡在解析階段做防呆正規化，避免
# 下游把這個字串誤判成「有辨識出值」，違反條款 15／US-1.2「不確定的欄位是 null」的精神。
_NULL_LIKE_STRINGS = frozenset({"null", "none", "n/a"})


def _normalize_null_like(value: Any) -> Any:
    """把模型誤填的字串 `"null"`／`"none"`／`"n/a"` 正規化成真正的 `None`。"""
    if isinstance(value, str) and value.strip().lower() in _NULL_LIKE_STRINGS:
        return None
    return value


# --- 圖片驗證與編碼 -----------------------------------------------------------


def _validate_image(image_path: Path) -> None:
    """驗證圖片格式（JPG／JPEG／PNG）與大小（≤5MB）（條款 27，HEIC 已排除見模組 docstring）。

    Args:
        image_path: 本機圖片檔案路徑。

    Raises:
        VisionError: 檔案不存在、副檔名不符、內容格式與副檔名不符，或超過 5MB。
    """
    if not image_path.is_file():
        raise VisionError(USER_MESSAGE_FILE_NOT_FOUND, f"找不到檔案：{image_path}")

    if image_path.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise VisionError(USER_MESSAGE_BAD_FORMAT, f"副檔名不符：{image_path.suffix!r}")

    size_bytes = image_path.stat().st_size
    if size_bytes > MAX_IMAGE_SIZE_BYTES:
        raise VisionError(
            USER_MESSAGE_TOO_LARGE, f"檔案大小 {size_bytes} bytes 超過上限 {MAX_IMAGE_SIZE_BYTES}"
        )

    try:
        with Image.open(image_path) as img:
            img.verify()
            actual_format = img.format
    except (OSError, UnidentifiedImageError) as exc:
        raise VisionError(USER_MESSAGE_BAD_FORMAT, f"PIL 無法解析圖片：{image_path}（{exc!r}）") from exc

    if actual_format not in ALLOWED_PIL_FORMATS:
        raise VisionError(USER_MESSAGE_BAD_FORMAT, f"內容實際格式為 {actual_format!r}，非 JPEG/PNG")


def _resize_image_bytes(
    raw_bytes: bytes, pil_format: str, max_long_edge_px: int, jpeg_quality: int
) -> bytes:
    """把圖片位元組縮到長邊上限內，已經在上限內的圖片原樣回傳（不重新編碼）。

    Args:
        raw_bytes: 原始圖片位元組（已通過 `_validate_image()`）。
        pil_format: `"JPEG"` 或 `"PNG"`，決定輸出編碼格式與是否套用 JPEG 品質參數。
        max_long_edge_px: 長邊像素上限，只會縮小不會放大（`Image.thumbnail()` 的保證）。
        jpeg_quality: JPEG 品質（1–95），只在 `pil_format == "JPEG"` 時生效。

    Returns:
        縮圖後的圖片位元組；若原圖已在長邊上限內，回傳未變動的 `raw_bytes`（避免無謂的
        重新壓縮造成畫質流失）。
    """
    with Image.open(io.BytesIO(raw_bytes)) as image:
        if max(image.size) <= max_long_edge_px:
            return raw_bytes

        image = ImageOps.exif_transpose(image)  # 手機照片常見 EXIF 旋轉，縮圖前先轉正
        image.thumbnail((max_long_edge_px, max_long_edge_px), Image.LANCZOS)

        buffer = io.BytesIO()
        save_kwargs: dict[str, Any] = {"format": pil_format}
        if pil_format == "JPEG":
            if image.mode != "RGB":
                image = image.convert("RGB")
            save_kwargs.update(quality=jpeg_quality, optimize=True)
        else:
            save_kwargs["optimize"] = True
        image.save(buffer, **save_kwargs)
        return buffer.getvalue()


def _encode_image_data_url(image_path: Path) -> str:
    """把圖片縮圖後讀成 base64 data URL，供 Vision API 的 `image_url` 欄位使用。

    注意：不重用 `_validate_image()` 內驗證用的 `Image` 物件——呼叫過 `verify()` 後該物件
    已失效，這裡改用 `read_bytes()` 直接重新讀取原始位元組。縮圖上限見 `ENCODE_MAX_LONG_EDGE_PX`
    模組常數註解——手機原圖動輒 3000×4000，遠超過 Vision API 自己會做的縮放上限，先在這裡
    縮小可以省下大半上傳時間，不影響辨識結果。

    Args:
        image_path: 已通過驗證的本機圖片檔案路徑。

    Returns:
        `data:image/jpeg;base64,...` 或 `data:image/png;base64,...` 格式的字串。
    """
    pil_format = "PNG" if image_path.suffix.lower() == ".png" else "JPEG"
    mime_type = "image/png" if pil_format == "PNG" else "image/jpeg"
    raw_bytes = image_path.read_bytes()
    resized_bytes = _resize_image_bytes(
        raw_bytes, pil_format, ENCODE_MAX_LONG_EDGE_PX, ENCODE_JPEG_QUALITY
    )
    encoded = base64.b64encode(resized_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def make_display_thumbnail_bytes(raw_bytes: bytes, filename: str) -> bytes:
    """給 UI 顯示用的縮圖位元組，直接可傳給 `st.image()`。

    跟送進 Vision API 的縮圖（`_encode_image_data_url()`）是兩組獨立的尺寸／品質設定——
    顯示只需要幾百 px 寬，遠比送進 API 的 1600px 版本小，沒必要重用同一份編碼結果。

    Args:
        raw_bytes: 使用者上傳的原始圖片位元組。
        filename: 上傳檔名，用來判斷輸出格式（`.png` 保留 PNG，其餘一律視為 JPEG）。

    Returns:
        縮圖位元組。

    Raises:
        UnidentifiedImageError: 內容無法解碼為圖片。
        OSError: PIL 開啟或處理圖片時失敗。
    """
    pil_format = "PNG" if Path(filename).suffix.lower() == ".png" else "JPEG"
    return _resize_image_bytes(
        raw_bytes, pil_format, DISPLAY_THUMBNAIL_MAX_LONG_EDGE_PX, DISPLAY_THUMBNAIL_JPEG_QUALITY
    )


# --- OpenAI Vision API 呼叫 ----------------------------------------------------


def _get_client() -> OpenAI:
    """建立 OpenAI client，讀 `.env` 的 `OPENAI_API_KEY`。

    Returns:
        已設定逾時的 OpenAI client。

    Raises:
        VisionError: 環境變數 `OPENAI_API_KEY` 未設定。
    """
    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise VisionError(USER_MESSAGE_NO_API_KEY, "環境變數 OPENAI_API_KEY 未設定")
    return OpenAI(timeout=REQUEST_TIMEOUT_SECONDS)


def _call_vision_api(client: OpenAI, image_path: Path) -> dict[str, Any]:
    """呼叫 GPT-4o-mini Vision，回傳結構化 JSON dict（條款 14 錯誤處理）。

    用 Structured Outputs（`response_format={"type": "json_schema", ...}`）強制回應符合
    `_LABEL_INFO_JSON_SCHEMA`，比手刻 prompt 要求 JSON 再自行解析更可靠，不需要額外的
    JSON 修復邏輯。

    Args:
        client: 已建立的 OpenAI client。
        image_path: 已通過驗證的本機圖片檔案路徑。

    Returns:
        含 `region`／`winery`／`vintage`／`grape` 四個欄位的 dict。

    Raises:
        VisionError: API 呼叫失敗，或回應內容無法解析為合法 JSON。
    """
    started = time.perf_counter()
    data_url = _encode_image_data_url(image_path)
    try:
        response = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "請辨識這張酒標的產區、酒莊、年份、品種。"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            response_format={"type": "json_schema", "json_schema": _LABEL_INFO_JSON_SCHEMA},
        )
    except openai.OpenAIError as exc:
        logger.error("Vision API 呼叫失敗：%s", exc, exc_info=True)
        raise VisionError(USER_MESSAGE_API_FAILED, f"chat.completions.create() 失敗：{exc!r}") from exc

    elapsed = time.perf_counter() - started
    if elapsed > RECOGNITION_TARGET_SECONDS:
        logger.warning("酒標辨識耗時 %.1f 秒，超過 US-1.2 的 5 秒目標", elapsed)

    try:
        return json.loads(response.choices[0].message.content)
    except (json.JSONDecodeError, IndexError, AttributeError, TypeError) as exc:
        raise VisionError(USER_MESSAGE_BAD_RESPONSE, f"API 回應無法解析為 JSON：{exc!r}") from exc


# --- 公開函式 ---------------------------------------------------------------


def recognize_label(image_path: str | Path) -> LabelInfo:
    """辨識酒標照片，輸出產區／酒莊／年份／品種四個欄位（T-10、US-1.2）。

    Args:
        image_path: 本機圖片檔案路徑。

    Returns:
        `LabelInfo`，不確定的欄位為 `None`。

    Raises:
        VisionError: 檔案不存在、格式不符、超過 5MB、API Key 未設定，或 API 呼叫失敗。
    """
    path = Path(image_path)
    _validate_image(path)
    client = _get_client()
    payload = _call_vision_api(client, path)
    return LabelInfo(
        region=_normalize_null_like(payload.get("region")),
        winery=_normalize_null_like(payload.get("winery")),
        vintage=payload.get("vintage"),
        grape=_normalize_null_like(payload.get("grape")),
    )


# --- CLI ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m src.vision",
        description="GPT-4o-mini Vision 酒標辨識測試（T-10）",
    )
    parser.add_argument("--image", required=True, help="酒標圖片的本機路徑")
    parser.add_argument("--verbose", action="store_true", help="顯示 DEBUG 等級的開發者 log")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 進入點。

    Args:
        argv: 命令列參數；`None` 表示直接讀 `sys.argv`。

    Returns:
        行程結束碼，0 為成功、1 為可預期的辨識錯誤、2 為參數用法錯誤。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        label_info = recognize_label(args.image)
    except VisionError as exc:
        # 條款 18：使用者只看到白話訊息，技術細節寫進 log。
        logger.error("技術細節：%s", exc.technical_detail)
        print(f"\n{exc.user_message}")
        return 1

    print(json.dumps(label_info.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
