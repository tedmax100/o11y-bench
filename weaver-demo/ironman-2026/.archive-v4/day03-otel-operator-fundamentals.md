---
title: "【Day3】OTel Operator 基礎概念：從一次性部署到持續調和"
series: "2026 鐵人賽：AIOps with OpenTelemetry"
tags: [OpenTelemetry, Kubernetes, 鐵人賽]
---

# Day3：OTel Operator 基礎概念——從一次性部署到持續調和

Day1 那個壞味道三，講的是 `payment` 團隊自己包了一層 SDK wrapper，之後 semantic convention 就再也沒升級過；根因是**每個服務的 OTel 設定都是各自安裝、各自維護的，沒有一個中央機制去管**。今天要開始處理這個根因，但不急著動手裝東西——先花一天，把「Kubernetes Operator」這個概念本身講清楚。因為 Day4 要做的所有操作，如果沒有先理解 Operator 在解決什麼問題，看起來就只會是一堆 `kubectl apply` 的變形，感受不到它跟「手動貼一堆 YAML」到底差在哪裡。

## 先回到最直覺的做法：手動貼 YAML，哪裡不夠用

假設你是平台團隊的一員，今天要幫一個新服務接上 OTel。最直覺的做法，是寫一份 Collector 的 Deployment YAML，`kubectl apply` 上去，服務就能把資料送到 Collector。這個動作本身沒有問題——它確實會把 Collector 跑起來。

問題出在「後續」。過幾天，公司決定把 Collector 的 batch size 調整一下、換一個 exporter 設定，你要嘛手動改一次 YAML 再重新 apply，要嘛寫一支腳本幫你重複這件事。再過一陣子，新加入的服務也要接 OTel，你又得重複一次「寫 YAML、apply、確認跑起來」的流程。每一次變動，都得靠一個人（或一支腳本）**主動去做一次性的動作**，而 Kubernetes 集群本身，並不知道「這個 Collector 應該長什麼樣子」這件事——它只知道你曾經 apply 過一份 Deployment，至於這份 Deployment 之後有沒有被誰手動改壞、Pod 是不是被意外刪掉了，集群不會自己去把它修回你原本想要的樣子。

這就是「一次性部署」的侷限：你做的是**下指令**，不是**表達期望**。而 OTel 在企業內真正麻煩的地方，恰好都不是一次性的事——Collector 設定要跟著流量規模調整、sidecar 要注入到每一個新起的 Pod 裡、SDK 版本要在幾十個服務之間逐步推進——這些全部都是需要**持續**維護、而不是做一次就結束的工作。

## Operator pattern：把「期望狀態」寫下來，讓系統自己去逼近它

Kubernetes Operator 想解決的正是這個問題。它的核心想法可以拆成兩個角色：

**CRD（Custom Resource Definition）**：讓你能定義一種新的 Kubernetes 資源類型，用來描述「我期望的狀態是什麼」。這份描述本身只是一段宣告式的 YAML——它不是一個指令，而是一句「我要的東西長這樣」的陳述句。

**Controller**：一個持續在背景跑的迴圈，職責是不斷去比對「這份 CR 描述的期望狀態」跟「集群裡實際的狀態」，一旦兩者出現落差，就自動採取行動去把實際狀態拉回期望狀態。這個持續比對、持續修正的過程，叫做 **reconciliation loop（調和迴圈）**。

這跟手動 `kubectl apply` 最根本的差異，不在於「YAML 變成了另一種格式的 YAML」，而在於：一次性 apply 是「我現在下一個指令，指令執行完，這件事就結束了」；Operator 則是「我描述一個我要的世界長什麼樣子，接下來不管發生什麼——Pod 被刪了、設定被誰手動改了、新的 Pod 起來了——都會有一個持續在跑的東西，把世界拉回我要的樣子」。前者是離散的動作，後者是一個永遠不會停下來的承諾。

