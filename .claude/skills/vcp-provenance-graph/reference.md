# vcp-provenance-graph：索引新舊與怎麼讀那張圖

## 索引比紀錄舊嗎（只看檔案時間，不重算 sha）
```powershell
$data = '<data_root>'
$built = (Get-Item "$data/indexes/provenance.sqlite3").LastWriteTime
Get-ChildItem "$data/runs", "$data/measure", "$data/artifacts", "$data/submit" -Recurse -File -ErrorAction SilentlyContinue |
  Where-Object LastWriteTime -gt $built | Select-Object -First 5 FullName
```
有輸出 → `sync`；沒輸出 → 直接畫。這只是便宜的預判。要保證最新（例如要交出去的版本）就直接 `sync`：它收進所有新增並印出新的 `hash`；不必先跑 `status`——一樣全掃，但只看不修。

## 形狀
| 形狀 | 是什麼 |
|---|---|
| 圓柱 | dataset 版本：名稱、samples hash 前 8 碼、樣本數 |
| 平行四邊形 | split plan 與它的子集 |
| 方框 | run；第二行是 provenance 等級（receipt > export > declared） |
| 六角 | fusion run 與配方 |
| 旗形 | judgement：預登記 id 與判決；指向它的箭頭標讀過的子集 |
| 跑道 | submission（畫在測試集的框裡） |
| 雙邊框 | 同一種 artifact kind 併成一個節點：`xN` 與非 VALID 的數量 |
| 粗箭頭 | 資料改版：diff id 與變更數 |
| 粗框 | 有 backup manifest 涵蓋 |

一個框 = 一個 dataset 名稱：它的版本、split、在它上面訓練的 run、在它的 measure 目錄裡做的 judgement。

## 顏色（節點取成員中最差的狀態）
- 紅 **BROKEN**：證據缺、壞、驗不過，或上游有 BROKEN。
- 琥珀 **STALE**：資料改版後，這個 run、讀數或衍生物要重做。
- 藍 **REVIEW**：未分類的變更可能影響它，要人判。
- 無色 VALID。沒給 `--head` 取相對每個現行 head 最差的；給了只看相對那個 head。

## overview 摺掉了什麼
| 被摺的 | 在圖上變成 |
|---|---|
| reading | run → judgement 箭頭上的子集 |
| access_receipt、export | run 的 `grade` |
| source_audit、materialized_cache | 不畫（dataset 仍在） |
| dataset_diff、sample | 粗箭頭上的 diff 與變更數 |
| backup | 粗框 |

被摺掉又不是 VALID 的，列在圖下「Not drawn, not VALID」與命令的 human 輸出列。`--detail full` 除 sample 外全畫（sample 級的問題用 `vcp provenance impact --sample`）。

## VERDICT 與失敗
| 看到 | 意思 | 下一步 |
|---|---|---|
| `status=WARN broken=N` | 範圍內有 N 個 BROKEN 實體；圖照樣寫出 | 讀 human 列的原因：證據壞修證據，索引舊了 `sync` |
| `oversize=true` | `.md` / `.mmd` 超過 Mermaid 預設 500 邊 / 50,000 字 | 改寫 `.html`，或用 `--dataset` / `--entity` 縮小 |
| `FAIL … provenance index … run vcp provenance rebuild` | 還沒建索引 | `rebuild`（先講要花的時間） |
| `FAIL mismatch: graph_gaps metadata; rebuild required` | 索引是 0.9.0 以前的 vcp 建的 | `rebuild` 一次；重建後舊版 vcp 也讀得動 |
| `FAIL exists:` | `--out` 是別人的檔 | 換路徑；不要刪對方的檔 |
| `FAIL out_in_data_root:` | 圖指到 data root 裡 | 寫到比賽 repo（例如 `reports/`） |
| `FAIL out_not_absolute:` | `--out` 解析不成絕對路徑（例如 Windows 長路徑前綴打錯） | 給一般的絕對路徑 |
| `FAIL not_a_head:` | `--head` 給了已經被改版取代的版本 | 用錯誤訊息列出的 head |
| `FAIL ambiguous:` | `--head` 的名稱對到多個版本 | 用完整 `dataset:<name>@<hash>` |

## 常見的紅色來源
- **比賽程式把自己格式的 `manifest.json` 寫進 `artifacts/<kind>/<id>/`**：vcp 的產物 schema 讀不懂，整個 kind 變紅。該改的是比賽程式（寫到 `artifacts/` 以外，或改用 `vcp.artifact.writer.ArtifactWriter`），不是索引。
- **`artifacts/<kind>/` 裡的半途目錄**（`.…tmp`、只有 `spec.json`）：確認沒有程序在寫，再由產生它的程式清理；vcp 自己留下的半途檔用 `vcp artifact clean`（先不加 `--apply` 看清單）。
- **`artifact verification failed: … extra=1`**：產物目錄被多放了檔。產物不可改寫：修正開新 id 加 `--supersedes`。
- **run 目錄沒有 `run.yaml`**：半途的 run；確認沒有訓練在寫，再決定補登記還是搬走。
