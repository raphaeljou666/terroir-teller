# TerroirTeller（風土說書人）

> **一句話介紹**：拍下酒標，用該年份該產區的真實氣候資料，推測這瓶酒可能的風味走向，並解釋為什麼。
>
> **專案定位**：一週內完成的個人 side project，同時作為履歷作品，對應 104 上「Agentic AI × RAG × 系統整合」職缺方向。

---

## 為什麼叫 TerroirTeller

**Terroir（風土）** 是酒界核心術語，指一款酒的產地自然條件總和——氣候、土壤、地形。同一支酒不同年份為什麼喝起來不一樣？很大一部分就是 terroir 中「氣候」這個因子在作祟。

**Teller** 就是「說書人」，這個系統的角色不是評分，而是**把氣候資料翻譯成看得懂的風味故事**。

---

## 快速導覽（3 分鐘理解全貌）

**要解決的痛點**：市面上很多酒沒有中文品飲筆記，或者背標只寫行銷話術，看不出「為什麼這年份跟去年不一樣」。

**核心解法**：
1. 拍酒標 → GPT-4o-mini Vision 讀出產區/年份/品種
2. Agent 呼叫氣候 API 抓該年份該產區的真實氣候
3. 計算氣候距平（今年比 30 年平均暖多少、雨多少）
4. RAG 檢索釀酒氣候學知識，生成風味推測
5. 驗證環節：拿知名年份對比公開評論，確認方法論有效

**主要技術棧**：Python + OpenAI API（GPT-4o-mini + Vision + Embedding）+ Chroma 向量資料庫 + Open-Meteo 氣候 API + Streamlit 介面

---

## 影片 DEMO