拿 Day1 的壞味道三來對照，就能看出這個模型解決的正是那個根因：如果 SDK 版本、Collector 設定是靠每個團隊各自手動維護的，版本漂移只會隨時間越拉越開，因為沒有人被指派持續盯著這件事；但如果 SDK 版本、Collector 設定，是被平台團隊寫成一份 CR、由一個 controller 持續調和著，版本漂移這件事，理論上從一開始就不會有機會發生——因為調和迴圈本身就在持續把狀態拉回平台團隊定義的規範。

## 調和迴圈實際上長什麼樣子

上面講的「持續比對、持續修正」聽起來有點抽象，拿真實的 OTel Operator 原始碼對照一下，會具體很多。它的 controller 裡，`Reconcile()` 這個函式，大致是這樣一個流程：先從 k8s 讀出這份 CR 目前寫的期望狀態；如果讀不到（代表 CR 已經被刪除），就直接結束，交給 k8s 的垃圾回收機制去清理子資源；接著根據這份期望狀態的 spec，在記憶體裡「算出」一份「應該存在哪些 k8s 資源」的清單（該有一個什麼樣的 Deployment、什麼樣的 Service、什麼樣的 ConfigMap）；再去 cluster 裡查一次「目前實際存在哪些屬於這份 CR 的資源」；最後把這兩份清單拿去比對，該建立的建立、該更新的更新、該刪除的刪除。

這裡有一個容易被忽略、但很重要的性質，叫**冪等性（idempotency）**：這整套 `Reconcile()` 邏輯，必須能被重複呼叫任意多次，而且每次呼叫的結果都要一樣。這不是一個可有可無的加分項，而是這個模型能不能成立的前提——因為 Kubernetes 從來不保證某個事件只會觸發一次 `Reconcile()`：網路抖動會讓同一個事件重試好幾次，Operator 本身重新啟動時，也會對集群裡所有既存的 CR 重新跑一次完整的 Reconcile。如果邏輯寫成「執行一個一次性的步驟」（例如「建立一個 Deployment」，重複執行就會出錯），這整個模型立刻就會垮掉；只有邏輯寫成「持續讓現狀逼近期望狀態」（用「先查現在有沒有，沒有才建立，有就檢查有沒有改動」的方式寫），重複呼叫才會永遠得到同一個結果。這也是為什麼 Operator 官方文件會特別強調：`ctrl.Result` 這個回傳值，本身也是為了配合這套「反覆執行」的設計而存在的——它可以說「這次沒事，等下一次事件再處理」，也可以主動要求「馬上重新跑一次」，或是「隔幾秒後重新跑一次」，讓整個系統即使某一步失敗了，也有機制自己回頭再試一次，而不需要一個人手動介入。

還有一個小細節，能讓「持續調和」這句話變得很具體：controller 在啟動時，不只監聽這份 CR 本身的變化，也會同時監聽它建立出來的所有子資源（Deployment、Service……）。這代表如果有人手動跑了一次 `kubectl delete deployment` 把 Operator 建立的 Collector Deployment 刪掉——這是真實團隊裡常發生的意外，可能是清理環境時手滑、也可能是另一個自動化腳本誤刪——Operator 會立刻偵測到「這個子資源不見了」，觸發一次新的 Reconcile，把它重新建回來。這正是「持續調和」跟「一次性 apply」最直觀的差異：後者被手動改動之後，除非有人發現、有人再跑一次指令，否則現狀就會一直錯下去；前者的錯誤，會在下一輪迴圈裡自動被修正。

## 冪等性不是抽象原則，是真代碼裡的具體選擇

上面講的「冪等性」聽起來像一句公理，但真代碼裡有一個具體例子，能讓這件事變得很扎實。OTel Operator 的 `Reconcile()`（`internal/controllers/opentelemetrycollector_controller.go:265` 附近）裡有一段升級邏輯，大致是：如果偵測到這份 CR 需要升級（例如 Operator 版本换了，CR 裡存的 spec 格式是舊版的），就先執行升級動作，然後回傳 `ctrl.Result{Requeue: true, RequeueAfter: 1 * time.Second}`——而不是接著往下用「升級前讀到的那份 instance」繼續跑後面的 BuildCollector、reconcileDesiredObjects。

