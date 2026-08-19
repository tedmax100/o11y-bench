---
title: "【Day39．番外】人介入之後，這套系統記得什麼"
series: "2026 鐵人賽：AIOps with OpenTelemetry"
tags: [OpenTelemetry, AIOps, Governance, Evaluation, 鐵人賽]
---

# Day39（番外）：讓 agent 學人做過的事，以及一個為了被答錯而生的事故

> 一個人跟 agent 說「這個判斷是錯的」
> 這句話值多少
> 取決於它被寫進哪張表
> 而昨天為止
> 它被寫進一張沒有人會讀的表

前一篇番外做了案例記憶：agent 每查完一次事故，那次的結論、還有走不通的路，都會留下來給下一次用。做完之後我拿它去跑 A/B，結果什麼都證明不了，因為 3 個 seed 之下的雜訊底線就有 ±67 個百分點。

那篇的重點因此變成那把尺，而不是那個功能。但收尾的時候有一句話我一直放不下：**人做的事，一件都沒被記下來。** agent 學自己，人在旁邊標了半天，系統只是安靜地收著。今天補的就是這件事，順便補一個讓它有機會被量出來的環境。

程式碼在範例 repo [`OTel_AIOps_Agent`](https://github.com/tedmax100/OTel_AIOps_Agent) 的 [`ironman-2026/day39/`](https://github.com/tedmax100/OTel_AIOps_Agent/tree/main/ironman-2026/day39)。驗證環境：本機 k3d 叢集（2026-08-19 實測）、加上重新烘過的 `demo-services-o11y-stack` image。測試從 495 條長到 532 條。

先講清楚今天的形狀：四條回饋通道接上了、第二個事故劇本做出來也驗過了、烤進 stack image 了，**但一次真實 RCA（Root Cause Analysis，根因分析）都還沒跑過**。所以今天沒有分數，只有機制跟環境。

## 三條寫了沒人讀的表

先把現況攤開。人跟這套系統互動的地方只有三個，全部都有紀錄，全部都沒有回到下一次的判斷：

| 人做了什麼 | 系統記了什麼 | 下一次執行知道嗎 |
| --- | --- | --- |
| 在 plugin 上標「這個診斷是錯的」 | `calibration` 一列 `correct=0` | ❌ |
| 駁回一個提案 | `action_requests.status='rejected'` | ❌ |
| （執行器自己）跑完一份 runbook | `runbook_feedback` 一列 | ❌ |

第三列不是人做的，但它跟前兩列是同一種病。`runbook_feedback` 從執行器寫好那天就在收資料，讀它的只有一支健康報表 endpoint，所以一份連續三次驗證失敗的 runbook，下一次照樣以權威的語氣被貼進 prompt 裡。

```mermaid
flowchart LR
    P["人標註<br/>correct=0"] --> C["calibration"]
    R["人駁回提案"] --> A["action_requests"]
    E["執行器跑完 runbook"] --> F["runbook_feedback"]
    C --> X1["只拿來算 overconfidence"]
    A --> X2["沒有任何地方讀"]
    F --> X3["一支報表 endpoint"]
    X1 -.->|"斷在這裡"| N["下一次執行的 prompt / 治理關卡"]
    X2 -.->|"斷在這裡"| N
    X3 -.->|"斷在這裡"| N
```

## 「你錯了」是這套系統最貴的一筆資料

先講第一條。`confirm_from_label()` 這支函式的註解，我當初是這樣寫的：

> a wrong run teaches nothing at this layer: knowing the answer was wrong is not knowing the answer.

這句話對 `cases.root_cause` 那個欄位是對的，對整個案例是錯的。「有人真的看過這件事，然後說 payment 那個版本不是元凶」。這是整套系統會產出的最貴的一筆證據，一個人花時間看完逐字稿才有的。把它丟掉，等於允許下一次執行毫無阻力地走回同一個錯誤答案。

所以它進去了，但進的是**死路那半，不是知識那半**。一個被推翻的假設，跟一條在這個環境問不出東西的查詢，本來就是同一種東西：兩者都不告訴你答案是什麼，都告訴你不要往哪裡走。

```
[1] a human says "wrong", and the case keeps it
    root_cause after a wrong verdict: None
    ruled out [hypothesis] payment v2.5.0 new_validator rejects odd cents
              evidence: latency was flat on v2.5.0 — 0.041s vs 0.059s
              disproved_by: human
```

`root_cause` 還是 `None`，因為判錯確實沒有告訴我們答案。但那個假設現在掛在案例上，帶著那個人打的那句話。

誰有資格否證，用的是跟根因那半一模一樣的**允許清單**形狀：

```
[2] who may disprove — and what an unfamiliar source gets
    ui                     culprit        -> recorded  (a person, on a run that blamed someone)
    eval-harness           culprit        -> recorded  (the grader)
    remediation-verified   culprit        -> ignored   (the run grading its own fix)
    some-new-bot           culprit        -> ignored   (a source nobody has heard of)
    ui                     inconclusive   -> ignored   (a person, on a run that blamed nobody)
```

前四列的道理跟前一篇一樣：這段文字最後會進 prompt，所以不認得的來源要忽略而不是相信，agent 自己驗證自己的修復更不算數。

最後一列是唯一需要想一下的。一個判錯的 `inconclusive`，意思是「它該怪人的時候卻誰都沒怪」，桌上根本沒有假設可以推翻。而**那時候人寫的更正說明就是答案**，把它記在「已排除」這個標題底下，比什麼都不記還糟。

> 更正說明我最後決定存成 evidence，不升格成根因。那是一個自由文字框，可能是答案、可能是提示、也可能是「見 thread」，而 `root_cause` 是下一次執行會當成定論來讀的欄位。要宣告根因，應該要有一個明講「我要宣告根因」的地方。

## 一次拒絕，不該讓人再打第二次

第二條。`reject(request_id, actor)` 以前只有這兩個參數，也就是說「人為什麼不准這次 rollout」這個資訊在系統裡不存在。下一次提一模一樣的提案是必然的，那不是模型固執，是紀錄只允許這樣。

現在它多一個 `reason`，而且那句話會變成事故上的一條死路：

```
[3] a declined action, and the proposal that is not made again
    status=rejected  decision_note='we roll forward here, never back'
    next run's gate: ESCALATE — a person declined this on 2026-08-19: we roll forward here, never back
    same action, another target  bound: False
    another action, same target  bound: False
```

判 ESCALATE 而不是 PROPOSE，是因為 ESCALATE 的語意本來就是「交回給人，連動作都不要預填」。留在 PROPOSE 只會讓人再打一次同樣的拒絕，而第二次拒絕比第一次少了資訊，它講的是我們的固執，不是那個動作。

後兩列是邊界。綁的是 `(動作, 目標)` 這一對，不是動作本身：「不要重啟 payment」不是在講重啟別的東西。

plugin 那邊也順手補完了。核准／駁回這兩個 endpoint 從寫好到現在，介面上一直沒有按鈕，人要按只能 `curl`。現在按鈕接上去了，駁回會跳一個對話框問理由，而且對話框自己會說明為什麼值得填：不填的話，案例只學到「有人說不行」。

### 一個沒有預期到的接縫

這段是我覺得最值得寫的。單元測試全綠之後，我用 `TestClient` 打了一次真的 API，結果駁回沒有留下任何死路。

原因是：提案是在調查進行中產生的，而 `investigations` 那一列是在調查結束時才寫。我本來讓駁回靠 `run_id` 去反查它屬於哪個事故，那就變成一個在讀「稍後才會出現的資料」的查詢。一次跑到一半掛掉的執行、或是人按得夠快的情況，理由就無處可歸。

改法是把 `case_key` 在提案產生的當下就釘在 `action_requests` 上，`run_id` 反查降級成舊資料的 fallback。**人什麼時候按那個按鈕完全不受我們控制，所以不能依賴一列稍後才出現的資料。**

> 這個接縫我是真的沒想到。單元測試我寫了五條、全綠，因為測試裡我很自然地把提案建在 scope 裡面，那是我腦中的「正常流程」。真實世界的問題從來不是正常流程 QQ

## runbook 的成績單

第三條。`runbook_feedback` 現在被兩個地方讀：prompt（貼 runbook 的時候附一行成績單）跟治理關卡。

判準只有一份（`store._rb_verdict`），報表跟關卡共用。一個頁面說這份 runbook 沒事、治理平面卻當它停用，比兩邊都錯還糟。

```
[5] a runbook's own record, read twice
    never executed         insufficient_data  proven_good=False gate=propose  0 recorded execution(s) — too few to rate
    one failure, one run   insufficient_data  proven_good=False gate=propose  1 recorded execution(s) — too few to rate
    three clean            healthy            proven_good=True  gate=propose  3/3 verified clean
    failing verification   needs_review       proven_good=False gate=propose  verify_failed 75% (3/4) — the symptom survived the fix
    its undo did not work  suspended          proven_good=False gate=escalate rollback_failed x1 — this runbook's undo did not work
```

`rollback_failed` 不看比率是刻意的：那裡重要的不是幾成，是逃生門被試過而且沒有用。一個可逆的動作之所以被允許，前提就是它撤得掉。反過來，一次失敗除以一次執行等於 100%，那是謠言不是量測，這條規則本來只寫在演習那天的報表規則裡，現在寫進程式，順帶修正了舊的健康報表（以前 1/2 會印成 50%）。

不過 `gate` 那一欄從頭到尾沒有出現 AUTO，而那不是這道門的功勞。目前 registry 裡每一個動作都是 `requires_approval=True`，而且乾淨的資料庫上 human-label 下限本來就沒過。所以今天真正改變結果的只有 `suspended` 那一列，`needs_review` 那條降級路徑在現在的設定下量不到。

```mermaid
flowchart TB
    L["人的判決 correct=0"] --> D1["case_ruled_out<br/>kind=hypothesis"]
    J["人的駁回 + 理由"] --> D2["case_ruled_out<br/>kind=action"]
    RB["runbook 的執行結果"] --> V["_rb_verdict<br/>healthy / needs_review / suspended"]
    D1 --> P["下一次的 prompt<br/>Already ruled out here"]
    D2 --> P
    D2 --> G["治理關卡<br/>ESCALATE，不再提同一個提案"]
    V --> P
    V --> G
```

## 沒有東西是永遠成立的

前三條接上之後才長出來的債。以前只有 agent 自己的死路會累積，現在人的否決也會，而「不要在營業時間 rollout」這種話永遠不會過期。

```
[6] nothing here is true forever
    recalled now:                       1
    recalled past the age cutoff:       0
    a person retracts it:               {'cases': 1, 'dead_ends': 1}
    recalled after the retraction:      0
    occurrences kept:                   1
```

三件事。死路 30 天、案例 90 天，因為舊的那個明確 TTL（Time To Live，存活時間）只蓋到寫入當下就知道會短命的那種，人寫的那些一個都沒有。召回區塊印出日期，因為那條年齡線是一刀切，不是「線以內都還成立」的保證。最後是 `POST /cases/{key}/forget`：年齡線只處理慢慢飄移，環境是禮拜二重建的、政策是禮拜二改的，得有人能在禮拜二講這句話。撤回清掉根因跟死路，`occurrences` 留著，事故發生過這件事不在爭議範圍。

下一次執行實際看到的東西長這樣：

```
[4] what the next run is handed
    ## Past cases for this service (reference — current evidence wins)

    ### Already ruled out here — do not spend budget re-checking
    - [action] k8s.rollout_undo on demo/payment-service (not during business hours) — ruled out by a person [2026-08-19]
    - [hypothesis] payment v2.5.0 new_validator rejects odd cents (latency was flat on that version) — ruled out by a person [2026-08-19]
```

寫這段的時候還修掉一個安靜的 bug：死路原本是用召回到的案例的 key 去撈的，也就是說要先有一個確認過根因的案例，死路才召回得到。而人的否證正好發生在「還沒有人答對」的時候，所以這條路一天都不會通。

## 第二個事故：為了被答錯而生的那一個

上面四節做完，還是量不出效果。而擋在量測前面的不只是 seed 數量，還有環境。

前一篇那個對照題六次全錯，我原本以為是召回污染，查完才發現關掉召回的三次錯得一模一樣。原因是這座 demo 只有一個響亮的事故：payment 拒絕奇數分，而 `reason=new_validator_odd_cents` 就長在 Prometheus 的 label 上。原因、症狀、答案全在同一個服務裡，於是 agent 可以答對，但從來沒有往告警指名的服務外面看過一眼。環境本身在教它這個習慣。

所以加第二個劇本，形狀刻意跟第一個相反：

| 劇本 | 壞的東西 | 告警在哪 | 原因在哪 |
| --- | --- | --- | --- |
| `bad-validator` | payment 拒絕奇數分 | payment-service | payment-service |
| `session-cache` | user-service 的 auth check 掉進慢的 session store | **order-service** | **user-service** |

```mermaid
flowchart TB
    A["告警：order-service 的訂單在 auth 那步失敗"] --> B["orders_total{status=cancelled, reason=auth}"]
    B --> C{"order-service 的指標<br/>有講原因嗎"}
    C -->|"沒有，只到 reason=auth"| D["得先問：auth 是誰在做"]
    D --> E["trace：order-service → user-service"]
    E --> F["user_auth_checks_total{reason=session_store_timeout}"]
    F --> G["cache.miss：session 快取被關掉了"]
```

治理先行，這是這系列一路的做法：新的詞彙要先進 Weaver registry，程式碼才准發出來。`app.fail_reason` 多一個 `session_store_timeout`、新增 `app.cache.name`、`user_auth_checks_total` 從「不帶任何 attribute」變成帶 outcome 跟 reason（以前「auth 在失敗」這句話光看指標根本講不出來），並把一直掛著 `reserved — not yet emitted` 的 `event.cache.miss` 真的接上。

旗標改成每個請求讀一次，從自己的 ConfigMap 掛進去，所以不用重啟。第一個劇本要重啟 payment，那會讓 pod rollout 跟故障落在同一分鐘，延遲圖表就有兩種解釋。

活的叢集上實測（下面這些數字是 `verify_incident.py` 直接打 Prometheus 跟 Loki 撈出來的，不是估的）：auth check 的 p95 從大約 1ms 變成 **0.483s**，order-service 的 p95 跟著變成 **0.483s**，大約 9% 的訂單掛在 auth 那步。25 筆訂單從 0.39 秒變成 7.6 秒，關掉之後回到 0.44 秒。

> 加一個事故劇本的成本比我想的高：要動 registry、動服務碼、動 k8s manifest、動 stack 的資料產生器，還要重新烘一次 image。但這筆錢一定得花。一座只有一個事故的 demo，量出來的分數講的是那個事故，不是那隻 agent。

## 三個只有真的跑過才會知道的坑

### 一、lint 自己把 bug 種回去

`flags.py` 有一行 `except json.JSONDecodeError, OSError:`。這是 [PEP 758](https://peps.python.org/pep-0758/) 的寫法，Python 3.14 收，3.12 不收。而這個 repo 跑 3.14、service 的 image 是 `python3.12-bookworm-slim`。所以它在我這邊 parse 得過、lint 得過，然後 rollout 上去直接 CrashLoopBackOff。

我加上括號，跑 `ruff format`，**它把括號拿掉了**。因為根目錄的 ruff 設定寫著 `target-version = "py314"`，格式化器認為那個括號多餘。

```
except (json.JSONDecodeError, OSError):   # 我修好的
except json.JSONDecodeError, OSError:     # ruff format 之後
```

這個 bug 是檢查工具自己種回去的，而且每一道檢查都是綠的。修法是給 demo-services 自己的 ruff 設定，`extend` 根目錄那份再把 `target-version` 改成 `py312`，跟它真正跑的 runtime 對齊。

> 這件事我覺得比那個語法本身有意思：**工具鏈的假設跟執行環境的假設不一致的時候，檢查全綠不代表任何事情。** 而且它壞的方向剛好是最難察覺的那種，因為出事的地方離改動的地方隔了一次 docker build。

### 二、`increase()` 回報 0，而事故正在發生

第一次跑驗證腳本，第 3 段是這樣的：

```
user_auth_checks_total by outcome, 15m
    status=authorized                                             77.138
    reason=session_store_timeout status=error                      0.000
```

而同一個視窗的 Loki 有 8 筆 `user.auth_failed`，raw counter 也確實是 8。

原因是我剛重新 build 過 image，pod 重啟讓 counter 歸零，那個視窗裡的樣本長這樣：

```
13, 13, 13, 13, 8, 8, 16
```

`increase()` 處理得了 reset，但當它落在只看得到 `8, 8` 那段平的區間時，答案就是 0。

**這對值班的人為什麼危險**：`0.000` 跟「沒有這回事」長得一模一樣，而它旁邊那一列 `status=authorized` 有一個很漂亮的數字，看起來整個查詢是健康的。一隻 agent 走到這一步拿到 0，最合理的下一步就是回頭去怪 order-service 自己，而那正是這個劇本要考的東西。

這跟這個 repo 記過的另外兩個坑是同一類：histogram 的預設毫秒 bucket 讓 `histogram_quantile` 回傳一個看起來很像真的常數、Loki 的 `count_over_time` 配 `query_range` 讓總量膨脹一百多倍。**共通點都是查詢成功、數字錯誤、沒有任何東西會抱怨。**

### 三、我的探測腳本一開始是錯的，而且錯得有意義

探測腳本第一版跑出來，駁回沒有綁住任何東西。查下去發現是我把提案寫在 case scope 外面，而 `create_from_decision` 是從 scope 讀它屬於哪個事故的。

有趣的是這不是 bug，那是一個合法的狀態：從 chat 進來的那條路徑就會產生沒有 scope 的提案。真實路徑是在 scope 裡面，所以產品行為是對的，錯的是探測腳本。**寫探測腳本的價值有一半在這裡：它逼你把「真實路徑到底長怎樣」講清楚一次。**

## 兩個事故同時活著，就等於沒有事故

最後一段是把第二個劇本烤進 stack image。這件事本來只是「讓 fixture 可重現」，做下去才發現有一個非做不可的決定。

第二個事故不能跟第一個一樣「一直持續到資料結尾」。因為**兩個都活在 `now` 的事故，從任何單一告警的角度看是分不開的**——每一個查詢視窗都同時包含兩者，一題 fixture 通過了，你沒辦法確定它答的是哪一個。

所以 session-cache 是一個有結束時間的窗（資料結尾往前 7 小時到 5 小時），而 fixture 的 `startsAt` 多了一個相對寫法：

```yaml
startsAt: now-6h
```

不能寫絕對時間，因為資料結尾是跟著 `O11Y_SCENARIO_TIME_ISO` 動的。所以 fixture 只說「我的事故在多久以前」，時鐘還是 stack 的。

`wait_ready` 也跟著改成每個事故各檢查一支 counter。只查一支的話，一顆用舊 generator 建的 image 會回報 ready，然後那題 fixture 失敗，看起來就像 agent 答錯了。

烤好的資料（24 小時，結束在 `2026-08-19T14:59:39Z`）：

```
                        窗內        接近資料結尾
auth p95                0.482s      0.005s
order p95               0.483s      0.024s
session_store_timeout   26.4        0.0
orders cancelled/auth   26.4        3.1   （這是基線的偶發失敗）
```

Loki 在同一個窗有 177 筆 `cache.miss` 配 27 筆 `order.cancelled`，Tempo 也查得到那些從 webapp 開始、錯在 user-service span 上的 trace。

## 今天沒做的事

- **一次真實 RCA 都沒跑過。** 四條通道接上了、劇本驗過了、烤進 image 了，但 agent 沒有被它們影響過任何一次。今天沒有分數。
- **雜訊底線 ±67 個百分點還在。** 要有結論得加 seed，而該加到多少目前只知道下界。
- **`increase()` 那個 0 沒有被擋下來。** 我寫進文件了，但 fixture 的流程檢查裡沒有任何一條會在 agent 讀到 0 的時候要求它去對照日誌或 raw counter。
- **`needs_review` 那條降級路徑量不到**（上面第三節）。
- **plugin 沒在真的 Grafana 裡點過。** 駁回理由的輸入框驗到型別、lint、API 契約為止。
- **年齡線 30 天／90 天是憑感覺挑的**，沒有任何量測支撐。
- **eval 的流程檢查沒回饋給 agent。** 它連續三次犯同一個錯，報表寫下來，agent 不知道。這是最後一條沒接的通道。
- **匹配還是硬的。** `symptom` 恆為空字串，換一個 alertname 就學不到。放寬的兩種直覺做法都是反方向的，這個留給後面。

## 小結

總結來說，今天做的事情用一句話講就是「把三張寫了沒人讀的表接回去」，加上一個為了讓 agent 答錯而設計的事故。沒有分數，也沒有任何一個數字變好。

但有兩件事我覺得帶得走。一是**一句「你錯了」值多少，取決於它被寫進哪張表**。同一個人花同樣的時間標註，資料落在 `calibration` 就只是一個統計樣本，落在案例上才會改變下一次的行為，而這兩者的成本差距只有幾十行程式碼。二是量測的前提不只是樣本數，還有環境：一座只有一個響亮事故的 demo，會安靜地訓練出一隻「把手邊唯一認識的事故套到所有症狀上」的 agent，而你在分數上看不出來，因為它答對了。

實際用途上，這幾天做的東西最直接的價值是把「加 seed」跟「加事故」變成兩筆算得出來的帳，而不是兩句感覺。

> 這篇裡面我最喜歡的一段，是那個 `except` 括號被 ruff 拿掉的地方。
> 寫了那麼多天治理，結果最後是我自己的 lint 設定跟 runtime 對不上 XD
