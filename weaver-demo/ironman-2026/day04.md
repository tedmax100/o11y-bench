---
title: "【Day4】annotation 做 auto-instrumentation：真實的 before/after trace 對比"
series: "2026 鐵人賽：AIOps with OpenTelemetry"
tags: [OpenTelemetry, Kubernetes, 鐵人賽]
---
# Day4：annotation 做 auto-instrumentation——真實的 before/after trace 對比

Day3 裝完 Operator 之後，刻意留了一個空白：`Instrumentation` CR 宣告了，但沒有接上任何一個 Pod——五個服務仍然靠自己 Dockerfile 裡的 `opentelemetry-instrument uvicorn ...` 在跑。今天把這個空白填上：挑一個服務（`api-gateway`），把它從「自己在 Dockerfile 裡寫死 zero-code 指令」換成「靠 annotation 讓 Operator webhook 注入」，然後真的抓 before/after 的 trace 出來比對，誠實講兩者到底差在哪、哪裡沒有差。

## 改動只有兩處

**Dockerfile**：拿掉 `opentelemetry-instrument` 這個包裝指令，變回最單純的 `uvicorn`：

```dockerfile
# No `opentelemetry-instrument` wrapper here on purpose — the Operator's
# webhook injects instrumentation via PYTHONPATH + an init container instead.
CMD ["uvicorn", "api_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**k8s manifest**：在 Pod template 加一個 annotation：

```yaml
template:
  metadata:
    annotations:
      instrumentation.opentelemetry.io/inject-python: "python-instrumentation"
```

值填的是 Day3 那份 `Instrumentation` CR 的名字（同 namespace，所以不用寫 `demo/python-instrumentation`，直接寫 CR 名稱就夠）。除此之外，`23-api-gateway.yaml` 裡原本那一串 `OTEL_SERVICE_NAME`、`OTEL_EXPORTER_OTLP_ENDPOINT`、`OTEL_RESOURCE_ATTRIBUTES` 全部沒有動——這是刻意的，待會第一個要看的真實現象，就是這些值在 webhook 手裡到底會不會被蓋掉。

rebuild image、重新 apply、重啟 deployment 之後：

```
$ kubectl -n demo get pods -l app=api-gateway
NAME                           READY   STATUS     RESTARTS   AGE
api-gateway-5866859f9b-9pnpg   0/1     Init:0/1   0          10s
```

`Init:0/1` 這一格，就是 Day3 講的「webhook 攔截 Pod 建立請求、把東西塞進 Pod spec」在真實世界的樣子——一個新出現的 init container，正在跑。

## webhook 真的塞了什麼進去：完整看一次 Pod spec

等 Pod Ready 之後，`kubectl get pod ... -o yaml` 把整份 spec 印出來，跟 Day3 一樣，不用猜，直接讀真實輸出。

**Init container**：

```yaml
initContainers:
  - name: opentelemetry-auto-instrumentation-python
    image: ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-python:0.64b0
    command: ["cp", "-r", "/autoinstrumentation/.", "/otel-auto-instrumentation-python"]
    volumeMounts:
      - { mountPath: /otel-auto-instrumentation-python, name: opentelemetry-auto-instrumentation-python }
