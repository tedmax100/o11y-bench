# OpenTelemetry Weaver — 教學與本專案用法

> 一份「從零理解 Weaver」+「我們在 `demo-services` 怎麼用」的深入文件。
> 對應程式碼：[`demo-services/weaver/`](../demo-services/weaver/)。

---

## 0. TL;DR

- **Weaver 是什麼**：管理「semantic conventions（語意慣例）」的工具——你的 telemetry
  （metrics / logs / traces）的屬性與模型該怎麼命名、什麼型別、低/高基數，
  全部寫成一份 YAML registry，然後用 Weaver 去 **驗證 / 產文件 / 產程式碼 /
  比對線上資料**。
- **它治理的單位是「attribute（屬性）」**，span / metric / event 只是把屬性掛上去的容器。
- **本專案怎麼用**：在 `demo-services/weaver/` 建了一份 registry，涵蓋三種訊號的慣例，
  加了一條自訂 policy（高基數 `biz.*` 不准當 metric label），接進 `mise` 與 GitHub CI。

```bash
# 一行驗證（需 Docker）
bash demo-services/scripts/weaver.sh check --policy
```

---

## 1. 它解決什麼問題

在一個多服務系統裡，telemetry 的「慣例」通常散落各處、靠人腦與 code review 維持：

- 同一個概念，A 服務叫 `user_id`、B 服務叫 `userId`、C 服務叫 `uid`。
- 有人手滑把 `order_id` 加進 metric 的 label → Prometheus time-series 爆炸。
- log 欄位 schema 沒人寫下來，dashboard / 告警查詢全靠口耳相傳。
- 改了一個屬性名，不知道下游哪些 dashboard / 查詢會壞。

我們這個 repo 的 [`o11y_shared/events.py`](../demo-services/shared/src/o11y_shared/events.py)
其實已經用註解在「手動治理」這件事：

```python
class BizEvent(StrEnum):
    """... 每加一個就放大 sum by(event) 的 label 空間 ...
    Never include dynamic ids (order_id, user_id) here ..."""
```

這段「口頭規則」正是 Weaver 要自動化的東西——把它變成一份**機器可驗證的標準**，
放進 CI 自動擋。

---

## 2. 核心概念

### 2.1 Registry（登記表）

一個 **registry** = 一個資料夾，裡面有：

```
registry/
  manifest.yaml          # registry 的中繼資料（名稱、schema_url）
  model/
    *.yaml               # 一個或多個 model 檔，內含 groups
```

`manifest.yaml` 範例（我們的）：

```yaml
name: demo-services-biz
description: >-
  Semantic conventions for the o11y-bench demo-services telemetry ...
schema_url: https://tedmax100.github.io/o11y-bench/demo-services/schemas/0.1.0
```

> ⚠️ 版本演進注意：舊版欄位 `semconv_version` + `schema_base_url` 已 deprecated，
> 新版用單一 `schema_url`（結尾帶版本）。檔名也從 `registry_manifest.yaml`
> 改成 `manifest.yaml`。我們踩過這兩個 deprecation 警告，已用新寫法。

### 2.2 Group（群組）= 訊號的容器

每個 model 檔的核心是 `groups:`，每個 group 有一個 `type`：

| `type` | 對應訊號 | 關鍵欄位 |
|---|---|---|
| `attribute_group` | 共用 | `attributes`（定義一批可被別人 `ref` 的屬性） |
| `metric` | Metric | `metric_name`、`instrument`、`unit`、`attributes` |
| `event` | Log（結構化事件） | `name`（事件名）、`attributes`、`body` |
| `span` | Trace | `span_kind`、`attributes` |
| `resource` | Resource | `attributes` |

**重點心智模型**：你在 `attribute_group` 裡「定義」屬性（給它 id、型別、brief、
基數說明），然後在 metric / event / span 裡用 `ref:` 去「引用」它，再加上
`requirement_level`（required / recommended / opt_in / conditionally_required）。

### 2.3 Attribute（屬性）— 真正的主角

一個屬性的定義（節錄自我們的 `common.yaml`）：

