# Backlog — 開發待辦清單

> **白話**：Backlog 是「待辦事項清單」，把 User Story 拆成一個個可以直接動工的技術任務。每個任務都要小到能一次寫完（0.5–1 天），有明確驗收條件。
>
> **優先級**：P0 = 必做（不做產品跑不起來）、P1 = 加分（有時間再做）、P2 = 未來（本次不做）

---

## 快速閱讀

| 統計 | 數量 |
|---|---|
| **P0 任務** | 15 個 |
| **P1 任務** | 5 個 |
| **總預估工時** | 40–50 小時（約 7 天 × 每天 6 小時） |

---

## P0 任務清單

### 環境準備

| ID | 任務 | 預估 | 依賴 | 驗收條件 |
|---|---|---|---|---|
| T-01 | 建立 Python 專案結構、Git 初始化 | 0.5h | — | 目錄結構符合 `06_TechSetup.md`，有 `.gitignore` |
| T-02 | 安裝套件、寫 `requirements.txt` | 0.5h | T-01 | `pip install -r requirements.txt` 可跑通 |
| T-03 | 設定環境變數（OPENAI_API_KEY） | 0.5h | T-01 | 用 python-dotenv 讀取成功 |

### 資料準備

| ID | 任務 | 狀態 | 預估 | 依賴 | 驗收條件 |
|---|---|---|---|---|---|
| T-04 | 建立 10–15 個知名產區座標表 | ✅ 完成 | 2h | — | `data/regions.json` 含名稱、經緯度、半球、主要品種。**實際產出 20 個產區**，由知識庫 frontmatter 自動生成 |
| T-05 | 撰寫氣候風味知識庫 | ✅ 完成 | 4h | — | 採兩層架構（Schema v0.3）：`data/knowledge/climate_rules/` 10 則 + `data/knowledge/regions/` 20 產區共 88 則，**合計 98 則**，每則含 frontmatter、情境/影響、出處與 confidence 分級 |

> **T-05 設計異動說明**：原規劃為單層 30–50 則氣候規則（每則 3–5 行）。實作時升級為兩層混合架構——`climate_rules/` 保留原本「氣候異常 → 風味推論」的核心用途，`regions/` 新增產區百科層以支援品種、風土、產區對比類問題。詳見 `CLAUDE.md` 條款 34–38。

### 核心邏輯

| ID | 任務 | 狀態 | 預估 | 依賴 | 驗收條件 |
|---|---|---|---|---|---|
| T-06 | 實作 Open-Meteo API 呼叫函式 | ✅ 完成 | 2h | T-02 | `src/climate.py` 的 `fetch_season_climate()`，依 `hemisphere` 判斷生長季，回傳 `SeasonClimate`（含 to_dataframe）。CLI：`python -m src.climate --region "Bordeaux" --year 2019` |
| T-07 | 實作 GDD 與距平計算函式 | ✅ 完成 | 3h | T-06 | 輸入氣候資料，輸出 GDD 值與距平百分比 |
| T-08 | 抓 30 年基準線氣候資料並快取 | ✅ 完成 | 2h | T-06 | `get_baseline()` 抓 1991–2020 共 30 季存成 `data/cache/{產區}_baseline_1991_2020.json`，第二次查詢不再打 API。`--warm-cache-all` 可一次預熱 20 產區 |
| T-09 | 建立 Chroma 向量資料庫、匯入知識 | ✅ 完成 | 2h | T-05 | 可用 `chromadb.query()` 檢索到相關知識 |
| T-10 | 實作 GPT-4o-mini Vision 酒標辨識函式 | ✅ 完成 | 3h | T-02 | 輸入圖片路徑，輸出結構化 JSON |

