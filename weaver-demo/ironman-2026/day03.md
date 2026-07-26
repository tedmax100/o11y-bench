---
title: "【Day3】OTel Operator：把「持續維護」從人身上搬到迴圈裡"
series: "2026 鐵人賽：AIOps with OpenTelemetry"
tags: [OpenTelemetry, Kubernetes, GitOps, 鐵人賽]
---
# Day3：OTel Operator——把「持續維護」從人身上搬到迴圈裡

Day1 那個壞味道三，講的是 `payment` 團隊自己包了一層 SDK wrapper，之後 semantic convention 就再也沒升級過。根因不是那個團隊懶，是**每個服務的 OTel 設定都是各自安裝、各自維護的，沒有一個中央機制持續盯著它**。

今天處理這個根因。順序是：先把 Operator pattern 在解決什麼問題講清楚（不然後面所有操作看起來都只是 `kubectl apply` 的變形），再真的裝一次、把手寫的 Collector 換成 CR、逐欄位對照真實輸出，最後把這些 CR 收進一個 GitOps 入口——因為治理資產如果不能被 review，它就只是某台機器上的狀態。

程式碼在範例 repo [`OTel_AIOps_Agent`](https://github.com/tedmax100/OTel_AIOps_Agent) 的 [`day04/`](https://github.com/tedmax100/OTel_AIOps_Agent/tree/main/day04) 與 [`day06/`](https://github.com/tedmax100/OTel_AIOps_Agent/tree/main/day06)（**那邊的資料夾沿用原本的日號，沒有跟著文章重編**——它們是每一天當下那組 stack 的完整快照，重編會失去時間順序）。

## 手動貼 YAML，哪裡不夠用

假設你要幫一個新服務接上 OTel。最直覺的做法是寫一份 Collector 的 Deployment YAML，`kubectl apply` 上去。這個動作本身沒問題——它確實會把 Collector 跑起來。

問題出在「後續」。過幾天要調 batch size、換 exporter 設定，你得手動再改一次；新服務要接進來，又得重複一次「寫 YAML、apply、確認起來了」。每一次變動，都靠一個人**主動去做一次性的動作**，而集群本身並不知道「這個 Collector 應該長什麼樣子」——它只知道你曾經 apply 過一份 Deployment。之後這份 Deployment 被誰手動改壞、Pod 被意外刪掉，集群不會自己修回你原本要的樣子。

這就是「一次性部署」的侷限：**你做的是下指令，不是表達期望。** 而 OTel 在企業內真正麻煩的地方，恰好都不是一次性的事——Collector 設定要跟著流量規模調整、sidecar 要注入到每一個新起的 Pod、SDK 版本要在幾十個服務之間逐步推進。

## Operator pattern：宣告期望，讓迴圈去逼近它

Operator 把這件事拆成兩個角色。**CRD** 讓你定義一種新的資源類型，用來描述「我期望的狀態」——它是陳述句，不是指令。**Controller** 是一個持續在背景跑的迴圈，不斷比對「CR 描述的期望」跟「集群裡的實際狀態」，有落差就自動修正，這叫 **reconciliation loop**。

差別不在「YAML 變成另一種 YAML」，而在：一次性 apply 是「我下一個指令，執行完就結束」；Operator 是「我描述一個我要的世界，接下來不管發生什麼，都有一個東西持續把世界拉回這個樣子」。**前者是離散的動作，後者是一個不會停下來的承諾。**

這裡有一個性質決定了整個模型能不能成立：**冪等性**。`Reconcile()` 必須能被重複呼叫任意多次而結果一致——因為 Kubernetes 從來不保證一個事件只觸發一次：網路抖動會重試、Operator 重啟時會對所有既存 CR 重跑一輪。所以邏輯不能寫成「建立一個 Deployment」（重複執行就出錯），只能寫成「先查現在有沒有，沒有才建、有就比對有沒有偏移」。

真代碼裡有一個很扎實的例子。OTel Operator 的 `Reconcile()` 偵測到 CR 需要升級時，會先執行升級，然後直接回傳 `ctrl.Result{Requeue: true, RequeueAfter: 1 * time.Second}`——**寧可結束這一輪、一秒後重跑一輪全新的**，而不是拿著記憶體裡那份「升級前」的 instance 繼續往下算。因為升級動作本身改動了 CR 的內容，繼續用舊 spec 算出來的「期望狀態」是錯的。與其在同一輪裡小心同步，不如讓下一輪重新從 k8s 讀一次最新狀態——邏輯永遠只需要相信「這一輪讀到的就是當下最新的期望」。

還有一個細節讓「持續」變得具體：controller 不只監聽 CR 本身，也監聽它建立出來的所有子資源。有人手滑 `kubectl delete deployment` 把 Collector 刪掉（真實團隊裡常發生），Operator 會立刻偵測到並重建。**一次性 apply 的錯誤會一直錯下去直到有人發現；調和迴圈的錯誤會在下一輪自動修正。**

順帶提一個讀者自己動手時會撞到的東西：`kubectl delete otelcol` 之後物件卡在 `Terminating` 不消失，通常不是壞了，是 **Finalizer** 在等 Operator 清掉那些沒辦法掛 `ownerReference` 的 cluster-scoped 子資源（`ClusterRole` 之類）。連刪除都不是一次性動作，要等調和確認清乾淨才算數。

### 兩個 CR，兩種行為

| | `OpenTelemetryCollector` | `Instrumentation` |
|---|---|---|
| 描述什麼 | 我要一個什麼樣的 Collector 實例（exporter、pipeline、sidecar／daemonset／gateway） | 符合什麼條件的 Pod 該被注入哪一種語言的 auto-instrumentation |
| 誰執行 | controller 建立並維護 Deployment/StatefulSet | **admission webhook** 在 Pod 建立的當下改寫它的 spec |
| 負責 | **部署行為** | **注入行為** |

Operator 一共有四種 CR，另外兩個今天只需要有印象：`TargetAllocator`（多個 Collector 副本怎麼分工抓 Prometheus target）、`OpAMPBridge`（透過 OpAMP 協議遠端下發設定、遠端升級，適合 Collector 跑在 k8s 之外）。它們處理的都是「規模變大之後」才浮現的問題。

## 為什麼「注入」比「教會每個團隊寫 OTel」划算

`Instrumentation` CR 值得單獨停一層，因為它解決的不是「少改幾行程式碼」，是一個平台工程的成本問題。

沒有 auto-instrumentation 的話，平台團隊走的是「出一個 library，請大家自己接上去」。這條路有兩筆代價：**每個團隊都得先認識 OTel API/SDK**（怎麼建 tracer、怎麼設屬性、怎麼跟框架整合）——這是疊加在每個團隊身上的重複成本，不是平台付一次就結束；而**平台團隊自己的成本也不會少**，光出一份 library 加文件通常不夠讓幾十個團隊動起來，還得巡迴演講、辦訓練、一個團隊一個團隊盯進度。

auto-instrumentation 把等式換掉：Pod 帶對 annotation 就在建立的當下被注入，服務程式碼一行都不用改。平台團隊要做的事從「說服／教會每一個團隊」變成「把 CR 設好、把 annotation 規範公告出去」——**一次性的成本，跟團隊數量無關**。這是這系列第一次出現的平台工程判準，後面會反覆用到：**一個機制的成本會不會隨團隊數線性成長。**

這裡也順便講清楚一條岔路。OTel 官方那篇〈[Don't Wrap OpenTelemetry](https://opentelemetry.io/blog/2026/dont-wrap-opentelemetry/)〉反對的是另一種「降低門檻」的做法：平台團隊自己包一層 wrapper library 把 OTel API 藏起來。文章指出的代價很具體——wrapper 常會破壞 OTel API 刻意做的效能設計（1-3 個屬性時的零記憶體配置），若內部又用 name 快取 instrument，還會引入 hashing 甚至 mutex，讓熱路徑變成序列化瓶頸；更長期的問題是團隊學會的是「你們公司的 wrapper」而不是 OTel，知識遷移不過去，而 wrapper 本身要平台團隊永遠跟著上游維護。

**「包一層 wrapper」跟「自動注入」表面上都在講降低門檻，走的卻是相反的方向**：前者製造一個需要自己維護、還會跟標準漂移的分岔版本——正是 Day1 壞味道三的根因；後者讓大家直接用官方 SDK，只是連「接上去」都不用自己做。官方給的替代建議也跟這系列的路線完全對得上：共用的是 SDK 初始化設定（exporter、sampling、resource attribute），真正要治理埋點規範的地方交給 compile-time 的程式碼生成——文章點名的正是 Weaver，也就是 Day5 之後的主角。

## 裝起來：一次性的安裝，換來持續的調和

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm install opentelemetry-operator open-telemetry/opentelemetry-operator \
  --namespace opentelemetry-operator-system --create-namespace \
  --set admissionWebhooks.certManager.enabled=false \
  --set admissionWebhooks.autoGenerateCert.enabled=true
```

那兩個 `admissionWebhooks` 設定值得停一下：webhook 跑在 HTTPS 上需要憑證，正式環境會用 cert-manager 簽發並輪替，這裡讓 chart 自己產一張 self-signed（一年後過期要手動處理）。**這是一個刻意的取捨，不是更好的做法**——demo 環境夠用而已。

```
$ kubectl get crd | grep opentelemetry
instrumentations.opentelemetry.io
opampbridges.opentelemetry.io
opentelemetrycollectors.opentelemetry.io
targetallocators.opentelemetry.io
```

四個 CRD，正好對上前面那張表加另外兩種。

在動手改 `demo-services` 之前，先誠實記一筆：跑 `./scripts/up.sh` 時五個服務全部 `CrashLoopBackOff`，原因是 `shared/src/o11y_shared/flags.py` 裡有一行 Python 2 的例外語法 `except json.JSONDecodeError, OSError:`——Python 3 直接 `SyntaxError`。這不是這次改動造成的，是這份共用套件很早就帶著的 bug，只因為 CI 的 eval harness 走的是另一條完全不碰 k8s 的路徑（自包含 image、跑合成資料），從來沒被實際跑過 k3d 的人踩到。順手改成 `except (json.JSONDecodeError, OSError):`。**「有一條路徑從來沒有人真的走過」這件事，會在這系列反覆出現**——今天是 CI 沒跑到的分支，Day5 是 `-r .` 的假綠燈，Day12 會把它變成一個方法論。

### 把手寫的 Deployment 換成 CR

原本 `13-otel-collector.yaml` 是純手寫的 `Deployment` + `Service` + `ConfigMap`。pipeline 設定原封不動搬進 `spec.config`：

```yaml
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: otel                      # ← 這個名字很關鍵，見下
  namespace: demo
spec:
  mode: deployment
  replicas: 1
  image: otel/opentelemetry-collector-contrib:0.111.0
  config:
    receivers:
      otlp:
        protocols:
          http: { endpoint: 0.0.0.0:4318 }
          grpc: { endpoint: 0.0.0.0:4317 }
    processors:
      batch: { timeout: 5s }
    exporters:
      otlp/tempo: { endpoint: tempo.demo.svc:4317, tls: { insecure: true } }
      prometheusremotewrite:
        endpoint: http://prometheus.demo.svc:9090/api/v1/write
        resource_to_telemetry_conversion: { enabled: true }
      otlphttp/loki: { endpoint: http://loki.demo.svc:3100/otlp, tls: { insecure: true } }
    service:
      pipelines:
        traces:  { receivers: [otlp], processors: [batch], exporters: [otlp/tempo] }
        metrics: { receivers: [otlp], processors: [batch], exporters: [prometheusremotewrite] }
        logs:    { receivers: [otlp], processors: [resource, batch], exporters: [otlphttp/loki] }
```

**CR 故意叫 `otel` 而不是 `otel-collector`。** Operator 幫 CR 建立 Service 的命名規則是 `<CR 名稱>-collector`——叫 `otel-collector` 會生出 `otel-collector-collector` 這種疊字。叫 `otel`，生出來的正好是 `otel-collector`，而五個服務的 `OTEL_EXPORTER_OTLP_ENDPOINT` 本來就寫死指向它。**這代表整個遷移，五個 app 的 manifest 一行都不用改。**

```
$ kubectl -n demo get otelcol,svc
NAME                                          MODE         VERSION   READY   IMAGE
opentelemetrycollector.../otel                deployment   0.156.0   1/1     ...contrib:0.111.0

service/otel-collector               ClusterIP   4317/TCP,4318/TCP
service/otel-collector-headless      ClusterIP   4317/TCP,4318/TCP
service/otel-collector-monitoring    ClusterIP   8888/TCP
```

三個 Service，不是一個。`headless` 給 StatefulSet 場景用，`monitoring` 是 Collector 自身的 Prometheus endpoint（`8888`）——這兩個是手寫 YAML 從來沒有過的東西，Operator 幫你補上了。而 `monitoring` 這一個在 Day4 會變得很重要：**要觀測 collector 自己，資料就從這裡來。**

### 逐欄位看真實輸出：哪些是我寫的，哪些是 schema 幫我決定的

`kubectl -n demo get otelcol otel -o yaml` 印出來的 `spec` 分成兩塊：**我寫的**（`config`、`mode`、`replicas`、`image`）跟**Operator 自動補齊的**（`upgradeStrategy: automatic`、`managementState: managed`、`ipFamilyPolicy: SingleStack`、一整段 `targetAllocator` 的預設值）。我從來沒寫過 target allocator 的任何一行，但 CRD 的 schema 定義了預設值，apply 之後全部自動補齊寫回 etcd。

**這正是「CRD 描述期望狀態」字面上的意思：你交出去的是一份不完整的期望，API server 用 schema 幫你補完。** 設定檔沒填的地方是空的；CRD 沒填的地方，是 schema 幫你決定的。同一件事在 `Instrumentation` CR 上更誇張——我只寫了 exporter endpoint 跟 propagators，印出來卻連 `dotnet`、`go`、`apacheHttpd`、`nginx` 的預設映像檔與 `resourceRequirements` 都補齊了。

最值得看的是 `status`：

```yaml
status:
  conditions:
    - lastTransitionTime: "2026-07-23T09:02:15Z"
      message: Successfully reconciled
      observedGeneration: 1
      reason: Reconciled
      status: "True"
      type: Ready
  observedGeneration: 1
  scale:
    replicas: 1
    selector: app.kubernetes.io/component=opentelemetry-collector,app.kubernetes.io/instance=demo.otel,...
    statusReplicas: 1/1
```

`observedGeneration: 1` 對上 `metadata.generation: 1`——controller 已經處理過這一版期望。改一次 `spec.config`，`generation` 跳到 2，而在 reconcile 完成前 `observedGeneration` 會暫時停在 1，**兩者落差的那段時間就是「現實還沒追上期望」的真實窗口**。

這一段不只是給人 debug 用的。對照 Day2 講的「決策級遙測」：它是一段**結構化、機器可讀、直接回答一個具體問題**的狀態描述——「這個 Collector 現在跟不跟得上它該有的設定」。一個 agent 在查事故原因時，可以直接讀這個欄位來排除「這個服務的 instrumentation 設定其實從來沒被成功套用」這種可能。**這是這系列第一個「本來就存在、但沒有人拿去給 agent 用」的訊號**，Day15 畫拓撲時會回頭問同一個問題：controller 為了 `Owns()` 監聽而維護的那份「誰擁有誰」的關係，能不能也變成 Signal Plane 的輸入，而不是每次都要 agent 從 trace 反推。

### `Instrumentation` CR 宣告了，但今天故意不接上去

```yaml
apiVersion: opentelemetry.io/v1alpha1
kind: Instrumentation
metadata:
  name: python-instrumentation
  namespace: demo
spec:
  exporter:
    endpoint: http://otel-collector.demo.svc:4318
  propagators: [tracecontext, baggage]
  python:
    env:
      - name: OTEL_EXPORTER_OTLP_PROTOCOL
        value: http/protobuf
```

但**沒有任何一個 Pod 加上 `instrumentation.opentelemetry.io/inject-python` annotation**。五個服務的 Dockerfile 目前 `CMD` 還是 `opentelemetry-instrument uvicorn ...`——這就是 Day1 壞味道三「各自安裝」的具體樣子：每個服務自己在映像檔裡打包了 zero-code 指令，各自決定要不要更新。現在就加 annotation 會變成兩套注入機制同時作用在同一個 Pod 上，製造一個自己找的麻煩。留白是刻意的，Day4 才動「誰負責注入」這個更敏感的變數。

## 收尾：讓這些 CR 進得了 GitOps

到這裡，治理資產（Collector pipeline、注入規則）已經從「手寫 Deployment」變成「宣告式的 CR」。但還缺一件事，而它跟語法對不對無關，跟**審查**有關：**沒有任何地方讓第二個人在東西真的套用到叢集之前，先看一眼「這個改動會不會讓某個服務突然沒有 trace」。**

原本的 `up.sh` 是逐檔案 apply：

```bash
for f in "${ROOT}"/k8s/[0-9]*-*.yaml; do kubectl apply -f "$f"; done
```

兩個問題。**沒有單一產出物可以 diff**——一個 PR 同時改了 `16-instrumentation.yaml` 跟 `23-api-gateway.yaml`，reviewer 看到兩個獨立的檔案 diff，要自己在腦中重建合起來的效果，檔案一多就沒人會真的做。**沒有東西模擬 GitOps controller 實際會做的事**——Argo CD／Flux 不會逐檔案 apply，而是先解析成一份 manifest 再套用；本地流程跟它不一致，「本地測過」跟「GitOps 套出來」就是兩件事。

改動小得可以：一個 `kustomization.yaml` 列出 `resources`，然後

```bash
kubectl kustomize "${ROOT}/k8s" | kubectl apply -f -
```

沒有 template、沒有 overlay、沒有 `commonLabels`——刻意選最小的一步。目前只有一個環境，拆 base/overlay 是「還沒有需求就先設計」。今天只解決一件事：**把「哪些檔案算數」宣告出來，變成一個可以被單一指令解析的入口。** Day7 的 CI gate 要在 PR 上跑檢查，總得先有一個東西可以跑檢查，而不是 N 個檔案。

特地沒加 `commonLabels` 是有原因的：`api-gateway` 的 Pod 有一個 `git_version` label，透過 Downward API 餵進 `OTEL_RESOURCE_ATTRIBUTES`：

```yaml
- name: GIT_VERSION
  valueFrom:
    fieldRef:
      fieldPath: metadata.labels['git_version']
```

`commonLabels` 會蓋到這個 `fieldRef` 依賴的空間。它不會讓 apply 失敗，只會讓一個現有的隱含契約多一層不確定性。**這正是今天最想留下的一句話：GitOps 工具不會告訴你「這樣改安不安全」，語法檢查跟安不安全是兩件事。**

### PR 該看什麼：一份誠實地還只是人肉的清單

`kubectl kustomize` 能保證的只有「這是合法的 Kubernetes 資源集合」。保證不了的，寫成一份 `GITOPS-REVIEW.md`：

1. **新增／改名一個 Deployment，卻沒有同步碰 instrumentation annotation。** 少了 `inject-python`，Pod 一樣 Ready、一樣 serve 流量，但不會有任何 trace 送出去——`kubectl apply` 不報錯，`kubectl get pods` 也看不出來。
2. **改掉一個被別的資源依賴的 label。** `git_version` 改名不會讓 apply 失敗，會讓 `service.version` 從此悄悄消失在新的 span 裡。
3. **改 Collector 的 resource limits。** Day4 會示範 collector 被壓到 `OOMKilled` 時 app 端完全看不到任何 exporter 錯誤——所以這個數字的審查規格該跟改程式碼一樣嚴謹，不是「反正只是個數字」。
4. **instrumentation 的 exporter endpoint 跟 Collector 的 Service name 還對得上嗎。** 這兩個檔案靠字串 `http://otel-collector.demo.svc:4318` 互相參照，沒有任何 schema 幫你檢查這條連結還在不在——**這就是命名漂移的 Kubernetes YAML 版本**，Day6 之後 weaver 要解決的正是同一個問題的另一半。
5. **diff 的範圍跟 PR 宣稱的改動一致嗎。** `kubectl kustomize k8s/ | kubectl diff -f -` 能對出這個 PR 實際上會動到叢集裡的哪些東西，包含作者自己沒意識到動了的部分。這比讀檔案級 diff 誠實。

這五條裡，只有第 3 條在後面真的被示範過「不做會出事」。**其餘四條目前都只是推論，沒有一條被驗證過**，而且這份清單不是任何形式的自動防護——會不會被人照著看、看不看得出來，完全沒驗證。要讓它從「PR 衛生習慣」變成治理，下一步是把其中至少一條變成可執行的檢查（例如 CI 裡確認每個 Deployment 都有對應的 annotation），而那就是 Day7 跟 Day13 要做的事。**今天留白，是為了讓「從人工到自動」這個對比是真的，不是提前把答案劇透掉。**

## 今天沒做的事

沒有導入 Argo CD 或 Flux 本身。今天只做到「本地流程跟 GitOps controller 的行為對齊」，真的讓套用動作從 git push 觸發是另一件事，不在今天硬湊。

沒有把 `Instrumentation` CR 接到任何 Pod 上，理由前面說了——那是 Day4 的實驗。

沒有回答「CR 裡的版本號該不該全公司統一」。Operator 的調和迴圈保證的是「CR 寫什麼版本，跑起來就是什麼版本」，它**不會**替你決定那個版本號該是多少。十幾個團隊各自維護各自的 `OpenTelemetryCollector` CR，「每份 CR 的版本號是不是一致」就會變成一個沒人管的問題——**跟 Day1 壞味道三的 SDK 版本漂移是同一個根因換了位置重演一次**，只是從 SDK 沒人管變成 Collector 沒人管。這個問題的機制面答案是 `OpAMPBridge`（中控端一次性、可稽核地下發版本），治理面答案要等 Day9 講 breaking change 的三層驗證模型。

明天：把某一個服務的 annotation 換上去、同時從 Dockerfile 拿掉手動的 `opentelemetry-instrument`，實地對比 trace 差在哪；然後把 collector 壓到 `OOMKilled`，示範「注入了不代表送達」——那條線會一路接到後面講資料可信度的地方。