這個「升級完，寧可提前結束這一輪、一秒後重新跑一輪全新的 Reconcile」的設計，正是冪等性在真實程式碼裡長出來的樣子：因為升級動作本身已經改動了 CR 的內容，如果嫌麻煩、想省一輪，直接拿著記憶體裡那份「升級前」的舊 instance 繼續算後面的步驟，算出來的「期望狀態」就會是錯的——用舊 spec 建出來的 Deployment，並不是升級後真正該有的樣子。與其在同一輪裡小心翼翼地手動同步這份資料，不如直接讓下一輪 Reconcile 重新從 k8s 讀一次最新狀態，邏輯永遠只需要相信「這一輪讀到的就是當下最新的期望狀態」。這也是為什麼冪等性從來不是「加分項」，而是這整個模型唯一站得住腳的寫法。

同一份 Reconcile 裡還有一個容易被忽略、但同樣重要的細節：**Finalizer**。像 `ClusterRole`、`ClusterRoleBinding` 這種 cluster-scoped 的資源，沒辦法像 Deployment、Service 那樣掛 `ownerReference`（ownerReference 只能指向同一個 namespace 裡的物件），也就沒辦法單純依賴 k8s 內建的垃圾回收機制，在 CR 被刪除時自動連帶刪掉它們。Operator 的作法，是在 CR 上加一個 Finalizer 標記——這等於告訴 k8s：「使用者說要刪這個 CR 沒問題，但先別真的刪，等我自己先把這些 cluster-scoped 的子資源清乾淨，再放行讓你刪掉它」。這也解釋了一個常見的疑惑：如果讀者自己動手做 Day4、Day10 的練習時，`kubectl delete otelcol` 之後物件卡在 `Terminating` 遲遲不消失，很可能不是哪裡壞了，而是 Finalizer 正在等 Operator 完成清理——這正是「持續調和」延伸到「刪除」這件事上該有的樣子：連刪除都不是一次性動作，而是要等調和迴圈確認清乾淨了才算數。

## OTel Operator 在集群裡扮演的兩個角色

把這套模型套到 OpenTelemetry 上，OTel Operator 主要引入兩種 CR，各自負責不同的事，這張分工圖值得先記住，Day4 會逐一對照真實的 YAML 欄位：

**`OpenTelemetryCollector` CR**：描述「我要一個什麼樣的 Collector 實例」——用什麼 exporter、什麼 pipeline、部署成 sidecar 還是 daemonset 還是獨立的 gateway。Controller 看到這份 CR，就會去建立、維護對應的 Collector Deployment/StatefulSet，並持續確保它符合這份描述。這是負責「**部署行為**」的那一半。

**`Instrumentation` CR**：描述「符合什麼條件的 Pod，應該被自動注入哪一種語言的 auto-instrumentation」。這份 CR 不會自己直接改 Pod，而是被一個 admission webhook 拿去用——當一個新 Pod 帶著特定 annotation 被建立時，webhook 會攔截這個建立請求，依照 `Instrumentation` CR 裡的規則，把 instrumentation 的 init container、環境變數注入進 Pod spec 裡，再放行讓它真正建立。這是負責「**注入行為**」的那一半。

這兩者合起來，才是「中央化」這件事真正的樣子：`OpenTelemetryCollector` 讓資料的收集端不再是每個團隊各自手動維護的 Deployment，`Instrumentation` 讓每個新服務、每個新 Pod，不需要工程師手動改程式碼加 SDK，就能自動被套上平台團隊訂好的 instrumentation 規則與版本。兩者都是同一套 Operator pattern 的具體實例：平台團隊描述期望狀態，controller 跟 webhook 負責持續讓現實逼近它。

## 為什麼「注入」比「教會每個團隊寫 OTel」更划算

`Instrumentation` CR 這個機制，值得單獨停下來想一層：它解決的其實不只是「少改幾行程式碼」的方便，而是平台工程（Platform Engineering）想要**大規模**推行 OTel 埋點時，一個真正的成本問題。