```yaml
- id: app.outcome              # 屬性 id（= 實際 emit 的 key）
  stability: development       # development / stable / release_candidate
  brief: Terminal outcome of a business operation. Used as a metric/span label.
  type:                        # enum 用 members；或 string/int/double/boolean
    members:
      - id: created            # 程式可用的識別子（不能有點）
        value: created         # 實際送出的字串值
        brief: Order created.
        stability: development
      - id: authorized
        value: authorized
        ...
```

非 enum 的就直接 `type: string` / `type: int`：

```yaml
- id: biz.user.id
  type: string
  stability: development
  brief: The user identifier.
  examples: ["u-1", "u-17"]
```

### 2.4 命名空間（為什麼一定要有點）

Weaver 內建的 `otel.rego` policy 會對「沒有點分隔命名空間」的扁平屬性名報
`missing_namespace`。也就是說 `status`、`reason`、`user_id` 這種扁平 key
**不符合** semconv 慣例；要寫成 `app.outcome`、`app.fail_reason`、`biz.user.id`。

命名空間還有兩條相關檢查：
- `illegal_namespace` / `extends_namespace`：某屬性名剛好是另一個屬性的前綴
  （例如同時有 `app.upstream` 和 `app.upstream.status_code`）會被擋。
  → 我們因此把 `upstream` 拆成 `app.upstream.service`（string）和
  `app.upstream.status_code`（int），避免 `app.upstream` 既是屬性又是命名空間。

---

## 3. Weaver 的四個主要指令

| 指令 | 用途 | CI 價值 |
|---|---|---|
| `weaver registry check` | 驗證 registry 自身一致性 + 跑 policy | ⭐ 當 lint gate |
| `weaver registry resolve` | 把所有 ref / extends 攤平成單一解析後文件 | debug / 給其他工具吃 |
| `weaver registry generate` | 用模板產出 code / docs（Jinja-like） | 單一真實來源 |
| `weaver registry live-check` | 拿**線上實際 OTLP** 比對 registry | ⭐ 抓 cardinality drift |

三種訊號裡，只有 **`live-check` 會跨三訊號一起看**——它吃進 traces + metrics + logs
的真實資料，逐筆比對 registry，產一份 compliance 報告（缺屬性、型別不符、
用了沒登記的屬性…）。

---

## 4. Policy（用 Rego 寫自訂規則）

`check` 預設會跑內建的 semconv policies（命名空間、格式…）。你也可以加自己的
**Rego**（OPA 語言）規則。policy 結構是：package `after_resolution`，
規則 `deny contains ...`，吃的是「解析後的 registry」（`input.groups[_]`）。

我們的 [`policies/biz_policies.rego`](../demo-services/weaver/policies/biz_policies.rego)：

```rego
package after_resolution
import rego.v1

# biz.* 是高基數識別碼，不准當 metric label
deny contains high_cardinality_metric_label(group.id, attr.name) if {
	group := input.groups[_]
	group.type == "metric"
	attr := group.attributes[_]
	startswith(attr.name, "biz.")
}

high_cardinality_metric_label(group_id, attr_id) := violation if {
	violation := {
		"id": "high_cardinality_metric_label",
		"type": "semconv_attribute",
		"category": "attribute",
		"group": group_id,
		"attr": attr_id,
	}
}
```

這條 policy 把 `events.py` 那段口頭規則變成自動檢查。我們做過**負向測試**：
故意把 `biz.user.id` 塞進一個 metric，`check --policy` 立刻 exit 1 並印出：

```
Violation: semconv_attribute
  - Message: id=high_cardinality_metric_label, ... attr=biz.user.id
```

---

## 5. 我們怎麼用（本專案實作走讀）

### 5.1 檔案佈局

```
demo-services/weaver/
  registry/
    manifest.yaml
    model/
      common.yaml      # app.* / biz.* 屬性 + resource group
      metrics.yaml     # 6 個 metric 慣例
      events.yaml      # 每個 BizEvent → 一個 event group
      spans.yaml       # 3 個業務 span 慣例
  policies/
    biz_policies.rego  # 自訂高基數 policy
  README.md
demo-services/scripts/weaver.sh   # 包 otel/weaver 容器的執行器
```

### 5.2 我們先盤點「真實 telemetry 表面」

寫 registry 之前，先把五個服務實際 emit 的東西全盤出來——registry 要對齊真實
程式碼才有意義：

