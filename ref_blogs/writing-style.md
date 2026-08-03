# 寫作風格指南（tedmax100）

從 ref1~ref5 五篇鐵人賽日誌萃取。用途：讓 AI 或協作者產出的文字讀起來像本人寫的。

> **適用範圍**：一般部落格文章。
>
> **`weaver-demo/ironman-2026/` 底下的 `dayNN.md` 不適用這份**，那個目錄有自己的 `CLAUDE.md`，是 Day1–Day4 定稿後校準過的收緊版本，衝突時一律以它為準。已知衝突：破折號密度（那邊一篇最多一兩個）、粗體密度（那邊一節大概一個）、小結寫法（那邊明文「不要用力」、不要金句收尾）、禁止整齊排比、禁止「我得誠實講」這類宣告。
>
> 這份的 §8「引用與連結紀律」已經搬進那份 CLAUDE.md 的「引用與連結」一節，兩邊要一起維護。

---

## 0. 一句話總結

**正文是工整的教科書腔，blockquote 是真心話頻道。** 兩層並存才是這個人的聲音，缺一層就不像了。

---

## 1. 雙聲道結構（最重要的特徵）

### 正文層
- 第一人稱複數「我們」，偶爾「開發者/團隊」。
- 定義 → 分類 → 應用 → 對照的推進方式。
- 句子完整、少省略、少感嘆。
- 這一層幾乎不吐槽。

### Blockquote 層
用 `>` 包起來，承擔正文不能講的話。四種用途：

1. **開場金句／TL;DR**
   ```
   > # 站在未來，規劃現在
   > 就是可觀測性工程與系統性能工程的核心精神
   ```
   ```
   > 工欲善其事，必先利其器！
   > 但決定用什麼工具，用工具做什麼事情來解決什麼問題之前
   > 看見全貌，理解流程與依賴關係，是最為重要的。
   ```

2. **業界慘案**（「舉個現實案例」開頭很常見）
   ```
   > 舉個現實案例，團隊僅仰賴 log，但真的出問題時，找不到能定位問題的 log…
   > 此時團隊能做的事情只有**觀察**與**等待**。
   ```

3. **吐槽／反諷**
   ```
   > 此時，就更不用說引入 Prfoile了。災難ㄚ！！ 天崩地裂一開 掰。
   > 最怕的就是不復盤、不盤點跟償還技術債，一直疊床架屋的團隊。這種我只能說「很棒！」。
   ```

4. **文末彩蛋／預告**（放在 `## 小結` 之後）
   ```
   > 第一天跟最後一天總是特別好寫 :) 能寫得很長又完整。
   > 中間的 28 天就...我就會原形畢露了
   ```
   ```
   > 講這麼多，測量這些要幹麻？
   > 別急，後面會提大家朗朗上口的 **80/20 法則*。
   ```

**規則**：一篇至少 2 段 blockquote，開頭一段、結尾一段。中段每 2~3 個 H2 可插一段。

---

## 2. 語氣：自降姿態，不當權威

即使已經出書、翻譯過《可觀測性工程》，語氣依然是往下壓的：

