
---
title: "【Day4】安裝 OTel Operator：拆解真實的 CRD 實作"
series: "2026 鐵人賽：AIOps with OpenTelemetry"
tags: [OpenTelemetry, Kubernetes, 鐵人賽]
---
# Day4：安裝 OTel Operator——拆解真實的 CRD 實作

Day3 把 Operator pattern 的詞彙站穩了：CRD、Controller、reconciliation loop、`OpenTelemetryCollector` 負責部署行為、`Instrumentation` 負責注入行為。今天要做的事很單純——把這些詞彙對回真實的東西：真的裝一次 Operator，把 `demo-services` 手寫的 Collector Deployment 換成一份 Operator 管理的 CR，再宣告一份 `Instrumentation` CR，然後逐欄位對照 `kubectl get otelcol,instrumentation -o yaml` 的真實輸出，指認哪一段屬於「部署行為」、哪一段屬於「注入行為」。

在動手之前，先誠實交代一件事：這篇文章開始寫之前，我回頭盤點了一下 `demo-services` 現在的狀態，發現兩個問題——一個是這系列自己欠的債，一個是純粹的意外。順手一起處理掉，過程本身也值得記一筆。

## 動手前，先還兩筆債

**債務一：Day1 的壞味道，之前只寫在文章裡，程式碼裡其實沒有。** Day1 講的「`userId` 跟 `user_id` 並存」，回頭檢查 `order-service` 的 `CreateOrderRequest`，欄位一路都乾乾淨淨只有 `user_id`——這個壞味道當時只存在於敘述裡，沒有真的種進程式碼。如果放著不管，Day10 的 `weaver registry infer`、Day11 的漂移偵測，到時候會沒有真實素材可以抓，變成自己講的故事自己圓不回來。所以在裝 Operator 之前，先把它種成真的：

- `order-service` 的 `CreateOrderRequest` 加一個 `Field(alias="userId")` + `populate_by_name=True`——FastAPI 這層現在會**同時接受** `userId` 或 `user_id` 當 request key，兩者都會被正確解析成 `req.user_id`，呼叫端完全感覺不到差異。
- `api-gateway` 的 `/api/orders` 是一個「thin proxy」，本來就不會把 body 解析成 order-service 的 model——它只是把原始 bytes 轉發出去。我讓它多做一件事：peek 一眼原始 JSON body，把**呼叫端實際用的那個 key**（`userId` 或 `user_id`，不做任何正規化）直接寫進這次請求的 log 跟 span attribute。
- `scripts/load.sh`（負責產生流量的腳本，扮演「前端工程師」的角色）現在有 1/4 的機率送 `userId`，其餘送 `user_id`。

這三個改動合起來，重現的正是 Day1 講的那個故事：`order-service` 有 alias 悄悄接住兩種拼法，但**沒有經過這層轉換的服務**（這裡是 `api-gateway`），會把呼叫端原始送來的拼法，原封不動地寫進自己的 telemetry。實際跑一輪流量，`api-gateway` 的 log 立刻就長這樣——同一個 `http.request_received` 事件,同一支程式碼路徑，屬性名稱卻不一樣：

```json
{"event": "http.request_received", "path": "/api/orders", "user_id": "u-2"}
{"event": "http.request_received", "path": "/api/orders", "userId": "u-5"}
```

這不是我編出來的範例輸出，是真的對跑在 k3d 裡的 `demo-services` 送了 12 筆 `/api/orders` 請求、直接從 `kubectl logs deploy/api-gateway` 撈出來的兩行。這份漂移，會在 Day10 用 `weaver registry infer` 反推 schema 時再被挖出來一次。

**債務二：一個跟這系列完全無關、但擋住整個 stack 的語法錯誤。** 為了跑上面那個實驗，我照著 README 跑 `./scripts/up.sh`，結果五個服務全部 `CrashLoopBackOff`。查下去發現 `shared/src/o11y_shared/flags.py` 裡有一行：

```python
except json.JSONDecodeError, OSError:
```

