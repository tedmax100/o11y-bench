---
title: "【Day14】那張圖準不準：邊對不對，跟名單上該有誰"
series: "2026 鐵人賽：AIOps with OpenTelemetry"
tags: [OpenTelemetry, AIOps, Tempo, Loki, Signal Plane, 鐵人賽]
---

# Day14：拓撲對帳，跟一個問了三次才問對的問題

> 一張沒有人驗過的架構圖
> 跟一張畫錯的架構圖
> 在會議室的投影幕上長得一模一樣

昨天那張九宮格，中間「對帳」那一欄三格全是空的，而其中一格的具體長相是這樣：

```console
$ uv run python -c "from app.signals.dq import dq_verdict; print(dq_verdict())"
{'proven_good': False, 'score': None, 'note': 'topology not reconciled against live traces; DQ unproven'}
```

DQ 是 Data Quality（資料品質）。這句 `DQ unproven` 的意思不是圖畫錯了，是從來沒有人去驗過。今天就是去驗那一次，而驗完會發現要問的其實是兩個問題：那些邊對不對，以及**那張圖上該有誰**。

程式碼在範例 repo [`OTel_AIOps_Agent`](https://github.com/tedmax100/OTel_AIOps_Agent) 的 [`ironman-2026/day14/`](https://github.com/tedmax100/OTel_AIOps_Agent/tree/main/ironman-2026/day14)。要跑的 `reconcile.py` 是 agent 服務自己的原始碼，我一個字都沒改，這個資料夾裡放的是「跑之前該先確認什麼」的兩支工具。驗證環境是 Tempo 2.6.0、Loki 3.x、Prometheus，都跑在同一座 k3d 叢集裡。

## 一張沒人對過的圖，比沒有圖更危險

`topology.yaml` 宣告了六條邊：誰呼叫誰。agent 拿它來決定「這個服務的錯誤該怪它自己，還是怪它下游」。

問題是這種宣告的圖有一個很難察覺的失效模式。它不會壞掉，它只會慢慢跟現實脫節。某個團隊上週把同步呼叫改成走訊息佇列了，某個服務三個月前就沒有人在打了，而那份 YAML 一個字都不會變。**沒有圖的時候 agent 會說「我不知道依賴關係」，有一張過期的圖，它會很有自信地把你指向一個早就不存在的方向。**

所以這張圖必須是一份持續跟遙測對齊的東西，不是一頁 wiki。而要對齊，得先回答一個問題：怎麼從 trace 裡看出一條邊。

答案比想像中簡單，而且不需要任何額外的埋點。用 httpx 加 FastAPI 的自動注入時，服務 A 呼叫服務 B，A 會產一個 CLIENT span，B 會產一個 SERVER span，而 B 的 SERVER span 的 parent 就是 A 的 CLIENT span。也就是說，`service.name` 剛好在呼叫的那一刻改變。

```mermaid
flowchart LR
    W["span: POST /api/orders<br/>service.name = webapp"] --> G["span: POST /api/orders<br/>service.name = api-gateway"]
    G --> O["span: create order<br/>service.name = order-service"]
    O --> P["span: POST /charge<br/>service.name = payment-service"]
    W -.->|"父子之間服務變了<br/>= 一條觀察到的邊"| G
```

所以判斷條件只有一行：一個 span 的服務跟它 parent 的服務不一樣，那就是一條觀察到的邊。

```python
for sid, svc in svc_of.items():
    psvc = svc_of.get(parent_of.get(sid))
    if psvc and svc and psvc != svc:
        edges.add((psvc, svc))
```

把一批 trace 這樣掃過去，取聯集，就得到「觀察到的邊」的集合。跟宣告的那六條做集合運算，就是兩份清單：宣告了但沒觀察到，以及觀察到但沒宣告。

## 第一次跑，六條邊全滅

```console
$ uv run python -m app.signals.reconcile
topology v1.0.0 reconciled against 0 traces
  declared=6 observed=0 dq_score=None
  declared but not observed (stale or low traffic):
      api-gateway → order-service
      api-gateway → payment-service
      api-gateway → user-service
      order-service → payment-service
      order-service → user-service
      webapp → api-gateway
```

六條邊，一條都沒觀察到。如果照字面讀，這份報告在說「你宣告的整張圖跟現實完全對不上」。

而它其實只是在說沒有流量。`traces_sampled` 是 0，那一行就寫在最上面，但它排在報告的第一行、字很小，而下面那六條紅通通的邊佔了整個畫面。`dq_score` 給的是 `None` 而不是 `0.0`，這個區分是對的（沒有資料，跟資料顯示一致性為零，是兩件事），但這個區分只活在那個回傳值裡，沒有活在人看到的畫面上。

會沒有流量，是因為 `reconcile` 在搜尋的時候套了一個過濾器 `{ trace:duration > 5ms }`。這個過濾器是必要的，我寫了一支小工具去看那段時間 Tempo 裡到底有什麼：

```console
$ python3 ironman-2026/day14/tempo_probe.py http://localhost:3210 120
http://localhost:3210 → Tempo 2.6.0 (rev e85bbc57d)
  last 120s: 214 traces
    slowest seen           : 1ms
    survives the >5ms filter: 0
    ⚠ reconcile would sample 0 traces here and report every declared
      edge as unobserved. That is 'no traffic', not 'the graph is wrong'.
```

214 筆 trace，最長 1 毫秒，全部是 kubelet 打的 `GET /health` 跟 `GET /healthz`。健康檢查會把 Tempo 灌滿，而且它們不跨服務，一條邊都貢獻不了。不濾掉它們，那個取樣上限會被探針吃光。**但同一個過濾器，在沒有應用流量的時候會把畫面清成全空，而全空跟「圖全錯」在報告上長得一模一樣。**

在拿到這個結論之前我先繞了一大圈：`kubectl port-forward` 把 Tempo 轉到本機 3200，curl 得到回應、reconcile 拿到 0 筆、collector 說它送出了四萬多個 span 而且零失敗、Tempo 的 log 說它正在寫 block，每一個元件都說自己沒事，資料卻不在。真相是那句 port-forward 根本沒成功（`bind: address already in use`），本機 3200 早就被另一座 k3d 叢集的 load balancer 佔著，那座叢集裡也有一個 Tempo，版本 2.10.3，資料本來就停在兩小時前。兩個東西共用一個埠號，失敗的那個安靜地退場，而活著的那個照常回答問題。我後來把「這個位址上的 Tempo 到底是哪一版」加進那支探針工具的第一行輸出，就是因為這件事：**一個對帳工具在報告差異之前，得先能證明它在跟正確的對象講話。**

> 這是這個系列第幾次遇到「空結果沒有錯誤訊息」我已經數不清了。第一天是 Prometheus 回 `status: success` 加空陣列，這次是一個對帳報告的空集合。形狀完全一樣：查詢成功、結果是空的、而空的原因有兩種，工具不告訴你是哪一種。

## 那條邊到底存不存在

灌了 70 秒的流量再跑一次，這次像樣了：

```console
topology v1.0.0 reconciled against 50 traces
  declared=6 observed=5 dq_score=1.0
  declared but not observed (stale or low traffic):
      api-gateway → payment-service
```

取樣 50 筆 trace，宣告六條、觀察到五條，`dq_score` 是 1.0。那個 1.0 的意思要看清楚，它算的是「觀察到的邊裡面，有幾成是宣告過的」。1.0 代表沒有任何一條真實流量走在圖上沒有的路徑上，這是刻意選的方向，因為`未宣告的邊`是比較危險的那一種：它代表有人加了一個依賴而沒有人知道。

剩下那條 `api-gateway → payment-service` 是宣告了但沒觀察到。到這裡，一份正常的排查會停在「這條邊大概是舊的，去問問看還有沒有人在用」。我沒有停在那裡，因為那個括號裡寫著 `stale or low traffic`，兩種可能塞在同一個清單裡。

先去翻原始碼，api-gateway 確實有這條路（`POST /api/payments` 直接代理到 payment 的 `/charge`），只是 `load.sh` 的端點組合裡沒有直接打它，付款都是經由 order-service 進去的。手動打了十二次之後再跑，結果沒變，還是 `unobserved`。於是我去 Tempo 裡撈一筆真的走過那條路的 trace，把同一個函式套上去：

```console
$ uv run python -c "... edges_from_trace(raw) ..."
edges in that one payment trace: {('api-gateway', 'payment-service')}
```

**那條邊看得見，是對帳沒有看到它。** 問題出在取樣：`reconcile` 預設抓 50 筆 trace，而那段時間 Tempo 裡的 trace 由結帳流量主導，我那十二筆付款請求根本沒被抽中。同一份程式碼、同一個視窗、同一座 stack，只把取樣數往上調：

```console
max_traces=50   sampled=50   observed=5  dq=1.0  unobserved=[('api-gateway', 'payment-service')]
max_traces=100  sampled=100  observed=5  dq=1.0  unobserved=[('api-gateway', 'payment-service')]
max_traces=300  sampled=300  observed=6  dq=1.0  unobserved=[]
```

50 跟 100 說這條邊死了，300 說一切正常。而預設值是 50。

```mermaid
flowchart TB
    U["一條邊出現在<br/>「宣告了但沒觀察到」"] --> Q{"為什麼沒看到？"}
    Q -->|"這條路真的沒人走了"| A["圖過期了<br/>該去改 signal.yaml"]
    Q -->|"走的人太少<br/>沒被抽樣抽到"| B["圖是對的<br/>是對帳的取樣不夠"]
    Q -->|"那段時間沒有應用流量"| C["什麼都不能斷定<br/>traces_sampled=0"]
    A --> S["報告上長得一模一樣"]
    B --> S
    C --> S
```

三種原因，一種呈現。而它們的處置完全相反：第一種要去改宣告，第二種要去改對帳的參數，第三種什麼都不該做。這件事讓我對這個模組的評價往下修了一格。它不是壞掉，它做的事情是對的，但它**把一個統計取樣的結果，用一個斷言的語氣印出來**。`observed` 是一個下界，不是一個事實；`unobserved` 是「我沒看到」，不是「它不存在」。這兩個詞現在讀起來的份量是一樣的。

> 稀有路徑本來就是最難用取樣看到、也最值得知道的東西。錯誤處理的分支、降級的備援路徑、只有月底才會走的結算流程，全部都是低流量高風險。而一個照流量比例取樣的對帳，剛好對它們最盲。

## 那更前面的問題：這張圖上該有誰

上面整件事有個前提我沒有質疑過：那張圖上的五個服務，是誰決定的。

答案是我。`topology.yaml` 裡那五個節點，是五個服務各自的 `signal.yaml` 編出來的，而那五份檔案是人寫的。如果有第六個服務跑起來卻沒有人寫那份宣告，這整套東西不會有任何反應。

agent 服務裡本來就有一個函式在做這件事，`list_service_names()`，讀的是 Loki 的 `label/service_name/values`，而 `topology.py` 也早就有一支 CLI 把它接起來了。跑一次：

```console
$ uv run python -m app.signals.topology validate
topology v1.0.0 aligns with 5 live services
```

宣告五個，活著五個，完全對齊。這是第一個答案，而它是錯的。

> 順帶一提，那個 `start`／`end` 不是可有可無的。Loki 的 label values 端點沒給時間範圍會回一個空陣列而不是報錯，這個坑我在別的地方踩過一次。又是同一個形狀：查詢成功、結果是空的、沒有任何東西說你少給了參數。

會去懷疑，是因為前面翻 Tempo 的時候看到過一個不在那五個裡面的名字。所以我把同一個問題分別問了三個 store：

```console
$ curl -s ".../loki/api/v1/label/service_name/values" | jq -c .data
["api-gateway","order-service","payment-service","user-service","webapp"]

$ curl -s ".../api/v1/label/service_name/values" | jq -c .data
["aiops-agent","api-gateway","order-service","payment-service","user-service","webapp"]

$ curl -s ".../api/v2/search/tag/resource.service.name/values" | jq -c '[.tagValues[].value]|sort'
["aiops-agent","api-gateway","order-service","payment-service","user-service","webapp"]
```

Loki 說五個，Prometheus 說六個，Tempo 說六個。多出來的那個是 `aiops-agent`，也就是這個系列從第一天用到現在的那隻 agent 自己。它跑在同一個 namespace 裡、有 trace、有 metric，就是沒有 log 進 Loki。我把視窗拉到七天，Loki 還是沒看過它。

原因查得到：demo 那組服務共用一份 `o11y_shared/logging.py`，裡面把 OTLP 的 logger provider 接起來了；agent 服務沒有這個東西，它的 log 只進 stdout。所以這不是資料掉了，是這個服務從一開始就沒有把 log 送進來，而它的另外兩種訊號都好好的。

```mermaid
flowchart TB
    A["aiops-agent<br/>正在跑，有流量"] --> M["metric → Prometheus ✓"]
    A --> T["trace → Tempo ✓"]
    A --> L["log → 只進 stdout ✗"]
    L --> Q["list_service_names 只讀 Loki"]
    Q --> R["所以它不存在<br/>而報告說「完全對齊」"]
```

`list_service_names()` 沒有 bug，它做的事情跟它的 docstring 一字不差，「Loki 裡現在有哪些 `service_name`」。問題出在呼叫它的那一行，把這個答案當成了「現在有哪些服務」。這兩句話在五個服務都乖乖送三種訊號的時候是同一件事，在第六個服務只送兩種的時候就不是了。而**偏偏是那些不完整的服務最需要被發現**，因為「這個服務沒有送 log」本身就是一件該有人知道的事。

所以現在這個檢查有一個很難看的性質：一個服務越不合規，它越不容易被這個檢查抓到。這跟前面那份上線 checklist 的第八項是同一種諷刺，那次是命名寫錯的服務躲過了值域檢查，這次是不送 log 的服務躲過了存在性檢查。

## 那就三個都問，然後多一個離開碼

改法本身沒什麼技術含量，把三個 store 都問一遍，取聯集：

```console
$ python3 ironman-2026/day14/topology_watch.py \
    --topology aiops-agent/service/app/signals/topology.yaml \
    --loki ... --prom ... --tempo ... --lookback 6h

# topology watch — declared 5, lookback 6h
  loki        sees  5: api-gateway, order-service, payment-service, user-service, webapp
  prometheus  sees  6: aiops-agent, api-gateway, order-service, payment-service, user-service, webapp
  tempo       sees  6: aiops-agent, api-gateway, order-service, payment-service, user-service, webapp
  ~ 'aiops-agent' is missing from loki but present in others
  ✗ live 'aiops-agent' is not declared (seen by prometheus, tempo)
```

`exit=1`。而只問 Loki 的版本，也就是一開始那個答案，`exit=0`。同一座叢集、同一個時間、同一份拓撲，一個說有漂移，一個說完全對齊。

那行 `~` 是我後來才加的，它單獨列出「有些 store 看得到、有些看不到」的服務。這一行本身就是一個訊號：一個服務只出現在三分之二的 store 裡，通常代表它的遙測有一塊沒接上，而不是它半死不活。這個資訊比最後那行漂移判定更早、也更可行動。

前面抱怨過對帳報告把三種原因塞進同一個畫面，這支腳本至少把最後一種拆出來了：

```console
$ python3 ironman-2026/day14/topology_watch.py --topology ... --loki http://localhost:9999
  ! loki did not answer (Connection refused) — treating it as no evidence
  no source answered; cannot tell alignment from silence
$ echo $?
2
```

| 碼 | 意思 | 排程上該怎麼反應 |
| --- | --- | --- |
| 0 | 宣告的跟活著的一致 | 什麼都不用做 |
| 1 | 有漂移 | 通知擁有那個服務的團隊 |
| 2 | 問不到，什麼都不能斷定 | 通知平台團隊，這是監控自己壞了 |

2 跟 0 分開是這支腳本唯一真正重要的設計。**把「查不到」算成「沒有漂移」，這個排程就變成一個永遠不會響的告警**，而那正是這個系列從第一天追到現在的那個形狀，只是這次它會發生在一個沒有人盯著的 cron 裡。

而一放上排程，`--lookback` 這個參數的性質就變了。手動跑的時候它只是「我想看多久以前」，排程跑的時候它變成一條規則：一個服務多久沒有訊號，就算它死了。六小時對這組 demo 服務很夠，但對一個只有月底跑的結算服務，六小時的視窗每天都會把它報成死的，然後那個團隊會在兩週內學會忽略這個通知。這跟前面那個取樣問題是同一件事的另一個版本，低頻的東西最容易被誤判成不存在，而低頻不代表不重要。

> 這個參數該設多少，答案不在平台團隊手上。只有那個服務的團隊知道自己的正常閒置週期有多長，所以合理的設計大概是讓它跟著服務走，寫進各自那份 `signal.yaml`，而不是一個全公司一體適用的數字。今天沒有做到這一步。

## 誰擁有宣告，誰擁有對帳，誰收到通知

從平台工程的角度，這裡有幾條界線要畫清楚。

那六條邊的宣告是**產品團隊擁有的**，寫在各自服務的 `signal.yaml` 裡，每個服務只宣告自己打出去的邊，這個設計讓新加服務不用去改一份公用檔案。而對帳這件事剛好相反，它必然是**平台團隊的**，因為它要跨所有服務去看 Tempo，任何單一團隊都拿不到全貌。所以介面是：平台團隊提供對帳，產品團隊收到「你宣告的這條邊我沒看到」的通知，然後由他們決定那是該刪的舊邊還是流量太低。平台團隊不該替他們決定，因為只有那個團隊知道那條路是不是只有月底才會走。

而漂移有兩個方向，該找的人不一樣：

```mermaid
flowchart TB
    D{"哪個方向的漂移"} -->|"宣告了<br/>但沒有任何訊號"| A["服務下線忘了改宣告？<br/>還是遙測斷了？"]
    D -->|"活著<br/>但沒有宣告"| B["有服務上線<br/>沒走上線流程"]
    A --> AO["找那個服務的團隊<br/>只有他們知道是哪一種"]
    B --> BO["找平台團隊<br/>這是流程漏洞，不是誰的疏忽"]
    D -->|"只有部分 store 看得到"| C["遙測有一塊沒接上"] --> CO["找那個團隊<br/>但帶上「哪個 store 看不到」"]
```

「活著但沒有宣告」多半根本不是漂移，是有一個服務上線的時候沒有走上線流程，那是平台團隊的流程漏洞。今天這個 `aiops-agent` 剛好是這一種，而它的擁有者是我自己，這其實蠻公平的。

至於通知該長什麼樣，那行 `seen by prometheus, tempo` 是刻意留的。收到通知的人第一個問題一定是「你憑什麼說它活著」，先把證據放進訊息裡，可以省掉一輪來回。同樣的道理套回對帳報告上，那句 `stale or low traffic` 把判斷丟回給讀的人卻沒給他判斷需要的東西，至少得補上「這條邊上一次被觀察到是什麼時候」跟「這次取樣涵蓋了多少比例的流量」。

## 值班的時候會怎樣

把這件事放回凌晨三點。agent 說「order-service 在噴錯，但它的下游 payment-service 是健康的，所以問題應該在 order-service 自己」。

如果那張圖裡 `order-service → payment-service` 這條邊因為取樣不足被判定成不存在，agent 就不會去看 payment，也不會把它列進候選。你拿到的是一段推理完整、語氣肯定、而且少了一整個分支的結論。**它不會告訴你它少看了哪裡，因為它自己也不知道。**

反過來，如果 `dq_score` 跟那份 `unobserved` 清單有跟著送到值班的人面前，至少你會知道「這個判斷是建立在一張有兩條邊沒被驗證的圖上」。這個資訊現在算得出來，`context.py` 也真的會把漂移標記注進去，但前提是有人跑過對帳。而今天之前，沒有人跑過。

## 今天沒做的事

- **對帳跟那支 watch 都沒有排程。** 今天全部是手動敲的，而一個要靠人記得跑的資料品質檢查，跟前面那些「寫好了但沒有在跑」的東西是同一個問題。要真的排上去，得先決定 exit 2 的時候通知誰、以及連續幾次 exit 1 才值得吵人。
- **沒有處理取樣。** 最直接的做法是把取樣數調高，但那只是把界線往後推。比較對的方向大概是讓對帳留下歷史，用「這條邊連續幾天沒被看到」取代「這一次沒看到」。
- **`unobserved` 沒有帶上時間。** 一條邊上次被觀察到是十分鐘前還是三個月前，是決定要不要動手刪它的關鍵，而現在這兩者在報告上完全一樣。
- **沒有量「取樣涵蓋率」。** 要判斷 50 筆夠不夠，得先知道那個視窗裡總共有多少筆，而那個數字現在只有我手動用探針工具問出來。
- **沒有把 `list_service_names()` 改掉。** 今天做的是一支獨立的腳本，agent 服務裡那個只讀 Loki 的函式一個字都沒動，所以 `topology validate` 那條路現在依然會給出那個假綠燈。要決定的是「多一個資料源」還是「換一個資料源」，兩者對其他呼叫端影響不同。
- **沒有處理服務改名。** 一個服務從 `user-service` 改叫 `identity-service`，這支腳本會報成一個死掉加一個新增，而不是一次更名。
- **`--lookback` 沒有下放到各服務自己宣告。**

## 小結

總結來說，今天做的事其實只有「把一支寫好很久的程式跑起來，然後把一個問題問三次而不是一次」。但跑之前我以為的結果是「圖大概八成準」，跑完拿到的是三個不同的答案，取決於我把取樣數設成多少；而那個「完全對齊」的綠燈，是因為我問的那個 store 剛好是唯一看不到第六個服務的那一個。

比較有價值的大概是那條 `api-gateway → payment-service`，它被報成一條死掉的邊，實際上活得好好的，而我是靠著撈一筆單獨的 trace、把同一個函式套上去，才證明它存在。**一個對帳工具說「這裡有差異」的時候，你還是得有辦法去驗證那個差異本身。** 這跟第一天寫評分器時得出的那句「一個會給錯答案的評分器比沒有評分器更糟」是同一件事，只是這次的受測物換成了一張圖。

要繼續往下做之前，這張圖上的節點至少得先是活的、邊至少得先被驗過。接下來要處理的是圖以外的東西：那些宣告出來的欄位，跑起來之後到底有沒有照著送。

> 那一個多小時，我對著一座根本不相干的 Tempo 做根因分析。查得很認真，方法也沒錯，就是查錯了那一台 XD
> 然後又寫了一個「誰漏了宣告」的檢查，第一個被漏掉的是它自己。這種巧合我只能說「很棒！」
