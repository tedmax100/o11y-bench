# 官方 Demo 3.0 把 Weaver 搬進了生產：拆解 opentelemetry-demo 的 telemetry-schema

> 驗證環境：weaver 0.25.1（本機）、opentelemetry-demo `3684411`（2026-07-29）、demo 3.0.0（2026-07-24 發布）
> 本文所有指令都在本機實跑過，輸出直接貼在文中。

2026 年 7 月 24 日，opentelemetry-demo 發了 3.0.0，官方部落格的標題直接叫 [_We Broke the Demo_](https://opentelemetry.io/blog/2026/we-broke-the-demo/)。

**他們把所有自訂屬性從 `app.*` 改名成 `demo.*`。**每一個服務、每一段 instrumentation、每一個 dashboard、每一個 6859 個 fork——全部受影響。

這篇不是講「demo 3.0 有什麼新功能」（那篇部落格自己講得很好）。這篇要拆的是**一件對我們更有用的事**：這次改名之所以能發生，關鍵是 Weaver；而 3.0 順手把一整套 Weaver 工作流搬進了 repo——`telemetry-schema/` 目錄、CI gate、還有一個把 schema 跑成網站的服務。

**這是目前公開可看、semconv 之外最完整的 Weaver 生產級用法。**而且它用的是 `definition/2`。

---

## Part 1 — 為什麼非改不可，以及為什麼拖了三年

`app.*` 這個命名空間，demo 從 2022 年就在用。問題是 **2023 年 OpenTelemetry 正式把 `app.*` 保留給「client-side application attributes」**——一個官方 demo 帶頭違反自己的命名規範，這件事卡了很久。

官方文章講了拖三年的兩個原因：

1. **沒有工具做系統性轉換。**跨十幾種語言、十幾個服務，手工改名等於保證漏。
2. **沒有人力。**

兩個障礙後來一起被解掉：Martin Thwaites 把 **Weaver** 導進來做自動化轉換，Florian Bourgey 透過 Bloomberg mentorship 加入。

翻 CHANGELOG 可以看到改名是**拆成一系列 PR 逐個 domain 進行**的，不是一次大爆炸：

```text
* [telemetry] Rename payment telemetry attributes:
  `app.payment.amount` to `demo.payment.amount`,
  `app.payment.card_type` to `demo.payment.card_type`,
  … across checkout, payment, and telemetry schema.   (#3390)

* [telemetry] Rename shipping and quote telemetry attributes:
  `app.shipping.amount` to `demo.shipping.amount`,
  `app.quote.items.count` to `demo.shipping.quote.items_count`,
  … across checkout, quote, shipping, and telemetry schema.   (#3391)
```

注意每一條的結尾都是 **「across ⟨服務們⟩, and telemetry schema」**——**schema 跟程式碼是同一個 PR 一起改的**。這就是有 registry 跟沒有 registry 的差別：改名不再是「全域搜尋取代，祈禱沒漏」，而是「改 schema，然後讓 schema 告訴你哪些地方要跟著改」。

順帶一提，這次改名不只是加前綴，有幾個是**趁機修語意**的：

| 舊 | 新 | 為什麼 |
| --- | --- | --- |
| `app.user.id` | `user.id` | 官方 semconv 已經有了，**直接刪掉自訂的** |
| `app.quote.items.count` | `demo.shipping.quote.items_count` | 併進 shipping 命名空間 |
| `app.currency.conversion.from` | `demo.exchange.from` | 改成業務語言 |
| `app.cache_hit` | `demo.recommendation.cache_hit` | 補上歸屬的服務 |
| `app_recommendations_counter` | `demo.recommendation.requests` | 從 Prometheus 風格改成 OTel 風格 |

**`app.user.id` → `user.id` 那條最值得學**：整理自己的 registry 時，第一件事應該是問「這個屬性官方是不是已經有了」。demo 的答案是有，於是自訂的那個直接刪除。

實測現在的 repo：

```bash
grep -rn 'app\.product\.\|app\.cart\.\|app\.order\.\|app\.payment\.\|app\.user\.' src/ | wc -l
# → 0     清乾淨了
```

---

## Part 2 — `telemetry-schema/`：真實世界最大的 `definition/2` registry

改名只是導火線，留下來的資產是這個目錄。

```bash
find telemetry-schema -type f | wc -l     # → 32
grep -rl "definition/2" telemetry-schema/ | wc -l   # → 31（manifest.yaml 不算）
```

**31 個檔案，全部是 `file_format: definition/2`。**

這件事本身就很值得說：`definition/2` 目前狀態是 Alpha，weaver 自己會警告「not yet stable」，官方 semconv 也只搬了 24/250 個檔案。**而 opentelemetry-demo 整包 registry 從第一天就是 v2。**要看真實世界怎麼寫 v2，這裡是目前最好的範本。

### 三層目錄結構

```text
telemetry-schema/
├── manifest.yaml
├── attributes/      11 個檔案 — 按「業務領域」切
│   ├── ad.yaml  cart.yaml  exchange.yaml  feature_flag.yaml
│   ├── order.yaml  payment.yaml  product.yaml  recommendation.yaml
│   └── request.yaml  shipping.yaml  user.yaml
├── services/        13 個檔案 — 按「服務」切，一個服務一個檔
│   ├── ad.yaml  cart.yaml  checkout.yaml  currency.yaml  email.yaml
│   ├── frontend.yaml  load_generator.yaml  payment.yaml  product_catalog.yaml
│   └── quote.yaml  react_native_app.yaml  recommendation.yaml  shipping.yaml
└── metrics/         7 個檔案 — 按「服務」切
    └── ad.yaml  cart.yaml  currency.yaml  email.yaml
        payment.yaml  recommendation.yaml  shipping.yaml
```

**這個「屬性按領域、用法按服務」的切法是整套設計的核心**，它正好對應到 v2 的「定義 vs 使用」分離。

**`attributes/cart.yaml`——只講「這個屬性是什麼」**：

```yaml
file_format: definition/2
attributes:
  - key: demo.cart.items.count
    type: int
    brief: Number of items in cart
    stability: stable
    note: The count of items currently in the shopping cart
    examples: [0, 3, 7]
```

**`services/cart.yaml`——只講「cart 服務會用哪些屬性」**：

```yaml
file_format: definition/2
attribute_groups:
  - id: service.cart
    visibility: public
    brief: Cart service attributes
    stability: stable
    attributes:
      - ref: user.id                  # ← 官方 semconv 的
      - ref: demo.product.id          # ← 定義在 attributes/product.yaml
      - ref: demo.product.quantity
      - ref: demo.cart.items.count    # ← 定義在 attributes/cart.yaml
```

**注意 `service.cart` 這個 group 跨越了三個來源**：官方 semconv 的 `user.id`、product 領域的兩個、cart 領域的一個。這正是 v2 `ref` 組合能力的價值——**「服務用什麼」和「屬性屬於哪個領域」是兩個正交的維度**，v2 讓你可以分別表達，不用二選一。

**`metrics/cart.yaml`**：

```yaml
file_format: definition/2
metrics:
  - name: demo.cart.add_item.latency
    brief: Cart add item operation latency
    instrument: histogram
    unit: "s"
    stability: stable
    annotations:
      service: cart          # ← 這個 annotation 等下會變成文件的導覽結構
```

`annotations.service` 是**自訂的** annotation，semconv 規範裡沒有這個東西。它的用途下一節揭曉。

### manifest：把官方 semconv 當依賴

```yaml
id: otel-demo
name: OpenTelemetry Demo Telemetry Schema
semconv_version: 1.40.0
schema_url: https://opentelemetry.io/schemas/1.40.0
dependencies:
  - name: otel
    registry_path: https://github.com/open-telemetry/semantic-conventions/archive/refs/tags/v1.40.0.zip[model]
```

**釘在 v1.40.0 的 zip，不是 main。**這就是為什麼 `services/cart.yaml` 可以直接 `ref: user.id`——它從官方 registry 引進來的。

manifest 的 description 還藏了一句很誠實的話：

> This registry was generated by **analyzing actual attribute usage across the codebase**, not copied from an external source. It reflects the real telemetry instrumentation in the demo application.

**registry 是從既有程式碼「逆向」出來的，不是先設計再實作。**這對想在 brownfield 專案導入 Weaver 的人是很重要的路徑確認——你不需要先有完美的 schema 才能開始。

---

## Part 3 — `telemetry-docs`：schema 變成一個跑起來的服務

這是我覺得最值得抄的部分。demo 3.0 新增了一個叫 `telemetry-docs` 的服務，**它的唯一工作就是把 registry 變成一個網站**，跟其他 19 個服務並排列在 `compose.yaml` 的 `services:` 底下：

```bash
make start
# …
# Go to http://localhost:8080/telemetry/ for the Weaver generated telemetry documentation.
```

三段式 Dockerfile：

```dockerfile
# Stage 1: Weaver 產生 Markdown
FROM docker.io/otel/weaver:v0.25.0 AS registry-builder
COPY telemetry-schema /workspace/telemetry-schema
COPY src/telemetry-docs/templates /workspace/templates

RUN /weaver/weaver registry generate \
    --registry=/workspace/telemetry-schema \
    --templates=/workspace/templates \
    markdown \
    /workspace/docs

RUN /weaver/weaver registry package \
    --registry=/workspace/telemetry-schema \
    --resolved-schema-uri=https://opentelemetry.io/schemas/1.40.0 \
    --output=/workspace/docs/schema \
    --v2

# Stage 2: MkDocs build 成靜態站
FROM python:3.14.6-slim-bookworm AS site-builder

# Stage 3: nginx（帶 OTel instrumentation）伺服
FROM nginxinc/nginx-unprivileged:<version>-otel
```

**第三段最有巧思**：文件站自己也是被 instrument 的，而且處理了高基數問題：

```text
/attributes/*.html  →  GET /attributes/{business_domain}
/services/*.html    →  GET /services/{service_name}
```

**一個講「怎麼做好可觀測性」的文件站，自己示範了低基數 span 命名。**

### 實跑一次 generate

```bash
weaver registry generate \
  --registry=telemetry-schema \
  --templates=src/telemetry-docs/templates \
  markdown /tmp/demodocs
```

```text
/tmp/demodocs/README.md
/tmp/demodocs/mkdocs.yml
/tmp/demodocs/attributes/{ad,cart,exchange,feature_flag,order,payment,
                          product,recommendation,request,shipping,user}.md
/tmp/demodocs/services/{ad,cart,checkout,currency,email,frontend,load_generator,
                        payment,product_catalog,quote,react_native_app,
                        recommendation,shipping}.md
```

`services/cart.md` 產出長這樣：

```markdown
# Cart Service Telemetry Schema

## Attributes

| Attribute | Stability | Type | Description |
|-----------|-----------|------|-------------|
| [`demo.cart.items.count`](../attributes/cart.md#democartitemscount) | stable | int | Number of items in cart |
| [`demo.product.id`](../attributes/product.md#demoproductid) | stable | string | Product identifier |
| [`demo.product.quantity`](../attributes/product.md#demoproductquantity) | stable | int | Product quantity |
| [`user.id`](../attributes/user.md#userid) | stable | string | User identifier |

## Metrics

| Metric | Instrument | Unit | Stability | Description |
|--------|------------|------|-----------|-------------|
| `demo.cart.add_item.latency` | histogram | s | stable | Cart add item operation latency |
| `demo.cart.get_cart.latency` | histogram | s | stable | Cart retrieval operation latency |
```

**跨檔案的相對連結是模板自動接的**——`demo.product.id` 定義在 product 領域，但出現在 cart 服務頁，連結自動指過去。這種東西手寫文件根本維持不住。

### 模板怎麼把「服務」變出來的

`templates/markdown/weaver.yaml` 是整套的關鍵，它用 jq filter 把 resolved registry 重新切一次：

```yaml
  - pattern: service.md.j2
    filter: >
      (.groups | map(select(.type == "attribute_group" and (.id | startswith("service."))))) as $services |
      (.groups | map(select(.type == "metric"))) as $metrics |
      $services | map(
        (.id | split(".") | .[1]) as $svc_name |
        {
          id: $svc_name,
          groups: [.],
          metrics: [$metrics[] | select(.annotations.service == $svc_name)]
        }
      )
    application_mode: each
    file_name: services/{{ ctx.id | snake_case }}.md
```

拆解：

1. 撈出所有 `service.*` 開頭的 attribute_group
2. 從 group id 切出服務名（`service.cart` → `cart`）
3. 用 **`annotations.service == $svc_name`** 把 metric 掛回服務
4. `application_mode: each` → 每個服務產一頁

**這就是 `annotations.service` 的用途**：semconv 沒有「metric 屬於哪個服務」的概念（metric 是全域的），demo 用一個自訂 annotation 補上這個維度，再靠模板組出以服務為中心的文件。**annotations 是 v2 給你的擴充點——規範管不到的維度，你自己加。**

### 一個我實測才發現的機制

模板的 nav filter 在找 `registry.*` 開頭的 group：

```jq
.groups[] | select(.type == "attribute_group" and (.id | startswith("registry.")))
```

但 `telemetry-schema/` 裡**一個 `registry.*` group 都沒有**：

```bash
grep -rn "id: registry\." telemetry-schema/ | wc -l   # → 0
```

那 11 個 attributes 頁面是哪來的？resolve 一次就知道：

```bash
weaver registry resolve -r telemetry-schema --format json -o /tmp/demores.json
```

```text
registry.telemetry-schema.attributes.ad
registry.telemetry-schema.attributes.cart
registry.telemetry-schema.attributes.exchange
registry.telemetry-schema.attributes.feature_flag
…
```

**weaver 會用檔案路徑自動合成 group id。**v2 的頂層 `attributes:` 沒有 group 概念，但 resolve 成 v1 形狀時 weaver 得給它一個 id，於是用 `registry.<路徑>` 生一個。

**所以 demo 的 `attributes/` 目錄怎麼切，文件的分頁就怎麼切。**檔案佈局變成了結構——這在 v1 是做不到的（v1 每個 group 都得自己取 id）。這個行為沒有寫在任何文件裡，是我 resolve 出來才看到的。要抄這套的話，記得你的目錄名稱會直接變成使用者看到的分類。

---

## Part 4 — CI：兩道防線，分工很清楚

### 防線一：`weaver-check`

`.github/workflows/checks.yml`：

```yaml
  weaver-check:
    name: Weaver check
    runs-on: ubuntu-latest
    steps:
      - name: Run Weaver registry check
        run: |
          docker run --rm \
            --mount "type=bind,source=${GITHUB_WORKSPACE_PATH}/telemetry-schema,target=/home/weaver/source,readonly" \
            otel/weaver:v0.22.1 \
            registry check -r source
```

而且它掛進了 required checks 清單：

```yaml
    needs: [ …, checklicense, weaver-check, react-native-build, … ]
```

**schema 壞掉 = PR 進不去。**這就是 merge gate。

### 防線二：telemetry sanity tests

`test/telemetry/` 是 3.0 新增的 pytest 框架，跑在同一個 docker network 裡，直接查 Jaeger / Prometheus / OpenSearch，確認每個服務真的有送出遙測。

它的 README 有一句話定義了分工，我覺得應該裱起來：

> Does NOT validate semantic conventions or attributes (**weaver's job**)

**Weaver 管「schema 對不對」，sanity test 管「資料有沒有真的流出來」。**兩件事不要混在一起——這個切法比很多公司內部的做法都清楚。

官方文章也提到，正是因為有了這套 sanity test，他們才敢大膽升級依賴，一口氣清掉 62 個 OpenSSF Scorecard 標記、300 多個 CVE。**測試框架的價值不只是抓 bug，是解鎖你原本不敢做的事。**

---

## Part 5 — 我實測抓到的四個縫隙

抄之前先知道哪裡還沒補完。這四個都是我本機跑出來的。

### 一、三個地方三個 weaver 版本

| 位置 | 版本 |
| --- | --- |
| `.github/workflows/checks.yml`（CI gate） | `otel/weaver:v0.22.1` |
| `src/telemetry-docs/Dockerfile`（實際產文件） | `otel/weaver:v0.25.0` |
| `src/telemetry-docs/README.md`（文件裡寫的） | `otel/weaver:v0.21.2` |

**CI 驗的版本比實際產文件的版本舊三個 minor。**`definition/2` 還在 Alpha、每個版本都在動，這個落差遲早會咬人：CI 綠燈但 build 掛掉。README 那個 0.21.2 則是單純沒跟上。

**要抄的話**：把 weaver 版本抽成一個變數，CI 和 Dockerfile 共用。

### 二、`--future` 一開，39 個錯

```bash
weaver registry check -r telemetry-schema           # exit 0（只有警告）
weaver registry check -r telemetry-schema --future  # exit 1，39 個 ×
```

拆解那 39 個：

```text
31 × File format `definition/2` is not yet stable        ← 每個檔案一個，格式本身的問題
 8 × The signal group `metric.demo.*` does not set `requirement_level`
```

前 31 個沒得救（等 v2 穩定），**但後面 8 個是現在就能修的**——8 個 metric 全都沒寫 `requirement_level`：

```text
metric.demo.ad.requests
metric.demo.cart.add_item.latency
metric.demo.cart.get_cart.latency
metric.demo.exchange.conversions
metric.demo.notification.confirmations
metric.demo.payment.transactions
metric.demo.recommendation.requests
metric.demo.shipping.items_shipped
```

（`requirement_level` 在訊號層級只有兩個值：`recommended` = 預設就該發、`opt_in` = 要明確開啟。）

CI 沒加 `--future`，所以這些現在是隱形的。**你自己的新 registry 應該加 `--future`**——把未來的錯誤提早暴露。

### 三、一個定義了但沒人用的屬性

比對 schema 定義的屬性和 `src/` 裡實際出現的：

```text
schema 定義的屬性: 37
在 src/ 找不到的:  1
    demo.shipping.items_count
```

翻 CHANGELOG，`app.shipping.items_count` 確實被改名成 `demo.shipping.items_count`，但程式碼裡現在只有 `demo.shipping.quote.items_count`。**大改名留下的孤兒。**

37 個裡只掉 1 個，其實相當乾淨。但這帶出第四點——

### 四、沒有任何東西驗證「程式碼真的照 schema 送」

```bash
grep -rn "live-check\|live_check" . --exclude-dir=.git
# （空）
```

**demo 沒有用 `weaver registry live-check`。**現在的 CI 只驗 schema 自己合不合法，不驗程式碼有沒有照著送。所以：

- schema 定義了但沒人送 → 不會被抓（上面那個孤兒就是）
- 程式碼送了 schema 沒定義的屬性 → 也不會被抓
- 屬性型別跟 schema 不符 → 還是不會被抓

這是整套流程目前最大的缺口，而 weaver **已經有工具**了。`live-check` 可以接在 sanity test 那條線上：反正測試已經在跑真流量、已經在查後端了，把 OTLP 分流一份給 live-check 就能補上這一段。

> 補一個實務地雷：`live-check` 預設聽 4317，跟一堆東西會撞。demo 環境裡要另外指定 port。

---

## Part 6 — 你可以抄什麼

按「立刻能做 → 需要投資」排：

**1. 用「屬性按領域、用法按服務」切目錄。**這是 demo 最漂亮的設計決策，成本只是開幾個資料夾。`attributes/<domain>.yaml` 放定義，`services/<service>.yaml` 放 `ref` 組合。而且因為 weaver 會用路徑合成 group id，**目錄佈局直接變成文件的分類**。

**2. 導 registry 時先問「官方有沒有」。**demo 把 `app.user.id` 整個刪掉改用 semconv 的 `user.id`。每刪掉一個自己的屬性，就少維護一份東西、多一分跟生態系互通的機會。

**3. registry 可以從既有程式碼逆向出來。**demo 的 manifest 明講是「analyzing actual attribute usage across the codebase」。不用等到有完美設計才開始——先把現況寫成 schema，再逐步收斂。

**4. 把 `registry check` 設成 required check。**一個 docker run 的事，但它把 schema 從「文件」變成「規則」。

**5. 想清楚 Weaver 和整合測試的分工。**demo 的那句 "Does NOT validate semantic conventions or attributes (weaver's job)" 值得直接抄進你的 README。

**6. 把 schema 文件跑成服務。**這個投資最大（一個三段式 Dockerfile + 一組 Jinja 模板），但效果也最誇張——telemetry schema 從一堆沒人看的 YAML，變成跟 Jaeger、Grafana 並列在同一個入口的網站。

**7. 用 `annotations` 補規範沒有的維度。**demo 用 `annotations.service` 把 metric 掛回服務——semconv 沒有這個概念，但你的組織需要。這是 v2 給你的正式擴充點。

**8. 補上 demo 還沒做的那一塊：`live-check`。**如果你要抄這整套，這是唯一一個「抄完還要自己加」的部分。schema 驗證只管到 schema 自己，程式碼有沒有照做是另一回事。

---

## 最後

`app.*` → `demo.*` 這件事拖了三年，不是因為難，是因為**沒有工具能保證改得完整**。

Weaver 補上的就是這個。而補上之後順手長出來的東西——registry、CI gate、文件服務——比原本要解的那個改名問題有價值得多。

如果你手上有一個「早就知道該改但一直不敢動」的命名問題，這篇大概就是你要的那個推力。

---

## 參考

- [We Broke the Demo](https://opentelemetry.io/blog/2026/we-broke-the-demo/) — 官方 3.0 公告
- [opentelemetry-demo](https://github.com/open-telemetry/opentelemetry-demo) — repo 本體
- [`telemetry-schema/`](https://github.com/open-telemetry/opentelemetry-demo/tree/main/telemetry-schema) — 本文拆解的 registry
- [`src/telemetry-docs/`](https://github.com/open-telemetry/opentelemetry-demo/tree/main/src/telemetry-docs) — 文件服務與 Weaver 模板
- [`test/telemetry/`](https://github.com/open-telemetry/opentelemetry-demo/tree/main/test/telemetry) — sanity test 框架
- demo 3.0.0 發布於 2026-07-24；本文驗證於 commit `3684411`（2026-07-29）