如果沒有 auto-instrumentation，平台團隊想讓全公司的服務都有 OTel 資料，走的是「出一個 SDK/library，請大家自己接上去」這條路。這條路要成立，隱含了兩個代價，而且都不小：第一，每個開發團隊（PG）都得先**認識 OTel API + SDK**——怎麼建立 tracer、怎麼開 span、屬性要怎麼設、跟現有框架怎麼整合——這是實實在在的額外認知負擔，而且是疊加在每個團隊、每個工程師身上的重複成本，不是平台團隊付一次就結束。第二，平台團隊自己的成本也不會少：光是出一份 library、寫一份文件，通常不夠讓幾十個團隊真的動起來，往往還得**到處巡迴演講、辦教育訓練、一個團隊一個團隊去盯進度**，才能讓推行速度不要卡死在「沒空看文件」這一關。這兩筆成本，一筆壓在使用端，一筆壓在平台端，合起來就是「手動接 SDK」這條路真正的價格。

Auto-instrumentation 把這個等式整個換掉：`Instrumentation` CR 加上 admission webhook，讓一個 Pod 只要帶對 annotation，就能在建立的當下被自動注入對應語言的 instrumentation agent，開發團隊完全不需要碰 OTel API，服務本身的程式碼一行都不用改。平台團隊要做的事，從「說服/教會每一個團隊」，變成「把 `Instrumentation` CR 設定好、把 annotation 規範公告出去」——這是一次性的宣導成本，跟團隊數量無關，不會隨著公司規模線性成長。這正是 Operator pattern 在「注入行為」上真正想解決的問題：不是省幾行 code，而是把「讓所有服務有一致的監測能力」這件事，從一件需要跟每個團隊逐一博弈的社交工程，變成一個平台團隊可以自己說了算、系統性推行的機制。