- **Metrics**：`orders_total`(status,reason)、`order_create_duration_seconds`(status)、
  `payment_charges_total`(status,reason)、`payment_charge_duration_seconds`(status)、
  `user_lookups_total`(op)、`user_auth_checks_total`。
- **Logs（events）**：`BizEvent` 全部成員，加上各 call site 傳的 `extra={...}` 欄位
  （`user_id`、`order_id`、`amount_cents`、`reason`、`upstream`…）。
- **Spans**：目前是 FastAPI/httpx **auto-instrumentation**，沒有手寫 span。

### 5.3 三個訊號各自怎麼模

**Metric**（`metrics.yaml`）：

```yaml
- id: metric.app.orders.count
  type: metric
  metric_name: app.orders.count
  instrument: counter
  unit: "{order}"
  attributes:
    - ref: app.outcome
      requirement_level: required
    - ref: app.fail_reason
      requirement_level:
        conditionally_required: when `app.outcome` is not `created`.
```

**Event / Log**（`events.yaml`）— 每個 BizEvent 一個 group，`name` 就是事件名：

```yaml
- id: event.order.created
  type: event
  name: order.created
  attributes:
    - ref: biz.order.id
      requirement_level: required
    - ref: biz.user.id
      requirement_level: required
    - ref: biz.amount_cents
      requirement_level: required
```

**Span**（`spans.yaml`）— 業務操作的「目標慣例」，等之後手寫 span 時要滿足：

```yaml
- id: span.app.order.create
  type: span
  span_kind: server
  attributes:
    - ref: biz.user.id
      requirement_level: required
    - ref: app.outcome
      requirement_level: required
    - ref: biz.order.id
      requirement_level:
        conditionally_required: when the order was created.
```

### 5.4 兩個關鍵設計決策

1. **registry 是「目標標準」，不是現狀鏡像。**
   我們把屬性名全部寫成 **命名空間化**（`app.*` 低基數、`biz.*` 高基數識別碼），
   讓 `weaver registry check` 乾淨通過。但現在服務還是 emit 扁平 key
   （`status`、`user_id`…）。扁平 → 命名空間的對照表寫在
   [`weaver/README.md`](../demo-services/weaver/README.md)，那份表就是日後 migration 的
   checklist。`live-check` 對線上現狀會報這些 gap——那份報告就是待辦清單。

2. **這次不動服務程式碼。**
   因為 `git_repo` / `service` / `status` 這些 key 會被 OTel Collector 提升成
   Loki / Prometheus 的 label，改名會牽動 Grafana dashboard 與 grading 查詢，
   所以 conformance（讓 live-check 轉綠）刻意留作後續 PR。

### 5.5 模型過程順手抓到的真實 bug（治理的價值）

- **`status` 一詞兩義**：在 metric 上是業務結果 enum（`created`/`authorized`…）；
  但在 `api-gateway` / `webapp` 的 log 裡卻是 HTTP 整數狀態碼
  （`status=resp.status_code`）。registry 把它拆成 `app.outcome`（enum）與
  `app.upstream.status_code`（int）——這兩個 log 點應該改用後者。
- **`reason` enum 過寬**：混了 metric-label 用值（`auth`、`payment`）與 log-only 用值
  （`auth_failed`、`new_validator_odd_cents`）。先當一個 open enum，未來值得拆。

> 這就是 Weaver 的核心價值：**逼你把慣例寫下來的過程，本身就會照出不一致。**

### 5.6 接進工具鏈

**執行器** [`scripts/weaver.sh`](../demo-services/scripts/weaver.sh)：包 `otel/weaver:v0.24.0`
容器（釘版本、免本機安裝、自動判斷有沒有 tty 所以 CI 也能用）。

**mise 任務**（`mise.toml`）：

```toml
[tasks."weaver:check"]
dir = "demo-services"
run = "bash scripts/weaver.sh check --policy"
```

**GitHub CI**（`.github/workflows/ci.yml`）：新增獨立 job，`ubuntu-latest` 內建 Docker，
不需 Python 環境：

```yaml
  weaver:
    name: Weaver Registry
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@...
      - run: bash demo-services/scripts/weaver.sh check --policy
```

> 踩雷紀錄：這個 repo 是 **fork**，且 workflow 的 `push` 原本只認 `main`，
> 所以 push 到 `feat/*` 分支不會觸發。我們在 `push.branches` 加了 `feat/**`，
> 之後 push feature 分支就會自動跑 CI（含 Weaver job），實測 `Weaver Registry`
> job 在真 CI 為 **success ✅**。

