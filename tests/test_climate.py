"""`src.climate` 的單元測試。

全部離線執行，不打 Open-Meteo API：需要 API 回應的地方一律用假回應替換。測試重點放在最容易
寫錯、又最難用肉眼發現的地方——南半球生長季跨年、快取是否真的擋掉重複請求、缺值有沒有被偷偷
補值（條款 15）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
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


# --- GDD 與距平計算（T-07） --------------------------------------------------


def _daily_frame(
    dates: list[str], temps: list[float | None], precips: list[float | None]
) -> pd.DataFrame:
    """組一份最小可用的每日 DataFrame，供 `_season_climate_metrics` 系列測試使用。"""
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "temp_mean": temps,
        "precipitation_mm": precips,
    })


def _make_anomaly_season(
    vintage_year: int,
    dates: list[str],
    temps: list[float | None],
    precips: list[float | None],
    region_name: str = "Bordeaux",
) -> climate.SeasonClimate:
    """組一個指定溫度／降雨的 `SeasonClimate`，供距平計算測試自訂數值用。"""
    region = climate.find_region(region_name)
    start, end = climate.growing_season_range(vintage_year, region["hemisphere"])
    daily = [
        climate.DailyRecord(
            date=d, temp_max=t, temp_min=t, temp_mean=t,
            precipitation_mm=p, sunshine_hours=5.0,
        )
        for d, t, p in zip(dates, temps, precips)
    ]
    return climate.SeasonClimate(
        region_canonical=region["region_canonical"],
        region_zh=region["region_zh"],
        country=region["country"],
        hemisphere=region["hemisphere"],
        latitude=region["latitude"],
        longitude=region["longitude"],
        vintage_year=vintage_year,
        season_start=str(start),
        season_end=str(end),
        timezone="UTC",
        source="test-fixture",
        is_partial=False,
        daily=daily,
    )


def _make_anomaly_baseline(
    seasons: list[climate.SeasonClimate], region_name: str = "Bordeaux"
) -> climate.ClimateBaseline:
    """組一個由自訂 `SeasonClimate` 清單構成的假基準線。"""
    region = climate.find_region(region_name)
    years = [season.vintage_year for season in seasons]
    return climate.ClimateBaseline(
        region_canonical=region["region_canonical"],
        region_zh=region["region_zh"],
        country=region["country"],
        hemisphere=region["hemisphere"],
        latitude=region["latitude"],
        longitude=region["longitude"],
        start_year=min(years) if years else climate.BASELINE_START_YEAR,
        end_year=max(years) if years else climate.BASELINE_END_YEAR,
        timezone="UTC",
        source="test-fixture",
        fetched_at="2026-08-17T00:00:00Z",
        seasons=seasons,
    )


def test_GDD_只計入超過基礎溫度的部分且缺值不計入() -> None:
    frame = _daily_frame(
        ["2019-04-01", "2019-04-02", "2019-04-03", "2019-04-04"],
        [5.0, 15.0, None, 25.0],
        [0.0, 0.0, 0.0, 0.0],
    )
    metrics = climate._season_climate_metrics(frame, harvest_window_days=4)
    # max(5-10,0)=0、max(15-10,0)=5、缺值那天跳過不計、max(25-10,0)=15。
    assert metrics["gdd"] == pytest.approx(0 + 5 + 15)
    assert metrics["gdd_missing_days"] == 1


def test_逐年算_GDD_再平均_跟_先平均氣溫再算_GDD_結果不同() -> None:
    # 年份 A：兩天都是 12°C，逐日 GDD 為 2、2，年總和 4。
    # 年份 B：兩天都是 6°C，逐日 GDD 為 0、0，年總和 0。
    season_a = _make_anomaly_season(
        2020, ["2020-04-01", "2020-04-02"], [12.0, 12.0], [0.0, 0.0]
    )
    season_b = _make_anomaly_season(
        2021, ["2021-04-01", "2021-04-02"], [6.0, 6.0], [0.0, 0.0]
    )
    baseline = _make_anomaly_baseline([season_a, season_b])

    per_year = climate._baseline_year_metrics(baseline)
    assert per_year[2020]["gdd"] == pytest.approx(4.0)
    assert per_year[2021]["gdd"] == pytest.approx(0.0)

    # 正確做法：逐年算完 GDD 再平均 → (4 + 0) / 2 = 2.0。
    correct_mean_gdd = sum(metrics["gdd"] for metrics in per_year.values()) / len(per_year)
    assert correct_mean_gdd == pytest.approx(2.0)

    # 錯誤做法（先把兩年同一天的氣溫平均，再算 GDD）：每天平均溫都是 9°C，
    # max(9-10,0)=0，兩天加總還是 0——跟正確答案的 2.0 明顯不同，證明順序不能顛倒。
    wrong_avg_day1 = (12.0 + 6.0) / 2
    wrong_avg_day2 = (12.0 + 6.0) / 2
    wrong_gdd = max(wrong_avg_day1 - climate.GDD_BASE_TEMP_C, 0) + max(
        wrong_avg_day2 - climate.GDD_BASE_TEMP_C, 0
    )
    assert wrong_gdd == pytest.approx(0.0)
    assert correct_mean_gdd != pytest.approx(wrong_gdd)


def test_百分比距平與_z_score_對照手算數值() -> None:
    # 基準線三年 GDD 分別為 10、20、30：平均 20，母體標準差 sqrt(200/3) ≈ 8.16497。
    per_year_metrics = {
        1991: {"gdd": 10.0, "gdd_missing_days": 0},
        1992: {"gdd": 20.0, "gdd_missing_days": 0},
        1993: {"gdd": 30.0, "gdd_missing_days": 0},
    }
    vintage_metrics = {"gdd": 26.0, "gdd_missing_days": 0}

    metric = climate._build_metric_anomaly(
        "gdd", "gdd", "gdd_missing_days", vintage_metrics, per_year_metrics
    )

    assert metric.baseline_mean == pytest.approx(20.0)
    assert metric.pct_anomaly == pytest.approx(30.0)  # (26-20)/20*100
    assert metric.z_score == pytest.approx((26.0 - 20.0) / (200 / 3) ** 0.5, rel=1e-6)


def test_缺值天數會標記在輸出而不是默默略過() -> None:
    season = _make_anomaly_season(
        2022, ["2022-04-01", "2022-04-02"], [15.0, None], [1.0, 2.0]
    )
    baseline = _make_anomaly_baseline([
        _make_anomaly_season(2019, ["2019-04-01", "2019-04-02"], [10.0, 11.0], [0.5, 0.5]),
        _make_anomaly_season(2020, ["2020-04-01", "2020-04-02"], [12.0, 13.0], [1.0, 1.0]),
        _make_anomaly_season(2021, ["2021-04-01", "2021-04-02"], [None, 14.0], [None, 2.0]),
    ])

    anomaly = climate.compute_climate_anomaly(season, baseline)

    assert anomaly.gdd.vintage_missing_day_count == 1
    assert anomaly.gdd.baseline_missing_day_count == 1
    assert anomaly.gdd.baseline_years_with_missing_data == (2021,)


def test_基準線標準差為_0_時_z_score_為_None_不拋錯() -> None:
    per_year_metrics = {
        1991: {"gdd": 5.0, "gdd_missing_days": 0},
        1992: {"gdd": 5.0, "gdd_missing_days": 0},
        1993: {"gdd": 5.0, "gdd_missing_days": 0},
    }
    vintage_metrics = {"gdd": 10.0, "gdd_missing_days": 0}

    metric = climate._build_metric_anomaly(
        "gdd", "gdd", "gdd_missing_days", vintage_metrics, per_year_metrics
    )
    assert metric.z_score is None
    assert metric.pct_anomaly == pytest.approx(100.0)


def test_基準線平均值為_0_時_百分比距平為_None_不除以_0() -> None:
    per_year_metrics = {
        1991: {"season_precip_mm": -10.0, "season_precip_missing_days": 0},
        1992: {"season_precip_mm": 10.0, "season_precip_missing_days": 0},
        1993: {"season_precip_mm": 0.0, "season_precip_missing_days": 0},
    }
    vintage_metrics = {"season_precip_mm": 5.0, "season_precip_missing_days": 0}

    metric = climate._build_metric_anomaly(
        "season_precipitation_mm", "season_precip_mm", "season_precip_missing_days",
        vintage_metrics, per_year_metrics,
    )
    assert metric.baseline_mean == pytest.approx(0.0)
    assert metric.pct_anomaly is None
    assert metric.z_score is not None


def test_基準線有效年數不足時拋出_InsufficientBaselineDataError() -> None:
    season = _make_anomaly_season(2022, ["2022-04-01"], [15.0], [1.0])
    baseline = _make_anomaly_baseline([
        _make_anomaly_season(2019, ["2019-04-01"], [12.0], [1.0]),
    ])
    with pytest.raises(climate.InsufficientBaselineDataError):
        climate.compute_climate_anomaly(season, baseline)


def test_基準線完全沒有每日資料時拋出_InsufficientBaselineDataError() -> None:
    baseline = _make_anomaly_baseline([])
    with pytest.raises(climate.InsufficientBaselineDataError):
        climate._baseline_year_metrics(baseline)


def test_採收前降雨代理值有明確起訖日() -> None:
    frame = _daily_frame(
        ["2019-04-01", "2019-04-02", "2019-04-03"],
        [15.0, 15.0, 15.0],
        [1.0, 2.0, 3.0],
    )
    metrics = climate._season_climate_metrics(frame, harvest_window_days=2)
    assert metrics["pre_harvest_window_start"] == "2019-04-02"
    assert metrics["pre_harvest_window_end"] == "2019-04-03"
    assert metrics["pre_harvest_precip_mm"] == pytest.approx(2.0 + 3.0)


def test_人類可讀摘要符合目標句型且標註採收日為代理值() -> None:
    def _metric(
        pct: float, missing: int = 0, missing_years: tuple[int, ...] = ()
    ) -> climate.MetricAnomaly:
        return climate.MetricAnomaly(
            metric_name="x", vintage_value=0.0, vintage_missing_day_count=missing,
            baseline_mean=100.0, baseline_std=10.0, baseline_year_count=30,
            baseline_missing_day_count=len(missing_years),
            baseline_years_with_missing_data=missing_years,
            pct_anomaly=pct, z_score=pct / 10,
        )

    anomaly = climate.ClimateAnomaly(
        region_canonical="Bordeaux", region_zh="波爾多", vintage_year=2019,
        baseline_start_year=1991, baseline_end_year=2020,
        gdd=_metric(12.0), season_precipitation=_metric(-20.0),
        pre_harvest_precipitation=_metric(-56.0, missing=2, missing_years=(1995,)),
        harvest_proxy_window_start="2019-10-02", harvest_proxy_window_end="2019-10-31",
    )

    summary = climate.format_anomaly_summary(anomaly)
    assert "GDD比 30 年平均高12%" in summary
    assert "生長季降雨比 30 年平均少20%" in summary
    assert "非實際採收日" in summary
    assert "註：" in summary  # 有缺值時要附加缺值說明


@pytest.mark.skipif(
    not (climate.CACHE_DIR / "bordeaux_2019.json").exists()
    or not (climate.CACHE_DIR / "bordeaux_baseline_1991_2020.json").exists(),
    reason="需要本機已執行過 --warm-cache-all 或單一產區快取，CI/新 clone 無此檔案",
)
def test_Bordeaux_2019_距平為正_已知暖年份的方向性檢查() -> None:
    season = climate.fetch_season_climate("Bordeaux", 2019)
    baseline = climate.get_baseline("Bordeaux")
    anomaly = climate.compute_climate_anomaly(season, baseline)
    assert anomaly.gdd.pct_anomaly > 0


# --- 氣候方向分類（T-16 後續修正） ---------------------------------------------


def _make_direction_anomaly(
    gdd_pct: float | None, precip_pct: float | None = 0.0
) -> climate.ClimateAnomaly:
    """組一份只有距平百分比有意義的 `ClimateAnomaly`，方向分類只讀這兩個欄位。"""

    def _metric(pct: float | None) -> climate.MetricAnomaly:
        return climate.MetricAnomaly(
            metric_name="x", vintage_value=0.0, vintage_missing_day_count=0,
            baseline_mean=100.0, baseline_std=10.0, baseline_year_count=30,
            baseline_missing_day_count=0, baseline_years_with_missing_data=(),
            pct_anomaly=pct, z_score=None if pct is None else pct / 10,
        )

    return climate.ClimateAnomaly(
        region_canonical="Bordeaux", region_zh="波爾多", vintage_year=2019,
        baseline_start_year=1991, baseline_end_year=2020,
        gdd=_metric(gdd_pct), season_precipitation=_metric(precip_pct),
        pre_harvest_precipitation=_metric(0.0),
        harvest_proxy_window_start="2019-10-02", harvest_proxy_window_end="2019-10-31",
    )


@pytest.mark.parametrize(
    ("case", "gdd_pct", "expected"),
    [
        ("2013 Bordeaux", -3.63, "cooler"),
        ("2011 Napa Valley", -11.17, "cooler"),
        ("2003 Bordeaux", 16.70, "warmer"),
        ("2018 Napa Valley", 3.48, "warmer"),
        ("2019 Bordeaux", 6.28, "warmer"),
    ],
)
def test_五個T16驗證案例的方向分類都正確(
    case: str, gdd_pct: float, expected: str
) -> None:
    """用 docs/07_ValidationReport.md 記錄的實測距平值當回歸測試。

    2018 Napa 的 +3.48% 是綁死 deadband 上限的案例：deadband 若取到 3.48 以上，這個
    公認的偏暖年份就會被判成方向不明，檢索退回無過濾狀態。
    """
    anomaly = _make_direction_anomaly(gdd_pct)
    assert climate.classify_gdd_direction(anomaly) == expected, case


@pytest.mark.parametrize("gdd_pct", [1.99, -1.99, 0.0])
def test_距平落在deadband內視為方向不明(gdd_pct: float) -> None:
    assert climate.classify_gdd_direction(_make_direction_anomaly(gdd_pct)) is None


@pytest.mark.parametrize(("gdd_pct", "expected"), [(2.0, "warmer"), (-2.0, "cooler")])
def test_距平剛好等於deadband邊界時判定為有方向(gdd_pct: float, expected: str) -> None:
    assert climate.classify_gdd_direction(_make_direction_anomaly(gdd_pct)) == expected


def test_pct_anomaly為None時方向為None() -> None:
    """基準值為 0 導致無法算百分比時，不硬猜方向（呼應條款 15）。"""
    assert climate.classify_gdd_direction(_make_direction_anomaly(None)) is None


@pytest.mark.parametrize(
    ("gdd_pct", "precip_pct", "expected_phrases"),
    [
        (-11.17, 31.5, ("偏涼", "降雨偏多")),
        (16.70, 0.0, ("偏暖", "降雨接近平均")),
        (3.48, -31.4, ("偏暖", "降雨偏少")),
        (0.0, 0.0, ("溫度接近平均", "降雨接近平均")),
    ],
)
def test_查詢字串含明確方向詞而非高少措辭(
    gdd_pct: float, precip_pct: float, expected_phrases: tuple[str, ...]
) -> None:
    """查詢字串要用知識庫規則的用字，不能沿用摘要句的「高」「少」。"""
    query = climate.format_direction_query(_make_direction_anomaly(gdd_pct, precip_pct))
    for phrase in expected_phrases:
        assert phrase in query
    assert "比 30 年平均" not in query


def test_build_anomaly_payload附上方向與查詢字串欄位() -> None:
    payload = climate.build_anomaly_payload(_make_direction_anomaly(-11.17, 31.5))
    assert payload["temperature_direction"] == "cooler"
    assert "偏涼" in payload["direction_query"]
    assert "summary" in payload
    assert payload["gdd"]["pct_anomaly"] == -11.17  # 原本的距平欄位要照樣保留


# --- 逐月比較（T-15） --------------------------------------------------------


def test_month_sequence北半球從四月排到十月() -> None:
    assert climate._month_sequence("N") == [4, 5, 6, 7, 8, 9, 10]


def test_month_sequence南半球從十月跨年排到四月不從中間斷開() -> None:
    assert climate._month_sequence("S") == [10, 11, 12, 1, 2, 3, 4]


def test_season_monthly_stats依月份數字分組() -> None:
    season = _make_anomaly_season(
        2019,
        ["2019-04-01", "2019-04-02", "2019-05-01"],
        [10.0, 12.0, 20.0],
        [1.0, 2.0, 5.0],
    )
    stats = climate._season_monthly_stats(season)
    assert stats.loc[4, "temp_mean"] == pytest.approx(11.0)
    assert stats.loc[4, "precipitation_mm"] == pytest.approx(3.0)
    assert stats.loc[5, "temp_mean"] == pytest.approx(20.0)


def test_season_monthly_stats依年月字串分組供人工除錯() -> None:
    season = _make_anomaly_season(2019, ["2019-04-01"], [10.0], [1.0])
    stats = climate._season_monthly_stats(season, by_calendar_month=False)
    assert list(stats.index) == ["2019-04"]


def test_baseline_monthly_stats對每季的月統計取平均而非日均值() -> None:
    # 兩個 4 月，天數不同（2 天 vs 3 天）：如果誤用「攤平後對日降雨取平均再乘天數」
    # 會算錯，正確做法是各自先算「該季 4 月總降雨」再對這兩個數字取平均。
    season_a = _make_anomaly_season(2019, ["2019-04-01", "2019-04-02"], [10.0, 12.0], [1.0, 1.0])
    season_b = _make_anomaly_season(
        2020, ["2020-04-01", "2020-04-02", "2020-04-03"], [14.0, 16.0, 18.0], [3.0, 3.0, 3.0]
    )
    baseline = _make_anomaly_baseline([season_a, season_b])

    stats = climate._baseline_monthly_stats(baseline)

    assert stats.loc[4, "precipitation_mm"] == pytest.approx((2.0 + 9.0) / 2)
    assert stats.loc[4, "temp_mean"] == pytest.approx((11.0 + 16.0) / 2)


def test_build_monthly_comparison南半球依生長季順序排列且涵蓋兩個年份(
) -> None:
    season = _make_anomaly_season(
        2019, ["2018-10-01", "2019-04-30"], [15.0, 18.0], [2.0, 3.0], region_name="Mendoza",
    )
    baseline = _make_anomaly_baseline(
        [_make_anomaly_season(
            2018, ["2017-10-01", "2018-04-30"], [14.0, 17.0], [1.0, 2.0], region_name="Mendoza",
        )],
        region_name="Mendoza",
    )

    comparison = climate.build_monthly_comparison(season, baseline)

    assert list(comparison["month"]) == [10, 11, 12, 1, 2, 3, 4]
    assert comparison.iloc[0]["month_label"] == "10月"
    # 10 月與 4 月都有實際資料，中間月份(11~3) 因為沒有每日資料，聚合結果應為 NaN。
    assert comparison.loc[comparison["month"] == 10, "temp_mean_vintage"].iloc[0] == pytest.approx(15.0)
    assert comparison.loc[comparison["month"] == 4, "temp_mean_vintage"].iloc[0] == pytest.approx(18.0)
    assert pd.isna(comparison.loc[comparison["month"] == 12, "temp_mean_vintage"].iloc[0])


def test_build_monthly_comparison北半球依生長季順序排列() -> None:
    season = _make_anomaly_season(2019, ["2019-04-01", "2019-10-31"], [12.0, 16.0], [1.0, 2.0])
    baseline = _make_anomaly_baseline(
        [_make_anomaly_season(2018, ["2018-04-01", "2018-10-31"], [11.0, 15.0], [1.0, 2.0])]
    )

    comparison = climate.build_monthly_comparison(season, baseline)

    assert list(comparison["month"]) == [4, 5, 6, 7, 8, 9, 10]
    assert comparison.loc[comparison["month"] == 4, "temp_mean_baseline"].iloc[0] == pytest.approx(11.0)