```

它做的事很單純：把一整包預先裝好的 Python auto-instrumentation 套件（`opentelemetry-distro` + 各種 instrumentor），複製到一個跟主容器共用的 `emptyDir` volume 裡，然後結束。它不執行任何 app 邏輯，純粹是「把檔案準備好」。

**主容器（`gateway`）多出來的東西**，這才是真正有趣的部分：

```
PYTHONPATH = /otel-auto-instrumentation-python/opentelemetry/instrumentation/auto_instrumentation:/otel-auto-instrumentation-python
```

這一行解釋了「為什麼 Dockerfile 不用寫 `opentelemetry-instrument` 也能動」——Python 直譯器啟動時本來就會找 `PYTHONPATH` 裡的模組，`auto_instrumentation` 這個套件內部用的是 Python 的 `sitecustomize` 機制，直譯器一啟動就自動把 SDK 設好、把 FastAPI/httpx 的 instrumentor 掛上去，完全不需要在 `CMD` 裡包一層指令去手動觸發。跟 Day3 講的「有一個 admission webhook 在 Pod 建立的當下把東西注入進去」，在這裡具體對上了——只是 Python 語言注入的形式是 `PYTHONPATH`，不是像 Java 那樣的 `-javaagent`。

再來是我原本猜測會被蓋掉、但實際上**沒有被蓋掉**的東西：

```
OTEL_SERVICE_NAME = api-gateway                                    # 我自己寫的值，原封不動
OTEL_EXPORTER_OTLP_ENDPOINT = http://otel-collector.demo.svc:4318  # 我自己寫的值，原封不動
```

webhook 的邏輯是「補齊沒有的，不動已經存在的」——這兩個我在 `23-api-gateway.yaml` 裡本來就手動寫了，webhook 看到容器已經有這個 env var，就跳過，不覆蓋。

真正被 webhook 動過手腳的，是 `OTEL_RESOURCE_ATTRIBUTES`。我原本寫的值是：

```
service.namespace=demo,deployment.environment=demo,service.version=$(GIT_VERSION),git_repo=$(GIT_REPO),git_version=$(GIT_VERSION)
```

注入之後變成：

```
service.namespace=demo,deployment.environment=demo,service.version=$(GIT_VERSION),git_repo=$(GIT_REPO),git_version=$(GIT_VERSION),k8s.container.name=gateway,k8s.deployment.name=api-gateway,k8s.namespace.name=demo,k8s.node.name=$(OTEL_RESOURCE_ATTRIBUTES_NODE_NAME),k8s.pod.name=$(OTEL_RESOURCE_ATTRIBUTES_POD_NAME),k8s.replicaset.name=api-gateway-5866859f9b,service.instance.id=demo.$(OTEL_RESOURCE_ATTRIBUTES_POD_NAME).gateway
```

不是覆蓋，是**在後面接上一段**——webhook 額外注入了 `OTEL_NODE_IP`、`OTEL_POD_IP`、`OTEL_RESOURCE_ATTRIBUTES_POD_NAME`、`OTEL_RESOURCE_ATTRIBUTES_NODE_NAME` 這幾個透過 Downward API 取值的 env var，再把 `k8s.pod.name`、`k8s.node.name`、`k8s.replicaset.name`、`service.instance.id` 這些原本我完全沒寫的 k8s 拓撲屬性，接在我自己那串資源屬性後面。這代表：**中央治理（Operator 知道要幫每個 Pod 補上 k8s 身份）跟團隊自訂（我自己的 `git_repo`/`git_version` join key）不是互斥的，是疊加的**——這也回答了 Day3 結尾提過的伏筆：Operator 幫 Collector/子資源維護的那些關係資訊，現在真的變成一條可以往下遊傳遞的訊號了。

## before / after：對同一條下單流程，真的各截一次 trace

在切換前後，各對 `webapp → api-gateway → order-service → user/payment-service` 這條下單流程送一次真實請求，把 `trace_id` 從 log 裡撈出來，直接查 Tempo 的 `/api/traces/{traceID}`。

**Before**（`opentelemetry-instrument` 包裝指令，trace `e981d8a3...`）：

```
api-gateway   POST /api/orders   {http.route: /api/orders, user_id: u-1}
order-service POST /api/orders   {http.route: /api/orders}
user-service  GET /api/users/{user_id}/authcheck
payment-service POST /charge
webapp        POST /api/{path:path}
```

**After**（annotation 注入，trace `46f0a0df...`，這次故意送 `userId` 而不是 `user_id`）：

```
api-gateway   POST /api/orders   {http.route: /api/orders, userId: u-4}
order-service POST /api/orders   {http.route: /api/orders}
user-service  GET /api/users/{user_id}/authcheck
payment-service POST /charge
webapp        POST /api/{path:path}
```

兩條 trace 的服務數、span 數、`http.route` 完全一樣。span name 依然是 FastAPI 的 route template（`POST /api/orders`），不是「checkout」這種業務語意——**annotation 注入沒有讓這件事變得更好，也沒有變得更差，它跟 Day1 講的「span 沒有業務語意」這個壞味道完全無關**，因為兩種注入方式底層用的是同一套 FastAPI instrumentor，抓到的是同一層技術語意。

而 Day3 加進 `api-gateway` 的那段「把呼叫端原始 key 寫進 span attribute」的程式碼——`before` trace 是 `user_id: u-1`，`after` trace 是 `userId: u-4`（因為這次我故意送了 `userId`）——**兩邊都正確地把呼叫端實際用的 key 標了上去，一模一樣**。

## 誠實講：annotation 到底換到了什麼、沒換到什麼

這是今天最容易被誤解的地方，值得講清楚：**annotation 注入換掉的是「誰負責遞送 instrumentation agent」，不是「自動抓到多少東西」。**

- 換掉的：Dockerfile 不用再寫 `opentelemetry-instrument`，不用每個團隊自己記得要不要升級這個 wrapper 版本、要不要跟上新的 semantic convention——這件事現在是平台團隊透過 `Instrumentation` CR 中央宣告一次，所有掛上這個 annotation 的服務都吃到同一份版本、同一份設定。這正是 Day3 一路鋪陳的「各自安裝」變成「中央調和」——只是這次終於在 Day4 落地成一個看得到 diff 的真實案例。
- 沒換掉的：FastAPI/httpx 這些通用函式庫的 auto-instrumentation，本來就只抓得到 HTTP method、route template、status code 這些**技術語意**層面的東西；`api-gateway` span 名稱依然是 `POST /api/orders`，不會自動變成「checkout」。想要業務語意，或想要「把呼叫端原始 key 標上去」這種特定的資安/治理需求，都得靠 Day3 那段手寫的程式碼——**annotation 換的是遞送機制，不是免費多送你語意**。這段程式碼不管用哪種注入方式都得自己寫，也都會照常運作，因為它呼叫的是 `trace.get_current_span().set_attribute(...)`，是 app 自己在跟 OTel API 對話，不是 auto-instrumentation 幫你做的事。

換句話說，「annotation 覆蓋不到的地方」不是「這次少抓到了什麼東西」，而是「這東西從一開始，兩種注入方式都沒有幫你抓」——annotation 解決的是治理/維運層面的問題（誰維護版本、誰記得升級），不是資料豐富度的問題。這是這系列一路強調的「誠實」的具體一次示範：如果只截圖 before/after 的 trace 長得一樣，讀者可能會誤以為「反正一樣，那何必裝 Operator」；真正的價值在維運層面，不在單次 trace 的資料內容上，這件事必須講清楚，不能靠一張漂亮截圖含糊帶過。

## 延伸：annotation 注入不是 Python 專屬，公司環境的多語言案例

今天的示範只用了 Python，靠 `PYTHONPATH` 這種 language-specific 的 auto-instrumentation 機制。但 annotation 驅動注入這件事本身是通用的，公司內部另一個環境（Java / PHP / 其他語言混跑）剛好把「同一套 annotation 機制，換一種語言會長什麼樣子」示範得很清楚，值得記錄——但先說明：這套設定當時**還沒有在真實叢集完整驗證過**（沒確認過 webhook 一定能成功注入），下面講的是設計，不是「已經跑通」的結論，跟這系列一貫的誠實態度一致。

**Java：兩個 annotation 要一起加，缺一不可**

Java 沒有走 `PYTHONPATH` 那種路，是 operator 提供的 `-javaagent`：

```yaml
annotations:
  instrumentation.opentelemetry.io/inject-java: "opentelemetry-operator-system/java"
  sidecar.opentelemetry.io/inject: "opentelemetry-operator-system/sidecar"
