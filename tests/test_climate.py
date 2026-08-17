"""`src.climate` 的單元測試。

全部離線執行，不打 Open-Meteo API：需要 API 回應的地方一律用假回應替換。測試重點放在最容易
寫錯、又最難用肉眼發現的地方——南半球生長季跨年、快取是否真的擋掉重複請求、缺值有沒有被偷偷
補值（條款 15）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from src import climate


# --- 生長季日期區間 ---------------------------------------------------------


def test_北半球生長季為當年四月到十月() -> None:
    start, end = climate.growing_season_range(2019, "N")
    assert (start, end) == (date(2019, 4, 1), date(2019, 10, 31))


def test_南半球生長季從前一年十月跨到當年四月() -> None:
    # Mendoza（門多薩）2019 年份的生長季是 2018-10 到 2019-04，不是 2019 年的 10 月。
    start, end = climate.growing_season_range(2019, "S")
    assert (start, end) == (date(2018, 10, 1), date(2019, 4, 30))


def test_半球代號不分大小寫且容許空白() -> None:
    assert climate.growing_season_range(2020, " s ") == climate.growing_season_range(2020, "S")


def test_未知半球代號會拋錯() -> None:
    with pytest.raises(climate.ClimateDataError):
        climate.growing_season_range(2019, "X")


def test_兩個半球的生長季都涵蓋各自的仲夏() -> None:
    # 半球寫反最直接的症狀就是抓到反季節，用「仲夏有沒有被涵蓋」來擋這種錯。
    north_start, north_end = climate.growing_season_range(2019, "N")
    south_start, south_end = climate.growing_season_range(2019, "S")
    assert north_start <= date(2019, 7, 15) <= north_end  # 北半球仲夏在 7 月
    assert south_start <= date(2019, 1, 15) <= south_end  # 南半球仲夏在 1 月
    assert south_start.year == 2018 and south_end.year == 2019


# --- 產區查詢 ---------------------------------------------------------------


def test_產區清單每筆都有必要欄位且半球合法() -> None:
    regions = climate.load_regions()
    assert len(regions) == 20
    for region in regions:
        assert region["hemisphere"] in {"N", "S"}
        assert -90 <= region["latitude"] <= 90
        assert -180 <= region["longitude"] <= 180


@pytest.mark.parametrize("name", ["Bordeaux", "bordeaux", " 波爾多 ", "波尔多"])
def test_可用正式名或別名找到產區(name: str) -> None:
    assert climate.find_region(name)["region_canonical"] == "Bordeaux"


def test_查無產區時拋出可辨識的例外並附兩層訊息() -> None:
    with pytest.raises(climate.RegionNotFoundError) as excinfo:
        climate.find_region("Santorini")
    error = excinfo.value
    assert "Santorini" in error.user_message
    assert "regions.json" in error.technical_detail


def test_產區代號與知識庫資料夾一致() -> None:
    assert climate.region_slug(climate.find_region("Napa Valley")) == "napa_valley"


# --- API 回應解析 -----------------------------------------------------------


def _fake_payload(**overrides: Any) -> dict[str, Any]:
    """組一份格式與 Open-Meteo 相同的假回應。"""
    daily = {
        "time": ["2019-04-01", "2019-04-02"],
        "temperature_2m_max": [16.8, 15.9],
        "temperature_2m_min": [6.8, 8.4],
        "temperature_2m_mean": [12.5, 12.1],
        "precipitation_sum": [0.3, 1.1],
        "sunshine_duration": [3600.0, 7200.0],
    }
    daily.update(overrides)
    return {"daily": daily}


def test_日照秒數換算為小時() -> None:
    records = climate._parse_daily_records(_fake_payload())
    assert [record.sunshine_hours for record in records] == [1.0, 2.0]


def test_缺值保留為_None_不做補值() -> None:
    # 條款 15：API 沒給的值就是沒有，不能用平均值或推測值填補。
    payload = _fake_payload(temperature_2m_mean=[None, 12.1], sunshine_duration=[None, 7200.0])
    records = climate._parse_daily_records(payload)
    assert records[0].temp_mean is None
    assert records[0].sunshine_hours is None


def test_回應陣列長度不足時以_None_補齊() -> None:
    payload = _fake_payload(precipitation_sum=[0.3])
    records = climate._parse_daily_records(payload)
    assert records[1].precipitation_mm is None


# --- 資料結構 ---------------------------------------------------------------


def _fake_season(vintage_year: int = 2019) -> climate.SeasonClimate:
    region = climate.find_region("Bordeaux")
    start, end = climate.growing_season_range(vintage_year, region["hemisphere"])
    records = climate._parse_daily_records(_fake_payload())
    return climate._build_season(region, vintage_year, start, end, records, is_partial=False)


def test_生長季可序列化為_JSON_再還原() -> None:
    season = _fake_season()
    restored = climate.SeasonClimate.from_dict(season.to_dict())
    assert restored == season


def test_轉成_DataFrame_後日期為時間型別且已排序() -> None:
    frame = _fake_season().to_dataframe()
    assert list(frame.columns) == [
        "date", "temp_max", "temp_min", "temp_mean", "precipitation_mm", "sunshine_hours",
    ]
    assert frame["date"].is_monotonic_increasing
    assert str(frame["date"].dtype).startswith("datetime64")


def test_基準線切片只取區間內的日期() -> None:
    records = {
        record.date: record
        for record in climate._parse_daily_records(
            _fake_payload(time=["2019-03-31", "2019-04-01", "2019-04-02"])
        )
    }
    sliced = climate._slice_records(records, date(2019, 4, 1), date(2019, 4, 2))
    assert [record.date for record in sliced] == ["2019-04-01", "2019-04-02"]


# --- 快取行為（T-08 的核心驗收點）------------------------------------------


def test_同一產區年份第二次查詢不再打_API(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(climate, "CACHE_DIR", tmp_path)
    call_count = {"n": 0}

    def fake_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        call_count["n"] += 1
        return _fake_payload()

    monkeypatch.setattr(climate, "_request_archive", fake_request)

    first = climate.fetch_season_climate("Bordeaux", 2019)
    second = climate.fetch_season_climate("Bordeaux", 2019)

    assert call_count["n"] == 1, "第二次查詢應該讀快取，不該再打 API"
    assert first == second


def test_指定_refresh_時會忽略快取重抓(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(climate, "CACHE_DIR", tmp_path)
    call_count = {"n": 0}

    def fake_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        call_count["n"] += 1
        return _fake_payload()

    monkeypatch.setattr(climate, "_request_archive", fake_request)

    climate.fetch_season_climate("Bordeaux", 2019)
    climate.fetch_season_climate("Bordeaux", 2019, use_cache=False)
    assert call_count["n"] == 2


def test_快取檔損毀時重新抓取而不是整個掛掉(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(climate, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(climate, "_request_archive", lambda *a, **k: _fake_payload())

    (tmp_path / "bordeaux_2019.json").write_text("{壞掉的 JSON", encoding="utf-8")
    season = climate.fetch_season_climate("Bordeaux", 2019)
    assert season.day_count == 2


def test_基準線快取版本不符時視為失效(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(climate, "CACHE_DIR", tmp_path)
    cache_path = tmp_path / "x.json"
    climate._write_json_cache(cache_path, {"cache_schema_version": "0.1", "seasons": []})
    assert climate._read_baseline_cache(cache_path) is None


# --- 錯誤處理 ---------------------------------------------------------------


def test_查詢未來年份時明確告知查不到而不是回傳空資料() -> None:
    future_year = date.today().year + 2
    with pytest.raises(climate.ClimateDataError) as excinfo:
        climate.fetch_season_climate("Bordeaux", future_year)
    assert str(future_year) in excinfo.value.user_message


class _FakeResponse:
    """最小可用的假 httpx 回應，只提供 `_request_archive` 會用到的介面。"""

    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


def test_遇到_429_會等待重試而不是當成參數錯誤放棄(monkeypatch: pytest.MonkeyPatch) -> None:
    # 批次預熱 20 個產區時實際踩到過：429 是每分鐘上限，等一下就好，不該歸類成不可重試。
    responses = [
        _FakeResponse(429, {"reason": "Minutely API request limit exceeded."}),
        _FakeResponse(200, _fake_payload()),
    ]
    monkeypatch.setattr(climate, "RATE_LIMIT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(climate.httpx, "get", lambda *a, **k: responses.pop(0))

    payload = climate._request_archive(44.84, -0.58, date(2019, 4, 1), date(2019, 4, 2))
    assert payload["daily"]["time"] == ["2019-04-01", "2019-04-02"]
    assert not responses, "第一次 429 後應該重試第二次"


def test_每小時用量上限不重試並拋出可辨識的例外(monkeypatch: pytest.MonkeyPatch) -> None:
    # 等 65 秒解不了每小時上限，硬重試只是讓 20 個產區各卡三次。
    calls = {"n": 0}

    def fake_get(*_args: Any, **_kwargs: Any) -> _FakeResponse:
        calls["n"] += 1
        return _FakeResponse(429, {"reason": "Hourly API request limit exceeded."})

    monkeypatch.setattr(climate.httpx, "get", fake_get)
    with pytest.raises(climate.ApiQuotaExceededError) as excinfo:
        climate._request_archive(44.84, -0.58, date(2019, 4, 1), date(2019, 4, 2))

    assert calls["n"] == 1
    assert "一小時" in excinfo.value.user_message


def test_批次預熱碰到用量上限會停手並標記剩下的產區(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(climate, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(climate, "WARM_CACHE_DELAY_SECONDS", 0.0)
    calls = {"n": 0}

    def fake_baseline(region_name: str, refresh: bool = False) -> climate.ClimateBaseline:
        calls["n"] += 1
        if calls["n"] >= 3:
            raise climate.ApiQuotaExceededError(climate.USER_MESSAGE_QUOTA_HOUR, "HTTP 429")
        return climate._assemble_baseline(climate.find_region(region_name), [_fake_season()])

    monkeypatch.setattr(climate, "get_baseline", fake_baseline)
    results = climate.warm_all_baselines()

    assert len(results) == 20
    assert calls["n"] == 3, "碰到用量上限就該停手，不該把剩下 17 個產區跑完"
    assert list(results.values()).count("ok（1 年）") == 2
    assert all("未抓取" in value for value in list(results.values())[2:])


def test_參數錯誤的_4xx_不重試直接拋錯(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_get(*_args: Any, **_kwargs: Any) -> _FakeResponse:
        calls["n"] += 1
        return _FakeResponse(400, {"reason": "Data corrupted at path ''."})

    monkeypatch.setattr(climate.httpx, "get", fake_get)
    with pytest.raises(climate.ClimateDataError) as excinfo:
        climate._request_archive(44.84, -0.58, date(2019, 4, 1), date(2019, 4, 2))

    assert calls["n"] == 1
    assert "400" in excinfo.value.technical_detail


def test_錯誤訊息分兩層() -> None:
    error = climate.ClimateDataError("資料暫時無法取得。", "HTTP 500: upstream timeout")
    # 條款 18：使用者看不到 stack trace 這類技術細節。
    assert "HTTP" not in error.user_message
    assert error.technical_detail
