---
title: "【Day6】Operator 設定轉 GitOps：從 kubectl apply 到可 PR review 的 Kustomize"
series: "2026 鐵人賽：AIOps with OpenTelemetry"
tags: [OpenTelemetry, Kubernetes, GitOps, 鐵人賽]
---
# Day6：Operator 設定轉 GitOps——CRD 從 `kubectl apply` 改成可 PR review 的檔案

先老實說這天的份量：這不是 Day10-11 那種「weaver 自動攔命名漂移」等級的治理機制。今天沒有新工具去偵測任何東西，`kubectl kustomize` 也不會幫你檢查漏加 annotation 這種語意錯誤——它做的事很小，就是把 Day4、Day5 那個「改一個 YAML、`kubectl apply -f`、看 Pod 有沒有起來」的迴圈，換成一個能被 GitOps controller（Argo CD/Flux）以同樣邏輯解析的單一入口，順便寫下一份目前只能人肉核對的 checklist。這是一天偏基礎設施衛生（PR workflow）的日子，不是這系列因果鏈「治理→資料可信度→agent 決策」裡的必經環節；放在這裡單純是因為它是 Day11 CI Gate 要接的同一個「單一入口」的前置動作——CI 要在 PR 上跑檢查，總得先有一個東西可以跑檢查，而不是 N 個檔案。今天只把這個入口做出來，檢查本身留給 Day11 真正落地。

這個舊流程撐不住的地方，跟「YAML 對不對」無關，跟「審查」有關：**沒有任何一個地方讓第二個人在東西真的套用到叢集之前，先看一眼「這個改動會不會讓某個服務突然沒有 trace」**。

## 改動前：`up.sh` 在做的事

```bash
# Day5 為止
for f in "${ROOT}"/k8s/[0-9]*-*.yaml; do
  kubectl apply -f "$f"
done
```

這一段能動，但有兩個問題，都跟「審查」有關，跟「YAML 對不對」無關：

1. **沒有單一產出物可以 diff。** 一個 PR 改了 `16-instrumentation.yaml` 也改了 `23-api-gateway.yaml`，reviewer 看到的是兩個獨立的檔案 diff，要自己在腦中重建「這兩個改動合起來，套用到叢集之後會長什麼樣子」。檔案數一多，這件事沒人會真的做。
2. **沒有東西模擬 GitOps controller 實際上會做的事。** 如果之後要接 Argo CD / Flux，它們不會逐檔案 `kubectl apply`，而是先把整包 resources 解析成一份 manifest 再套用。如果本地流程跟這個行為不一致，「本地測試過」跟「GitOps 套用出來」就可能是兩個不同的結果。

## 改動：一個 `kustomization.yaml`

```yaml
# k8s/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - 00-namespace.yaml
  - 10-prometheus.yaml
  - 11-loki.yaml
  - 12-tempo.yaml
  - 13-otel-collector.yaml
  - 14-grafana.yaml
  - 15-aiops-agent.yaml
  - 16-instrumentation.yaml
  - 20-payment-service.yaml
  - 21-user-service.yaml
  - 22-order-service.yaml
  - 23-api-gateway.yaml
  - 24-webapp.yaml
```

沒有 template、沒有 overlay、沒有 `commonLabels`——刻意選最小可行的一步。這個系列裡的服務目前只有一個環境（demo），套 Helm 的 values 樣板化或 Kustomize 的多 overlay 都是「還沒有這個需求就先設計」，屬於這系列一路強調要避免的過度工程。今天要解決的問題只有一個：**把「哪些檔案算數」這件事宣告出來，變成一個可以被單一指令解析的清單**。

`up.sh` 對應改成：

```bash
# Day6
kubectl kustomize "${ROOT}/k8s" | kubectl apply -f -
```

`kubectl kustomize` 把 `resources` 清單解析成一份完整的 manifest 流，這正是 Argo CD / Flux 指向這個目錄時會做的同一件事——本地跑的指令跟 GitOps controller 實際套用的邏輯第一次一致。

## 為什麼特地沒加 `commonLabels`

Kustomize 一個常見的入門用法是加 `commonLabels`，讓每個資源都被蓋上同一組標籤。這裡刻意沒加，因為 `23-api-gateway.yaml` 的 Pod 有一個 `git_version` label，透過 Downward API 餵進 `OTEL_RESOURCE_ATTRIBUTES`：

