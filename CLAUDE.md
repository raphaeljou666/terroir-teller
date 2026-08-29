# CLAUDE.md — TerroirTeller 專案開發規範

> **這份檔案的用途**：本檔為 Claude Code（或其他 AI 輔助開發工具）在此專案內作業時必須遵守的規範。所有由 AI 產生的程式碼、註解、文件、commit message，都必須符合以下條款。
>
> **放置位置**：專案根目錄 `terroir-teller/CLAUDE.md`
>
> **優先級**：本檔規範 > 使用者當下指令中的預設風格。若使用者明確覆寫某條款，以使用者為準，但需在回覆中提示「已覆寫規範 #X」。

---

## 【類別 1】語言與用字

### 1. 統一使用繁體中文
專案所有輸出、註解、docstring、變數命名說明、commit message 一律使用繁體中文。程式關鍵字、識別字、套件名稱仍用英文（例：`def calculate_gdd():`，但函式內註解用繁中）。

### 2. 禁止簡體字與中國用語
禁止使用簡體字。禁止使用中國用語，統一使用台灣慣用詞。常見對照：

| 使用 ✅ | 避免 ❌ |
|---|---|
| 軟體 | 軟件 |
| 硬體 | 硬件 |
| 程式 | 程序 |
| 專案 | 項目 |
| 檔案（指電腦檔案時） | 文件 |
| 資料 | 數據（部分情境可用，但優先資料） |
| 影片 | 視頻 |
| 網路 | 網絡 |
| 記憶體 | 內存 |
| 螢幕 | 屏幕 |
| 使用者 | 用戶 |
| 呼叫 | 調用 |
| 除錯 | 調試 |
| 整合 | 集成 |
| 載入 | 加載 |
| 遠端 | 遠程 |
| 存取 | 訪問 |
| 函式 | 函數（數學上可用） |
| 陣列 | 數組 |
| 變數 | 變量 |
| 物件 | 對象 |
| 元件 | 組件 |
| 建置 / 建構 | 構建 |
| 部署 | 部署（相同） |
| 登入 | 登錄 |
| 回呼 | 回調 |
| 快取 | 緩存 |
| 預設 | 默認 |
| 迴圈 | 循環 |
| 列印 / print | 打印 |

### 3. 產地與專業術語保留原文
產地、酒莊名、品種名、法定產區術語（AOC、DOC、AVA 等）一律保留原文（例：Bordeaux, Pinot Noir, Grand Cru, Château Margaux）。**第一次出現時後方加繁中括號解釋**，之後可直接使用原文。

範例：
- ✅ Bordeaux（波爾多，位於法國西南部的知名葡萄酒產區）
- ✅ Pinot Noir（黑皮諾，早熟且對氣候敏感的紅酒品種）
- ❌ 波爾多（直翻不保留原文）
- ❌ 皮諾諾亞

### 4. 產品範圍：先以葡萄酒為主
本專案 v1 範圍**僅涵蓋葡萄酒**。清酒、啤酒、烈酒相關功能列為 P2 未來擴充，開發階段不實作，避免範圍蔓延。

---

## 【類別 2】程式風格與命名

### 5. 遵循 PEP 8
- 函式與變數用 `snake_case`
- 類別用 `PascalCase`
- 常數用 `UPPER_SNAKE_CASE`
- 縮排 4 空格、每行 ≤ 100 字元

### 6. Type hints 與 docstring
所有對外函式加上 type hints 與 Google 風格 docstring。範例：

```python
def calculate_gdd(
    daily_temps: list[float],
    base_temp: float = 10.0
) -> float:
    """計算生長積溫（Growing Degree Days）。

    Args:
        daily_temps: 每日平均溫度列表（攝氏）。
        base_temp: 基礎溫度，超過此值的部分才累計。預設 10°C。

    Returns:
        累計的生長積溫值。數字越大代表該生長季氣候越溫暖。
    """
    return sum(max(t - base_temp, 0) for t in daily_temps)
```

### 7. 單一職責原則
函式盡量單一職責，超過 50 行考慮拆分。一個函式做一件事、名字說得清楚。

---

## 【類別 3】專案架構與範圍守則