這裡也剛好呼應 OTel 官方部落格〈[Don't Wrap OpenTelemetry](https://opentelemetry.io/blog/2026/dont-wrap-opentelemetry/)〉那篇文章的立場：文章反對的是另一條常見的「省認知負擔」捷徑——平台團隊自己包一層 wrapper library，把 OTel API 藏在裡面，讓開發團隊覺得「簡單」。但文章指出，這條路換來的往往是反效果：wrapper 常常會意外破壞 OTel API 刻意做的效能設計（例如 1-3 個屬性時的零記憶體配置），如果 wrapper 內部又用 name 快取 instrument，還會多引入 dictionary lookup、hashing、甚至 mutex lock，讓本該是熱路徑的量測呼叫變成序列化瓶頸；更長期的問題是，開發團隊學會的是「你們公司的 wrapper」，不是 OTel 本身，換團隊、換公司、甚至只是要看懂官方文件時，這份知識完全遷移不過去，而 wrapper 本身還要平台團隊持續跟著 OTel 上游演進去維護，是一筆永遠還不完的技術債。這正是 Day1 壞味道三裡 `payment` 團隊自己包 SDK wrapper、之後再也沒跟上 semantic convention 的那個根因——換句話說，「包一層 wrapper 讓大家好上手」跟「自動注入不用大家碰 API」，表面上都在講「降低使用門檻」，但走的是完全相反的兩條路：前者製造了一個需要平台團隊自己維護、還會跟真實標準漂移的分岔版本；後者則是讓大家直接用官方標準的 SDK，只是連「接上去」這個動作本身都不用開發團隊自己做。

這也是為什麼官方文章給的建議，跟 Operator/Weaver 這條路線完全對得上：與其寫 runtime wrapper，不如提供「SDK 初始化的共用設定」（exporter、sampling、resource attribute 這些基礎設施層的東西），真正需要治理埋點規範的地方，交給 compile-time 的程式碼產生工具去做型別安全的產出——文章點名的正是 OTel Weaver，這正是這系列 Day11 之後要細講的東西：Instrumentation CR 負責讓服務「有」OTel 資料，Weaver 負責讓這份資料「符合規範」，兩者分工，都不需要靠一層 wrapper 去達成。

這系列接下來會反覆碰到的，就是這兩個 CR；但實際上 OTel Operator 一共提供四種 CR，另外兩個先簡單提一下讓你有印象就好，不會深入：`TargetAllocator` CR 處理的是「多個 Collector 副本要怎麼分工去抓 Prometheus 的 scrape target，避免每個副本重複抓同一批指標」；`OpAMPBridge` CR 則是透過 OpAMP 這個協議，讓 Collector 的設定可以被遠端動態下發、甚至遠端觸發版本升級，而不必每次都回頭改 CR 再讓 controller reconcile 一次。這跟前面講的「升級要 `Requeue` 重跑一輪」剛好是同一件事的兩種規模：CR 層級的升級，是平台團隊改一次 YAML、讓 controller 的 reconcile loop 去追平；而 OpAMP 要處理的，是「不透過 k8s API、直接跟正在跑的 Collector process 對話」這條路——中控端可以直接下發新設定、甚至指示 Collector 去下載並切換到新版本二進位檔，跳過「改 CR → controller 感知 → 重建 Pod」這整套流程，適合 Collector 部署在 k8s 之外（例如 edge、VM）、沒有 CR 可以改的場景。兩者處理的都是「規模變大之後」才會浮現的進階問題，今天不用記住細節，只需要知道：Operator 這個模式一旦成立，能長出來的 CR 不會只有「部署」跟「注入」這兩種，任何一種需要持續被維護的運維知識，都可以照樣被包成一個新的 CR、配一個新的 controller——版本管理與升級，只是恰好在 CR 層跟 OpAMP 層都各自長出了一套對應的機制。

## 為什麼這件事對治理這麼重要

先回到治理這件事本身。治理最怕的不是「規則沒訂好」，而是「規則訂了，卻沒有東西持續在檢查現實有沒有符合規則」——Day1 的壞味道三，根因就是這個：`payment` 團隊當年的決定在那個當下是對的，但沒有任何東西持續在盯著「這個決定，三年後還符不符合現在的規範」。Operator 的冪等調和迴圈，補的正是這個洞：規則一旦寫成 CR，不再是「訂了就結束」，而是有一個東西永遠在背景比對「現實有沒有偏離這份規則」，偏離了就自動修正。這代表治理從「靠人記得去檢查」變成「系統結構上就不允許長期偏離」——這跟 Day11-12 要講的 `weaver check`／CI Gate 其實是同一個治理邏輯的兩個層面：Weaver 攔的是「這次 PR 裡的遙測資料違不違規」，是程式碼進來的那一刻做一次性檢查；Operator 攔的是「這個服務跑起來之後，它的 SDK 版本、Collector 設定，有沒有偷偷跟規範脫節」，是跑起來之後持續在檢查。少了 Operator 這一層，Weaver 檢查得再仔細，也只能保證「當初合併的那一刻是乾淨的」，管不到合併之後、系統持續運行中慢慢長出來的漂移。

這個漂移的風險，不是只有「服務端 SDK 版本」會遇到，Collector 本身也一樣會老化。`OpenTelemetryCollector` CR 裡通常會指定一個 image tag（也就是版本號），而這個版本號，同樣不會因為時間過去就自己跟著升級——如果平台團隊沒有主動去改這份 CR 的版本欄位，某個團隊的 Collector 就會永遠停在部署當下的那個版本，即使上游已經修了資安漏洞、修了某個 exporter 的 bug、甚至改了某個 semantic convention 的預設行為。更麻煩的是，一旦有十幾個團隊各自維護各自的 `OpenTelemetryCollector` CR，「每個 CR 裡寫的版本號是不是都一致」這件事本身，就會慢慢變成一個沒人管的問題——跟 Day1 壞味道三的 SDK 版本漂移，其實是同一個根因換了一個位置重演一次：不是 SDK 沒人管，而是換成 Collector 沒人管。

這也是為什麼 Operator 補的這個洞，不能只看「有沒有 Operator」，還要看「Operator 管理的這份規範本身，有沒有統一被治理」：Operator 能保證的是「CR 寫的是什麼版本，cluster 裡實際跑的就是什麼版本」——調和迴圈確保不會漂移；但「CR 裡寫的版本號，該不該全公司統一成同一個」，是治理要回答的另一個問題，Operator 本身不會替你決定。這正是前面提到 `OpAMPBridge` 的地方留下的伏筆：如果版本升級只靠「平台團隊一個一個去改每份 CR 的版本欄位」，統一升級這件事，規模一大就會變回一次性的手動作業；而 OpAMP 這條路徑，是讓中控端可以直接對所有正在跑的 Collector 下發「切到某個版本」的指令，把「全公司 Collector 版本要不要統一、什麼時候统一升級」這個治理決策，從「靠人一份一份改 CR」，變成「有一個機制可以一次性、可稽核地推行下去」。換句話說：Operator 的調和迴圈保證「寫下去的規範不會被現實悄悄偏離」，但「規範本身該長什麼樣子、要不要統一」，仍然是治理要先決定的事——這兩件事合起來，才是完整的版本治理。

## 這對後面的 AIOps agent 有什麼幫助

再往後看，這件事對 Signal Plane、對 agent 能不能做出可信判斷，也有直接關係，只是比較間接、容易被忽略。

第一層關係，是**資料本身的可信度**。前面 Day2 講過，agent 要做判斷，前提是資料語意一致、可信；Operator 持續調和這件事，保證的正是「資料的產生方式（SDK 版本、Collector pipeline）本身，長期維持在同一個已知的規範下」，而不是每個服務各自漂移到不知道哪個版本。少了這一層，就算 Day8-18 把 semantic convention 定義得再漂亮，實際跑在 cluster 裡的服務，遲早會因為沒人管而慢慢偏離這份定義——Operator 是讓「定義」跟「現實」不會隨時間拉開距離的機制。

第二層關係，更直接：controller 為了做 `Owns()` 監聽，本身就必須維護一份「這個 CR 擁有哪些子資源」的關係——這其實已經是一份**依賴/擁有關係圖**的雛形，只是目前只在 Operator 內部用來判斷「要不要重新調和」，還沒有被當成一份可以輸出、可以被 agent 讀取的訊號。Day19 要畫 `topology.py` 的服務拓撲圖時，這正是一個值得回頭問的問題：Operator 已經知道的這份結構關係，能不能也變成 Signal Plane 的資料來源之一，而不是每次都要 agent 自己從 trace 裡反推拓撲。

第三層關係，跟「信心分數」有關。CR 本身有一個 `status` 欄位，記錄的是「controller 上一次調和之後，現實跟期望狀態是不是一致、有沒有卡在某個步驟」——這其實已經是一段**結構化、機器可讀的狀態描述**，而不是給人看的一張圖。對照 Day2 講的「決策級遙測」，這正是那個方向該有的樣子：不是一條給人看的儀表板曲線，而是一段直接回答「現在系統符不符合期望狀態」這個具體問題的訊號，agent 可以直接讀這個欄位，作為判斷「這次事故，會不會是因為某個服務的 instrumentation 設定，其實從來沒有被成功套用」的其中一條線索。

## 今天不碰 cluster，先把詞彙站穩

今天完全不會跑任何 `kubectl` 指令——這是刻意的。這幾個詞（CRD、Controller、Reconciliation loop、冪等性、`OpenTelemetryCollector` CR、`Instrumentation` CR）先在腦子裡站穩，明天 Day4 才會回到真實 cluster，把 Operator 裝起來，逐欄位對照 `kubectl get otelcol,instrumentation -o yaml` 印出來的內容，指認哪些欄位屬於「部署行為」、哪些屬於「注入行為」。今天要記住的只有一句話：**Operator 解決的不是「怎麼把 YAML 寫得更好」，而是把「持續維護」這件原本壓在人身上的責任，轉移成一個系統裡永遠在跑的迴圈**。