> **T-06／T-08 實作備註**
>
> - 基準線區間採 WMO 標準的 1991–2020，不是原文件寫的 1990–2020（後者是 31 年）。`03_UserStory.md`、`05_Roadmap.md` 已同步。
> - 基準線只打一次 API 抓完整區間，再於本機切成 30 個生長季，比一年打一次少 30 倍請求。快取保留 30 年的每日原始值而非只存平均，因為 GDD 有 `max(t - base, 0)` 截斷，先平均再算跟先算再平均結果不同，T-07 會需要原始值。
> - 抓資料時發現 Mendoza 原座標 `-33.65, -69.35` 落在 ERA5 海拔 1725m 的安地斯山網格，2019 生長季算出 13.1°C／1793mm，與當地半沙漠氣候差很遠。已改為 Uco Valley 的 `-33.58, -69.10`（925m），修正後為 18.8°C／288mm。
> - Open-Meteo 免費方案的每小時額度大約只夠抓 13–14 個產區的基準線，`--warm-cache-all` 要分兩次跑。429 分兩種：每分鐘上限等 65 秒可解，每小時／每日上限會直接停手不硬重試。
> - ERA5 在山區會高估降雨（Barolo、Central Otago、Marlborough 都約為實測值的兩倍），這是 25km 網格的先天限制，移動座標救不了。T-13 報告與 T-20 README 要寫進方法論限制，見 README「氣候資料的來源與限制」。

> **T-07／T-09 實作備註**
>
> - 距平的標準差採母體標準差（`ddof=0`），是 WMO 30 年氣候常態期的慣例算法：30 年視為該常態期的完整母體，不是抽樣估計。跟 pandas 的預設 `ddof=1` 不同，需明確指定。
> - GDD 必須逐年算完再取平均，不能先把 30 年的日溫平均起來再算——`max(t - 10, 0)` 的截斷運算不能跟平均互換順序，先平均會讓冷涼產區的 GDD 被低估得更嚴重。
> - 缺值天數不設排除門檻：某一年即使缺值天數偏多，仍照樣用剩下的有效天數計算，只在輸出中標註缺了幾天（`missing_day_count`），不整年排除、不補值。
> - 「採收前 30 天降雨」沒有真實採收日資料，用生長季結束日往前推 30 天當代理指標，輸出明確標註「非實際採收日」。
> - 知識庫檔案本身沒有 `layer` frontmatter 欄位（跟原規劃不同），匯入時依檔案所在資料夾（`climate_rules/` 或 `regions/`）合成這個欄位，不是讀出來的。
> - 踩到一個 chromadb 1.x 的雷：`get_collection()`／`get_or_create_collection()` 若不明確傳入 `embedding_function`，會悄悄退回內建的本地模型，導致查詢向量跟寫入向量不同空間、檢索結果錯誤但不會報錯。`src/retrieval.py` 所有 collection 存取一律走同一個內部 helper 帶入同一組 embedding function，避免踩到。

### Agent 主流程

| ID | 任務 | 狀態 | 預估 | 依賴 | 驗收條件 |
|---|---|---|---|---|---|
| T-11 | 定義 5 個 function calling tools 的 schema | ✅ 完成 | 2h | T-06~T-10 | JSON schema 符合 OpenAI 規範 |
| T-12 | 實作 Agent orchestrator（tool routing） | ✅ 完成 | 4h | T-11 | 能自主呼叫 tools 完成完整流程 |
| T-13 | 實作風味推測報告生成 prompt | ✅ 完成 | 3h | T-12 | 報告含推測、限制、來源引用；system prompt 內建下方拒答清單 |

> **T-10／T-11 實作備註**
>
> - HEIC 支援決定排除：原規劃含 JPG/PNG/HEIC，實作階段改為只驗證 JPG/JPEG/PNG，避免額外依賴 `pillow-heif`。已覆寫 CLAUDE.md 條款 27，`03_UserStory.md` US-1.1 同步標註。
> - Vision 辨識用 OpenAI Structured Outputs（`response_format={"type": "json_schema", ...}` + `strict: True`）強制回應符合四欄位 schema，取代手刻 prompt 要求 JSON 再自行解析、修復的作法，回應格式異常的機率更低。
> - 5 個 tools 定案為：氣候距平查詢、氣候知識檢索、風土/產區比較知識檢索、酒標辨識、產區合法性檢查。每個 dispatch 函式捕捉底層自訂例外轉成 `{"error": True, ...}` 回傳，不讓例外中斷 orchestrator——但 `check_region_validity` 刻意例外：查無產區是「查詢成功、答案為否」而非工具故障，回傳 `{"valid": False, "reason": ...}`，兩種 shape 不要日後誤合併。
> - `src/tools.py` 額外附上 `TOOL_DISPATCH` 註冊表（工具名稱 → dispatch 函式），超出 T-11 本身「schema 符合規範」的驗收條件，是為 T-12 準備的查表呼叫便利設計。