### 8. 遵循目錄結構
嚴格遵循 `06_TechSetup.md` 定義的目錄結構，不隨意新增最上層資料夾。新增檔案前先想「這個歸屬哪一層」。

### 9. 遵守 Non-Goals
遵守 `01_PRD.md` 第 5 節列出的 Non-Goals：
- 不做即時通路庫存查詢
- 不做百萬酒款資料庫
- 不做風味斷言
- 不做使用者帳號、登入、雲端儲存
- 不做行動裝置原生 App

### 10. 新功能想法先進 Backlog
新功能靈感先寫進 `04_Backlog.md` 的 P2 區，不當場實作。保護 7 天主線不被打亂。

---

## 【類別 4】安全性

### 11. 禁止硬編碼機敏資訊
絕對不能把 API Key、密鑰、Token 寫進程式碼或 commit。一律用 `.env` + `python-dotenv` 讀取。

### 12. `.env` 必須加入 `.gitignore`
提供 `.env.example` 讓其他人知道要設哪些變數，`.env` 本體不進版本控制。

---

## 【類別 5】外部 API 呼叫規範

### 13. Open-Meteo 基準線資料必須快取
30 年基準線氣候資料**首次抓取後必須存本機**（JSON 或 pickle），不能每次啟動都重打 API。快取路徑：`data/cache/`。

### 14. 外部 API 呼叫必須有錯誤處理
所有外部 API 呼叫必須有 `try/except` 錯誤處理與明確錯誤訊息。網路失敗、API 壞掉不能讓整個 app 掛掉。

### 15. 禁止編造氣候資料
API 呼叫失敗時要明確告知使用者「資料暫時無法取得」，**不能用推測值或亂數填補**。這是產品可信度的底線。

---

## 【類別 6】AI 生成內容規範（產品層）

### 16. 風味推測用保留措辭
風味推測一律使用「可能偏向」「傾向於」「相對可能」等保留措辭，**禁止用「絕對是」「一定會」「必定」等斷言句**。對應 PRD 定位：可解釋的脈絡解讀層，不是預言家。

### 17. 每段推測必須附引用
每段風味推測必須附引用來源（引用的知識庫片段或氣候資料出處）。使用者要能追溯「這個推測從哪裡來」。

### 18. 錯誤訊息要分兩層
- **給使用者看的訊息**：白話、有溫度（例：「這張酒標我讀不太清楚，可以手動輸入嗎？」）
- **給開發者看的 log**：詳細技術資訊（stack trace、API 回應）

使用者不需要看到 stack trace。

---

## 【類別 7】成本控制

### 19. 開發階段用小樣本測試
開發階段先跑 1 個產區、10 個知識片段，確認邏輯對再跑全量。避免調 bug 時每次都燒 OpenAI 費用。

---

## 【類別 8】版本控制與文件同步

### 20. Commit 訊息前綴規範
每完成一個子任務就 commit，訊息用以下前綴：
- `feat:` 新功能
- `fix:` 修 bug
- `docs:` 文件
- `test:` 測試
- `refactor:` 重構
- `chore:` 雜項（環境設定、依賴更新）

範例：`feat: 加入 Open-Meteo API 呼叫函式`

### 21. Backlog 進度同步
每完成一個 P0 任務要更新 `04_Backlog.md` 對應項目的勾選狀態，讓進度可視化。

### 22. 文件與程式碼一致性
程式碼與 `docs/` 出現落差時，優先修正到一致（例：改了目錄結構就同步更新 `06_TechSetup.md`）。文件跟現況脫節就失去參考價值。

---

## 【類別 9】AI 輔助開發自律

### 23. AI 產生的程式碼必須親自 review
AI 產生的程式碼必須逐行看過並理解，禁止整段複製貼上。面試被問「這段為什麼這樣寫」時要答得出來，否則履歷加分變扣分。

### 24. 不確定就問，不要瞎猜
遇到不確定的技術選擇、需求解讀時**先問使用者**，不要瞎猜實作。少走冤枉路。

