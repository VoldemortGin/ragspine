# AIA Group 2026 Interim Results：pdfspine 页面与图表盘点

## 范围与方法

本盘点只读取 `data/samples/aia-group-2026-interim-results-presentation.pdf`。文件 SHA-256 为 `df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e`，共 71 页。唯一 PDF 入口是本项目锁定的 `pdfspine 0.10.0`；未使用其他 PDF parser，也未调用模型或远端服务。

逐页索引来自 `get_text("dict")`、`get_cdrawings()`、`get_image_info()`、`get_images(full=True)`、`get_xobjects()`、`get_fonts(full=True)`、`get_bboxlog()` 和 `get_svg_image()`。第 10、19、25 页另通过 `get_pixmap(dpi=150)` 做人工视觉复核。全部页面均为 `960 × 540` PDF point、rotation 0、cropbox 等于 mediabox。

坐标统一写成 `[x0, y0, x1, y1]`，原点在页面左上角，单位为 PDF point。pdfspine 的文字 bbox 已是左上坐标；`get_cdrawings()` 的底左坐标在记录前按页面高度显式换算。标题是顶部最大字号文字的观察值，分隔页使用首个可见文本。它们是检索线索，不是语义 gold。

表中的“图像”是 `get_image_info()` 报告的 placement 数；“向量”是 drawing/object item 数。复杂度是盘点启发式：存在 image placement、至少 500 个 drawing items、至少 150 个文字 spans、至少 20 个 compound drawings 或至少 500 个 SVG clipPath 时记为高；中/低只用于排定人工复核顺序，不能证明 SVG 完整性或图表语义正确。

## 首批候选结论

首条真实图表链建议使用第 25 页右侧的 Interim Dividend 双柱图，候选 bbox 为 `[731.5, 145.0, 944.2, 385.0]`。它在同一区域内提供标题、单位、两个时期、两个精确值及两根矩形柱，没有图外 legend：

| 观察 | pdfspine 可见文本/图元 | bbox |
| --- | --- | --- |
| 标题 | `Interim Dividend per share` | `[768.26, 150.1088, 909.7597, 162.4184]` |
| 单位 | `(HK cents)` | `[809.9, 163.3088, 865.0338, 175.6184]` |
| 时期 | `1H25` / `1H26` | `[777.86, 365.39, 806.48, 378.77]` / `[871.63, 365.39, 900.25, 378.77]` |
| 数值 | `49.00` / `53.90` | `[777.17, 206.15, 807.326, 219.53]` / `[869.86, 192.97, 900.016, 206.35]` |
| 变化 | `+10%` | `[819.72, 209.7738, 855.9713, 225.4284]` |
| 两柱 | drawing 34 / 35 | `[775.37, 224.2, 808.852, 358.01]` / `[869.12, 210.82, 902.602, 358.01]` |
| 基线 | drawing 36 | `[745.23, 358.01, 932.73, 358.01]` |

“49.00 属于 1H25、53.90 属于 1H26”以及文字与柱的配对来自人工查看 pdfspine 渲染页后的 **provisional** 关系候选；尚无独立资格凭据，不能直接标为 verified。`+10%` 的圆形/箭头是 compound geometry，首链应保留 native SVG，不按 drawing observations 重画。

其他候选及阻断证据：

- 第 10 页 donut：bbox `[690, 140, 945, 365]`。可见 `Agency VONB 1H26`、`57%`、`35%`、`8%`、`<1% Others` 和类别文字。类别到扇区/百分比的关系仍是 provisional；外置标签需要 occurrence 级证据。
- 第 10 页左侧 VONB 双柱：bbox `[30, 95, 305, 345]`。可见单位 `VONB ($m)`、`1H25`、`1H26`、`937`、`+20%`，但没有 1H25 的显式数值；不得用 `937 / 1.20` 推导并冒充源值。中间 Agency VONB 图同样只有 `818` 和 `+24%`，缺基期显式值。
- 第 19 页 waterfall：bbox `[35, 95, 655, 480]`。文字层提供 `$b`、`79.7`、`+6.6`、`+1.0`、`(0.3)`、`87.1`、`(1.9)`、`(1.7)`、`83.4`、`$3.6b` 及类别；但 pdfspine 在图内报告 6 个 image placements。它是 hybrid raster/vector 压力用例，不适合首条 native-vector 成功验收。
- 第 25 页长期 dividend 图：bbox `[350, 135, 725, 395]`。可见标题、legend、`33.00` 和 `193.08`；渲染中可见的 2011–2025 年份没有出现在 pdfspine 文字 spans 中，因此这些年份不能当作已抽取字段。整页还有 1,518 个 drawing items 和 36 个 compound drawings。

