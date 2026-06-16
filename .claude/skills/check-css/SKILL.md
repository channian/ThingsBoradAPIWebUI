# check-css

掃描 `static/index.html` 中使用的所有 CSS class，與 `static/css/v3.css` 中定義的 class 交叉比對。

輸出：
1. **缺少定義**：HTML 中用到但 v3.css 裡沒有定義的 class（排除 Vue 動態 class）
2. **未使用**：v3.css 中定義但 HTML 裡沒有引用的 class
3. **摘要**：總共幾個 class、幾個缺少、幾個未使用

如果有缺少的 class，檢查是否在 `static/css/style.css`（舊版）中有定義，標注建議遷移到 v3.css。
