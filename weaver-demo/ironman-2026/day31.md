---
title: "【Day31】一個內容完全正確、格式完全沒用的回答"
series: "2026 鐵人賽：AIOps with OpenTelemetry"
tags: [OpenTelemetry, AIOps, Grafana, LLM, 鐵人賽]
---

> 它把告警規則寫得一字不差
> 只是包在 ```` ```yaml ```` 裡
> 所以那顆按鈕沒有出現，使用者得自己複製貼上

昨天把打字那條路的假設樹跟信心分數補起來了。今天處理答案的另一半：**它輸出的東西怎麼變成使用者真的能操作的介面。**

在 Grafana 裡，agent 的回答不是純文字。回答裡的 fenced block 會被 plugin 換成活的面板，`alert` 提案會變成一張有「Create alert」按鈕的卡。這是一份契約，而契約有兩端：prompt 那邊負責寫對，parser 那邊負責認得出來。今天兩端都斷過一次。

程式碼在範例 repo [`OTel_AIOps_Agent`](https://github.com/tedmax100/OTel_AIOps_Agent) 的 [`ironman-2026/day31/`](https://github.com/tedmax100/OTel_AIOps_Agent/tree/main/ironman-2026/day31)。

## 契約本身

```mermaid
flowchart LR
    A["agent 的回答<br/>散文 + fenced blocks"] --> P["splitQueryBlocks<br/>（plugin）"]
    P --> M1["```promql<br/>活的時序圖"]
    P --> M2["```logql 10<br/>活的 logs 面板<br/>資訊行的數字是行數上限"]
    P --> M3["```traceql 3<br/>活的 traces 表"]
    P --> M4["```alert<br/>提案卡 + Create alert 按鈕"]
    P --> M5["其他<br/>純文字"]
```

這個設計有一個好處值得講：**面板不是把 agent 查到的資料畫出來，是把它用的查詢再跑一次。** 所以使用者看到的數字跟 agent 引用的數字來自同一句查詢，但由 Grafana 自己去取，時間範圍還能自己拉。agent 的角色是「把中文翻成查詢」，不是「當一個資料中轉站」。

Day25 那個把 72 KB 壓成 5 KB 的摘要，也是靠這件事才敢做——模型手上那份被壓過的數字只是拿來推理的，畫給人看的那份是面板自己去查的。

`render_probe.py` 就是把這條契約在終端機上跑一次，不用開瀏覽器：

```console
$ uv run python render_probe.py "近10筆 payment-service 的 log"
answer: 75 chars, 1 fenced block(s)

```logql 10  -> live logs panel
     panel row limit: 10
     {service_name="payment-service"}
```

這題走的是 Day30 講的 `lookup` 快速路徑：整個回答就是一句查詢，**面板本身就是答案**，不需要任何散文。

## 斷點一：使用者不可能弄錯的東西

「幫我設一個告警」這條路最後會打到 `/alerts/provision`，把規則寫進 Grafana。第一次真的按下去：

```console
$ curl -X POST localhost:8091/alerts/provision -d '{"title":"payment decline rate high", …}'
{"detail":"grafana rejected the rule: {\"message\":\"invalid alert rule: folder does not exist\"}"}
```

Grafana 講得沒錯，那個 folder 真的不存在。但**那個 folder 是 `AlertSpec` 的預設值 `aiops` 選的，使用者從頭到尾沒看過這個欄位。** 他做的事情只有按一顆按鈕，然後拿到一句「folder 不存在」，接下來要他去 Grafana 建一個叫 aiops 的資料夾再回來重按。

這跟 Day13 那條判準是同一件事：一個機制如果失敗之後還要人去補一個他根本沒參與的前置條件，那個成本就是設計者推給使用者的。所以改成送規則之前先確認 folder，沒有就建（409 也當成功，那代表別人剛好同時建了）：

```console
$ curl -X POST localhost:8091/alerts/provision -d '…'
{"ok":true,"uid":"bfudlf17fvw8wb","title":"payment decline rate high"}

$ curl -H "Authorization: Bearer $TOKEN" localhost:3001/api/v1/provisioning/alert-rules
bfudlf17fvw8wb  payment decline rate high  aiops  5m
```

規則真的進去了，而且是可以在 UI 上編輯的那種（送的時候帶 `X-Disable-Provenance`，不然它會變成一個 UI 上動不了的檔案管理物件）。

## 斷點二：模型照著自己的習慣寫

接著我用正常的方式問一次：

```console
$ 幫我對 payment-service 的拒絕率設一個告警，超過 5% 就通知

好的，這是一個 payment-service 的拒絕率告警設定…

```yaml
alert: PaymentDeclinedRateHigh
expr: sum(rate(payment_charges_total{…,status="declined"}[5m])) / sum(rate(…)) > 0.05
for: 5m
labels:
  severity: warning
