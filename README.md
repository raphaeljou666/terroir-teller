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

# 4. 啟動（Day 4+ 完成 app.py 後可用）
streamlit run app.py
```

---

## 文件索引

| 檔案 | 內容 | 適合閱讀時機 |
|---|---|---|
| [`docs/01_PRD.md`](docs/01_PRD.md) | 產品需求文件 | 想理解「這個產品要解決什麼」 |
| [`docs/02_Persona.md`](docs/02_Persona.md) | 目標使用者輪廓 | 想理解「這個產品給誰用」 |
| [`docs/03_UserStory.md`](docs/03_UserStory.md) | 使用者故事 | 想理解「使用者會怎麼用」 |
| [`docs/04_Backlog.md`](docs/04_Backlog.md) | 開發待辦清單 | 開始寫程式前的任務拆解 |
| [`docs/05_Roadmap.md`](docs/05_Roadmap.md) | 7 天執行時程 | 每天知道自己要做什麼 |
| [`docs/06_TechSetup.md`](docs/06_TechSetup.md) | 技術工具、模型、IDE 環境配置 | 動工前的環境準備 |
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
