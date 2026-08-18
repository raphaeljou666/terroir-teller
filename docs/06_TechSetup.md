# TechSetup — 技術規劃、工具、模型、IDE 環境配置

> **白話**：這份是「動工前的準備清單」。所有要裝的東西、要辦的帳號、要看的文件、專案要長什麼結構，都在這裡。第一天照著這份把環境建好，之後就不用再回頭想。

---

## 1. 技術棧總覽

| 類別 | 選擇 | 白話為什麼選它 |
|---|---|---|
| **程式語言** | Python 3.11+ | AI 生態最完整，套件最多 |
| **主要 LLM** | OpenAI GPT-4o-mini | 便宜、function calling 好用、有 Vision |
| **Embedding 模型** | OpenAI text-embedding-3-small | 便宜、跟主模型同生態 |
| **向量資料庫** | Chroma（本機模式） | 一行程式碼裝好，不用另外開 server |
| **氣候資料來源** | Open-Meteo Historical API | **免費、不用 API Key**、資料回溯至 1940 年 |
| **前端介面** | Streamlit | 純 Python 寫 UI，一天內做完 |
| **HTTP 呼叫** | httpx 或 requests | requests 較好上手 |
| **環境變數管理** | python-dotenv | 業界標準 |
| **版本控制** | Git + GitHub | 履歷投遞時的作品呈現載體 |

---

## 2. 模型選擇說明

### 2.1 主 LLM：GPT-4o-mini

- **用途**：Agent 大腦（決定要呼叫哪個 tool）、風味推測報告生成、酒標 Vision 辨識
- **成本**：輸入約 $0.15 / 百萬 tokens、輸出約 $0.6 / 百萬 tokens、圖片約 $0.001 / 張
- **一週開發預估總費用**：< $2 美元
- **參考連結**：<https://platform.openai.com/docs/models/gpt-4o-mini>

### 2.2 Embedding：text-embedding-3-small

- **用途**：把知識片段跟查詢文字都轉成向量，做語意檢索
- **成本**：$0.02 / 百萬 tokens（極便宜）
- **維度**：1536 維
- **參考連結**：<https://platform.openai.com/docs/guides/embeddings>

### 2.3 為什麼不用 Claude 或 Ollama

- **Claude**：品質更好，但 function calling 生態略新，教學資源比 OpenAI 少。如果職缺是 Anthropic 相關可以改用 Claude
- **Ollama（本機）**：零成本但效果較弱、Vision 支援複雜、對新手 debug 難度較高

---

## 3. 主要套件清單（`requirements.txt`）

```txt
openai>=1.50.0
chromadb>=0.5.0
streamlit>=1.38.0
python-dotenv>=1.0.0
httpx>=0.27.0
pandas>=2.2.0
plotly>=5.24.0
pillow>=10.4.0
pyyaml>=6.0
pytest>=8.3.0
```

**白話說明**：
- `openai`：呼叫 GPT-4o-mini 與 embedding 的官方 SDK
- `chromadb`：本機向量資料庫
- `streamlit`：做網頁介面
- `python-dotenv`：從 `.env` 檔讀 API Key
- `httpx`：呼叫 Open-Meteo API
- `pandas`：處理氣候資料表格
- `plotly`：畫氣候距平圖表
- `pillow`：處理上傳圖片
- `pyyaml`：解析知識庫檔案的 YAML frontmatter
- `pytest`：跑離線測試

---

## 4. 專案目錄結構