```

這裡的設計是 app 送 OTLP 到本機 sidecar（`localhost:4318`），sidecar 再轉發到中心化的後端——跟今天 `api-gateway` 直接送到叢集內 `otel-collector` Service 的拓撲不一樣，是「先進 sidecar，sidecar 再統一出口」。因為掛了 sidecar，一個 Pod 至少會有兩個 container，webhook 沒辦法保證猜對要幫哪個 container 注入 agent，所以還要多加一個：

```yaml
  instrumentation.opentelemetry.io/container-names: "<app container 名稱>"
```

這是今天「annotation 補齊沒有的、不動已經存在的」那段可以延伸的另一面——**沒填 `container-names` 不會報錯，只是 agent 沒被注入，資料悄悄不出現**。跟今天 `OTEL_SERVICE_NAME`/`OTEL_EXPORTER_OTLP_ENDPOINT` 不被覆蓋是「因為已存在所以被跳過」不同，這裡是「因為猜不到目標，直接放棄注入」，同樣是靜默、同樣容易被忽略，但成因不一樣，值得分開記。

**PHP-FPM：annotation 注入不需要語言本身支援 auto-instrument**

PHP 沒有 operator 支援的自動注入機制，能用的只有 sidecar 模式：

```yaml
annotations:
  sidecar.opentelemetry.io/inject: "opentelemetry-operator-system/sidecar-php-fpm"