#### T-13 附帶規格：Agent 拒答清單

以下範圍**不納入知識庫、也不由 Agent 回答**，需寫進 system prompt 的拒答邏輯，遇到就禮貌轉向。這些是功能規格（要寫進 prompt 才會生效），不是文件規範：

| # | 拒答範圍 | 範例問題 | 處理方式 |
|---|---|---|---|
| 1 | 通路、庫存、價格 | 「這支在哪買最便宜？」 | 對應 PRD Non-Goals，明確告知不提供 |
| 2 | 酒款評分、排名 | 「這支幾分？Parker 給幾分？」 | 不提供評分，轉向風味特徵描述 |
| 3 | 風味斷言 | 「這支好不好喝？值不值得買？」 | 一律用保留措辭（條款 16），不做價值判斷 |
| 4 | 搭餐配對 | 「配鴨胸可以嗎？」 | PRD Non-Goals，簡短說明非本系統專長後轉向風味描述 |
| 5 | 陳年潛力精確預測 | 「這支還能放幾年？」 | 只做風味推測，不預測具體年份 |
| 6 | 釀造工藝細節 | 「發酵溫度多少？用什麼橡木桶？」 | 超出氣候推論範圍 |
| 7 | 酒莊個別評論 | 「XX 酒莊的酒好嗎？」 | 只講產區共通性，不涉入個別酒莊評價 |
| 8 | 超出 20 產區清單的酒款 | 上傳希臘 Santorini 的酒 | 明確告知不涵蓋此產區，建議手動輸入其他資訊 |
| 9 | 非葡萄酒酒類 | 清酒、啤酒、烈酒、威士忌 | PRD Non-Goals，v1 僅葡萄酒（呼應條款 4）|
| 10 | 醫療、酒精攝取建議 | 「懷孕能喝嗎？」 | 一律拒答，建議諮詢專業醫療 |