`get_image_info()` 在第 1、5、19、37、40、41、42、50、52、69 页报告 image placements；`get_images(full=True)` 只在第 1、5、37 页返回资源。两个公开观察面的差异本身就是完整性风险，不能用“资源列表为空”推断页面无 raster。

## 71 页基本索引

| 页 | 可观察标题/文本 | 尺寸 pt | 文字 spans | 向量 drawings/items | 图像 | 复杂度证据 |
| ---: | --- | --- | ---: | ---: | ---: | --- |
| 1 | 2026 \| INTERIM RESULTS \| PRESENTATION \| 20 AUGUST 2026 | 960×540 | 4 | 9/977 | 1 | 高：raster 1, items 977 |
| 2 | DISCLAIMER | 960×540 | 33 | 12/165 | 0 | 高：clip 1697 |
| 3 | AGENDA | 960×540 | 7 | 4/310 | 0 | 低：compound 2, clip 21 |
| 4 | Strong 1H 2026 Results, Confident in the Outlook | 960×540 | 36 | 10/191 | 0 | 低：compound 5, clip 78 |
| 5 | AIA is Uniquely Positioned to Capture Growth Across Asia | 960×540 | 73 | 27/324 | 2 | 高：raster 2 |
| 6 | Taking Agency Performance to the Next Level | 960×540 | 59 | 22/270 | 0 | 中：compound 17, clip 102 |
| 7 | Quality Partnership Distribution, VONB Up 18% to $965m | 960×540 | 49 | 24/225 | 0 | 中：compound 11, clip 87 |
| 8 | Step-Up in OPAT Growth; Operating ROE Up 200 bps to 17.5% | 960×540 | 37 | 15/246 | 0 | 中：compound 12, clip 74 |
| 9 | BUSINESS HIGHLIGHTS | 960×540 | 1 | 5/189 | 0 | 低：compound 3, clip 8 |
| 10 | AIA China: VONB Up 20% to $937m; Best-in-Class Agency | 960×540 | 47 | 25/243 | 0 | 中：compound 14, clip 104 |
| 11 | AIA Hong Kong: Record 1H VONB of $1.2b | 960×540 | 62 | 30/248 | 0 | 中：compound 13, clip 115 |
| 12 | AIA Hong Kong: Strong Fundamentals Across Customer Segments | 960×540 | 66 | 26/246 | 0 | 中：compound 12, clip 115 |
| 13 | ASEAN: 32% of VONB; Strengthening Growth Momentum in 2Q | 960×540 | 79 | 29/255 | 0 | 中：compound 16, clip 129 |
| 14 | India: Excellent VONB Growth through Tata AIA Life | 960×540 | 49 | 18/224 | 0 | 中：compound 11, clip 95 |
| 15 | Sustained Growth Driving Strong Financial Track Record | 960×540 | 31 | 111/3968 | 0 | 高：items 3968, compound 74 |
| 16 | FINANCIAL PERFORMANCE | 960×540 | 1 | 5/189 | 0 | 低：compound 3, clip 7 |
| 17 | Strong Performance Across Key Financial Metrics | 960×540 | 37 | 13/243 | 0 | 中：compound 12, clip 51 |
| 18 | Attractive New Business Profile | 960×540 | 50 | 23/233 | 0 | 中：compound 11, clip 88 |
| 19 | EV Equity Up 6% Per Share over 1H26 | 960×540 | 63 | 14/216 | 6 | 高：raster 6 |
| 20 | High-Quality, Prudent EV; Small Sensitivity to Market Movements | 960×540 | 48 | 31/252 | 0 | 中：compound 15, clip 102 |
| 21 | Compounding New Business to Drive Future Earnings Growth | 960×540 | 55 | 26/198 | 0 | 中：compound 4, clip 105 |
| 22 | High-Quality Earnings Growth and Record Operating ROE | 960×540 | 42 | 15/203 | 0 | 低：compound 6, clip 73 |
| 23 | Quality In-Force Book Driving Attractive Cash Generation | 960×540 | 44 | 46/296 | 0 | 中：compound 17, clip 120 |
| 24 | Robust Capital Management Driving Shareholder Value | 960×540 | 33 | 10/204 | 0 | 中：compound 8, clip 49 |
| 25 | Interim Dividend Per Share Up 10% | 960×540 | 31 | 48/1518 | 0 | 高：items 1518, compound 36 |
| 26 | Strong 1H 2026 Results, Confident in the Outlook | 960×540 | 19 | 4/171 | 0 | 低：compound 3, clip 32 |
| 27 | DEFINITIONS AND NOTES (1 of 3) | 960×540 | 52 | 2/155 | 0 | 高：clip 1821 |
| 28 | DEFINITIONS AND NOTES (2 of 3) | 960×540 | 59 | 2/155 | 0 | 高：clip 1300 |
| 29 | DEFINITIONS AND NOTES (3 of 3) | 960×540 | 56 | 2/155 | 0 | 高：clip 1020 |
| 30 | APPENDIX | 960×540 | 1 | 5/189 | 0 | 低：compound 3, clip 7 |
| 31 | INDEX | 960×540 | 6 | 5/189 | 0 | 低：compound 3, clip 24 |
| 32 | Geographical Market Performance | 960×540 | 183 | 254/407 | 0 | 高：spans 183 |
| 33 | AIA Thailand: Clear Market Leader; VONB Up 13% in 2Q26 | 960×540 | 62 | 26/238 | 0 | 中：compound 13, clip 105 |
| 34 | AIA Singapore: VONB Up 19% in 2Q, Strengthening Wealth Solutions | 960×540 | 56 | 26/241 | 0 | 中：compound 14, clip 94 |
| 35 | AIA Malaysia: VONB Up 10%, Growth in Agency and Partnerships | 960×540 | 49 | 22/233 | 0 | 中：compound 12, clip 87 |
| 36 | AIA’s Profitable Growth Strategy | 960×540 | 32 | 50/363 | 0 | 高：compound 36 |
| 37 | Excellent Progress in AIA’s Integrated Health Strategy | 960×540 | 44 | 16/217 | 5 | 高：raster 5 |
| 38 | INDEX | 960×540 | 6 | 5/189 | 0 | 低：compound 3, clip 24 |
| 39 | VONB Up 14% Excluding Thailand | 960×540 | 35 | 16/198 | 0 | 中：compound 8, clip 63 |
| 40 | High-Quality Profitable New Business | 960×540 | 67 | 24/239 | 3 | 高：raster 3 |
| 41 | 1H26 ANW Movement | 960×540 | 53 | 3/156 | 5 | 高：raster 5 |
| 42 | 1H26 VIF Movement | 960×540 | 39 | 3/156 | 3 | 高：raster 3 |
| 43 | Risk Discount Rates and Risk Premium | 960×540 | 125 | 133/286 | 0 | 中：compound 1, clip 277 |
| 44 | Sensitivity Analysis: Embedded Value | 960×540 | 29 | 5/205 | 0 | 低：compound 2, clip 55 |
| 45 | Sensitivity Analysis: VONB | 960×540 | 25 | 5/201 | 0 | 低：compound 2, clip 50 |
| 46 | UFSG Up 10% Per Share | 960×540 | 38 | 21/190 | 0 | 低：compound 4, clip 67 |
| 47 | Net FSG Up 12% Per Share Driven by Strong In-Force Generation | 960×540 | 33 | 16/179 | 0 | 低：compound 3, clip 63 |
| 48 | INDEX | 960×540 | 6 | 5/189 | 0 | 低：compound 3, clip 24 |
| 49 | OPAT Up 13% Per Share | 960×540 | 37 | 16/196 | 0 | 中：compound 8, clip 66 |
| 50 | Operating Profit After Tax Up 13% Per Share | 960×540 | 49 | 13/196 | 2 | 高：raster 2 |
| 51 | Reconciliation of OPAT to Net Profit | 960×540 | 28 | 32/185 | 0 | 低：compound 1, clip 65 |
| 52 | Operating ROE of 17.5%; Comprehensive Equity of $97.8b | 960×540 | 60 | 14/180 | 5 | 高：raster 5 |
| 53 | $1.6b Net Investment Result from Non-Par and Surplus Assets | 960×540 | 63 | 15/218 | 0 | 中：compound 9, clip 116 |
| 54 | Comprehensive Equity of $97.8b; Confirms AIA’s Prudent EV | 960×540 | 44 | 21/213 | 0 | 中：compound 9, clip 69 |
| 55 | IFRS 17 Discount Rates and Illiquidity Premium | 960×540 | 95 | 104/257 | 0 | 中：compound 1, clip 200 |
| 56 | Other Sensitivity Analysis | 960×540 | 19 | 16/211 | 0 | 低：compound 7, clip 49 |
| 57 | INDEX | 960×540 | 6 | 5/189 | 0 | 低：compound 3, clip 24 |
| 58 | Total Invested Assets of $295.0b | 960×540 | 699 | 75/371 | 0 | 高：spans 699 |
| 59 | High-Quality, Diversified and Resilient Investment Portfolio | 960×540 | 83 | 88/335 | 0 | 中：compound 19, clip 191 |
| 60 | Fixed Income Portfolio | 960×540 | 29 | 50/271 | 0 | 中：compound 17, clip 88 |
| 61 | Total Bonds by Accounting Classification | 960×540 | 47 | 56/245 | 0 | 中：compound 9, clip 111 |
| 62 | Government and Government Agency Bond Portfolio | 960×540 | 71 | 60/327 | 0 | 高：compound 31 |
| 63 | Corporate Bond Portfolio by Rating | 960×540 | 73 | 92/304 | 0 | 中：compound 14, clip 176 |
| 64 | Corporate Bond Portfolio (Non-Par and Surplus Assets) | 960×540 | 79 | 100/284 | 0 | 中：compound 8, clip 218 |
| 65 | Structured Security Portfolio | 960×540 | 65 | 65/265 | 0 | 中：compound 12, clip 146 |
| 66 | AIA China: Prudent Investment Portfolio | 960×540 | 40 | 19/224 | 0 | 中：compound 13, clip 65 |
| 67 | Private Credit: Small Allocation, 2.8% of Non-Par and Surplus Assets | 960×540 | 54 | 53/220 | 0 | 中：compound 3, clip 118 |
| 68 | INDEX | 960×540 | 6 | 5/189 | 0 | 低：compound 3, clip 24 |
| 69 | Free Surplus up 29% before Shareholder Returns | 960×540 | 49 | 11/190 | 5 | 高：raster 5 |
| 70 | Shareholder Capital Ratio of 210% after $3.6b Shareholder Returns | 960×540 | 80 | 31/272 | 0 | 高：compound 21 |
| 71 | Disciplined Financial Leverage | 960×540 | 32 | 21/250 | 0 | 中：compound 17, clip 61 |

## 解释边界

本盘点记录的是 pdfspine 可观察结果与少量人工视觉核对，不是财务审计、图表 gold 或字段资格凭据。标题、单位、年份、数值和图元即使在同一区域，也不能仅凭文本相同或空间接近认定属于同一 series。任何 ChartIR、自然语言描述或 embedding 之前仍需同一 native SVG、同一 source revision、字段 occurrence 和独立资格流程的完整绑定。

机器可读的候选和 bbox 位于 `data/benchmarks/enterprise-pdf-rag/aia-2026-interim/manifest.json`。逐页原始计数、所选页 span/drawing 记录和 pdfspine 渲染图在被 Git 忽略的 `data/aia-inventory/`，只作为本地复核材料。
