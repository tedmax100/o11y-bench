---
title: "【Day32】一次「調查」其實是五次模型呼叫，而想比查慢四十倍"
series: "2026 鐵人賽：AIOps with OpenTelemetry"
tags: [OpenTelemetry, AIOps, Tracing, LLM, 鐵人賽]
---

> 使用者按下送出，七秒之後看到答案
> 那七秒裡發生了五次模型呼叫、一次查詢、兩塊錢的千分之一
> 而這些以前沒有一項是看得到的

Day28 確認了 agent 自己的推理過程一直有被 trace，Day30 把「從結論走回那條 trace」的欄位補上。今天是這條線的最後一段：**那條 trace 讀出來之後長什麼樣，以及它順便回答的另一個問題——一次調查到底多少錢。**

程式碼在範例 repo [`OTel_AIOps_Agent`](https://github.com/tedmax100/OTel_AIOps_Agent) 的 [`ironman-2026/day32/`](https://github.com/tedmax100/OTel_AIOps_Agent/tree/main/ironman-2026/day32)。

## 把一次調查攤平

`/traces/{id}` 做的事是把 Tempo 的 OTLP-JSON 轉成一棵節點樹：gen_ai 的 span 變成 `llm` / `tool` 節點，帶著 prompt、工具參數、token 用量跟算好的成本；其他變成 `http` / `business`。plugin 畫的是這棵樹，而 `trace_tree.py` 把同一份東西印在終端機上：

```console
trace 10d35edee3e4a743d43395ee6b55f5c8
  55 spans, 5 LLM call(s), 1 tool call(s), 18070 tokens, $0.001964
  models: ['gemini-2.5-flash-lite']

[http    ] POST /chat                                             7064ms
  [business] AIOps_Intent_Gate                                      1400ms
    [llm     ] ChatGoogleGenerativeAI.chat                            1397ms in=684 out=69 $9.6e-05
    [business] invoke_agent LangGraph                                 3094ms
      [business] LangGraph                                              3093ms
        [business] agent                                                  1491ms
          [llm     ] ChatGoogleGenerativeAI.chat                            1488ms in=10039 out=115 $0.00105
        [business] tools                                                    39ms
          [tool    ] query_prometheus                                        37ms
        [business] agent                                                  1553ms
          [llm     ] ChatGoogleGenerativeAI.chat                            1549ms in=4050 out=123 $0.000454
        [business] rubric_trace                                             4ms
    [business] AIOps_Findings_Extractor                               1445ms
      [llm     ] ChatGoogleGenerativeAI.chat                            1442ms in=2408 out=156 $0.000303
    [business] AIOps_FollowUp_Suggester                                904ms
      [llm     ] ChatGoogleGenerativeAI.chat                             902ms in=366 out=60 $6.1e-05
```

這一張圖回答了四個我以前只能用猜的問題。

**一，「一次調查」其實是五次模型呼叫。** 只有中間兩次在推理，其他三次分別是意圖閘門（這句話在不在範圍內、是查詢還是調查）、結論抽取（Day30 加的）、後續問題建議。使用者感覺是「問了一個問題」，帳單上是五筆。

**二，錢花在輸入，不是輸出。** 第一次推理輸入 10,039 個 token、輸出 115 個，而它就佔了整趟 53% 的成本。那一萬個 token 就是 Day23 量到的那七千多字元 context 加上系統 prompt。**這系列前面二十天做的所有 context 工程，成本都壓在這一格上。**

**三，慢的是想，不是查。** `tools` 那一層只花 39 毫秒，模型每次思考一秒半。我原本一直以為要優化的是查詢，看到這棵樹才知道優化查詢等於在七秒裡搶那 39 毫秒。

**四，Day30 那個信心分數不是免費的。** `AIOps_Findings_Extractor` 是 $0.000303，大約 15%。這筆帳昨天寫的時候我只寫「多一次 LLM 呼叫」，今天它有數字了。

> 順帶一提，這棵樹會這麼完整，是因為 `opentelemetry-instrumentation-langchain` 照著 gen_ai 語意慣例產 span，而我一行 instrumentation 都沒寫。這是這系列最後一次、也是最直接的一次「遵守慣例的回報」：別人做的工具直接看懂你的東西。

## 成本數字要自己講出它是怎麼算的

那個 `$0.001964` 是怎麼來的？一張寫死在 `traces.py` 裡的價格表，我手打的，從來沒有跟帳單對過。

這個形狀在這系列出現過太多次了。Day21 那個過期的 `git_version` 宣告、Day15 那張沒人對過的拓撲圖、Day18 那個沒講清楚邊界的 100%，全部是同一件事：**一份沒有人對帳的宣告，會在沒有人發現的情況下慢慢變成謊話。** 而成本數字特別危險，因為它會被拿去做決策（「這個功能太貴，關掉」）。

所以今天做的不是去把價格對準（我沒有那份帳單），是讓這個數字帶著它的來歷一起走：

```python
PRICES_AS_OF = (
    "2026-08-06, hand-entered from the public price list, "
    "never reconciled against billing"
)
```

```console
  55 spans, 5 LLM call(s), 1 tool call(s), 18070 tokens, $0.001964
  cost basis: 2026-08-06, hand-entered from the public price list, never reconciled against billing
```

這跟 Day20 講的溯源是同一件事的縮小版：一個數字如果沒有辦法讓人問「你怎麼算的」，它在決策裡就不該被當成事實。**我沒辦法讓它變準，但我可以讓它不假裝自己很準。**

## 為什麼要藏掉一部分 span

那條 trace 有 55 個 span，其中十幾個是 httpx 打 Prometheus/Loki/Tempo 的 client span。它們是真的，偶爾也很有用（Day25 那些 API 怪癖就是靠這一層看到的），但如果全部印出來，「它怎麼想」會被埋在一堆 `GET` 裡面。

所以預設濾掉，`--all` 才全印。這件事本身也是這系列反覆講的那條線：**資料完整跟資料可讀是兩個目標，而報告要服務的是後者。** Day18 那天為了同一個理由改過一次降噪。

## 對值班的人來說差在哪

這一頁的價值不在事故當下，在事故之後。

事故當下沒有人會去看 agent 的 trace，他們在救火。但隔天的檢討會上，「它為什麼會這樣判斷」這個問題如果只能靠回憶，這個工具的信任就會一直停在「看起來還可以」。有了這棵樹，那個問題變成：打開它，看到第一次推理的輸入是什麼、它查了什麼、拿回什麼、然後怎麼收斂。**可以被檢討的東西才會被改進，不能被檢討的東西只會被關掉。**

另外那個成本欄位也有一個很現實的用途：當有人問「這個 agent 一個月要多少錢」，答案不再是「呃，我估算一下」，而是一條可以按服務、按告警名稱切開的 trace 資料。而且它跟延遲、跟工具呼叫次數長在同一條 trace 上，所以「省錢」跟「變笨」之間的取捨是看得見的。

## 今天沒做的事

- **價格表沒有對帳。** 今天只讓它承認自己沒對過。真的要準，得接雲端帳單 API，或至少讓它在版本變動時發出警告。
- **成本沒有進指標。** 它現在只存在於個別 trace 裡，沒有一條「每天花多少」的 metric，也就沒辦法設告警。
- **span 的取樣是全開的。** 每個模型 span 都帶著完整 prompt，高頻使用下 Tempo 的量會很可觀，而 Day23 那個一小時保留期同時也代表「昨天的推理過程今天已經沒了」。
- **Trace Explorer 的 AI 分析我沒有評估過。** 那個功能會再叫一次模型去總結這條 trace，等於用 LLM 解釋 LLM，而它自己也沒有被任何 rubric 檢查。

## 小結

總結來說，今天最有用的不是那棵樹好不好看，是它把三個一直靠感覺的東西變成數字：一次調查是五次模型呼叫、錢有一半花在第一次推理的輸入、以及查詢只佔七秒裡的 39 毫秒。這三件事我在寫這個系列的前二十天裡，每一件都猜錯過。而那個成本數字最後沒有變準，只是變得誠實：它現在會自己說「我是手打的、沒有對過帳」，而這在決策場合裡比一個看起來很精確的數字有用。

> 「慢的是想，不是查」這件事，我是看到 39ms 那一行才真的接受的。
> 在那之前我還花了一天去優化查詢的位元組數 XD