這是 Python 2 的例外語法，Python 3 里從語法層級就直接是 `SyntaxError`——不是這次改動造成的，是這份共用套件從很早以前就帶著的一個既有 bug，只是因為 CI 的 eval harness 走的是另一條完全不碰 k8s stack 的路徑（一個自包含的 Docker image，跑合成資料，不會真的 import 到這個模組的這條分支），從來沒被實際跑過 k3d 的人踩到過。順手改成 `except (json.JSONDecodeError, OSError):`——一行修正，跟今天的主題無關，但不修，後面什麼都跑不起來。

這兩筆債都不是今天的主角，只是「要在真實環境裡動手」這件事,本來就會先撞見這些；記下來，是因為它們本身就是「有截圖有 diff」這個系列承諺的一部分。

## 裝 Operator：一次性的安裝，換來持續的調和

OTel Operator 官方發布 Helm chart，這是最直接的安裝方式：

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update

helm install opentelemetry-operator open-telemetry/opentelemetry-operator \
  --namespace opentelemetry-operator-system --create-namespace \
  --set admissionWebhooks.certManager.enabled=false \
  --set admissionWebhooks.autoGenerateCert.enabled=true
```

`admissionWebhooks` 這兩個設定值得停一下：Operator 的 webhook（就是 Day3 提過、`Instrumentation` CR 背後真正動手改 Pod spec 的那個 admission webhook）跑在 HTTPS 上，需要一張憑證。正式環境通常會裝 cert-manager 幫忙簽發、輪替；這裡圖簡單，讓 Helm chart 自己產生一張 self-signed 憑證（`autoGenerateCert.enabled=true`），一年後過期要手動處理——這是一個刻意的取捨，不是「更好的做法」，只是「demo 環境夠用的做法」。

裝完之後，集群裡多了四個 CRD：

```
$ kubectl get crd | grep opentelemetry
instrumentations.opentelemetry.io
opampbridges.opentelemetry.io
opentelemetrycollectors.opentelemetry.io
targetallocators.opentelemetry.io
```

正好對上 Day3 最後提過的四種 CR——今天只會碰前兩種。

## 把手寫的 Collector Deployment，换成一份 CR

`demo-services/k8s/13-otel-collector.yaml` 原本是一份純手寫的 `Deployment` + `Service` + `ConfigMap`：pipeline 設定寫在 ConfigMap 裡，Deployment 掛載它，Service 開兩個 port。這是 Day3 說的「一次性部署」——它會把 Collector 跑起來，但沒有任何東西持續盯著它。

換成 CR 之後，同一份 pipeline 設定原封不動地搬進 `spec.config`：

```yaml
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: otel
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
      resource:
        attributes:
          - { key: service, from_attribute: service.name, action: insert }
    exporters:
      otlp/tempo: { endpoint: tempo.demo.svc:4317, tls: { insecure: true } }
      prometheusremotewrite:
        endpoint: http://prometheus.demo.svc:9090/api/v1/write
        target_info: { enabled: false }
        resource_to_telemetry_conversion: { enabled: true }
      otlphttp/loki: { endpoint: http://loki.demo.svc:3100/otlp, tls: { insecure: true } }
    service:
      pipelines:
        traces: { receivers: [otlp], processors: [batch], exporters: [otlp/tempo] }
        metrics: { receivers: [otlp], processors: [batch], exporters: [prometheusremotewrite] }
        logs: { receivers: [otlp], processors: [resource, batch], exporters: [otlphttp/loki] }
```

這裡有一個命名上的小細節，決定了這次遷移能不能做到「其他東西都不用改」：**這個 CR 故意叫 `otel`,不叫 `otel-collector`。** Operator 幫 CR 建立子資源時，Service 的命名規則是 `<CR 名稱>-collector`——如果 CR 叫 `otel-collector`，生出來的 Service 就會變成 `otel-collector-collector`，一個多餘的疊字。叫 `otel`，生出來的 Service 名稱正好就是 `otel-collector`——而 `demo-services` 裡另外五個服務的 `OTEL_EXPORTER_OTLP_ENDPOINT`，本來就全部寫死指向 `http://otel-collector.demo.svc:4318`。這代表遷移這一步，五個 app 的 k8s manifest **一行都不用改**。

`kubectl apply -f 13-otel-collector.yaml` 之後：

```
$ kubectl -n demo get otelcol,svc
NAME                                           MODE         VERSION   READY   IMAGE
opentelemetrycollector.opentelemetry.io/otel   deployment   0.156.0   1/1     otel/opentelemetry-collector-contrib:0.111.0

NAME                                 TYPE        PORT(S)
service/otel-collector               ClusterIP   4317/TCP,4318/TCP
service/otel-collector-headless      ClusterIP   4317/TCP,4318/TCP
service/otel-collector-monitoring    ClusterIP   8888/TCP
```

猜對了：`otel-collector` 這個名字真的自動生出來了。而且是三個 Service，不是一個——`headless` 是給 StatefulSet 場景（`mode: statefulset`）用的，`monitoring` 是 Collector 自身的 Prometheus metrics endpoint（`8888` port），這兩個是這份手寫 YAML 從來沒有過的東西，是 Operator 幫忙補上的。

剩下一個 NodePort（原本讓 host 上的 xk6 腳本能直接打 Collector）沒辦法用同一招解決——Operator 生出來的 Service 是 ClusterIP，selector 也是 Operator 自己管的，不能手動加一個 NodePort 進去改它。解法是另外寫一個 NodePort Service，selector 對準 Operator 幫 Collector Pod 貼的 label：

```yaml
apiVersion: v1
kind: Service
metadata:
  name: otel-collector-nodeport
  namespace: demo
spec:
  type: NodePort
  selector:
    app.kubernetes.io/managed-by: opentelemetry-operator
    app.kubernetes.io/instance: demo.otel
    app.kubernetes.io/component: opentelemetry-collector
  ports:
    - { port: 4318, targetPort: 4318, nodePort: 30003, name: otlp-http }
```

這組 label 不是猜的——是從 `kubectl get otelcol otel -o yaml` 的 `status.scale.selector` 欄位裡原封不動抄出來的（下一節會看到完整輸出）。

## 逐欄位拆真實的 `kubectl get otelcol -o yaml`

裝完、跑起來之後，完整印出這份 CR：

```
$ kubectl -n demo get otelcol otel -o yaml
```

輸出裡，`spec` 底下大致分成兩塊，剛好對應 Day3 講的「這是宣告期望狀態的句子」：

- **我自己寫的部分**：`spec.config`（收進來的 pipeline 設定，一字不改地照抄原本的 ConfigMap）、`spec.mode: deployment`、`spec.replicas: 1`、`spec.image`。
- **Operator 自動補上預設值的部分**：`spec.upgradeStrategy: automatic`、`spec.managementState: managed`、`spec.ipFamilyPolicy: SingleStack`、一整段 `spec.targetAllocator`（`allocationStrategy: consistent-hashing`、`collectorNotReadyGracePeriod: 30s` 之類）。我從來沒有寫過 target allocator 的任何一行設定，但因為 CRD 的 schema 定義了預設值，`kubectl apply` 之後這些欄位全部自動補齊、寫回 etcd。這正是「CRD 描述期望狀態」這句話字面上的意思——你交出去的是一份不完整的期望，API server 用 schema 幫你補完，而不是你要自己知道每一個欄位該填什麼。

最值得停下來看的是 `status` 這一段——這是 Day3 講的「controller 上一次調和之後的結果」，字面意義上的**機器可讀狀態**：

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
    selector: app.kubernetes.io/component=opentelemetry-collector,app.kubernetes.io/instance=demo.otel,app.kubernetes.io/managed-by=opentelemetry-operator,app.kubernetes.io/name=otel-collector,app.kubernetes.io/part-of=opentelemetry,app.kubernetes.io/version=0.111.0
    statusReplicas: 1/1
```

`observedGeneration: 1` 對上 `metadata.generation: 1`——這兩個數字相等，代表 controller 已經看過、處理過「這一版」的期望狀態；如果我改一次 `spec.config`，`metadata.generation` 會跳到 2，而在 controller 完成這一輪 reconcile 之前，`status.observedGeneration` 會暫時停在 1，兩者出現落差的這段時間，就是「現實還沒追上期望」的真實窗口。`status.conditions[0].reason: Reconciled` 跟 `message: Successfully reconciled` 則是那句「這是不是一段結構化、機器可讀的狀態描述」的直接證據——不是一張給人看的圖，是一個可以被 agent 直接讀、直接拿來回答「這個 Collector 現在到底跟不跟得上它該有的設定」這個問題的欄位。這正是 Day3 最後一節埋的伏筆：這欄位不只是給人 debug 用，它本身就有資格成為 Signal Plane 的一條輸入。

## `Instrumentation` CR：宣告了，但今天故意不接上去

同一批，也套用了一份 `Instrumentation` CR：

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

`kubectl get instrumentation python-instrumentation -o yaml` 印出來的東西，比我寫的這幾行多得多——Operator 的 admission webhook 在建立這個物件的當下，自動把 `spec.python.image`、`spec.java.image`、`spec.nodejs.image`……每一種語言的 auto-instrumentation 映像檔預設值都補齊了，連我完全沒打算用的 `dotnet`、`go`、`apacheHttpd`、`nginx` 都各自有一組預設的 `resourceRequirements`（`limits.cpu: 500m` 之類）。這跟前面 `spec.targetAllocator` 的預設值補齊是同一件事——**這份 CR 的 schema，遠比我實際填寫的那幾行豐富**，這也是為什麼 Day3 反覆強調「CRD 是一種期望狀態的宣告」而不是「一份設定檔」：設定檔沒填的地方是空的，CRD 沒填的地方，是 schema 幫你決定的。

但今天特意不做一件事：**沒有把任何一個服務的 Pod annotation 加上 `instrumentation.opentelemetry.io/inject-python: "demo/python-instrumentation"`。** 現在五個服務的 Dockerfile，`CMD` 依然是 `opentelemetry-instrument uvicorn ...`——這是 Day1 壞味道三講的「各自安裝」的具體樣子：每個服務自己在映像檔裡打包了 zero-code 指令，各自決定要不要更新、要不要對齊版本。這份 `Instrumentation` CR 現在只是「宣告」，還沒有任何一個 Pod 真的透過 admission webhook 被它改過 spec——如果現在就把 annotation 也加上去，會變成兩套注入機制同時作用在同一個 Pod 上，反而製造出一個新的、自己找的麻煩。

留白，是因為這正是 Day5 要做的實驗：把某一個服務的 annotation 換上去，同時从它的 Dockerfile 拿掉手動的 `opentelemetry-instrument`，實地對比 before/after 的 trace 差在哪裡、annotation 覆蓋不到的地方（比如目前這幾支服務自己開的 business metrics/span）又是什麼樣子。今天只確認一件事：Operator 的 webhook 確實已經在運作、確實已經知道 Python 該用哪個 auto-instrumentation 映像檔——但「把某個服務接上去」這個動作，故意留到明天才做。

## 部署行為 vs 注入行為，今天實際看到的分工

回到 Day3 那張分工圖，現在可以填上真實看到的內容：

|                    | `OpenTelemetryCollector`（部署行為）                                   | `Instrumentation`（注入行為）               |
| ------------------ | ------------------------------------------------------------------------ | --------------------------------------------- |
| 今天做的事         | 把手寫 Deployment 換成 CR，Operator 接手持續調和                         | 宣告一份 CR，但沒有接上任何 Pod               |
| 真實可觀察到的效果 | `otel-collector` Service 自動生成、三個 port 就緒、原有 5 個服務零改動 | Operator webhook 自動補齊各語言預設映像檔版本 |
| 對應的「現況」訊號 | `status.conditions[type=Ready]`、`status.observedGeneration`         | 目前無 Pod 引用它，所以還沒有能觀察的注入效果 |
| Day5 要接著做的事  | （不變，持續調和中）                                                     | 挑一個服務接上 annotation，比較 before/after  |

今天完全沒有動到任何一個服務原本的 auto-instrumentation 設定，五個服務仍然靠自己 Dockerfile 裡的 `opentelemetry-instrument` 在跑——這是刻意的：先確認 Operator 本身站穩、Collector 遷移沒有破壞任何東西，再讓 Day5 去動「誰負責注入」這個更敏感的變數。

明天：annotation 覆蓋不改代碼的 before/after trace 對比，以及誠實講清楚它覆蓋不到的地方。
