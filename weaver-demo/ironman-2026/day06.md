---
title: "【Day6】Collector 三種部署模式實測，順便主動調到 OOMKilled"
series: "2026 鐵人賽：AIOps with OpenTelemetry"
tags: [OpenTelemetry, Kubernetes, 鐵人賽]
---
# Day6：Collector 三種部署模式實測，順便主動調到 OOMKilled

`demo-services/k8s/13-otel-collector.yaml` 裡現在跑的其實已經是三種模式裡的一種——一個獨立的 `Deployment`，五個服務全部把 `OTEL_EXPORTER_OTLP_ENDPOINT` 指到同一個 `otel-collector.demo.svc:4318`。這是 **gateway 模式**：叢集裡一份 collector，所有服務共用。今天要做的，是把 Day4 裝好的 Operator 派上用場，用 `OpenTelemetryCollector` CR 把同一份 config 分別跑成 **sidecar**（每個 Pod 自己帶一個 collector）跟 **daemonset**（每個 node 一個 collector），三種模式對同一支服務量出真實的 CPU / 記憶體 / 延遲數字，再誠實地把其中一種模式的 resource limit 調低，看著它真的開始丟資料，走一次「發現資料變少 → 定位是 collector 被 OOMKilled」的排查過程。

## 三種模式在管什麼

Day3 講過 `OpenTelemetryCollector` CR 的 `spec.mode` 決定 Operator 怎麼幫你把 collector 部署出去，今天真正把三個值都跑一次：

- **`deployment`（=今天叫的 gateway）**：獨立跑，多個服務共用同一份 collector。現在 `13-otel-collector.yaml` 手動維護的就是這個形狀，只是不是 Operator 管的。
- **`sidecar`**：collector 跟著 app 容器一起進同一個 Pod，靠 `sidecar.opentelemetry.io/inject: "true"` 這個 annotation 觸發 webhook 注入。
- **`daemonset`**：每個 node 一份，同 node 上的 Pod 就近送到本機的 collector，不用跨 node 打网路。

三份 CR 用的是同一份 `config.yaml`（跟現有 `13-otel-collector.yaml` 裡那份幾乎一樣，receivers/processors/exporters 不變，只有 `spec.mode` 不同），這樣量出來的差異才只反映「部署形狀」，不是配置差異造成的雜訊。

```yaml
# TODO：三份 CR（otelcol-gateway.yaml / otelcol-sidecar.yaml / otelcol-daemonset.yaml）
# 的完整 spec，跑完之後貼上來。
```

## 量測方法：同一支服務、同一組流量，切三次模式

固定用 `api-gateway` 當量測對象（Day5 剛把它轉成 annotation 注入，狀態最乾淨），流量用同一份 k6/xk6 腳本跑固定 QPS、固定時長，每種模式各跑一輪，中間留冷卻時間讓上一輪的資源用量歸零。

量三件事：
- **collector 容器的 CPU/記憶體**：`kubectl top pod` 搭配 Prometheus 上 collector 自己曝的 `otelcol_process_cpu_seconds`/`otelcol_process_memory_rss` 這類 self-telemetry 指標。
- **端到端延遲**：app 發出 span 到 Tempo 真的能查到這條 trace 之間的落差，不是 k6 量的 HTTP RTT——因為三種模式差異主要發生在 app→collector→backend 這段，不是使用者感受到的 HTTP 延遲。
- **是否有掉資料**：送出去的 span 數 vs. Tempo 裡查得到的 span 數，兩邊對不上就是掉資料的訊號，這個指標留給下一節的 OOMKilled 排查用。

```
# TODO：三種模式的實測數字，跑完貼這裡
模式         collector CPU (m)   collector Mem (Mi)   p50 延遲   p99 延遲   掉資料？
deployment   ?                   ?                     ?         ?          ?
sidecar      ?                   ?                     ?         ?          ?
daemonset    ?                   ?                     ?         ?          ?
```

