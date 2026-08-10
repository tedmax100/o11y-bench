---
title: "【Day35】五個旗艦 SLO 算出來：兩個是 0，一個是 0/0，一個量到的是我的手"
series: "2026 鐵人賽：AIOps with OpenTelemetry"
tags: [OpenTelemetry, AIOps, Governance, SLO, 鐵人賽]
---
# Day35：階梯上的位置，跟一份七筆的人工標註

> 這三十四天做出來的東西
> 有一半的價值不在它能做什麼
> 在它終於算得出自己現在幾分
> 而算出來的數字沒有一個好看

昨天蓋了一座只改名字的孿生環境，量出「我手上這份知識屬不屬於這裡」是 1.0 對 0.0，也讓那句「治理是環境的函數」第一次會改變治理判決。今天是最後一天，原本的計畫是驗收：把那本書給的五個量尺拿出來，對著這座系統自己的資料算一次，然後在成熟度階梯上標出真正的位置。

驗收確實做了，五個數字也確實都難看。但真正花掉今天的不是算分數，是算分數之前為了確認資料在哪，一路撞出來的三件事，而那三件的形狀一模一樣：**東西一直都在，只是沒有人在讀它。**

程式碼在範例 repo [`OTel_AIOps_Agent`](https://github.com/tedmax100/OTel_AIOps_Agent) 的 [`ironman-2026/day35/`](https://github.com/tedmax100/OTel_AIOps_Agent/tree/main/ironman-2026/day35)。跑的是從叢集裡撈出來的一份資料庫快照，不需要叢集也不需要 LLM（Large Language Model，大型語言模型）呼叫。今天的量測全部唯讀，但改了幾行產品程式碼，下面會標出來。

## 開場先修一個我這三天都搞錯的東西

打算算 SLO（Service Level Objective，服務水準目標）之前先確認資料在哪，結果第一步就撞到事情。

前面幾天我一直在講「標註只有 35 筆」「過去事故庫是 0 筆」，那些數字都是對著開發機上那份 `aiops.db` 量的。今天把叢集裡那顆 pod 掛的 `/data/aiops.db` 撈出來並排一看：

```
[1] the same schema, two stores, two different histories
  table              dev (aiops.db)    cluster /data
  calibration                    35               15
  investigations                  0               15
  action_requests                 0               13
  executions                      0                1
  audit                           0               28
  cluster labels by source: ui=7
  cluster non-self labels: 7   (Day31 measured this as 0 on the dev store)
```

兩件事情要更正。第一，`investigations` 那張表在叢集裡有 15 筆，不是 0，所以那個 JOIN 撈不到東西的結論只在開發機上成立。第二個更痛：`cluster non-self labels: 7`。那七筆的 `source` 是 `ui`，也就是有人真的在 plugin 頁面上按過對錯，時間全部落在 6 月 22 號那個下午。

我為了湊出「第一批非自我標註」花了整整一天去接評分器，而叢集裡一直躺著七筆真人按出來的標註。它們沒有被我看到的理由很單純：兩份資料庫檔名一樣、schema 一樣、程式碼一樣，只有掛載路徑不一樣。

```mermaid
flowchart TB
    subgraph DEV["開發機"]
        D1["eval/eval.db"] --> D2["aiops.db<br/>35 筆評分器標註"]
    end
    subgraph CL["叢集 pod"]
        C1["plugin 上按對錯<br/>7 筆 source=ui"] --> C2["/data/aiops.db<br/>15 筆 calibration<br/>15 筆 investigations"]
    end
    D2 --> Q1{"我這三天量的"}
    C2 --> Q2{"真的在服務流量上跑的"}

    classDef mine fill:#eaf2f8,stroke:#2874a6,color:#1b4f72
    classDef real fill:#fdebd0,stroke:#ca6f1e,color:#7e5109
    class D1,D2,Q1 mine
    class C1,C2,Q2 real
```

### 其實是三個

寫到這裡我想確認「開發機那份」到底是哪一份，就去翻 Day31 那支報告腳本讀哪裡。它寫的是 `DB = ROOT / "aiops.db"`，repo 根目錄。而那個檔案不存在。往下找，`aiops-agent/service/aiops.db` 有一個，98KB，schema 完整，每一張表都是 0 筆。

所以我寫了一段最笨的東西，把三個候選路徑並排印出來：

```bash
# 從 aiops-agent/service 底下跑
uv run python ../../OTel_AIOps_Agent/ironman-2026/day35/gate_probe.py
```

```
[0] three files named aiops.db, and which one has anything in it
  what day31's script reads    aiops.db             does not exist
  what the service writes      aiops.db             cal=0 inv=0 ar=0 exec=0
  the pod's volume (snapshot)  cluster-snapshot.db  cal=15 inv=15 ar=13 exec=1
```

三個檔案，同一個名字，同一份 schema，同一份程式碼寫出來的，而唯一有資料的是叢集那顆。`service/` 底下那個是某次在那個工作目錄跑起服務時被建出來的，然後就沒有人再寫過它。根目錄那個連建都沒建起來。

這件事躲過了什麼比較值得記：這個 repo 有 419 條測試，每一條都是綠的，也沒有一條會紅，因為每一條都用 `tmp_path` 開自己的暫存 db。**測試涵蓋的是「程式對某個 store 做的事情對不對」，沒有任何一條在問「上線之後它會對哪個 store 做這件事」。**

所以順手加了 `store.describe()`：回一個絕對路徑加各表筆數，掛在 `/healthz` 跟 `/investigations` 上。它不解決任何算術問題，只是讓「你在看哪一顆」變成 curl 得到的東西，而不是要 `kubectl cp` 下來才發現的東西。

跑第一次，輸出是上面那樣。跑第二次，第一行從 `does not exist` 變成 `cal=0`。我那支唯讀探測把它要找的檔案建出來了 XD 原因是 `_connect()` 開檔的時候會順手跑 `executescript(_SCHEMA)` 跟那幾條 `ALTER TABLE`，這對每一個寫入者都是對的（第一次寫入不該因為檔案不存在而失敗），對唯一一個「我只是想知道你是誰」的呼叫者剛好相反。

而它的另一半更難看，我是準備收工的時候才看到的：`git status` 說 `cluster-snapshot.db` 被改過了。同一支唯讀探測，在數那顆快照有幾筆的時候，順手把 `grading_mode` 這個欄位加了上去。也就是**下一段那份「叢集還沒看過這個欄位」的證據，是被這一段的程式碼銷毀的**。它證明的東西跟它做的事情剛好相反。

所以 `describe()` 現在完全不碰 `_connect()`：路徑不存在就直接回報不存在、各表筆數回 `None` 不回 `0`（「沒有檔案」跟「有檔案但是空的」是兩個不同的答案），存在的話用 `file:...?mode=ro` 開，讓 SQLite 自己保證這條路寫不了東西。

```python
# mode=ro 是 SQLite 自己擋的，所以不管這個模組之後長成什麼樣，
# 這一條路都不可能建檔、不可能遷移、不可能寫入。
conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
```

一個專門用來數這些檔案有幾個的工具，自己會多生一個出來，還會順手改掉其中一個。這兩件事是同一個形狀：程式碼對某個 store 做的事情都是對的，錯的是它對**哪一顆**做。

> 舉個現實案例，我看過一個團隊的 staging 跟 production 用同一份 config template，只有一個 endpoint 變數不同，結果排查了兩天的「資料沒進來」，最後發現有寫進去，只是寫到隔壁。同名、同 schema、同一份程式碼，是最難察覺的一種不一樣。

而這個發現底下還藏了一顆更難看的地雷。前幾天為了分辨「這筆 `correct` 在回答哪個問題」，我在 calibration 表上加了一個 `grading_mode` 欄位，而且刻意做成 NULL 不匹配任何篩選（不知道就不算）。那些加欄位的 `ALTER TABLE` 是開連線時自動跑的，叢集裡的 image 比程式碼舊，所以那個欄位在叢集那份資料庫上還不存在：

```
[2] the column the cluster has not seen yet
  cluster calibration.grading_mode present: False
  gate reads modes=('culprit',) (NULL never matches — fail-closed on unknowns)
  after migration, grading_mode present: True
  labeled rows: 7   eligible for the curve after: 0
```

第四行是我把快照複製一份、用現在的程式碼開一次連線之後量到的。**新 image 上線的那一秒，那七筆唯一的真人標註會變成 0 筆算數的。** 那個 fail-closed 的設計我到今天還是覺得是對的，但它套在一份已經存在的資料上，效果是把歷史全部作廢，而且不會有任何一行 log 講這件事。

## 修它：一行 UPDATE，換到一張七筆的可靠度圖

這個要修其實很簡單，而且它值得單獨拆一段，因為它是一個很典型的遷移錯誤：**加欄位的遷移寫了，填欄位的那半沒寫。**

`grading_mode` 的 NULL 語意是「不知道這筆的 `correct` 在回答哪個問題」，而叢集那七筆的模式**其實是知道的**：它們來自 plugin 上那顆對／錯按鈕，按的對象是一份指名了兇手的 RCA（Root Cause Analysis，根因分析），那就是 `culprit` 這種評分。所以正確的遷移不是只加欄位，是加欄位的同時把已知的填進去，剩下真的不知道的才留 NULL。

```bash
# 從 o11y-bench 主 repo 的根目錄跑，預設是對複製出來的一份做乾跑
python3 ironman-2026/day35/fix_grading_mode.py
```

```
[1] before
  as it stands
    labeled=0 non-self=0 ece=None overconfidence=None
    gate: propose  calibration unproven (0 labeled run(s) < 20); autonomy withheld

[2] backfill grading_mode='culprit' for labeled rows from ('ui',)
  rows updated: 7

[3] after
  with the known modes filled in
    labeled=7 non-self=7 ece=0.5643 overconfidence=0.3929
    gate: propose  calibration unproven (7 labeled run(s) < 20); autonomy withheld
```

`labeled` 從 0 變 7，關卡那句話也從「0 筆」變成「7 筆，還差 13 筆」。兩句都是紅燈、都是保留自主權，但第二句是可以靠做事解決的，第一句是把資料丟掉之後的紅燈。這個差別跟前幾天那個「還沒量夠」跟「量完了不該信」的差別是同一件事，只是往前挪了一層：**先確定資料還在，再去談它夠不夠。**

修完之後才第一次看得到那七筆真人標註畫出來的形狀：

```
  reliability diagram over the labels that now count
    band        n    stated   actual   gap
    [0.2,0.3)   2    0.2      0.5      0.3
    [0.8,0.9)   3    0.8167   0.0      0.8167
    [0.9,1.0)   2    0.95     0.5      0.45
```

中間那一列是今天最該記住的一行。三筆講 0.82 的判斷，人回頭看的結論是**三次全錯**。而 0.8 正是 `governance_conf_high`，也就是走向自主執行那條路的門檻。前幾天用評分器那批資料算的時候，這一格是「說 0.8、只對一半」，換成真人標註之後它變成三次全錯。兩批資料在同一格上指著同一個方向，只是真人那批更難看。

七筆下不了結論，這點沒有變。但它現在是一個可以累積的紅燈，而不是一個被自己的遷移擦掉的空表。

## 第三件：那張圖畫出來了，而沒有人在讀它

盯著上面那三列看了一陣子，我發現一件更難堪的事：**那張圖從 Day31 起每一次都在算，而授權那道門一次都沒有看過它。**

`compute_calibration()` 每次都算四個東西：ECE、MCE（Maximum Calibration Error，最大校準誤差）、Brier score，還有 `overconfidence`。而 `_calibration_verdict()` 從第一天起就只讀最後那一個。當時的理由很正當：它是唯一帶方向的，正的代表話說太滿，而治理真正怕的就是話說太滿那一側。

問題不在它不準，它準得很。問題在它是`帶號平均`（白話講就是正負會互相吃掉）。Day31 那篇已經把這件事拆給自己看過了，那天結論寫成一句觀察就放著：這隻 agent 在會的題目上低估自己 0.28、在不會的題目上高估自己 0.37，平均起來是漂亮的 `-0.0029`。

要補的不是「換一個更好的總分」。ECE 換上去一樣是總分，它只是剛好不會抵銷，還是回答不了值班的人真正在問的那句：它跟我說 0.8 的時候，我到底該不該信。那句話的答案一直都在分箱表裡。

所以新增一支純函數 `bin_evidence()`，把分箱表壓成三件平均數殺不掉的事實：

```mermaid
flowchart TB
    C["compute_calibration()<br/>ece / mce / brier / overconfidence / bins"]
    C -->|"以前只走這一條"| O["overconfidence<br/>帶號平均，會抵銷"]
    O --> G{"授權判斷"}
    C -->|"今天接上"| B["bin_evidence()"]
    B --> M["max_gap<br/>樣本夠的箱裡最差的那一箱"]
    B --> D["band_accuracy<br/>≥ conf_high 那一段的正確率"]
    B --> T["thin_bins<br/>被丟掉的薄箱，要講出來"]
    M --> G
    D --> G
    T --> G

    classDef old fill:#fadbd8,stroke:#c0392b,color:#78281f
    classDef new fill:#d5f5e3,stroke:#27ae60,color:#145a32
    class O old
    class M,D,T new
```

`max_gap` 取的是最大值而且是絕對值，所以它結構上就抵銷不掉。`band_accuracy` 是三個裡面最重要的：AUTO 只會在信心 ≥ `governance_conf_high`（0.8）的時候被授出，那就該問那一段的正確率。在低信心區間校準得漂亮，跟「它說 0.85 的時候可不可信」是兩個問題。上面那張七筆的圖之所以刺眼，就是因為它剛好落在這一段。

`thin_bins` 是為了回答 Day31 自己提過的一個反對意見。那天指出 `MCE = 1.0` 來自「信心 0.0、正確率 1.0」那一箱，agent 說「我不知道」然後被判定為對，數學上是最大誤差、直覺上一點都不危險，所以 MCE 不適合當門檻。今天再看，那個推論跳太快了：真正的問題不在 MCE 這個統計量，在那一箱只有兩筆。一個箱子裡只有一兩筆的時候，正確率只可能是 0.0 或 1.0，那不是測量，是一次擲硬幣披上小數點。所以薄的箱子不進 `max_gap` 的計算，但丟掉幾箱幾筆要寫進那句給人看的說明裡。**一個把自己的證據基礎悄悄縮小、然後回報一切正常的檢查，比不做這個檢查更糟。**

拿 Day31 那 35 筆跑一次。那份資料在上面那三個檔案裡都已經沒有了，所以是用那天自己印出來的分箱表重建的，每一箱只有一個信心值，筆數加正確率就把每一列釘死，驗證方式是看它算不算得回原本那三個數字（同一支 `gate_probe.py` 的第三段輸出）：

```
[2] day31's 35 rows, rebuilt from the diagram it published
  labeled=35  overconfidence=-0.0029  ece=0.1743  mce=1.0
  band        n    stated  actual  gap     read as
  [0.0,0.1)  2    0.0     1.0     1.0     too thin to count
  [0.1,0.2)  4    0.1     0.0     0.1     counted
  [0.3,0.4)  2    0.3     0.0     0.3     too thin to count
  [0.6,0.7)  12   0.6     0.5833  0.0167  counted
  [0.7,0.8)  6    0.7     0.8333  0.1333  counted
  [0.8,0.9)  6    0.8     0.5     0.3     DECISION BAND
  [0.9,1.0)  3    0.9     1.0     0.1     DECISION BAND
  evidence: worst counted bin 0.3 at [0.8,0.9); decision band n=9 accuracy=0.6667;
            2 bin(s)/4 run(s) dropped as thin
  before  -> AUTO     calibration ok (overconfidence -0.0029, 35 runs)
  after   -> PROPOSE  accuracy 0.6667 at confidence ≥ 0.8 (n=9) < 0.7;
                      autonomy withheld in the band it would be exercised in,
                      2 bin(s)/4 run(s) too thin to count
```

`-0.0029`、`0.1743`、`1.0` 三個數字都對得上，所以重建的是那 35 筆沒錯。同一批資料，同一個 `overconfidence`，`AUTO` 變成 `PROPOSE`，而且它講得出理由。那個 `MCE = 1.0` 的箱子兩筆，被標成 `too thin to count`，所以 Day31 提的反對意見今天不是靠「別用 MCE」解決的，是靠問那一箱有幾筆解決的。

成本要老實講：我從一個旋鈕變成四個。`governance_max_bin_gap`（0.25）、`governance_min_bin_count`（3）、`governance_min_band_accuracy`（0.7）跟昨天那個 `dq_min_env_fit = 0.9` 一樣，全部是拍的。多三個旋鈕讓「為什麼不放行」變得可以爭論，也讓「調鬆一點」變得容易，而且容易到不需要跟任何人商量。

> 我 Day31 寫了一整篇文章，主旨是「只看一個總分的團隊，跟只看單元測試涵蓋率的團隊是同一種病」。
> 然後我把那個總分留在門禁裡，讓它一路守到最後一天 QQ
> 寫成文章的洞跟修好的洞，中間差的距離比我以為的遠很多。

## 五個旗艦 SLO，一個一個算

那本書（[《代理式可靠性工程》（Agentic Reliability Engineering，簡稱 ARE）](https://learning.oreilly.com/library/view/agentic-reliability-engineering/0642572294809/)，3.6 那節）挑了五個量尺，說這五個是承重的，其他都是從這五個推出來的，而且要一起讀才有意義。它的理由很實際：一個團隊如果拿十五個 SLO 在運作，實際上等於沒有在運作任何一個。

先把定義寫清楚，再看這座系統各自量到什麼：

| SLO                                           | 問的問題                             | 公式                                  | 這座系統                   |
| --------------------------------------------- | ------------------------------------ | ------------------------------------- | -------------------------- |
| ARR（Autonomous Resolution Rate，自主解決率） | 它多常願意自己收掉一個事故           | 自主解決的事故 / 偵測到的事故         | 0 / 3 = 0%                 |
| DQ-SLO（Decision Quality，決策品質）          | 它動手的時候對不對                   | 成功的自主決策 / 全部自主決策         | 0 / 0，算不出來            |
| RL-SLO（Reasoning Latency，推理延遲）         | 從偵測到做出決定要多久               | 決策時間 − 偵測時間                  | n=11，但 8 筆是假的        |
| AE-SLO（Action Effectiveness，行動有效性）    | 動作有沒有真的達到目的、有沒有副作用 | 有效的復原 / 全部復原                 | 0 / 1 = 0%                 |
| CE（Calibration Error，校準誤差）             | 它講的信心跟實際結果差多少           | 分箱之後\|信心 − 實際正確率\| 的平均 | 叢集 0.5643、開發機 0.1743 |

跑出來長這樣：

```bash
# 從 o11y-bench 主 repo 的根目錄跑
python3 ironman-2026/day35/slo_report.py
```

```
[3] the five flagships, over the cluster store
  ARR     autonomously resolved / detected incidents = 0 / 3 -> 0.0%
            (measurable, and it is a real 0: propose=13 — no request was ever raised at AUTO)
  DQ-SLO  successful autonomous decisions / autonomous decisions = 0 / 0 -> undefined (0 denominator)
            (not a measurement: the denominator is empty by construction)
  RL-SLO  decision_commit - startsAt, n=11: max 979s
            8 of them share one startsAt (a replayed alert body): 679-979s — that is my hand, not the system
            the 3 distinct alerts: 10s, 12s, 16s
  AE-SLO  effective recoveries / recoveries = 0 / 1 -> 0.0%
            (n=1, and the one failure was a 401 — it measures a credential, not an action)
  CE      cluster store: labeled=7 ece=0.5643 overconfidence=0.3929
            dev store:     labeled=35 ece=0.1743 overconfidence=-0.0029
            gate floor is 20 labeled runs — neither store answers the same question twice
```

五個數字，四種不同的沒用法，值得一個一個講。

ARR 是 0%，而這是一個真的 0。分母那個 3 是產生過提案的事故指紋數（沒走到提案的告警不在這份帳裡，這是我這支腳本的口徑，不是書的）。十三筆行動請求，`autonomy` 欄位全部是 `propose`，一筆 AUTO 都沒有。這不是「沒量到」，是治理平面每一次都判斷不該放手，而理由前幾天已經拆過了：信心夠高但動作標了 `requires_approval`，加上校準沒過。書裡說 30% 的 ARR 代表以人為主、agent 輔助，85% 代表真的做出了代理式運維。0% 代表這隻 agent 是純顧問。

DQ-SLO 算不出來，而且不是資料不夠，是分母結構上為空。沒有任何一次自主決策，就沒有「自主決策對不對」這個問題。書裡最有用的一句話是 ARR 跟 DQ-SLO 必須一起讀：高 ARR 配低 DQ-SLO 是不安全的自主，高 DQ-SLO 配低 ARR 是護欄綁太緊。我這個組合連被評的資格都還沒有。

**RL-SLO 是今天最好笑的一個。** 它量的是告警的 `startsAt` 到寫下決策之間隔多久，資料是現成的，算出來十一筆，最大值 979 秒。但把 `startsAt` 排一下就發現，其中八筆共用同一個時間戳，因為那是我當初手工存下來的一份告警 JSON，後來一路複製著重放。那八筆量到的不是系統反應多快，是我那天下午重放了幾次。真正不同告警的三筆是 10 秒、12 秒、16 秒。一個做對了的量測，跟一個量到自己的量測，在報表上長得一樣。

AE-SLO 是 0/1。唯一那次執行昨天回了 401，所以「有效的復原」是 0。但這個 0 沒有在講這個動作好不好用，它在講一張憑證。n=1 的比率不該被寫成百分比，我還是把它印出來，因為看到 `0.0%` 旁邊那個 `n=1` 才會記得它不能拿去開會。

CE 是唯一一個兩邊都算得出來的，而它給出兩個相反的結論。開發機那 35 筆評分器標註算出來的過度自信是 `-0.0029`（前幾天已經拆過，那是低估跟高估互相抵銷出來的），而叢集那 7 筆真人標註算出來是 `+0.3929`，ECE（Expected Calibration Error，期望校準誤差）0.5643。門檻是 0.1。**這座系統唯一有的真人標註，說法是這隻 agent 嚴重話說太滿。** 七筆當然不夠下結論，關卡現在給的也還是「還差 13 筆」那種紅燈，但方向跟評分器那批剛好相反這件事本身就是資訊。而今天把分箱接上去之後，就算標註補到 20 筆，這道門也不會只因為那個平均數好看就放行了。

> 值班的人不會問 ARR 是幾成，他會問「昨天那個建議準不準」。這五個量尺真正的用途不是給人看，是給治理平面當旋鈕：DQ-SLO 掉下來，信心門檻就該自己升上去。這件事現在還沒有任何自動連動，全部是我手動讀數字。

## 順手抓到的一個縮寫撞名

算 DQ-SLO 的時候翻了一下自己寫的程式碼，發現 `app/signals/dq.py` 的 docstring 寫著它是「ARE flagship SLO #2」，而它算的是 data quality，也就是宣告的拓撲跟實際呼叫圖對不對得上、上次對帳多久以前。書上的 flagship #2 是 Decision Quality，問的是「它動手的時候對不對」。

兩件都是真的該量的事，兩件也都真的用同一個縮寫。這個形狀在這系列出現過好幾次了，最早那次是一個 `status` 欄位多了一個值，後來是我自己寫的 `correct` 欄位在兩種評分模式下意思不一樣。今天輪到縮寫本身。差別是這次它還沒造成傷害，因為讀它的只有我一個人。

## 階梯上的位置：L2，而 L2 的量尺是壞的

書裡的成熟度模型有五級三個維度，維度是可觀測性、推理、授權，治理不算第四個維度，因為治理就是在 runtime 執行授權的那個機制。每一級的定義是「這三個維度上都必須成立什麼，這一級才能被安全地運作」。

```mermaid
flowchart TB
    L1["L1 Reactive<br/>工具各自為政<br/>人執行所有變更"] --> L2["L2 Augmented<br/>遙測標準化<br/>agent 只給建議<br/>量尺：SAR"]
    L2 -.->|"Trust Ceiling"| L3["L3 Bounded<br/>低風險動作可自主<br/>量尺：ARR 20-40%<br/>CE < 25%"]
    L3 --> L4["L4 Adaptive<br/>自我批判、跨事故學習<br/>政策隨 SLO 自己收放"]
    L4 --> L5["L5 Systemic<br/>多 agent 協作<br/>量尺：Grounding Rate > 99%"]

    classDef here fill:#d5f5e3,stroke:#27ae60,color:#145a32
    class L2 here
```

六天前我猜這座系統在 L2，今天有數字之後，位置沒有變，但變得比較有內容：可觀測性那個維度其實已經摸到 L3 的門檻（拓撲圖可查詢、而且有在對帳），推理那個維度也接近 L3（多步、會分解問題、輸出結構化的候選行動），卡住的是授權。而授權卡住的原因不是程式碼，是證據。

L2 這一級有它專屬的量尺，叫 SAR（Suggestion Acceptance Rate，建議接受率）：人真的接受了多少比例的建議。書裡給的判讀很有意思，80% 以上代表這隻 agent 的推理值得聽，低於 60% 是在製造噪音，而高於 95% 反而代表它太保守，只講人本來就會做的事。

```
[4] Suggestion Acceptance Rate — the L2 gate metric (ARE 4.9)
  proposed=10, aborted=2, rollback_failed=1
  suggestions raised: 13   approved: 3   rejected: 0
  SAR = 23.1%
  actors who approved: day33-live, day33-live-2, nathan-smoke-test
  of the denominator, 10 expired without anyone opening them — SAR counts 'nobody looked' and 'a human said no' as the same event
```

23.1%，而這個數字一點都不能用。三個核准者全部是我在測試自己的東西，`rejected` 是 0（沒有人拒絕過任何建議，因為 plugin 上那顆按鈕還沒接起來），而分母裡有十筆是沒有人打開過的，它們的 TTL（Time To Live，存活時間）是 900 秒，全部早就過期，只是狀態機要有人來敲門才會把它們標成過期。

**SAR 把「人看過之後說不要」跟「沒有人看」算成同一件事。** 這兩件事對平台團隊的意義完全相反：前者是 agent 的推理品質問題，後者是介面問題，或者更難堪的，是根本沒有人在用。而書把 SAR 定成通往 L3 的門檻指標，也就是說如果我照著這個 23.1% 做決定，我會跑去改 prompt，而該修的是那顆按鈕。

至於再往上那一級，書裡把 L2 到 L3 那個交界叫`信任天花板`（Trust Ceiling），並且說這是整個模型最難的一段，難的不是 L4 到 L5。它的要求很硬：治理平面、行動契約、自動逆轉、被校準過的信心，四個必須**同時**到位，因為多數團隊帶著其中三個就去嘗試，然後第一次自主失敗剛好落在缺的那一個上面。

```
[5] the four L3 mechanisms (ARE 4.9: all four, simultaneously)
  governance plane, runtime-evaluated    13 decisions recorded, every one at PROPOSE; the kill switch has been ACTIONS_ENABLED=true in the deployment since 2026-06-22
  action contracts                       2 registered actions, both with reversible + requires_approval flags; dry_run abort=1, dry_run ok=2
  automatic reversal                     rollback fail=1
  calibrated confidence                  7 labeled runs in the cluster store vs a floor of 20
```

前三個有真實證據，而且證據是昨天那條軌跡給的：政策擋過一次單副本的回滾、乾跑算出過影響範圍、自動逆轉真的被觸發過（雖然它也 401 了）。第四個沒有。**這座系統缺的正好就是那一個，而它是四個裡面唯一不能靠寫程式補上的。**

> 舉個現實案例，我看過一個平台團隊把自動修復做得很完整，護欄、審核、稽核全齊，然後上線一年之後才發現產品團隊從來沒有點開過那個頁面。那個系統的 SAR 分母有幾千筆，分子是十幾筆。這種我只能說「很棒！」，因為每一個零件都是對的 XD

## 那自主權是什麼的函數

Series 1 最後一天量到一件事：同一組題目，帶著錯環境的治理資產考 3.5 分，完全不帶治理資產考 2.5 分，而為那座環境寫的第一版 agent 考 5.5 分。當時的結論是`治理是環境的函數`。那句話留下一個沒答的問題：那自主權呢，它是什麼的函數。

這七天做下來，答案很具體，而且有點無情：**自主權是標註數量跟校準誤差的函數。** 不是模型大小的函數，不是護欄數量的函數，也不是「我把治理平面寫得多完整」的函數。程式碼那一側六天前就寫完了，該紅的地方昨天也全部證明會紅，而現在擋著 AUTO 的東西只有兩個數字：標註筆數（叢集 7 筆、開發機 35 筆，門檻 20，而且要分得清楚哪些算數）跟過度自信（真人標註算出來 +0.39，容忍值 0.1）。

這兩個東西的共同點是它們都只能靠時間累積。標註要有人真的看過一次判斷然後說對或錯，校準曲線要有足夠多不同信心區間的紀錄才畫得出形狀。買不到，也沒辦法靠寫程式繞過去，唯一能做的是把產生標註這件事的成本壓低，讓它在日常工作裡自然發生，而不是排一天專門去補。

從平台工程的角度，這句話其實是好消息。「自主權是掙來的」如果只是一句態度，它就會變成一場沒有終點的會議；而它一旦被寫成兩個有門檻的數字，它就變成一件可以排進 backlog 的工作，也變成產品團隊可以自己看的儀表板。誰擁有那個數字也就清楚了：平台團隊擁有門檻跟量法，服務團隊擁有那些標註，因為只有他們知道那次判斷對不對。

## 三十四天串起來的那條線

最後把整條因果鏈畫一次。這系列從第一天那隻拿了 4.5 分、對著空結果編數字的 agent 開始，每一階段做的事情其實都在同一條鏈上：

```mermaid
flowchart LR
    G["治理<br/>registry / policy / CI gate"] --> T["拓撲與契約<br/>可查詢、會對帳的系統模型"]
    T --> A["agent 建議<br/>假設樹、信心、可被檢查的答案"]
    A --> E["執行與治理平面<br/>授權層級、影響範圍、自動逆轉"]
    E --> C["校準與 SLO<br/>標註、CE、五個旗艦量尺"]
    C -.->|"回饋"| G
    C -.->|"回饋"| T

    classDef done fill:#d5f5e3,stroke:#27ae60,color:#145a32
    classDef partial fill:#fdebd0,stroke:#ca6f1e,color:#7e5109
    class G,T,A done
    class E,C partial
```

前三格是綠的，實際跑過、也被檢查過。後兩格是橘的，機制都在，但拿到的真實輸入少到不足以支撐結論。而那兩條虛線的回饋箭頭是這張圖裡最重要的東西，也是唯一一段目前完全靠我手動接起來的：CE 變差的時候，沒有任何機制會自己把信心門檻調高，也沒有任何機制會回頭說「拓撲對帳該重跑了」。書裡把政策能被 SLO 狀態反過來調整這件事放在 L4，現在看得出來為什麼。

這條鏈還有一個講法，是這三十四天真正的主張：**遙測的品質決定 agent 判斷的品質，而遙測的品質是治理做出來的。** 中間任何一環是靠人記得去做，這條鏈就斷在那裡，而斷掉的樣子通常不是報錯，是一個看起來很正常的數字。

## 今天沒做的事

- **那四個新門檻都沒有實驗支持。** `max_bin_gap` 0.25、`min_bin_count` 3、`min_band_accuracy` 0.7 全部是拍的，跟昨天那個 `dq_min_env_fit = 0.9` 一樣。要讓它們有根據，得先有幾百筆分佈在不同信心區間的標註。
- **`n_bins` 固定是 10 沒動過。** 等寬十箱在四十幾筆資料上太細，箱子空一半。要不要改成等頻分箱是另一個決定，而換分箱方式會讓所有數字動一次，跟今天的改動混在一起就分不出來了。
- **`describe()` 沒有接到 plugin 上。** 它現在只在 API 回應裡，而真正需要看到「你在看哪一顆」的是那個在 Grafana 頁面上按對錯的人。也沒有任何東西在檢查那三個 `aiops.db` 只該有一個。
- **兩份 store 沒有合併。** 那 7 筆真人標註跟 35 筆評分器標註要不要放在同一個池子裡算，是一個判斷題（來源、環境、評分模式都不一樣），不是搬資料的問題。我傾向不合併，理由跟 Series 1 最後一天那個結論一樣：那兩批標註量的是兩座環境上的事。
- **回填只補了這一次，沒有變成遷移的一部分。** 今天那支腳本是外掛的，下一個加上去的欄位會再踩一次同樣的坑，除非把「加欄位」跟「填已知的值」綁在同一個動作裡。
- **`grading_mode` 沒有辦法回頭認人工按的那批以外的東西。** 剩下 8 筆是沒標註過的，模式仍然是 NULL，而那是對的。
- **五個 SLO 沒有一個進儀表板。** 今天是一支腳本印文字，而書裡明確講這五個該跟服務的可用性、延遲放在同一頁，不要開一個「AI 指標」的專屬頁面。
- **`dq.py` 那個撞名的 docstring 沒改。** 改字很快，但要先決定 data quality 這件事在這套詞彙裡叫什麼。
- **SAR 的分母沒有修。** 「沒有人看過」要跟「人拒絕了」分開，那需要一個「被開啟過」的紀錄，而那要等 plugin 那頁真的有人用。

## 到手的四件事

最後一天總要清一次帳，所以先講真的換到手的東西，而它們都不是「agent 變強了」。

**這套系統第一次能講出自己現在幾分。** 五個旗艦 SLO 現在有取數的路徑，數字全部難看，但難看跟量不出來是兩件不同的事。三十四天前我對這隻 agent 的描述只能是「它今天怪怪的」，現在我能說 ARR 0%、真人標註上的過度自信 +0.39、說 0.82 的那三筆判斷回頭看是三次全錯。後面這三句是可以排工作的，前面那句不是。

**階梯上的位置從猜變成有證據。** 信任天花板要求四個機制同時到位，而其中三個現在都有真實軌跡當憑據：政策擋過一次單副本的回滾、乾跑算出 25→24 那兩個 pod、自動逆轉真的被觸發過（雖然它也 401 了）。缺的是被校準過的信心，而它是四個裡面唯一不能靠寫程式補上的。

**標註管線接到了對的那一邊。** 今天那行 UPDATE 讓叢集裡唯一的七筆真人標註不會在下次部署時被自己的 fail-closed 設計擦掉，而它們一進場就給出一個比任何結論都有用的形狀：決定要不要放手的那一格，正好是這隻 agent 最不可信的一格。

**那道門終於在讀那張圖。** `bin_evidence()` 把分箱接上授權判斷之後，Day31 那個 `-0.0029` 的綠燈變成一句講得出區間、筆數跟差距的紅燈。這是這三十四天唯一一次，一個早就算出來的數字改變了系統的行為。

## 還缺什麼才敢叫完整的 AIOps

這一段照系統的層級寫，不照天數寫。上面那份「今天沒做的事」是這一天欠的，下面這些是這整套東西欠的。

- **Act 那一格從來沒有成功過一次。** `executions` 表只有一列，`success=0`，而那次失敗是 Kubernetes 回的 401。`execute → verify → settle window → 驗證失敗自動回滾` 的後半段至今沒有被真實輸入走過，而 ARR、DQ-SLO、AE-SLO 三個 SLO 全部卡在這裡：不是量不準，是分母是零。一個沒有成功執行過的自動修復系統，嚴格講目前只是一個提案系統。
- **學習迴圈只接上了管線，還沒有累積。** 42 筆標註來自三個 fixture，門檻的單位是 run 而真正該問的是獨立事故數，這兩個數字差一個數量級。更關鍵的是沒有任何機制讓 SLO 惡化自己收緊自主權：CE 上升的時候信心門檻不會自己升上去，DQ 掉下來的時候也不會自動多送幾件給人看。error budget 這件事現在連手動版本都沒有。
- **觀測基礎撐不起長時間的事故。** Tempo 只留一小時，所以超過一小時的事故，「去抓那條 trace」這一步結構上必失敗；指標沒有 pod 級的 label，多 replica 的 counter 疊成一條 series，`rate()` 會生出憑空的流量；評測只有一顆種子，同一份程式碼連跑三次分數會在 2.5 到 3.5 之間跳。這三件都不是 agent 的問題，但它們決定了 agent 的天花板。
- **沒有一個使用者不是我。** SAR 那個 23.1% 的三個核准者全是我在測試，`rejected` 是 0 因為那顆按鈕還沒接起來，分母裡十筆是沒有人打開過的。整套治理最核心的假設「錯誤訊息夠好，對方就能自己修好」，從來沒有被第二個團隊驗證過。這一項我覺得是最被低估的。
- **這套東西自己的運維可靠性。** 那張憑證失效了 46 天沒有任何地方會說；過期是被動的，沒人來按就永遠顯示成待處理；認領到一半死掉的那列沒有回收機制；`rollback_failed` 同時代表最安全跟最危險的兩種結果；而冪等把「試了但沒發生」當成「發生了」，一次暫時性失敗就讓那個事故永遠不能重試。這些坑的護欄腳本一支都沒進 CI，全部是「我記得跑」。
- **環境相依只擋得住自主權，擋不住那份錯的知識。** 昨天那個契合度已經會在提案上說「這座環境只有 0/16 的知識叫得動」，並且把 AUTO 收回來，這一格算是從結論變成了機制。但 catalog 還是照樣被注入，agent 拿著錯的名字去查、照樣拿回空結果，只是它的建議不會被自動執行。而那個 0.9 的地板是拍的，也沒有歷史，一次掉下來跟連續三天掉下來目前是同一件事。
- **規模、協作、成本都還是單點。** 一隻 agent、一個 namespace、五個服務。多 agent 之間怎麼交接、稽核帳本怎麼變得不可否認，這些連碰都沒碰。成本只量到單次調查兩萬六千個 token，沒有任何東西會在告警風暴的時候說「這一小時已經燒掉多少」。

七項底下其實是同一個東西。這三十四天最貴的坑，幾乎沒有一個是某個零件寫錯：Weaver 的假綠燈是路徑跟預期的縫、洩題是 prompt 跟評測的縫、守門看不到三分之一的 trace ID 是兩種 ID 格式的縫、昨天那個 401 是應用層治理跟叢集憑證的縫、今天這個是兩份同名資料庫的縫。**每個零件都對、都有測試、都有人擁有，壞的一直是零件中間那條沒有人擁有的接縫。** 而接縫不會報錯，它只會安靜地給你一個看起來很合理的數字。

所以再往下要的東西，我覺得不是再多一道護欄，是跨零件的端到端檢查，而且它要能自己定期跑。「這條路端到端真的通嗎」這個問題，這三十四天一直是我手動在扮演那個會問的人，而手動扮演的角色遲早會停。

務實一點的順序大概是這樣：先把那顆按鈕接起來讓標註在日常裡自然長出來，再把那幾支護欄腳本放進 CI，這兩件做完，才有資格談讓執行成功一次。

## 小結

總結來說，今天算出來的五個數字沒有一個能拿去證明這套東西有效：兩個是真的 0，一個分母是空的，一個 n=1，剩下那個在兩份資料上給出相反的結論。而這件事本身就是這三十四天最實際的收穫，因為三十四天前我連「該量什麼」都講不出來，只能說「它今天怪怪的」。

今天真正動到系統的只有三段很小的東西：一行把七筆標註的評分模式填回去的 UPDATE、一支回報「你在讀哪一顆 store」的 `describe()`、還有一支把分箱接上授權判斷的 `bin_evidence()`。三段都沒有讓任何分數變好，換到的是那批標註不會在下次部署時安靜消失、那三個同名檔案變得看得見，以及那道門不會再被一個抵銷出來的平均數騙過去。**這三十四天做的所有事情裡，價值最接近這一行的那一類，全部長得像這樣：讓一個已經存在的事實不要在傳遞的過程中掉掉。**

至於這座系統卡在 L2 到 L3 那個交界這件事，卡住的原因不是程式碼沒寫，是證據還沒累積。所以我最想留給下一個接手的人的，不是那些 policy 跟契約，是這句話：agent 的自主權不是設計出來的，是被別人的判斷一筆一筆餵出來的。

> 三十四天的最後一個修改是一行 UPDATE，改了七列，一個分數都沒變好。
> 我本來覺得這樣收有點寒酸，後來想想這三十四天最好用的東西幾乎都是這種大小 XD
> 謝謝看到這裡的你，如果只帶走一句，就帶走那句「你不需要相信它，你可以查它」。
