---
title: "【Day1】起手式：一個沒有治理的示範服務"
series: "2026 鐵人賽：AIOps with OpenTelemetry"
tags: [OpenTelemetry, AIOps, 鐵人賽]
---

# Day1：起手式——一個沒有治理的示範服務

這系列要走 44 天，分兩段。但在講任何 Operator、Weaver、Signal Plane 之前，我想先做一件比較笨的事：**故意做一個爛的**。

不是因為爛東西有趣，是因為接下來 33 天要學的每一項治理工具——`weaver registry check`、CI Gate、schema diff——都是在回答同一個問題：「這種爛東西，是怎麼長出來的，又要在哪一步被攔下來？」如果沒有先看過問題長什麼樣子，後面每一天的治理工具都會像是在解一個不存在的痛點。

## 一個企業內，這種爛東西是怎麼冒出來的

一個企業內，會有很多事業體，彼此都有自己的系統與服務邊界，也會有自己部門內的 coding guideline。這些邊界本身沒有問題——不同事業體本來就該有自己的節奏跟規範。問題是：**沒有任何機制保證，不同部門、不同服務產出的遙測資料，是一致的。**

為什麼一致性這麼重要？答案是兩個字：**治理**。不管是平台工程團隊要在線上做 troubleshooting，還是之後要把遙測資料餵給 LLM 分析、設計出真正能有效分析並處理問題的 AIOps agent，前提都是同一件事——我們需要的是高品質、且概念一致的遙測資料。正所謂「工欲善其事，必先利其器」，在線上環境的 troubleshooting 上，這是最重要的第一步；資料本身不一致，後面接的分析或 agent 判斷，地基就是歪的。

幾年前我介紹過 OpenTelemetry（OTel）這個遙測框架，但今天要先講清楚一件常被誤會的事：**裝了 OTel，不代表大家的遙測資料就會自動變得一致、便於治理。** 有的服務會輸出 `vcs.repository`，有的則完全沒輸出，還有的輸出的是舊版命名 `git.repository`；有的部門自己包裝了一層 SDK，而這層 wrapper 的 semantic convention 從裝上那天起就再也沒有升級過——結果就是同一間公司裡，有些部門用著新版 semantic convention，有些還停在舊版。而這一切之所以會發生，是因為每個部門的 OTel 都是**各自安裝**的，不是透過中央的形式統一注入設定；也因為沒有任何一個機制，能在 CI 階段就替各團隊掃出哪些遙測資料不滿足規範。

在開始講我們的 AIOps agent 之前，這系列會先按這個順序講：**OTel Operator**（這個 k8s operator 為什麼特別適合平台團隊，能把「各自安裝」變成「中央宣告、持續調和」）→ **OTel Weaver**（幫 AIOps agent 需要的 Signal Plane 先打好底，定義標準、甚至能做檢查）→ 後半段才輪到 AIOps agent 本身該怎麼設計。這個順序不是隨意排的：agent 的判斷品質，直接建立在前兩者打下的地基上。

系列走到後面，我們也會提供實測數據，直接比較「品質不太好的遙測資料」跟「高品質且一致的遙測資料」，餵給同一個 AIOps agent 分析、排查、給出處理建議時，兩者拿到的分數差多少——不只是講道理，是真的跑出數字來。

還有一件事，光是把 log/metrics/trace 三者做到一致，也還不夠。即使 trace ID、span ID 能把三種訊號關聯起來，這三者關聯得起來，不代表它們合起來就講得出一個完整的故事。實務上常見的情況是：你給 LLM 一條 trace，再丟幾個零星的 metrics 數字，然後讓它自己去查幾條 log——但「查哪幾條 log」、「這個異常前後該看哪個時間窗的 metrics」，這些串連的邏輯，其實是 LLM 自己腦補、自己去撈出來的，不是你的遙測資料本身就把這個故事準備好給它。換句話說，資料**一致**跟資料**足夠支撐一次決策**，是兩件不同的事——一致解決的是「大家講的是不是同一種語言」，但「這些訊號合起來夠不夠讓 agent 不用腦補就能下判斷」，是後面 Signal Plane、決策級遙測（decision-level telemetry）要處理的問題，我們會在 Day15 之後的 Signal Plane 篇章、以及 Day21 的 CEL（情境豐富層）具體展開。

