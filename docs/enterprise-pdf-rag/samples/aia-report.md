# AIA Group 2026 Interim Results presentation sample

## Identity and provenance

- **Issuer:** AIA Group Limited
- **Document:** 2026 Interim Results Presentation
- **Presentation date:** 20 August 2026
- **Reporting period:** 1H26; the consolidated results cover the six months ended 30 June 2026
- **Entity scope:** AIA Group. Some slides cover its Chinese Mainland segment, but this is not an AIA China standalone report.
- **Official source page:** [AIA Results and Reports](https://www.aia.com/en/investor-relations/overview/results-presentations)
- **Official PDF:** [AIA Group 2026 Interim Results Analyst Presentation](https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2026/AIA%20Group%202026%20Interim%20Results%20Analyst%20Presentation%20Final.pdf)
- **Downloaded:** 2026-09-19T13:05:42Z
- **Local fixture:** `data/samples/aia-group-2026-interim-results-presentation.pdf`

The presentation was chosen over AIA's 2025 annual and interim presentations because it is a current, compact analyst deck with dense labelled charts across operating, embedded-value, IFRS, investment, and capital topics. It is suitable for testing presentation-exported PDF structure without treating a long-form annual report as the source format.

## File verification

- **Media:** PDF 1.7; magic bytes `%PDF-1.7`
- **Size:** 1,028,554 bytes (about 0.98 MiB)
- **SHA-256:** `df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e`
- **Pages:** 71
- **Page geometry:** sampled pages are 960 x 540 PDF points
- **Verification tool:** local `pdfspine` 0.10.0 opened the fixture and reported `Document.page_count == 71`; all visual previews below were rendered through `Page.get_pixmap(dpi=150)`.

The PDF is publicly downloadable from AIA Group's official investor-relations site. No open-content licence is asserted here. The disclaimer on PDF page 2 restricts copying, reproduction, redistribution, and publication, so the file is a local test fixture governed by AIA's stated terms and should remain excluded from source distribution.

## Visually inspected chart pages

Page numbers below are 1-based PDF page numbers. Bounding boxes are approximate navigation clues in `[x0, y0, x1, y1]` PDF points with a top-left origin. They are not ground-truth annotations; ingestion must preserve the detected page and bounding-box provenance.

| Page | Visual content confirmed | Approximate chart region(s) |
| ---: | --- | --- |
| 5 | Four-period Group VONB vertical bars, CAGR callout, and a VONB-margin value strip | `[665, 135, 935, 455]` |
| 10 | Two labelled two-period vertical-bar charts plus an Agency VONB product-mix donut (57%, 35%, 8%, and <1%) | left bars `[25, 120, 325, 345]`; middle bars `[345, 150, 635, 345]`; donut with labels `[690, 165, 915, 355]` |
| 18 | Two composition donuts and a two-bar capital-return comparison joined by a 4.0x annotation | donuts `[35, 155, 455, 360]`; bars `[575, 145, 890, 375]` |
| 19 | EV Equity movement waterfall with positive and negative bridge items, subtotal, shareholder-return bracket, and labelled endpoints | `[35, 105, 650, 475]` |
| 25 | Long time-series stacked dividend bars with a trend arrow, plus a separate two-period bar comparison | stacked series `[355, 135, 720, 390]`; comparison `[750, 150, 935, 390]` |
| 45 | Diverging horizontal sensitivity bars around a zero axis, with eleven category labels and explicit positive/negative percentages | `[110, 100, 860, 515]` |
| 60 | Two multi-segment donuts for fixed-income type and maturity, with external legends and percentages | `[20, 110, 470, 400]`; `[520, 110, 925, 400]` |

For the first real Figure-to-SVG pipeline check, page 10 is the lowest-ambiguity target: its bar and donut labels are explicit, the chart regions are visually separated, and the expected values can be checked without estimating bar heights. Pages 19, 25, and 45 are stronger follow-on stress cases for bridge semantics, stacked series plus trend annotation, and signed-axis interpretation.