預期會看到的方向（跑完要驗證，不是先射箭畫靶）：sidecar 因為沒有跨 Pod 網路一跳，延遲應該最低，但因為每個 Pod 都帶一份 collector，總 CPU/記憶體開銷會被 replica 數放大；daemonset 界於中間；gateway 集中式最省資源，但共用一份 collector 代表這份 collector 的資源上限，就是全部服務的共同天花板——這正好是下一節要示範的東西。

## 主動把其中一種模式調到丟資料

選 **gateway 模式**動手調低 resource limit——因為它是「共用天花板」的形狀，最容易示範「一份 collector 被壓垮，會連帶影響好幾個服務」這件事，也最貼近真實世界裡最常見的踩坑模式（大家都覺得一份 collector 夠用，直到流量長大）。

```yaml
resources:
  limits:
    memory: "TODO：先跑一次正常負載記下平常用量，再往下調到明顯不夠"
```

調低之後重新對 `api-gateway` 送同一組流量，這次盯着三件事：

1. `kubectl get pods -n demo -w`——等 `otel-collector` 那個 Pod 出現 `OOMKilled`。
2. `kubectl describe pod otel-collector-... | grep -A5 "Last State"`——確認真的是 `OOMKilled` 不是別的原因（比如 liveness probe 失敗、或者只是 CrashLoopBackOff 的第一層症狀）。
3. Tempo 那邊查同一段時間的 trace 數量——這是「使用者視角」會先看到的症狀：不是報錯，是資料悄悄變少。

```
# TODO：真實 kubectl describe 輸出 + Tempo 查詢對照，貼這裡
```

## 排查順序：從「資料變少」倒推回「collector 被殺」

這段是今天最值得記錄的部分——不是「我知道答案所以我調低了 limit」，是反過來示範一次「如果你不知道原因，會怎麼一步步查到 collector 身上」：

1. **先確認不是 app 沒送**：查 app 容器的 log，確認 exporter 沒有報錯（如果 app 端已經在噴 `Failed to export` 之類的訊息，代表問題不在 collector 內部，是連線層級的，排查方向完全不同）。
2. **再確認是不是 collector 收到了但沒送出去**：`kubectl port-forward` 到 collector 的 metrics endpoint，看 `otelcol_receiver_accepted_spans` 跟 `otelcol_exporter_sent_spans` 這兩個數字有沒有落差——有落差代表資料卡在 collector 內部，不是 receiver 沒收到。
3. **最後才看 Pod 本身健不健康**：`kubectl get pod`、`kubectl describe pod` 看重啟次數跟 `Last State` 的 `Reason`。

```
# TODO：實際跑三步的輸出貼上來，尤其是 otelcol_receiver_accepted_spans
# vs otelcol_exporter_sent_spans 這組數字落差
```

這個順序刻意跟直覺相反——大部分人踩到「資料變少」的第一反應是去查 app，因為症狀是在 app 這邊的下游（dashboard 空的、trace 查不到）浮現的。但 app 端往往是無辜的：它已經把 span 送出去了，只是 collector 那端在半路把它吃掉。**症狀出現的地方，不一定是問題發生的地方**——這也是為什麼 Day1 就在鋪陳這系列會反覆回來講的東西：可觀測性系統本身的健康狀態，也需要被觀測，不能預設它永遠正常運作。

## 今天沒做的事

三種模式的量測只對 `api-gateway` 一個服務跑，其餘四個服務目前還是走 Day5 之前的舊路徑或尚未切換；量出來的數字也還沒有回頭去更新 Day18 那份「新服務上線 checklist」——要不要把「資源配置該選哪種模式」寫進 checklist，留到 Phase 1 收尾那天再決定。另外今天只示範了記憶體被壓垮的情境，CPU throttling 導致的資料延遲/丟失是另一種更隱蔽的失效模式，這系列目前還沒有踩過，先誠實記下這個空白。

明天：把這份 Operator 設定從 `kubectl apply` 搬進 GitOps，講 PR review 這類 CRD 改動時該看什麼。
