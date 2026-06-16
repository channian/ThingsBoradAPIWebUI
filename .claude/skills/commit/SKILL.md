# commit

檢查 `git diff` 和 `git status`，根據變更內容：

1. 用 conventional commit 格式撰寫 commit message（feat/fix/refactor/chore）
2. 第一行簡短英文摘要（< 70 字元）
3. 如果變更複雜，加一行空行後寫中文補充說明
4. Stage 相關檔案（不要 `git add .`，逐一指定）
5. Commit（不要 push）
6. 回報 commit hash 和摘要