```yaml
- name: GIT_VERSION
  valueFrom:
    fieldRef:
      fieldPath: metadata.labels['git_version']
```

`commonLabels` 會蓋到這個 selector 依賴的欄位空間——它不會讓 apply 失敗，只會讓某個現有的隱含契約（label 跟 fieldRef 的對應）多一層不確定性。這正是今天真正想留下的東西：**GitOps 工具本身不會告訴你「這樣改安不安全」，語法檢查跟安不安全是兩件事**。

## PR 該看什麼：一份寫給 reviewer 的 checklist

`kubectl kustomize` 能保證的只有「這是合法的 Kubernetes 資源集合」。它保證不了的、也是今天新增的 `GITOPS-REVIEW.md` 想收斂的：

1. **新增/改名一個 Deployment，卻沒有同步碰 `16-instrumentation.yaml` 或它的 annotation。** 少了 `instrumentation.opentelemetry.io/inject-python` 這個 annotation，Pod 一樣會 Ready、一樣能 serve 流量，但不會有任何 trace 送出去——`kubectl apply` 不會報錯，`kubectl get pods` 也看不出來。
2. **改掉一個被別的資源依賴的 label。** 前面提過的 `git_version` 就是一個例子——rename 它不會讓 apply 失敗，會讓 `service.version` 這個 resource attribute 從此悄悄消失在新的 span 裡。
3. **改 `13-otel-collector.yaml` 的 resource limits。** Day5 才示範過 collector 被壓到 `OOMKilled` 時，app 端完全看不到任何 exporter 錯誤訊息——這代表這個檔案的 resource 數字改動，審查規格應該跟改一段程式碼一樣嚴謹，而不是「反正只是個數字」。
4. **`16-instrumentation.yaml` 的 exporter endpoint 跟 `13-otel-collector.yaml` 的 Service name 是不是還對得上。** 這兩個檔案靠字串（`http://otel-collector.demo.svc:4318`）互相參照，沒有任何 schema 幫你檢查這條連結還在不在——這正是 Day10 之後 weaver 要解決的「命名漂移」問題的 Kubernetes YAML 版本，只是這裡還沒有工具幫忙攔。
5. **diff 的範圍跟 PR 宣稱的改動一致嗎？** 因為現在有單一入口，`kubectl kustomize k8s/ | kubectl diff -f -` 可以對出「這個 PR 實際上會改動叢集裡的哪些東西」——包含作者沒有意識到自己動到的部分（例如不小心加的 `commonLabels` 蓋到不該蓋的資源）。這個指令比讀 PR 的檔案級 diff 更誠實。

這五條裡，只有第 3 條（collector resource limits）是 Day5 真的重演過一次「不做會出事」的案例——調低 limit、看著 Pod 被 `OOMKilled`、在 Tempo 查到 trace 真的變少。其餘四條目前都只是「這樣做會很危險」的推論，沒有一條被實際示範過。誠實講清楚這份 checklist 現在的地位：它是提醒 reviewer 該看哪裡的清單，不是任何形式的自動防護，也還稱不上治理——會不會真的被人照著看、看不看得出來，今天完全沒有驗證過。要讓它變成真正的攔截點，下一步是把清單裡至少一條變成可執行的檢查（例如 CI 裡跑一個 script 確認每個 Deployment 都有對應的 instrumentation annotation）——那才是從「PR 衛生習慣」升級成「治理」的那一步，今天沒有做到。

## 今天沒做的事

沒有導入 Argo CD 或 Flux 本身——今天只把「本地流程跟 GitOps controller 的行為對齊」這一步做完，實際接上一個 GitOps controller、讓套用動作真的從 Git push 觸發，是另一天的範圍，不在今天硬湊。也沒有把 `kustomization.yaml` 拆成 base/overlay——目前只有一個環境，拆分是等有第二個環境（例如 staging）出現時才有意義的改動，現在做只是提前設計一個不存在的需求。也沒有把 `GITOPS-REVIEW.md` 這份人肉 checklist 自動化成任何形式的 policy-as-code——那正是 Day10-11 weaver + Rego 要做的事，這裡刻意留白，是為了讓那兩天的「從人工到自動」這個對比是真的，不是提前把答案劇透掉。

明天：回到 Weaver 本身，先把「為什麼 telemetry 需要 schema」這個問題講完整——一天純概念，不跑任何指令，是接下來好幾天動手做的地圖。