> **T-12／T-13 實作備註**
>
> - 拆成兩階段：`agent.py`（階段一）只做 tool routing 與資料蒐集，system prompt 短而機械化；`src/report.py`（階段二）另外呼叫一次 LLM 生成報告，system prompt 專注在四段結構、保留措辭、引用規則與拒答清單。兩者混在同一個 prompt 容易互相稀釋，分開後各自的 prompt engineering 都更聚焦。
> - 拒答清單第 8 項不只寫進 prompt，同時在 `agent.py` 的 `_process_tool_calls()` 用程式判斷 `check_region_validity` 回傳的 `valid: False` 立刻中止迴圈——階段二（報告生成）完全不會被呼叫，不是靠模型「選擇不寫」。實測 `--region "Santorini"` 與 `--image data/test_labels/beaujolais.jpg`（Beaujolais 確實不在 20 產區清單／別名內）都會在階段一就優雅收尾，正確印出白話說明並以 exit code 1 結束。
> - `MAX_ITERATIONS` 定為 6，實測 Bordeaux／Chianti 兩條路徑都在 3–4 輪內完成；用 `--max-iterations 1` 故意逼近上限測試，確認迴圈會優雅收尾——用當輪已蒐集到的（不完整）資料呼叫 `report.generate_report()`，report 會誠實說明「氣候距平資料目前無法取得」而不是編數字，`資料來源` 段落也如實印出「本次報告未能引用任何具體知識庫片段或氣候資料」。
> - 「資料來源」段落刻意不讓 LLM 生成，改由 `report._build_sources_section()` 從實際檢索到的 metadata 決定式組裝（confidence、sources 都是 frontmatter 原值，不重新評分）；`report._collect_cited_ids()` 會把不在檢索結果內的引用 id 直接過濾掉並記 log warning，避免假引用流到使用者畫面。
> - GPT-4o-mini 對「每段都要附引用標記」的指令遵從度最初不是 100%——同樣的輸入重跑兩次，一次三段都附了引用，一次完全沒附。這是模型能力限制，不是程式邏輯問題；當時的 `_extract_cited_ids()`／`_build_sources_section()` 兩種情況都能正確處理（有引用就列出、沒引用就誠實顯示「未能引用」），不會因為模型沒附標記就出錯或幻覺出引用，但「沒引用」發生的頻率本身牴觸 PRD「可解釋性 100% 有引用來源」的成功指標，動 T-14 UI 之前先修掉（見下方新增備註）。
> - **引用機制修正（動 T-14/T-15 前）**：把自由格式 Markdown＋regex 撈 `[chunk_id]` 的作法，改成仿 `src/vision.py` 的 OpenAI Structured Outputs 模式——`report._call_report_llm_once()` 用 `response_format={"type": "json_schema", ...}` + `strict: True` 逼模型輸出 `{flavor_inference: [{text, cited_ids}], climate_summary, limitations}`，`cited_ids` 的陣列元素用 `enum` 限定成當次實際檢索到的 chunk id，模型在 schema 層就不可能編造引用（`knowledge_hits` 為空時 `enum` 會是空陣列而不合法，改用不帶 `enum` 的簡化 schema）。`[chunk_id]` 括號標記改由 `report._render_flavor_section()` 依 `cited_ids` 決定式渲染進最終 Markdown，不再依賴模型自己在散文裡手寫——這才是真正修掉遵從度問題的關鍵，不只是把驗證挪到 schema 層。Strict 模式不支援 `minItems`，模型仍可能合法讓每段 `cited_ids` 都是空陣列，`report._generate_structured_body()` 加了一次性重試（`known_ids` 非空但全段都沒引用時，帶提醒重新呼叫一次），重試後仍空就誠實接受、不硬掰。原本 regex 版的 `_extract_cited_ids()` 退場，換成在結構化陣列上做事的 `_collect_cited_ids()`；`_ensure_limitation_caveats()` 也從搜尋 Markdown 標題插入字串，簡化成直接對 `limitations` 純字串欄位操作。實測連續跑 5 次 `python -m src.report --region "Bordeaux" --year 2019`，每次「風味推測」都有引用標記、「資料來源」都有實際條目，不再有「一次全附、一次全無」的不穩定情況。
> - 實測發現兩個 humanizer-zh 相關的細節：(1) 沒有明確要求「只用繁體中文」時，GPT-4o-mini 偶爾會夾雜簡體字（如「特征」而非「特徵」），在 system prompt 開頭加一句明確禁止後沒有再出現；(2) system prompt 若沒有限制轉折詞用法，模型容易每段開頭都塞「此外」，加一條「同一份報告最多出現一次」的規則後明顯改善。
> - ERA5 山區降雨高估的提醒最初用「如果產區是這三個之一」的措辭，模型偶爾會過度聯想到「山」「地形」而對非山區產區（如 Bordeaux）也提起這個說法；改成明確寫「只有『正好』是這三個產區才提，其他任何產區都不要提」後修正。`report._ensure_limitation_caveats()` 仍保留 belt-and-suspenders 的程式碼補句機制，作為 prompt 遵從度不足時的保底。
> - `src/report.py` 額外提供 `python -m src.report --region --year` 的獨立 CLI，跳過 `agent.py` 的 tool-calling loop、直接用 `climate`／`retrieval` 組資料，方便單獨除錯報告 prompt 的品質。

### 介面

| ID | 任務 | 狀態 | 預估 | 依賴 | 驗收條件 |
|---|---|---|---|---|---|
| T-14 | 建立 Streamlit 主頁面（上傳→報告） | ✅ 完成 | 4h | T-12 | 完整流程可用 |
| T-15 | 加入氣候距平視覺化圖表 | ✅ 完成 | 2h | T-14 | 折線圖+柱狀圖顯示對比 |