### 25. 第三方套件使用前驗證
AI 建議的第三方套件在使用前要驗證：
- 近期有維護（一年內有 commit）
- 下載量合理（週下載量 > 1000）
- 不是釣魚套件（套件名稱與描述吻合）

---

## 【類別 10】使用者體驗與介面

### 26. 手機瀏覽器優先
Persona 小雅的主要使用情境是「站在超市貨架前用手機拍酒標」，Streamlit 元件設計必須考慮手機瀏覽器：

- **拍照優先用 `st.camera_input()`**，會自動叫出手機相機——已覆寫規範 #26，見 T-18 實作
  備註：`st.camera_input()` 需要瀏覽器安全情境（HTTPS 或 localhost）才能取得相機權限，
  手機連區網 HTTP（條款 31 Phase 2 測試情境）會被瀏覽器擋掉，實測卡在權限請求畫面永遠
  拿不到授權，已移除、改用 `st.file_uploader()`（手機瀏覽器原生檔案選擇器本身就有拍照
  選項，拍照能力沒有消失）。等 Phase 3 部署到 Streamlit Community Cloud（原生 HTTPS）
  後可重新評估要不要加回來
- **避免寬表格**（手機會橫向捲動很難用），改用垂直堆疊卡片
- **按鈕之間留足夠間距**（手指點擊比滑鼠不精準）
- **圖表用 Streamlit 內建或 Plotly**，會自動響應式縮放
- **重要資訊放頁面上方**（手機螢幕小，避免使用者一直捲）

### 27. 上傳圖片驗證
上傳圖片要驗證格式（JPG / JPEG / PNG，本階段排除 HEIC——已覆寫規範 #27，見 T-10 實作，
`docs/03_UserStory.md` US-1.1 同步標註）與大小（≤ 5MB），不符合要明確提示。

### 28. 日期格式統一
所有日期一律用 `YYYY-MM-DD` 格式，避免各國日期格式混淆。

### 29. 時區處理明確
時區處理必須明確標註。Open-Meteo API 預設 UTC，若要顯示給使用者需轉換為當地時區並註明。

### 30. 端到端優先，優化其次
完成 Day 5（Agent 主流程）後，**先確保端到端能跑，再回頭優化**。呼應 `05_Roadmap.md` 的核心原則：寧可交出「完整但簡單」，也不要「複雜但半成品」。

---

## 【類別 11】部署與展示

### 31. 三階段部署策略
| 階段 | 方式 | 使用時機 |
|---|---|---|
| **本機開發** | `streamlit run app.py`（localhost:8501） | Day 1–6 快速迭代 |
| **手機測試** | `streamlit run app.py --server.address=0.0.0.0` + 區網 IP | Day 6 驗證手機體驗 |
| **正式產出** | 部署到 Streamlit Community Cloud，取得永久網址 | Day 7 之後，履歷投遞用 |

### 32. Streamlit Cloud 部署注意事項
部署到 Streamlit Community Cloud 時：
- OpenAI API Key 存在 Streamlit Cloud 的 Secrets 設定，不進 GitHub
- `requirements.txt` 版本要固定（例：`openai>=1.50.0`），避免部署時裝到破壞相容性的新版
- 部署後測試一輪完整流程，確認雲端環境跟本機行為一致

---

## 【類別 12】內容撰寫品質

### 33. 面向使用者的文字輸出需符合人性化寫作規範
所有面向使用者的文字內容——包含但不限於知識庫片段（`data/knowledge/`）、README、docs 文件、風味推測報告生成 prompt（對應 T-13）、錯誤訊息文案——撰寫或修改時一律依照 `.claude/humanizer-zh.md` 的規範去除 AI 寫作痕跡（誇大意義、宣傳性語言、模糊歸因、三段式列舉、破折號濫用等），確保讀起來自然、有觀點、不像罐頭式 AI 輸出。

---

## 【類別 13】知識庫結構與檢索

### 34. 知識庫檔案位置與命名
知識庫採**兩層混合架構**，兩層各司其職、禁止混放在同一資料夾：