```
```

這是一份**完全正確的 Prometheus 告警規則**，也完全沒有用。plugin 認的是 ```` ```alert ```` 的 JSON，看到 `yaml` 就當純文字印出來，那顆按鈕不會出現，使用者只能自己把它複製到 Grafana。

原因不難猜：訓練資料裡「告警規則」長得就是 Prometheus YAML 那個樣子，而我的契約寫在系統 prompt 中段的一個小節裡。**模型不是不聽話，是它有一個更強的先驗。**

第一次修法是在 prompt 裡明寫禁止項，而不只是說明正確格式：

```
- **NEVER emit a Prometheus-style rule (```yaml with alert:/expr:/for:/labels:).**
  It looks right and is useless here: the plugin only renders the ```alert``` JSON
  block as a card with the button…
```

再問一次，JSON 對了：

```console
```json
{"title": "payment decline rate high", "expr": "sum(rate(payment_charges_total{…}[5m])) / …",
 "threshold": 0.05, "comparison": "gt", "for_duration": "5m", …}
```
```

**內容一字不差，fence 還是錯的。** 卡片依然不會出現。

所以第二步是改接收方：```` ```json ```` 只要驗得成 AlertSpec 就當成提案，驗不成就照樣當程式碼區塊。prompt 那邊仍然要求 ```` ```alert ````，但接收端不再因為一個標籤就把一次可用的提案丟掉。

```console
$ uv run python -c "from app.alerts import parse_alert_blocks; …" < answer.txt
parsed 1 spec(s)
  payment decline rate high | threshold 0.05 | for 5m | ds prometheus | folder aiops
```

> 這件事講起來像 Postel's law（送出要嚴謹、接收要寬容），但我想強調的是另一半：**只靠 prompt 的契約是機率性的。** 它會在你沒改任何東西的情況下，因為換一個問法、換一個模型版本就不成立。所以凡是「模型必須輸出某個特定格式」的地方，接收端都要有 plan B，而且要有測試。

## 這條契約有兩份實作

還有一個我改的時候才注意到的問題：這個解析在 repo 裡有兩份，一份在 plugin 的 TypeScript（決定畫什麼），一份在服務端的 Python（`parse_alert_blocks`）。兩份的 regex 各寫各的。

今天兩邊都改了，但這就是 Day26 那個「同一個概念散成兩份」的形狀又出現一次。差別是這次跨語言，沒辦法用 import 收斂。**能做的只有讓其中一份有測試，而且在另一份旁邊寫清楚它是誰的鏡像。**

## 對值班的人來說差在哪

面板這件事的價值，在事故當下比平常大得多。

一段文字說「拒絕率從 1% 跳到 15%」，你會想確認：是哪個時間點跳的、現在還在跳嗎、是不是只有某一版。這三個問題如果要回頭再問 agent 三次，那這個工具就只是一個比較會講話的查詢器。但面板是活的，你可以直接把時間軸拉開、把游標移過去、把 legend 點掉一半，**這些都不用再花一次 LLM 呼叫，也不用相信 agent 有沒有算對。**

而那顆「Create alert」按鈕守的是另一件事：agent 只能提案，寫進 Grafana 的那一步永遠是人按的。這跟 Day27 那個「乾跑算出範圍、但不執行」是同一個立場——**它可以把事情準備到最後一步，但不能自己跨過那一步。**

## 今天沒做的事

- **面板的 datasource uid 是寫死的**（`prometheus` / `loki` / `tempo`）。換一座 Grafana 就會全部畫不出來，而且錯誤訊息只會說找不到資料源。
- **提案卡沒有預覽。** 按下去之前看不到這條規則在過去 24 小時會不會一直在燒，而那是最該先看的東西。
- **`json` fence 的寬容只做在 alert 上。** 如果模型哪天把 PromQL 包在 ```` ```prometheus ```` 裡，面板一樣不會出現。
- **兩份 parser 還是兩份。**

## 小結

總結來說，今天真正的題目不是渲染，是**契約在兩個不太可靠的端點之間怎麼撐住**：發送端是一個有自己習慣的模型，接收端是一段只認特定字串的 regex。中間任何一邊鬆一格，使用者拿到的就是一段看起來很專業、但必須自己複製貼上的文字。而那個 folder 的坑更直接：一個使用者從沒選過的預設值，失敗之後卻要他去補。這兩件事都不難修，難的是它們平常不會出現在任何測試裡，只會在真的按下去的時候發生。

> 那份 YAML 我盯著看了好幾秒，因為它真的寫得很好。
> 然後才想到：寫得再好，使用者還是得自己貼進去 XD