```
terroir-teller/
├── .env                       # API Key（不進 Git）
├── .env.example               # 環境變數範本（進 Git）
├── .gitignore
├── README.md
├── requirements.txt
│
├── app.py                     # Streamlit 主頁面
├── agent.py                   # Agent orchestrator
│
├── src/
│   ├── __init__.py
│   ├── vision.py              # GPT-4o-mini Vision 酒標辨識
│   ├── climate.py             # Open-Meteo API + GDD/距平計算
│   ├── retrieval.py           # Chroma RAG 檢索
│   ├── tools.py               # Function calling tools 定義
│   └── report.py              # 風味推測報告生成 prompt
│
├── data/
│   ├── regions.json           # 產區座標對照表（由知識庫 frontmatter 生成）
│   ├── knowledge/             # 知識庫（兩層混合架構，Schema v0.3）
│   │   ├── climate_rules/     # 氣候規則層：氣候異常 → 風味推論通用原理
│   │   │   ├── warm_dry.md
│   │   │   ├── cool_wet.md
│   │   │   ├── heatwave.md
│   │   │   └── ...            # 共 10 則
│   │   └── regions/           # 產區百科層：20 個產區 × 4–5 則
│   │       ├── bordeaux/
│   │       │   ├── 01_climate.md
│   │       │   ├── 02_grape_cabernet_sauvignon.md
│   │       │   ├── 03_grape_merlot.md
│   │       │   ├── 04_terroir.md
│   │       │   └── 05_comparison.md
│   │       └── ...            # 共 88 則
│   └── cache/                 # 30 年基準線氣候快取
│       └── bordeaux_baseline_1991_2020.json
│
├── tests/
│   ├── test_climate.py        # 氣候模組單元測試
│   └── test_validation.py     # 知名年份驗證測試
│
└── docs/                      # 這份文件包（可從本專案目錄搬進來）
    ├── 00_README.md
    ├── 01_PRD.md
    └── ...
```

---

## 5. 需要辦的帳號與 API Key

| 服務 | 用途 | 是否需付費 | 註冊網址 |
|---|---|---|---|
| **OpenAI Platform** | 主 LLM + Embedding + Vision | 需儲值（$5 起） | <https://platform.openai.com> |
| **Open-Meteo** | 氣候資料 | **完全免費，不需帳號、不需 Key** | <https://open-meteo.com> |
| **GitHub** | 程式碼託管 + 履歷呈現 | 免費 | <https://github.com> |

---

## 6. IDE 環境配置

### 6.1 建議 IDE：VS Code + 擴充套件

**必裝**：
- Python（Microsoft 官方）
- Pylance（型別檢查）
- GitLens（Git 視覺化）

**強烈建議（對應 JD 要求「熟悉 AI 輔助開發工具」）**：
- **Claude Code**（VS Code 擴充版）：直接在編輯器內對話寫程式，這是 JD 明確提到的工具，用它開發本專案有直接對應加分效果
- 或 **Cursor**（另一個 AI IDE）
- 或 **GitHub Copilot**

### 6.2 Python 虛擬環境

**推薦用 `venv`（Python 內建，最簡單）**：

```bash
# 建立虛擬環境
python -m venv .venv

# 啟用（macOS/Linux）
source .venv/bin/activate

# 啟用（Windows）
.venv\Scripts\activate

# 安裝套件
pip install -r requirements.txt
```

**進階選項**：`uv`（新一代快速套件管理器，2025 年逐漸普及）

### 6.3 `.env` 檔案設定

建立 `.env`（**不要 commit 進 Git**）：

```env
OPENAI_API_KEY=sk-proj-xxxxxxxxxxxxx
```

建立 `.env.example`（要 commit）：

```env
OPENAI_API_KEY=your-api-key-here
```

### 6.4 `.gitignore` 必備內容

```gitignore
.env
.venv/
__pycache__/
*.pyc
data/cache/
.streamlit/secrets.toml
```

---

## 7. 執行步驟（Day 1 環境驗證）

```bash
# 1. Clone 或建立專案
mkdir terroir-teller && cd terroir-teller
git init

# 2. 建虛擬環境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 3. 建立 requirements.txt（見第 3 節）
# 4. 安裝
pip install -r requirements.txt

# 5. 建立 .env 並填入 OpenAI API Key

# 6. 測試 OpenAI 連線
python -c "from openai import OpenAI; from dotenv import load_dotenv; load_dotenv(); print(OpenAI().models.list().data[0].id)"

# 7. 測試 Open-Meteo（不需 Key）
python -c "import httpx; r = httpx.get('https://archive-api.open-meteo.com/v1/archive?latitude=44.83&longitude=-0.58&start_date=2019-07-01&end_date=2019-07-07&daily=temperature_2m_max'); print(r.json())"

# 8. 測試 Streamlit
echo "import streamlit as st; st.write('hello')" > test.py
streamlit run test.py
```