[▶ 觀看操作影片](https://drive.google.com/file/d/1qaAS4amuOI9X7FEuMaLp3TwhdUllnzYa/view?usp=sharing)

影片走過核心流程：上傳酒標 → 辨識結果 → 風味推測報告 → 展開氣候比對表。

報告分三層——風味推測直接可見，氣候比對表與氣候摘要收在「影響原因」，限制說明與資料來源收在「說明與資料來源」。使用者先看到結論，想追根據再往下點。

---

## 快速開始

```bash
# 1. 複製專案
git clone https://github.com/raphaeljou666/terroir-teller.git
cd terroir-teller

# 2. 安裝套件
pip install -r requirements.txt

# 3. 設定環境變數
cp .env.example .env
# 編輯 .env，填入你的 OPENAI_API_KEY

# 4. 啟動
streamlit run app.py
```

---

## 技術架構

```
TerroirTeller
│
├── 介面層 · app.py ─────────────── Streamlit
│                                   上傳驗證（JPG/PNG、≤5MB）
│                                   報告三層分區顯示
│
├── 決策層 · agent.py ───────────── OpenAI Function Calling
│   │                               tool-calling 迴圈（上限 6 輪）
│   │                               只蒐集資料，不產生任何文字
│   │
│   └── 工具層 · src/tools.py ───── 5 支 tool 的 schema 與 dispatch
│       │                           錯誤一律轉 dict 回傳，不外拋
│       │
│       ├── src/vision.py ───────── GPT-4o-mini Vision 讀酒標
│       │                           產區 / 年份 / 品種
│       │
│       ├── src/climate.py ──────── Open-Meteo（ERA5）歷史氣候
│       │                           GDD 與距平計算
│       │                           30 年基準線本機快取 + 版本控管
│       │
│       └── src/retrieval.py ────── Chroma 向量檢索
│                                   text-embedding-3-small
│                                   兩層知識庫（98 則）
│                                   metadata 扁平化 → where 硬過濾
│
└── 生成層 · src/report.py ─────── 第二次 LLM 呼叫，專責寫報告
                                    Structured Outputs 綁定引用 id
                                    保留措辭 + 拒答清單
```

**三個關鍵設計**

- **兩階段 LLM**：`agent.py` 只蒐集資料、`report.py` 才寫報告，分兩次呼叫。tool-routing 要準跟報告要好讀是兩種不同的 prompt 目標，混在一起會互相稀釋。
- **硬規則用程式擋**：產區不在支援清單內時迴圈直接中止，報告生成根本不會被呼叫，不是靠 prompt 拜託模型別回答。
- **引用不可能造假**：Structured Outputs 把 `cited_ids` 用 `enum` 限定成「這次實際檢索到的 chunk id」，模型在 schema 層就編不出不存在的來源。

---

## 氣候資料怎麼算

氣候資料全部來自 [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api)，底層是 ECMWF 的 ERA5 重分析資料集。免費、不需 API Key，每日彙總以 UTC 曆日計算。

生長季怎麼取決於半球：北半球是當年 4 月到 10 月，南半球跨年，從前一年 10 月到當年 4 月。所以 Mendoza 的 2019 年份指的是 2018-10 到 2019-04 這一季。

資料本身的空間解析度限制、基準線的偏差、代理指標的形狀問題，整理在下面「方法論與限制」一節。

```bash
# 查單一年份的生長季氣候
python -m src.climate --region "Bordeaux" --year 2019

# 抓 30 年基準線（第一次會打 API，之後讀 data/cache/）
python -m src.climate --region "Mendoza" --baseline

# demo 前一次預熱 20 個產區的基準線快取
python -m src.climate --warm-cache-all
```

一次基準線請求要抓 30 年的每日資料，權重不低。Open-Meteo 免費方案的每小時額度大約只夠 13–14 個產區，所以 `--warm-cache-all` 要分兩次跑，中間隔一小時；碰到額度上限時它會停手並把剩下的產區標成「未抓取」，不會傻等。抓好的快取放在 `data/cache/`（約 12MB，已排除在版本控制外），之後查詢不再打 API。

---

## 方法論與限制

- **氣候只是風味的其中一個因子**。採收時機、發酵溫度、橡木桶選擇，系統完全看不到。報告講的是「這年氣候傾向讓哪種風味更可能出現」，不是這支酒實際喝起來的樣子。
- **氣候資料是 25 公里網格的平均值**，不是某座葡萄園。地形破碎的產區（Barolo、Central Otago）抓到的降雨可能是實測站的兩倍。系統算的是距平（今年 vs 同一網格的 30 年平均），偏差在相減時大半抵消，所以「比平均濕兩成」可信，但絕對降雨量不該當實測值讀。
- **基準線本身已被暖化墊高**。用的是 WMO 標準的 1991–2020，這 30 年比更早期基準暖，所以算出來的暖異常偏保守。
- **採收前 30 天降雨是估算值**，因為沒有真實採收日資料，只能用「生長季結束日往前推 30 天」推。偏暖早熟的年份會抓錯窗口，抓到採收後才下的秋雨。目前的處理是降低它在報告裡的權重並標註「參考價值有限」，不是假裝它準。
- **只涵蓋 20 個產區**（清單見 `data/regions.json`）。實測拍到的酒標很多不在清單內（Beaujolais、Chinon、Asti 都試過），這時系統會誠實說「不涵蓋此產區」，不硬掰一個知識庫沒有的答案。

---

## 文件索引

| 檔案 | 內容 | 適合閱讀時機 |
|---|---|---|
| [`docs/01_PRD.md`](docs/01_PRD.md) | 產品需求文件 | 想理解「這個產品要解決什麼」 |
| [`docs/02_Persona.md`](docs/02_Persona.md) | 目標使用者輪廓 | 想理解「這個產品給誰用」 |
| [`docs/03_UserStory.md`](docs/03_UserStory.md) | 使用者故事 | 想理解「使用者會怎麼用」 |
| [`docs/04_Backlog.md`](docs/04_Backlog.md) | 開發待辦清單 | 開始寫程式前的任務拆解，含技術決策與踩雷紀錄 |
| [`docs/05_Roadmap.md`](docs/05_Roadmap.md) | 7 天執行時程 | 每天知道自己要做什麼 |
| [`docs/06_TechSetup.md`](docs/06_TechSetup.md) | 技術工具、模型、IDE 環境配置 | 動工前的環境準備 |
| [`docs/07_ValidationReport.md`](docs/07_ValidationReport.md) | 知名年份驗證報告 | 想看「命中率 40% → 80% 的完整過程」 |
| [`docs/SYSTEM_DIAGRAM.html`](docs/SYSTEM_DIAGRAM.html) | 系統架構圖 | 想理解「系統怎麼串起來」 |
| [`CLAUDE.md`](CLAUDE.md) | Claude Code 開發規範 | AI 協作開發時遵守的規則 |

## 使用建議

1. **先讀** [`docs/01_PRD.md`](docs/01_PRD.md) 理解產品輪廓
2. **再讀** [`docs/05_Roadmap.md`](docs/05_Roadmap.md) 掌握整體節奏
3. **開工前讀** [`docs/06_TechSetup.md`](docs/06_TechSetup.md) 準備環境
4. **每天開工看** [`docs/04_Backlog.md`](docs/04_Backlog.md) 挑當日任務

---

## 專案結構

```
terroir-teller/
├── README.md
├── CLAUDE.md
├── requirements.txt
├── .env.example
├── docs/            # 專案規劃文件
├── src/             # 主程式碼
├── data/            # 產區資料、知識庫、快取
├── tests/           # 測試
└── notebooks/       # 實驗用 Jupyter notebook
```

---

**版本**：v1.0
**建立日期**：2026-08-15