---

## 6. 怎麼跑

```bash
# 需要 Docker 在跑（本機用 colima 也可）
mise run weaver:check                              # 內建 + 自訂 policy
mise run weaver:docs                               # 產 Markdown 文件（需模板，見下）

# 或直接呼叫
bash demo-services/scripts/weaver.sh check         # 只跑內建 policy
bash demo-services/scripts/weaver.sh check --policy # + 自訂 policy
```

成功時長這樣：

```
Weaver Registry Check
Checking registry `/home/weaver/registry`
ℹ Found registry manifest: /home/weaver/registry/manifest.yaml
✔ No `after_resolution` policy violation
```

---

## 7. 語言無關性（Java / Go / 其他語言都能用）

Weaver 本體是一支 **Rust 二進位**，registry 是純 YAML，所以大部分功能跟你服務
用什麼語言**完全無關**。唯一跟語言有關的只有 `generate`（codegen），因為它要一套
對應語言的 Jinja 模板。

| 功能 | 是否跟語言有關 | 說明 |
|---|---|---|
| `check` / `resolve` | ❌ 無關 | 驗的是 YAML registry 本身 |
| `live-check` | ❌ 無關 | 比對的是 **OTLP**。Java/Go/Python SDK 送出的 traces/metrics/logs 長得一樣，**同一份 registry 照樣驗** |
| `generate` 產 docs（Markdown） | ❌ 無關 | 文件模板與語言無關 |
| `generate` 產**程式碼** | ✅ 需該語言模板 | 例如產 Java 的屬性 key 常數類別 / enum |
| OTel SDK 端「runtime 自動套 registry」 | — | **沒有這種東西**（任何語言皆然）。registry 是建置/CI 期治理，不是 runtime 注入 |

**關鍵推論**：我們 `demo-services/weaver/` 這份 registry 是語言中立的。哪天某個服務
從 Python 改寫成 Java，慣例治理（`check` / `live-check`）一行都不用改。

### Java codegen

要從 registry 產 Java 程式碼，就餵一套 Java 模板：

```bash
weaver registry generate -r registry/ java ./out --templates templates/
```

OTel 官方的 Java semconv artifact（`io.opentelemetry.semconv:opentelemetry-semconv`）
本身就是用 Weaver 這套機制、從官方 registry 產出來的——所以「Weaver → Java code」
是已被官方驗證的成熟路徑。你可以沿用官方/社群的 Java 模板，或自己寫一份對應我們的
`app.*` / `biz.*` 命名。

> 對任何語言都成立的一點：Weaver 不會在 runtime 改你的 telemetry。要讓程式真的
> 用上這些命名，是「codegen 產出常數 → 程式 import 使用」這條路（Python 也一樣）。

---

## 8. 怎麼擴充

- **新增一個業務事件**：在 `events.yaml` 加一個 `event` group（記得 `name` + 屬性 ref），
  若用到新屬性，先在 `common.yaml` 的 `attribute_group` 定義它。
- **新增一條 cardinality 規則**：在 `biz_policies.rego` 加一條 `deny`。
- **想反向產 `events.py`**：用 `weaver registry generate` + Jinja 模板，
  讓 registry 變成 enum 的單一真實來源（見 §8）。

---

## 9. 後續路線（尚未做）

1. **`generate` 反吃回程式碼**：寫 `weaver/templates/` 的 Jinja 模板，從 registry
   產出 `events.py` 的 enum 與屬性常數 → registry 成為單一真實來源。
2. **服務 migration**：把扁平 key 改成命名空間 key，讓 `live-check` 轉綠
   （需同步處理 Loki/Prometheus label 與 grading 查詢）。
3. **`live-check` 進 CI**：對跑起來的 demo 比對 registry，抓「冒出來但沒登記」的
   屬性（cardinality drift）。

---

## 10. 參考連結

- Weaver repo：<https://github.com/open-telemetry/weaver>
- 官方介紹文：<https://opentelemetry.io/blog/2025/otel-weaver/>
- 本專案 registry 說明：[`demo-services/weaver/README.md`](../demo-services/weaver/README.md)