> **T-14／T-15 實作備註**
>
> - `app.py` 放根目錄（跟 `agent.py` 同層）。動 UI 前先把 `agent.py` 的 `main()` 拆出
>   `analyze()`（回傳結構化的 `AnalysisResult`，不直接 `print()`），CLI 與 Streamlit 共用
>   同一套「跑迴圈→判斷終止狀態→生成報告」邏輯，產區越界之類的分支不用寫兩次。
> - 上傳圖片走「UI 層直接呼叫 `vision.recognize_label()` 取得可編輯表單 → 確認／編輯後
>   用 `region`／`year` 呼叫 `agent.analyze()`」，刻意不透過 `agent.analyze(image_path=...)`
>   ——後者會讓 agent 自己的 tool-calling loop 再跑一次辨識，等於同一張圖片辨識兩次、多燒
>   一次 API 額度。副作用是 agent loop 自己蒐集到的 `gathered.label_info` 永遠是 `None`（
>   因為沒有走 image_path 路徑），`analyze()` 因此多一個 `label_info` 參數，UI 端把已確認
>   的酒莊／品種資訊直接帶進去，優先於 `gathered.label_info`——不然這條路徑會讓酒莊／品種
>   資訊憑空消失，`report.generate_report()` 確實有用到這兩個欄位。
> - Streamlit 每次互動都整支腳本重跑，圖片辨識與 `agent.analyze()` 都只掛在按鈕點擊／
>   偵測到新檔案時才執行，用 `st.session_state` 存結果，避免使用者編輯任一欄位就重新觸發
>   付費 API（`06_TechSetup.md` §11 踩雷點）。新圖片判斷用 `UploadedFile.file_id` 比對，
>   不是內容雜湊——Streamlit 專門為此設計的識別碼，比自己 hash 圖片位元組更輕量。用
>   Streamlit 的 `AppTest` 框架（不用真的開瀏覽器）驗證過：編輯表單欄位觸發的 rerun 不會
>   再打任何 OpenAI API。
> - 實測發現一個真的會讓畫面變成 stack trace 的雷：`st.image()` 顯示縮圖如果放在格式／
>   大小驗證**之前**，遇到內容無法解碼的檔案（例如副檔名是 `.jpg` 但內容不是合法圖片）
>   會直接拋 `UnidentifiedImageError` 讓整頁掛掉，牴觸條款 18／US-4.1「不會白畫面」的
>   要求。修法：驗證（副檔名、大小）先做完，`st.image()` 本身也包 `try/except`，捕捉到
>   解碼失敗一律轉成「格式看起來不是 JPG 或 PNG」的白話提示。
> - 產區欄位用純文字輸入框，刻意不做下拉選單限定 20 個產區——那樣會讓「輸入不在清單內
>   的產區」這條路徑在表單層就永遠無法觸發，等於悄悄拿掉 T-17 明確要測的手動輸入 fallback
>   行為。改用 `st.expander` 列出 20 個支援產區當輕量提示。
> - 手動輸入 fallback（T-17）不是獨立模式，是同一份表單：酒標辨識完全失敗、辨識不出
>   產區、或使用者根本沒上傳圖片，欄位就是空的，直接打字即可。實測 `chianti.jpg`（能
>   辨識出 `Chianti Classico`，透過既有的別名比對正確解析成 `Chianti`）、`beaujolais.jpg`
>   與 `idontknow.jpg`（後者實際辨識出一個不在 20 產區清單內的產區，跟預期的「完全辨識
>   不出」不同，但結果一樣——`region_not_covered` 分支正確顯示白話說明、表單保留可編輯、
>   不是白畫面）都通過。
> - `climate.build_monthly_comparison()` 依生長季順序（不是日曆 1–12 月）排列月份，靠
>   `_month_sequence()` 直接讀 `NORTHERN_SEASON`／`SOUTHERN_SEASON` 常數推導、用 `% 12`
>   處理南半球跨年 wrap-around；基準線的月統計刻意先算「每季各自的月加總／月均溫」再對
>   這些數字取平均，不是對攤平後的每日資料直接做「日均降雨」——後者只有在每個月天數剛好
>   一致時才會巧合對上。實測 Mendoza（10 月排到隔年 4 月）與 Bordeaux（4 月排到 10 月）
>   都正確、沒有從中間斷開。Barolo／Central Otago／Marlborough 的降雨柱狀圖下方固定加一行
>   ERA5 山區降雨高估的 caption，常數直接從 `report.MOUNTAIN_RAINFALL_BIAS_REGIONS` 匯入，
>   不重複定義產區清單。
> - **手機實測（條款 31）**：用真的手機連區網 IP 走完一輪，拍照入口正常叫出相機、版面不需
>   橫向捲動；兩張不在 20 產區清單內的酒標都正確擋下並保留表單可改，最後用 Chianti 酒標完整
>   跑通辨識與風味分析。環境前提記一下，這兩點實際卡住過：WSL2 要設 `networkingMode=mirrored`
>   （`.wslconfig`），且 Windows 防火牆要放行 TCP 8501 的對內連線，否則手機連不到。
> - **待辦：手機拍照到辨識結果偏慢**（實測 `vision.py` 有觸發「超過 US-1.2 的 5 秒目標」的 log
>   warning）。成因不是模型慢，是 `_encode_image_data_url()` 直接把原圖 `read_bytes()` 後
>   base64——手機原圖動輒 3–5MB／3000×4000，編碼後變 4–6.7MB 整份送進 OpenAI，而 Vision API
>   本來就會把圖縮到 2048 內、短邊 768 再切 512px tile，送超過這個尺寸的部分純屬浪費上傳時間
>   與 image token。**列入下一輪打磨處理，本輪不修**（條款 30：先確保端到端能跑，再回頭優化）。
>   另外注意 `_call_vision_api()` 的 `started = time.perf_counter()` 目前設在 encode 之後，
>   5 秒目標的量測沒把編碼時間算進去，改善幅度會被低估。