- 「先說我不是 DDD 高手，只是有稍微研究一點點。」
- 「以上只是 [Wiki](...) 的內容翻譯成中文而已 XD」
- 「昨天好像沒怎提到 :(」
- 「能不能完賽我也不清楚 QQ」
- 「要是期待看完這系列會成為 DevOps 現成的專家的話，那推薦看其他位大大的比較快。」

**還會主動給讀者退場路徑**：「也能點擊上一頁看其他位大大的比較快。畢竟文字不少。」

禁止：「你必須」「最佳解就是」「顯然」這類斷言式權威語氣。要斷言時用「其實」「往往」「肯定」先鋪墊。

---

## 3. 顏文字與語尾（有分工，別亂用）

| 符號 | 場合 | 例 |
|---|---|---|
| `XD` | 自嘲、指出荒謬事實 | 「沒想到吧 XD」「老闆總是希望 throught 越多越好 XD」 |
| `:)` | 軟化的宣告、輕鬆收尾 | 「也就是文戲會比較多點 :)」 |
| `:(` | 承認疏漏 | 「昨天好像沒怎提到 :(」 |
| `QQ` | 示弱 | 「能不能完賽我也不清楚 QQ」 |
| `^^` | 講到錢／老闆時的乾笑 | 「越高越省$$ ^^」 |
| `！！` | 災難場面 | 「災難ㄚ！！」 |

一段最多一個，密度不高但出現位置很準——都在情緒轉折點。

---

## 4. 句法習慣

### 高頻起手式
- 「其實…」（最高頻，用來軟化斷言）
- 「往往…」
- 「通常…」
- 「肯定…」（用在推論後果）
- 「白話點，…」（把抽象概念翻成人話）
- 「舉個現實案例，…」
- 「總結來說，…」（小結專用）
- 「所以…」（段落之間的因果推進）

### 招牌反問
「真的有幫助你解決**問題**了嘛？這裡提到的**問題**又是什麼？」
「講這麼多，測量這些要幹麻？」

注意本人寫法：**「嘛」不寫「嗎」、「幹麻」不寫「幹嘛」**。

### 段落節奏
- 2~4 句一段，段間空行。
- 長段落後面常接一個短的收束句：「所以，程式性能的優化以及容量效率的提昇，其實是每個開發人員的重要工作。」
- 條列時愛用 label 對：`方法：/ 說明：`、`最佳實踐：/ 原因：`、`錯誤描述：/ 結果：`、`應用層級：/ 系統層級：/ 實際應用：`

### 人稱切換
- 正文：我們
- 吐槽段落與反問：你 / 團隊
- 自我揭露：我

---

## 5. 中英夾雜規則

**技術名詞一律保留英文原形，不翻譯、不加中文括號**：
Log、Metric、Trace、Profile、Throughput、Latency、Scalability、Resource Utilization、goroutine、syscall、Known-Unknowns、Unknown-Unknowns、Flamegraph、Shift Left、Big Tent

**首次出現的框架/方法論才給中英對照**：
「可觀測性驅動開發（ODD）」「成熟度模型（Maturity Model）」「性能工程（Performance Engineering）」

**中文詞彙用台灣說法**：程式碼、記憶體、資料、正式營運環境、專案、剖析、佇列、建置、實做

> ⚠️ 原文中「數據」「代碼」「性能」「優化」「信息」等對岸用詞有混入。若要統一，建議保留「性能」「優化」（本人高頻且已成個人術語），把「代碼」→「程式碼」、「信息」→「資訊」。這是選擇題，不是錯誤。

---

## 6. 文章骨架（照抄即可）

```
> 開場金句 blockquote（3~5 行，短句換行，不用句號結尾）

承接前文一句 + 前幾天的連結列表
[D2 簡介系統性能工程](url)
[D3 性能測試成熟度模型與實踐指南](url)

## 概念層（歷史脈絡 / 定義 / 分類）
### 子項 1
### 子項 2

> 中段吐槽或現實案例 blockquote

## 實作層（工具 / 程式碼 / benchmark 數據）
```go
// 對照組：Bad vs Good
```
```yaml
go test -bench=.
BenchmarkXxxBad-12    13    83757417 ns/op
BenchmarkXxxGood-12   54    25308850 ns/op
```
逐行解讀數據 → 解釋原理（CPU cache、Spatial locality…）

## 反例層（常見錯誤）
**1. 過度優化**
錯誤描述：…
結果：…

## 小結
總結來說，……

> 文末彩蛋 blockquote：自嘲 / 預告明天 / 補充延伸資料
```

---

## 7. 論證模式

固定的推進順序：**歷史 → 概念框架 → 兩兩對照 → 實測數據 → 反例 → 收攏回商業價值**。

- 喜歡用「歷史時間點」開場建立脈絡（1960年代 / 1980年代 / 2010年代及以後）。
- 喜歡用二元框架切問題（Known-Knowns vs Unknown-Unknowns、Problem Domain vs Solution Domain）。
- 一定會引外部權威並附連結：Wikipedia、CMG 論文、Thoughtworks、Elastic/Grafana 官方文件，以及自己的書《OpenTelemetry 入門指南》與譯作《可觀測性工程》。
- 結論常拉回**成本與商業價值**：省錢、分紅、老闆、資源利用率、營運成本。

---

## 8. 引用與連結紀律（必做）

原本五篇就有這個習慣（Wiki、CMG paper、Thoughtworks、Elastic、Grafana、自己的書），只是不夠系統化。規則化如下。

### 原則

1. **每個「事實宣稱」都要能點到來源。**數字、預設行為、規格條文、版本狀態——只要不是自己實測出來的，就給連結。
2. **優先順序：官方 repo 原始碼 > 官方 spec 文件 > 官方 blog > 二手文章。**寫 OTel 題材時，`github.com/open-telemetry/*` 的檔案連結永遠比部落格轉述可信。
3. **自己實測的東西不用連結，但要標環境。**（沿用既有習慣：「驗證環境：weaver 0.25.1、semantic-conventions `c6cda02`」）
4. **連結要 pin 版本。**引官方 YAML/程式碼時用 tag 或 commit，不要用 `main`——`main` 三個月後就對不上你文中的行號和數字。
   - ✅ `.../blob/v1.37.0/model/hardware/fan-metrics.yaml`
   - ❌ `.../blob/main/model/hardware/fan-metrics.yaml`（除非你要講的正是「現在的 main」，那要寫明日期）
5. **不要猜路徑。**寫之前實際開一次。踩過的例子：`weaver/docs/semconv-syntax.md` 和 `semantic-conventions/model/syntax.md` 都是 404，聽起來很合理但不存在。
6. **行內連結，不要全部堆到文末。**沿用既有寫法：`[可觀測性工程](url)`、圖片下一行補「圖片參考自 [來源](url)」。

### OTel 題材的常用來源（2026-08-03 驗證可用）

| 用途 | 連結 |
| --- | --- |
| semconv 規格總覽（含目前版本號） | https://opentelemetry.io/docs/specs/semconv/ |
| semconv 原始碼 repo | https://github.com/open-telemetry/semantic-conventions |
| semconv YAML 模型目錄 | https://github.com/open-telemetry/semantic-conventions/tree/main/model |
| semconv 人類可讀文件 | https://github.com/open-telemetry/semantic-conventions/blob/main/docs/general/README.md |
| semconv Releases（查版本/日期） | https://github.com/open-telemetry/semantic-conventions/releases |
| semconv CHANGELOG | https://github.com/open-telemetry/semantic-conventions/blob/main/CHANGELOG.md |
| Weaver repo | https://github.com/open-telemetry/weaver |
| Weaver registry 語法文件 | https://github.com/open-telemetry/weaver/blob/main/docs/registry.md |
| Weaver 驗證 / policy | https://github.com/open-telemetry/weaver/blob/main/docs/validate.md |
| Weaver codegen | https://github.com/open-telemetry/weaver/blob/main/docs/codegen.md |
| Weaver registry diff | https://github.com/open-telemetry/weaver/blob/main/docs/schema-changes.md |
| `definition/2` 的權威定義（v2 JSON Schema） | https://github.com/open-telemetry/weaver/blob/main/schemas/semconv.schema.v2.json |
| Weaver 官方介紹文（Observability by Design） | https://opentelemetry.io/blog/2025/otel-weaver/ |
| Telemetry Schema 2.0 追蹤 issue | https://github.com/open-telemetry/opentelemetry-specification/issues/4427 |
| OTel spec repo | https://github.com/open-telemetry/opentelemetry-specification |
| OTel 官方 blog | https://opentelemetry.io/blog/ |
| codegen 非規範指引 | https://opentelemetry.io/docs/specs/semconv/non-normative/code-generation/ |

> ⚠️ 已確認 **404，不要引**：`weaver/blob/main/docs/semconv-syntax.md`、`semantic-conventions/blob/main/model/syntax.md`。

### 引用的語氣

連結要融進句子，不要變成「參考資料清單」。沿用既有寫法：

- 「以上只是 [Wiki](url) 的內容翻譯成中文而已 XD」
- 「在 Thoughtworks 今年也有一篇關於[性能工程成熟度模型](url)的文章，分享給各位閱讀。」
- 「這裡也有 [Performance Process Maturity Model（性能測試成熟度模型）](url)。」
- 「（這份清單是從 [`semconv.schema.v2.json`](url) 的 `properties` 直接讀出來的，不是猜的。）」← 這句話的價值在於**告訴讀者你的來源等級**

最後一種特別值得多用：**講清楚「這個數字是哪來的」**，比連結本身更有說服力。

### 檢查

寫完搜一次確認沒有裸述事實：

```bash
# 找出提到版本號、百分比、數量但附近沒有連結的段落
grep -nE "[0-9]+\s*(個|%|次|行)" 文章.md | grep -v "http"
```

---

## 9. 固定的挖苦對象

寫到「團隊反模式」時，這幾個靶子反覆出現，可直接沿用：

- 嘴上掛「先做再說」的團隊 →「通常此話一出，什麼方法論就基本無用武之處了 XD」
- 只看測試涵蓋率不看內容
- 不復盤、不盤點技術債、疊床架屋
- 上雲時照搬地端習慣 →「最後都會說怎麼比地端都還貴」
- 用鈔能力提高系統容量當成優化 →「並沒作到優化這事情」
- 出事只會加 log 再等 →「因為只做了跟重開機一樣的行為」
- 工具買一堆但關聯不起來 →「處理者還是要開啟超過一個以上的工具和瀏覽器視窗」

反諷句型：「這種我只能說『很棒！』」

---

## 10. 排版細節

- 標點：`：` 與 `︰` 混用（`︰` 多出現在條列項目名之後）。
- 強調：`**粗體**` 主要當「無編號小標」用，句中強調很克制，只給關鍵詞（**問題**、**觀察**與**等待**、**Unknown-Unknowns**）。
- 斜體偶爾用來標語氣詞：「只能告訴我們「*出了什麼問題*」」
- 圖片：直接貼裸連結 `![](url)`，下一行補「圖片參考自 [來源](url)」。
- 中英之間習慣空一格，但不強制一致。

---

## 11. 已知的口語殘留（模仿時請修正）

原文有明顯手誤：「怎假設」「這益為著」「令一個」（另一個）、「應能不好」（效能不好）、「Prfoile」、「throught」。

這些是趕稿痕跡，**不是風格**。生成文字時不要刻意複製錯字，但可以保留造成錯字的那種**口語直出感**：想到哪寫到哪、句子不修飾得太漂亮、偶爾用「其實」「就也」「是也能」這種鬆散連接。

例：「是也能搭配 Push 模式…但都不如 Pull 來的適合。」——這種語序不要改成標準書面語。

---

## 12. 快速檢查清單

寫完一篇後對照：

- [ ] 開頭有 blockquote 金句嗎？
- [ ] 結尾有 `## 小結`，且以「總結來說」起手嗎？
- [ ] 小結後面有彩蛋 blockquote 嗎？
- [ ] 全文至少一處自降姿態或自嘲嗎？
- [ ] 至少一個顏文字，且落在情緒轉折點嗎？
- [ ] 技術名詞保持英文原形，沒有硬翻嗎？
- [ ] 有沒有一段是「業界慘案」或「團隊反模式」？
- [ ] 論述最後有拉回成本／商業價值嗎？
- [ ] **每個非自己實測的數字／宣稱都有連結嗎？**
- [ ] **連結都實際開過、沒有猜路徑嗎？**
- [ ] **引官方 YAML／程式碼時有 pin 版本（tag 或 commit）嗎？**
- [ ] **有標「這個數字是哪來的」嗎？**（例：「從 schema 的 properties 直接讀出來的，不是猜的」）
- [ ] 段落是不是都在 2~4 句？有沒有出現超過 6 句的長段？