```

這證明了一件事：annotation 驅動注入的機制本身不依賴「這個語言有沒有 auto-instrumentation agent」——sidecar 模式只是幫你把一個 collector process 塞進同一個 Pod，網路層面能連到 `localhost` 就夠，app 端要自己用 SDK 把 OTLP 送過去、自己設好 `OTEL_SERVICE_NAME`。跟今天 Python annotation 注入（webhook 直接把 instrumentation 邏輯注入進 app process）是完全不同的兩種手段，殊途同歸都是「靠 annotation 觸發 webhook」。

PHP-FPM 還有一個 Python 長駐 process 完全不會碰到的問題：每個 request 都是全新的短命 process，SDK 只能送 delta metrics，沒辦法像長駐 process 一樣自己維護 cumulative 狀態。這個環境的做法是在 sidecar 內用 `deltatocumulative` processor，把同一個 Pod 內多個短命 worker process 送出的 delta 值疊加成正確的 cumulative 值再送出去——這是「注入機制要配合語言的 process 生命週期」的具體案例，Python 因為是長駐 process 天生不會踩到。

**這段延伸的誠實結論**：annotation 注入有兩種不同的實作手段（Python 這種「改寫 app process 本身」vs. sidecar 這種「旁邊塞一個轉發 process」），選哪種不是治理團隊說了算，是被語言本身的 process 模型決定的——有沒有 auto-instrumentation agent、是不是長駐 process，這兩個問題的答案，直接決定一個新語言加入時該走哪條路。

## annotation 注入之後，collector 本身也可能是那個沒被觀測到的東西

今天前面一路在講「annotation 注入了什麼、沒注入什麼」，都預設了一件事：只要 webhook 把 instrumentation 塞進去，資料就一定會安全送到後端。這個預設今天要親手戳破一次——把 `api-gateway` 現在指向的 `otel-collector`（`13-otel-collector.yaml` 那份獨立 Deployment）resource limit 主動調低，看著它被 `OOMKilled`，走一次「發現資料變少 → 定位是 collector 被壓垮」的排查。

```yaml
resources:
  limits:
    memory: "TODO：先跑一次正常負載記下平常用量，再往下調到明顯不夠"
```

重新對 `api-gateway` 送同一組流量，這次盯著三件事：

1. `kubectl get pods -n demo -w`——等 `otel-collector` 那個 Pod 出現 `OOMKilled`。
2. `kubectl describe pod otel-collector-... | grep -A5 "Last State"`——確認真的是 `OOMKilled`，不是 liveness probe 失敗或別的原因。
3. Tempo 那邊查同一段時間的 trace 數量——這是「使用者視角」會先看到的症狀：不是報錯，是資料悄悄變少。

```
# TODO：真實 kubectl describe 輸出 + Tempo 查詢對照，貼這裡
```

排查順序刻意不是先查 app：

1. **先確認不是 app 沒送**：查 app 容器的 log，確認 exporter 沒有報錯——如果 app 端已經在噴 `Failed to export`，問題出在連線層級，跟 collector 內部無關，排查方向完全不同。
2. **再確認是不是 collector 收到了但沒送出去**：`kubectl port-forward` 到 collector 的 metrics endpoint，比對 `otelcol_receiver_accepted_spans` 跟 `otelcol_exporter_sent_spans` 這兩個數字——有落差代表資料卡在 collector 內部，不是 receiver 沒收到。
3. **最後才看 Pod 本身健不健康**：`kubectl get pod`、`kubectl describe pod` 看重啟次數跟 `Last State` 的 `Reason`。

```
# TODO：實際跑三步的輸出貼上來，尤其是 otelcol_receiver_accepted_spans
# vs otelcol_exporter_sent_spans 這組數字落差
```

這個順序刻意跟直覺相反——大部分人踩到「資料變少」的第一反應是去查 app，因為症狀是在 app 這邊的下游（dashboard 空的、trace 查不到）浮現的。但 app 端往往是無辜的：它已經把 span 送出去了，只是 collector 那端在半路把它吃掉。**症狀出現的地方，不一定是問題發生的地方**——這也是今天最想留下的一句話：annotation 注入解決的是「instrumentation 怎麼進到 app process」，不代表資料從此穩定送達；可觀測性系統本身的健康狀態，也需要被觀測，不能預設它永遠正常運作。這條線後面會在 Signal Plane 談資料可信度的那幾天再接上。

## 今天沒做的事

只轉換了 `api-gateway` 一個服務，其餘四個（`payment`、`user`、`order`、`webapp`）依然是 Dockerfile 裡的 `opentelemetry-instrument`。這是刻意的——先讓一個服務完整跑過一次「annotation 注入 → 真的比對 trace → 誠實講差異」，確認整條路徑沒問題，再決定要不要把其餘四個一起搬過去（會在後面某一天回頭處理，不在今天的範圍內）。OOMKilled 的排查也只做了一種部署形狀（獨立 Deployment 共用一份 collector），sidecar/daemonset 這類不同拓撲會不會有一樣的失效模式，留給之後有需要時再驗證，不在今天的範圍內硬湊。

明天：回到 Weaver 本身。今天證明了「資料會不會送到」是一個獨立的問題，明天處理另一個——資料送出來了，但**每個欄位叫什麼、代表什麼、必不必填，由誰決定**。先講清楚 telemetry 為什麼需要 schema（那是團隊共識，不是資料庫 schema），再跑第一次 `weaver registry check`，最後用 `infer` 從這支服務的真實流量反推一份草稿，看看 `userId`／`user_id` 這兩套並存的命名會不會被一起學進去。
