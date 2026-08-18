"""`src.vision` 的測試。

格式／大小驗證與 API 呼叫錯誤處理都是離線測試（monkeypatch 掉 OpenAI client），永遠會跑、
不需要任何外部依賴；驗證用的合法小圖固定讀 `data/test_labels/fixtures/`（進版控的合成圖，
不是使用者的真實酒標照片，避免測試依賴不在版控裡的檔案）。真正呼叫 GPT-4o-mini Vision、
用真實酒標照片辨識的測試另外分一組，只在 `OPENAI_API_KEY` 存在且
`data/test_labels/` 下有使用者放入的真實照片時才會執行，兩個條件缺一都會 skip。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import openai
import pytest

from src import vision

FIXTURES_DIR = vision.PROJECT_ROOT / "data" / "test_labels" / "fixtures"
TEST_LABELS_DIR = vision.PROJECT_ROOT / "data" / "test_labels"

NEEDS_OPENAI_KEY = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"), reason="需要 OPENAI_API_KEY 才能測試真實酒標辨識"
)
NEEDS_TEST_LABELS = pytest.mark.skipif(
    not any(TEST_LABELS_DIR.glob("*.jpg"))
    and not any(TEST_LABELS_DIR.glob("*.jpeg"))
    and not any(TEST_LABELS_DIR.glob("*.png")),
    reason="data/test_labels/ 尚未放入使用者提供的真實酒標照片",
)


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, result: Any, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc

    def create(self, **kwargs: Any) -> _FakeResponse:
        if self._exc is not None:
            raise self._exc
        return _FakeResponse(json.dumps(self._result, ensure_ascii=False))


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self.chat = _FakeChat(_FakeCompletions(result, exc))


# --- 圖片驗證（離線） ----------------------------------------------------------


def test_不存在的檔案會拋出可辨識例外(tmp_path: Path) -> None:
    missing = tmp_path / "not_here.jpg"
    with pytest.raises(vision.VisionError) as exc_info:
        vision._validate_image(missing)
    assert exc_info.value.user_message == vision.USER_MESSAGE_FILE_NOT_FOUND, (
        f"預期「找不到檔案」的錯誤訊息，實際拿到：{exc_info.value.user_message!r}"
        f"（technical_detail={exc_info.value.technical_detail!r}）"
    )


def test_不支援的副檔名會拋出可辨識例外(tmp_path: Path) -> None:
    bad_ext = tmp_path / "label.gif"
    bad_ext.write_bytes(b"not a real gif, just bytes")
    with pytest.raises(vision.VisionError) as exc_info:
        vision._validate_image(bad_ext)
    assert exc_info.value.user_message == vision.USER_MESSAGE_BAD_FORMAT, (
        f"預期「格式不符」的錯誤訊息，實際拿到：{exc_info.value.user_message!r}"
        f"（technical_detail={exc_info.value.technical_detail!r}）"
    )


def test_超過五MB的檔案會拋出可辨識例外(tmp_path: Path) -> None:
    oversized = tmp_path / "label.jpg"
    oversized.write_bytes(b"0" * (vision.MAX_IMAGE_SIZE_BYTES + 1))
    with pytest.raises(vision.VisionError) as exc_info:
        vision._validate_image(oversized)
    assert exc_info.value.user_message == vision.USER_MESSAGE_TOO_LARGE, (
        f"預期「檔案太大」的錯誤訊息，實際拿到：{exc_info.value.user_message!r}"
        f"（technical_detail={exc_info.value.technical_detail!r}）"
    )


def test_內容格式與副檔名不符時拋出可辨識例外(tmp_path: Path) -> None:
    fake_jpg = tmp_path / "label.jpg"
    fake_jpg.write_text("這其實是純文字，不是圖片內容", encoding="utf-8")
    with pytest.raises(vision.VisionError) as exc_info:
        vision._validate_image(fake_jpg)
    assert exc_info.value.user_message == vision.USER_MESSAGE_BAD_FORMAT, (
        f"預期「格式不符」的錯誤訊息，實際拿到：{exc_info.value.user_message!r}"
        f"（technical_detail={exc_info.value.technical_detail!r}）"
    )


def test_合法JPG圖片通過驗證() -> None:
    jpg_path = FIXTURES_DIR / "valid_label.jpg"
    assert jpg_path.is_file(), f"測試用固定檔不存在，先確認已建立：{jpg_path}"
    vision._validate_image(jpg_path)  # 不拋出例外即為通過


def test_合法PNG圖片通過驗證() -> None:
    png_path = FIXTURES_DIR / "valid_label.png"
    assert png_path.is_file(), f"測試用固定檔不存在，先確認已建立：{png_path}"
    vision._validate_image(png_path)  # 不拋出例外即為通過


# --- API Key 與 client 建立（離線） ---------------------------------------------


def test_沒有API_Key時拋出可辨識例外(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision, "load_dotenv", lambda: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(vision.VisionError) as exc_info:
        vision._get_client()
    assert exc_info.value.user_message == vision.USER_MESSAGE_NO_API_KEY, (
        f"預期「API Key 未設定」的錯誤訊息，實際拿到：{exc_info.value.user_message!r}"
        f"（technical_detail={exc_info.value.technical_detail!r}）"
    )


# --- recognize_label 整體流程（離線，monkeypatch client） ------------------------


def test_recognize_label_在API呼叫失敗時拋出VisionError(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(vision, "load_dotenv", lambda: None)
    fake_client = _FakeClient(exc=openai.OpenAIError("模擬 API 逾時或額度用盡"))
    monkeypatch.setattr(vision, "OpenAI", lambda timeout=None: fake_client)

    with pytest.raises(vision.VisionError) as exc_info:
        vision.recognize_label(FIXTURES_DIR / "valid_label.jpg")
    assert exc_info.value.user_message == vision.USER_MESSAGE_API_FAILED, (
        f"預期「讀不清楚，可以手動輸入嗎」的錯誤訊息，實際拿到：{exc_info.value.user_message!r}"
        f"（technical_detail={exc_info.value.technical_detail!r}）"
    )


def test_recognize_label_在回應含null欄位時保留null(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(vision, "load_dotenv", lambda: None)
    fake_result = {"region": "Bordeaux", "winery": None, "vintage": 2019, "grape": None}
    fake_client = _FakeClient(result=fake_result)
    monkeypatch.setattr(vision, "OpenAI", lambda timeout=None: fake_client)

    label_info = vision.recognize_label(FIXTURES_DIR / "valid_label.jpg")
    assert label_info.region == "Bordeaux", f"region 應保留辨識結果，實際：{label_info.region!r}"
    assert label_info.vintage == 2019, f"vintage 應保留辨識結果，實際：{label_info.vintage!r}"
    assert label_info.winery is None, (
        f"不確定的欄位應為 None，不是猜測值或空字串，實際：{label_info.winery!r}"
    )
    assert label_info.grape is None, (
        f"不確定的欄位應為 None，不是猜測值或空字串，實際：{label_info.grape!r}"
    )


def test_recognize_label_回應無法解析為JSON時拋出VisionError(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(vision, "load_dotenv", lambda: None)

    class _BrokenResponse:
        choices = [_FakeChoice("這不是合法的 JSON")]

    class _BrokenCompletions:
        def create(self, **kwargs: Any) -> _BrokenResponse:
            return _BrokenResponse()

    class _BrokenClient:
        def __init__(self) -> None:
            self.chat = _FakeChat(_BrokenCompletions())

    monkeypatch.setattr(vision, "OpenAI", lambda timeout=None: _BrokenClient())

    with pytest.raises(vision.VisionError) as exc_info:
        vision.recognize_label(FIXTURES_DIR / "valid_label.jpg")
    assert exc_info.value.user_message == vision.USER_MESSAGE_BAD_RESPONSE, (
        f"預期「回應格式異常」的錯誤訊息，實際拿到：{exc_info.value.user_message!r}"
        f"（technical_detail={exc_info.value.technical_detail!r}）"
    )


# --- 真實酒標照片辨識（需要 OPENAI_API_KEY 且 data/test_labels/ 有真實照片） -------


@NEEDS_OPENAI_KEY
@NEEDS_TEST_LABELS
def test_真實酒標照片辨識回傳合理結構() -> None:
    sample = next(
        (
            path
            for pattern in ("*.jpg", "*.jpeg", "*.png")
            for path in TEST_LABELS_DIR.glob(pattern)
            if path.parent != FIXTURES_DIR
        ),
        None,
    )
    assert sample is not None, "NEEDS_TEST_LABELS 應保證至少有一張真實照片"
    result = vision.recognize_label(sample)
    assert isinstance(result, vision.LabelInfo), f"應回傳 LabelInfo，實際型別：{type(result)!r}"
    # 沒有 ground truth，不斷言具體內容是否正確，只確認型別與流程能跑完。