```
data/knowledge/
├── climate_rules/                  # 氣候規則層：氣候異常 → 風味推論的通用原理
│   └── warm_dry.md, cool_wet.md, heatwave.md ...
└── regions/                        # 產區百科層：品種、風土、產區對比
    └── bordeaux/
        ├── 01_climate.md
        ├── 02_grape_cabernet_sauvignon.md
        ├── 04_terroir.md
        └── 05_comparison.md
```

- Climate rules：`data/knowledge/climate_rules/{scenario}.md`，`scenario` 用 snake_case 英文
- Region：`data/knowledge/regions/{region_snake_case}/{seq}_{topic}.md`，`seq` 為兩位數序號
- 資料夾名要與檔案內 `region_canonical` 欄位對得起來（例：`bordeaux/` ↔ `"Bordeaux"`）
- 禁止使用中文檔名或中文資料夾名

### 35. YAML frontmatter 為必要格式
每個知識檔開頭都必須有 YAML frontmatter，`schema_version` 統一為 `"0.3"`。兩層欄位不同：

- **regions 層**：`region_canonical`、`region_zh`、`region_aliases`、`country`、`hemisphere`、`latitude`、`longitude`、`climate_zone`、`climate_type`、`topic`、`grape_focus`、`main_grapes`、`key_facts`
- **climate_rules 層**：`rule_type`、`condition`（temperature／precipitation／magnitude／duration）、`applies_to`（grape_types／climate_zones）

兩層共通必備：`id`、`schema_version`、`tags`、`sources`、`confidence`、`last_updated`。

匯入 Chroma 時：frontmatter 解析為 metadata dict、body 解析為 document text。

### 36. 兩層檢索邏輯與汙染防範
- Chroma collection 內用 metadata 欄位 `layer: "climate_rule"` 或 `layer: "region"` 區分兩層
- 一般查詢從兩層各取 Top-K 混合餵給 LLM；產區相關查詢可用 `region_canonical` 精準過濾
- **氣候推論主線只檢索 `climate_rules` 層與 regions 層中 `topic: "climate"` 的片段**。`terroir`、`comparison` 這類與氣候無關的片段，僅在使用者明確問到風土或產區比較時才檢索

這條防範是必要的：若不過濾，報告會從「氣候證據推論」漂移成「產區介紹」，失去本專案相對於一般酒類 App 的差異點，也違背 `01_PRD.md` 的產品定位。

### 37. 知識庫內容撰寫規範
- 全文繁體中文（台灣用語），遵守條款 1、2
- 品種名、產區名首次出現用「英文（中文譯名）」並列，之後可只用英文（呼應條款 3）
- 每則都要附出處，`confidence` 分三級：
  - `high`：3 個以上權威來源交叉驗證
  - `medium`：1–2 個來源，或來源之間有差異
  - `low`：單一來源或屬估算值
- 交付前依條款 33 套用 `.claude/humanizer-zh.md` 檢查，去除 AI 寫作痕跡

### 38. Agent 回答語氣與引用
- 使用「可能傾向」「通常呈現」「多數情況下」等保留措辭，禁止斷言句（呼應條款 16）
- 每段推測都要對應到 climate_rules 或 regions 的知識庫引用（呼應條款 17）
- 若問題無法從知識庫回答，誠實告知「本系統知識庫未涵蓋」，**不得憑 LLM 常識瞎編**（呼應條款 15 禁止編造資料的精神）

---

## 附錄：開始新任務前的自檢清單

Claude Code 每次開始新任務前，先自問：

- [ ] 這個任務對應到哪個 User Story / Backlog ID？
- [ ] 是否有相關 PRD 條款需要遵守？
- [ ] 是否會違反 Non-Goals？
- [ ] 產出的程式碼是否符合類別 1、2 的語言與命名規範？
- [ ] 是否需要處理錯誤、加日誌、寫測試？
- [ ] 這個任務會產出面向使用者的文字內容嗎？若會，是否已依 `.claude/humanizer-zh.md` 撰寫（條款 33）？
- [ ] 有動到知識庫嗎？檔案位置、frontmatter 欄位、檢索過濾是否符合條款 34–36？
- [ ] 完成後要更新哪些文件？

若任一項不確定，先問使用者再動工。

---

**版本**：v1.0
**建立日期**：2026-08-15
**維護者**：專案負責人