## 服務長什麼樣子

這個系列會一路沿用 `demo-services/` 這組服務：`api-gateway` → `order` → `user` / `payment`，一個很典型的訂單建立流程。今天要做的事，是在這組服務裡放進兩個真實團隊常見的命名壞味道。

**壞味道一：同一個概念，兩套命名並存**

`order` service 內部一路都用 `user_id`（snake_case，跟 Python 慣例一致）：

```python
class CreateOrderRequest(BaseModel):
    user_id: str
    ...

logger.info(f"order created for user {req.user_id}", user_id=req.user_id)
```

但如果今天有個前端工程師直接把 JS 慣用的 `userId`（camelCase）當成 request body 的 key 送進來，FastAPI 這層會用 alias 或手動轉換悄悄接住它——沒有人會在 PR 裡特別標註「這裡做了一次命名轉換」，因為程式碼能跑、測試會過。往後如果有第三個 service 直接讀這個 payload 而沒有經過同一層轉換，`userId` 就會原封不動地被送進 log 或 span attribute，跟其他服務的 `user_id` 並存。這不是打字錯誤，是**兩套團隊各自「正確」的命名習慣，在沒有共同合約的情況下第一次真正碰撞**。

![1-1](https://hackmd.io/_uploads/HJGCTwREzx.png)

**壞味道二：Span 名稱沒有語意**

`api-gateway` 目前用 FastAPI 的 auto-instrumentation，span name 預設長這樣：

```
GET /api/orders/{order_id}
POST /api/orders
```

這對 debug 單一 request 夠用，但拉不出「這是一次下單流程」的語意——trace 裡看不到「這是 checkout 這個業務動作」，只看得到 HTTP method 加 path template。等到 Day15 我們要畫 `topology.py` 的服務拓撲圖、要讓 agent 讀 trace 判斷「這個異常出現在哪個業務動作」時，span name 只有 HTTP 路徑會是第一個卡住的地方——agent 要嘛自己猜路徑對應哪個業務語意，要嘛就只能瞎猜。

![image](https://hackmd.io/_uploads/BJ4oAvREMe.png)

**壞味道三：各自安裝，版本漂移，沒有中央攔截機制**

前兩個壞味道是「同一時間點」的不一致，這第三個是「時間拉長之後」的不一致，也是最根本的一個。`payment` service 幾年前導入 OTel 時，團隊自己包了一層 SDK wrapper 方便內部使用；`user` service 是後來才加的，直接用官方 SDK 最新版。結果就是同一個系統裡，`payment` 停在舊版 semantic convention（例如還在用 `git.repository`），`user` 用的是新版（`vcs.repository`）——同一個概念，因為導入時間點不同，長出兩種寫法。

這背後真正的問題不是「有人沒跟上版本」，而是**每個服務的 OTel 設定都是各自安裝、各自維護的**：沒有一個中央機制在部署時統一注入 SDK 版本或 Collector 設定，也沒有任何一個 CI 階段，會在 PR 合併前先掃過一遍「這個服務輸出的遙測資料符不符合現在的規範」。所以版本漂移不是被發現的，是被忽略的——沒有人的職責是去發現它。

這一點對後面的目標特別關鍵：不管是平台工程團隊要做 troubleshooting，還是要把這些遙測資料餵給 LLM 做分析、設計出真正有效的 AIOps agent，前提都是資料本身**一致、可信、有共同的語意基礎**。工欲善其事，必先利其器——如果連「同一個欄位在不同服務裡代表同一件事」都無法保證，後面接的分析或 agent 判斷，地基就是歪的。

![image](https://hackmd.io/_uploads/H1kxyO0Efl.png)

## 這在真實團隊裡怎麼長出來的

這三個壞味道都不是「工程師偷懶」，也不是哪一個人一次做錯了什麼決定，而是一連串各自局部合理的選擇，順著時間疊出來的結果。試著把時間拉回這些決定發生的當下，重新走一遍：

一開始，前端工程師要送一個下單的 request，他照著 JS 圈子的慣例寫 `userId`——這在他當下的世界裡完全是對的，沒有任何理由懷疑這個命名。後端接手的人拿到這個 payload，為了不去改前端已經寫好的契約、也想盡快把功能出掉，就在自己這層加了一個 alias，把 `userId` 悄悄轉成 `user_id` 再往下傳。PR 描述寫的是「支援新的下單欄位」，程式碼能跑、測試也會過，沒有人會覺得這跟「命名治理」扯得上關係——因為在那個當下，這就只是一個功能開發的 PR。

差不多同一時期，`api-gateway` 掛上 auto-instrumentation，span name 就自動變成 HTTP method 加 path template。這在當時要除錯單一個 request 完全夠用，沒有人會在加 tracing 的那一刻，想到三個月後會需要這條 trace 去餵給一個 agent，判斷這是不是一次「checkout」的業務動作——那時候「agent 要讀 trace」根本還不是待辦事項。

再往前推得更早一點，`payment` 團隊在幾年前導入 OTel 時，自己包了一層 SDK wrapper，方便團隊內部統一呼叫——這在當年也是一個很合理、甚至值得稱讚的工程決定。只是包完之後，「semantic convention 要不要跟著官方版本升級」這件事，從來沒有變成任何人 sprint 裡的一個 ticket，日子久了，也就沒有人會想起還有這件事要做。

這三條路徑走到今天，交會在同一個系統裡：沒有人 review 的時候，會去看「這個 attribute key、這個 semconv 版本，跟其他 service 一不一致」——大家 review 的永遠是邏輯對不對、測試過不過。命名一致性、版本一致性，從頭到尾都不在任何人的 checklist 上，原因也很簡單：根本沒有一份 checklist 在管這件事，也沒有一個中央機制、一個 CI 步驟，會在事情發生的當下主動跳出來提醒任何人。

換句話說：**沒有治理的系統，不是因為沒人在乎品質，而是因為「命名一致性」跟「版本一致性」，從來沒有被明確指派給任何一個人、任何一段自動化流程去把關。** 每一個決定在它發生的當下都是局部最優解，時間拉長、決定疊加起來，才變成今天這個全域的爛攤子。

## 這篇之後，這個反面教材會被怎麼用

接下來 33 天，這個服務不會被丟掉，而是會被反覆拿出來對照、一層一層被治理工具修正——你可以把後面每一天，都看成是在回頭處理今天埋下的某一個坑：

Day2 會先退一步，不急著動手，把「AIOps 到底要解決什麼問題」的地圖先畫出來，讓今天這三個壞味道，各自對得上地圖上的哪一格。接著 Day3 會正式介紹 OTel Operator——CRD 加 controller 這套機制，怎麼把「各自安裝、各自維護」這個壞味道三的根因，變成一件由平台團隊中央宣告、持續調和的事，而不再是每個部門各憑本事。到了 Day5，會拿 `weaver registry infer` 直接對這個服務的 OTLP 流量反推一份 schema 草稿，你會看到自動生成的結果，是不是連 `userId`/`user_id` 這兩套並存的命名都一起學了進去。緊接著 Day6-7，會真的手動改一個 attribute 名稱，示範 `weaver check` 到底在哪一步、用什麼樣的格式把這種漂移攔下來，並且把它接進 CI Gate——這一步，正是在回答「沒有人會發現版本漂移」這句話該怎麼被打破。再往後，Day14 開始的 Signal Plane 那八天，則會回頭處理 span name 沒有語意這件事，怎麼具體影響 agent 讀 trace 判斷業務語意的能力，以及光靠 trace ID/span ID 串起三種訊號，為什麼還不足以讓 agent 不腦補就能下決策。

今天不用記住任何工具名稱，只需要記住一件事：**接下來要學的所有治理機制，都是在回答同一個問題——這個壞味道要在流程的哪一步、被誰攔下來。** 明天先把「AIOps 是什麼、不是什麼」講清楚，再繼續往下走。