---

## P1 任務（加分項）

| ID | 任務 | 狀態 | 預估 | 依賴 | 驗收條件 |
|---|---|---|---|---|---|
| T-16 | 知名年份驗證測試（3–5 案例） | | 3h | T-13 | 產出驗證報告 Markdown |
| T-17 | 手動輸入 fallback 介面 | ✅ 完成 | 1h | T-14 | Vision 失敗時可切換手動 |
| T-18 | 錯誤處理與 loading 狀態優化 | | 2h | T-14 | 各種例外都有友善訊息 |
| T-19 | 一鍵匯出報告為 Markdown | | 1h | T-14 | 下載按鈕產生 .md 檔 |
| T-20 | README 撰寫（含 demo 截圖、方法論限制） | | 2h | 全部 | 履歷投遞時可直接分享的 GitHub 頁面 |

---

## P2 任務（本次不做，未來可延伸）

- 支援清酒（需要不同氣候→風味知識庫）
- 支援使用者收藏與歷史紀錄
- 部署到雲端（Streamlit Community Cloud）
- 支援多語言介面
- 進階 tool：對比同產區多年氣候趨勢
- Huglin 指數（Huglin Index）作為 GDD 之外的補充積溫指標
- **產區→慣用品種推論**（影響 T-13）：酒標辨識（T-10）的 `grape` 欄位為 `null`、但產區有效
  時，在報告生成階段查 `data/regions.json` 的 `main_grapes` 欄位或知識庫品種介紹檔，補一句
  「這個產區通常以 XXX 品種為主」。**不能**放進 T-10 本身——`vision.py` 只回報酒標上實際印
  的內容，不確定的欄位一律 `null`，這是 US-1.2 的硬性驗收條件（呼應條款 15）。若之後採用，
  措辭要用條款 16 的保留語氣（「通常」「多數情況下」），且要明確標註「這是產區慣例推論，
  不是酒標上寫的」，跟辨識結果分開呈現，並附引用來源（呼應條款 17）。實測時發現大部分真實
  酒標拍到的產區（如 Beaujolais、Chinon、Asti）不在 20 產區清單內，這個功能只在使用者拍到
  清單內的 20 個產區時才有意義。

---

## 任務依賴關係圖

```
T-01 ──┬── T-02 ──┬── T-06 ── T-07 ── T-08
       │          ├── T-10
       └── T-03   └── T-09 ──────────────┐
                                          │
T-04 ─────────────────────────────────────┤
T-05 ────────────────────► T-09           │
                                          ▼
                        T-11 ── T-12 ── T-13 ── T-14 ── T-15
                                                          │
                                                          ▼
                                            T-16 ~ T-20（P1 加分）
```

---

## 每日建議挑選（對應 Roadmap）

| Day | 建議任務 |
|---|---|
| Day 1 | T-01, T-02, T-03, T-04, T-05（部分） |
| Day 2 | T-05（完成）, T-06, T-08 |
| Day 3 | T-07, T-09 |
| Day 4 | T-10, T-11 |
| Day 5 | T-12, T-13 |
| Day 6 | T-14, T-15, T-17 |
| Day 7 | T-16, T-18, T-19, T-20 |