**通過標準**：以上 8 步都沒錯誤訊息 → 環境設定完成

---

## 8. 主要參考文件（開發時常查）

### 核心文件

| 主題 | 網址 |
|---|---|
| **OpenAI Function Calling** | <https://platform.openai.com/docs/guides/function-calling> |
| **OpenAI Vision** | <https://platform.openai.com/docs/guides/vision> |
| **OpenAI Embeddings** | <https://platform.openai.com/docs/guides/embeddings> |
| **Chroma 教學** | <https://docs.trychroma.com/getting-started> |
| **Open-Meteo Historical API** | <https://open-meteo.com/en/docs/historical-weather-api> |
| **Streamlit Docs** | <https://docs.streamlit.io> |
| **Streamlit 拍照元件** | <https://docs.streamlit.io/develop/api-reference/widgets/st.camera_input> |

### 領域知識（釀酒氣候學）

| 主題 | 網址 | 用途 |
|---|---|---|
| Growing Degree Days（GDD）介紹 | <https://en.wikipedia.org/wiki/Growing_degree-day> | 建知識庫時參考 |
| Winkler Index（葡萄產區氣候分級） | <https://en.wikipedia.org/wiki/Winkler_scale> | 產區氣候分類 |
| Wine Folly 年份指南 | <https://winefolly.com/deep-dive/vintage-charts/> | 對照公開評論做驗證 |
| Vinous / Wine Advocate 年份報告 | 各知名酒評家網站 | 驗證環節用 |

---

## 9. 開發流程建議

### 9.1 Git commit 節奏

**每完成一個小任務就 commit**，訊息格式建議：

```
feat: 加入 Open-Meteo API 呼叫函式
fix: 修正南半球生長季月份判斷
docs: 補完 knowledge/warm_dry.md
test: 加入 2003 波爾多驗證案例
```

### 9.2 用 Claude Code 開發的建議提示語

> "根據 `05_Roadmap.md` Day 3 的計畫，我要實作 `src/climate.py`。輸入是產區座標與年份，輸出要包含每日氣候與 GDD 值。請先產出函式簽名與 docstring 讓我確認。"

> "這個函式跑起來報 KeyError，我把錯誤訊息與程式碼貼給你，幫我找出問題"

---

## 10. 費用預估總結

| 項目 | 一週開發用量 | 費用 |
|---|---|---|
| GPT-4o-mini 對話 | ~500 次呼叫 | ~$0.5 |
| GPT-4o-mini Vision | ~50 張圖 | ~$0.1 |
| Embedding | ~5 萬 tokens | <$0.01 |
| Open-Meteo | 無限次 | $0 |
| **總計** | | **< $1 美元** |

**建議 OpenAI 儲值**：$5 美元（可用很久）

---

## 11. 常見踩雷提醒

| 雷點 | 預防 |
|---|---|
| 把 `.env` commit 進 Git | Day 1 就先寫好 `.gitignore` |
| 每次跑都重打 Open-Meteo | Day 2 就實作快取到本機 |
| Chroma 每次重跑都重灌資料 | 用 `persist_directory` 參數 |
| Streamlit rerun 導致重複呼叫 API | 用 `@st.cache_data` 或 `st.session_state` |
| Vision 對每張酒標都失敗 | Day 4 就要準備手動輸入 fallback |
| Agent 陷入無限 tool 呼叫迴圈 | 設定 `max_iterations` 上限 |
| 產區資料量太大導致啟動慢 | 只做 10–15 個代表產區 |

---

## 12. 完成後的自我檢核

Day 7 結束時，這些應該都能勾起來：

- [ ] `streamlit run app.py` 開起瀏覽器有完整體驗
- [ ] 拿 3 支不同酒款跑完，都能拿到報告
- [ ] GitHub Repo README 有截圖、方法論說明、限制聲明
- [ ] 有錄一段 2–3 分鐘 demo 影片（可上傳 YouTube 不公開）
- [ ] 履歷 project 段落已更新，用 JD 對應語言描述
- [ ] 能用 3 句話說明「這個專案的技術亮點」
- [ ] 能用 1 句話說明「這個專案的限制」
