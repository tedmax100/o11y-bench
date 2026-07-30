# semconv `definition/2`：為什麼要有 v2、它改了什麼、我現在該不該搬

> 驗證環境：weaver 0.25.1、semantic-conventions `c6cda02`（2026-07-30）
> 本文所有 YAML 都實際跑過 `weaver registry check --v2`，結果附在文中。

如果你照著「從零開始」那篇把官方 semantic-conventions clone 下來跑過 `check`，一定會看到滿螢幕這行：

```text
⚠ File format `definition/2` is not yet stable: model/messaging/kafka.yaml
```

那篇的 Part 4 已經帶過「有 24 個檔案長得不一樣」，但沒有回答三個真正會影響決策的問題：**為什麼要重做一套語法？它到底改了什麼？我手上的 registry 該不該搬？**

這篇就是補這三題。

---

## 先釐清：有兩個「v2」，不是同一件事

這是最容易混淆的地方，先切乾淨：

| | 它是什麼 | 現在的狀態 |
| --- | --- | --- |
| **`file_format: definition/2`** | 你**手寫**的 registry YAML 語法（Semantic Convention Definition Language v2） | Alpha，weaver 會警告 |
| **`--v2` 旗標** | weaver **resolve 之後輸出**的 resolved schema 形狀（policy / template 看到的東西） | 已是上游標準，policy 和 template package 全部要求它 |
| **Telemetry Schema File Format 2.0** | OTel **spec 層**的 schema 檔（`schema_url` 指向的那個，目前 1.1.0） | 規劃中，spec issue #4427 |

三者互相獨立。**輸入檔還是 v1 語法，一樣可以（而且應該）加 `--v2` 輸出**——這是最多人搞錯的一點。這篇只講第一項，`definition/2`。

---

## Part 1 — 為什麼需要 v2

v1 的語法是 2020 年前後從「一份 markdown 表格的機器可讀版」長出來的，它有一個核心設計：**所有東西都是 `groups:` 底下的一個 item，用 `type:` 區分是 metric、span、event 還是 attribute_group。**

這個設計撐了五年，但踩到四個結構性的痛點。

### 痛點一：屬性沒有獨立身分，它「屬於」某個 group

v1 裡，一個屬性第一次出現時，是被定義在某個 group 裡面的：

```yaml
groups:
  - id: span.cart.add_item
    type: span
    attributes:
      - id: cart.session_id        # ← 定義在這裡
        type: string
        stability: stable
        requirement_level: required # ← 定義的同時就決定了 requirement
```

問題來了：`cart.session_id` 在 `cart.checkout` 這個 span 也要用，但那邊它是 `recommended` 不是 `required`。於是你要用 `ref:` 再宣告一次去 override。這造成兩件事：

1. **「屬性的定義」跟「屬性在某個訊號中的用法」混在一起。**`requirement_level: required` 到底是這個屬性的本質，還是它在這個 span 的用法？語法上分不出來。
2. **屬性的家在哪裡是隨機的。**要查 `cart.session_id` 的定義，你得先知道它剛好被寫在哪個 span 底下。實務上大家只好靠命名慣例——開一個 `attributes.xxx` 的 group 專門放定義，但那只是慣例，不是語法。

### 痛點二：`groups:` + `type:` 讓工具很難寫

因為所有訊號混在同一個 list，任何工具（template、policy、IDE）都得先做一次 `type` 分派。上游模板 `templates/registry/markdown/snippet.md.j2` 就是這樣開場的：

```jinja
{% if group.type == "event" -%}
{%- elif group.type == "resource" or group.type == "entity" -%}
{%- elif group.type == "metric" -%}
{%- elif group.type == "span" -%}
```

而且 JSON Schema 沒辦法對「metric 必須有 `unit`、span 必須有 `span_kind`」做精確驗證——因為它們是同一個型別的 oneOf 變體。錯誤訊息因此很難讀（Part 4 遷移時會撞到的 `does not match any schema in a 'oneOf' group` 就是這個問題的殘留）。

### 痛點三：共用只有「造 group」一條路，而造出來的 group 會洩漏

這一點光講抽象的「單一繼承不夠用」沒有說服力，直接拿 [`model/hardware/`](https://github.com/open-telemetry/semantic-conventions/tree/main/model/hardware) 來看——它是官方遷到 v2 最徹底的 domain（18 個檔案搬了 16 個），因為它痛得最徹底。

下面所有數字都是**同一組 16 個檔案**的 v1.37.0 版本 vs 現在的 v2 版本對比，不是拿整個目錄比。

#### 先把 v1 的三層關係攤開

風扇的 metric 定義**橫跨三個檔案**，得三個一起看才知道發生什麼事。

**第一層**，`model/hardware/registry.yaml` — 屬性的原始定義（27 個 `hw.*`，以下省略 `stability` / `examples`）：

```yaml
groups:
  - id: registry.hardware
    type: attribute_group
    attributes:
      - id: hw.id
        type: string
        brief: "An identifier for the hardware component, unique within the monitored host"
      - id: hw.name
        type: string
        brief: "An easily-recognizable name for the hardware component"
      - id: hw.parent
        type: string
        brief: "Unique identifier of the parent component"
      - id: hw.sensor_location
        type: string
        brief: "Location of the sensor"
      # …另外 23 個
```

**第二層**，`model/hardware/common.yaml` — 挑出「每個硬體 metric 都要帶的三個」：

```yaml
groups:
  - id: hardware.attributes.common
    type: attribute_group
    stability: development
    brief: 'Common hardware attributes'
    attributes:
      - ref: hw.id
        requirement_level: required
      - ref: hw.name
        requirement_level: recommended
      - ref: hw.parent
        requirement_level: recommended
```

**第三層**，`model/hardware/fan-metrics.yaml`（[v1.37.0](https://github.com/open-telemetry/semantic-conventions/blob/v1.37.0/model/hardware/fan-metrics.yaml)）—— 問題在這裡：

```yaml
groups:
  - id: metric_attributes.hw.fan
    type: attribute_group                    # ← 注意：它是 attribute_group，不是 metric
    brief: "Common attributes for fan metrics"
    extends: hardware.attributes.common      # ← 繼承上面那三個
    attributes:
      - ref: hw.sensor_location              # ← 再加這一個
        requirement_level: recommended

  - id: metric.hw.fan.speed
    type: metric
    metric_name: hw.fan.speed
    instrument: gauge
    unit: "rpm"
    extends: metric_attributes.hw.fan        # ← 整包拿過來，自己一個屬性都沒加

  - id: metric.hw.fan.speed_ratio
    type: metric
    metric_name: hw.fan.speed_ratio
    instrument: gauge
    unit: "1"
    extends: metric_attributes.hw.fan        # ← 同上

  - id: metric.hw.fan.speed.limit
    type: metric
    metric_name: hw.fan.speed.limit
    instrument: gauge
    unit: "rpm"
    extends: metric_attributes.hw.fan
    attributes:
      - ref: hw.limit_type                   # ← 只有這個多加了一個屬性
        requirement_level: recommended
```

所以 `metric_attributes.hw.fan` **長這樣**：它是一個 `attribute_group`（不是 metric、不會產生任何遙測資料），內容是「`hardware.attributes.common` 的三個 ＋ `hw.sensor_location`」＝ 4 個屬性。

#### 用 resolve 證明它是空殼

抽象講「它沒有獨立語意」不夠，直接把 v1.37.0 resolve 出來，看它跟 metric 的關係：

```bash
git clone --depth 1 -b v1.37.0 https://github.com/open-telemetry/semantic-conventions.git
cd semantic-conventions && weaver registry resolve -r model --format json -o /tmp/r137.json
```

四個 group 的最終屬性集合：

```text
-- hardware.attributes.common   (attribute_group)
     hw.id                    required
     hw.name                  recommended
     hw.parent                recommended

-- metric_attributes.hw.fan    (attribute_group)     ← 轉接頭
     hw.id                    required
     hw.name                  recommended
     hw.parent                recommended
     hw.sensor_location       recommended

-- metric.hw.fan.speed         (metric)
     hw.id                    required
     hw.name                  recommended
     hw.parent                recommended
     hw.sensor_location       recommended          ← 跟轉接頭一模一樣

-- metric.hw.fan.speed_ratio   (metric)
     hw.id                    required
     hw.name                  recommended
     hw.parent                recommended
     hw.sensor_location       recommended          ← 也一模一樣
```

**`metric_attributes.hw.fan` 的內容跟 `hw.fan.speed`、`hw.fan.speed_ratio` 完全相同，一個字都不差。**

這就是我說的「沒有獨立語意」：它不代表任何一個真實存在的東西——不是一個 metric、不是一個 span、不是一個會被使用者查詢的概念。它是一個**只為了少打幾行字而存在的中間變數**。

而它偏偏是 `type: attribute_group`，會被 weaver 當成 registry 的正式成員：進 resolved schema、被 policy 檢查、出現在生成的文件裡。**一個純粹的 authoring 細節，洩漏成了 registry 的公開內容。**

#### 等等——v1 每個 metric 各寫一次 `hw.sensor_location` 不行嗎？

會這樣問是對的。**答案是：可以，v1 完全寫得出來。**這不是我推測，實測給你看。建一個最小的 v1 registry，metric 直接 `extends` 共通組再自己追加屬性，中間不放轉接頭：

```yaml
groups:
  - id: hardware.attributes.common
    type: attribute_group
    stability: development
    brief: 'Common hardware attributes'
    attributes:
      - ref: hw.id
        requirement_level: required
      - ref: hw.name
        requirement_level: recommended
      - ref: hw.parent
        requirement_level: recommended

  # 不造轉接頭，metric 直接 extends 共通組 + 自己加 hw.sensor_location
  - id: metric.hw.fan.speed
    type: metric
    metric_name: hw.fan.speed
    stability: development
    brief: "Fan speed"
    instrument: gauge
    unit: "rpm"
    extends: hardware.attributes.common      # ← 共通的三個
    attributes:
      - ref: hw.sensor_location              # ← 自己再加一個
        requirement_level: recommended

  - id: metric.hw.fan.speed_ratio
    type: metric
    metric_name: hw.fan.speed_ratio
    stability: development
    brief: "Fan speed ratio"
    instrument: gauge
    unit: "1"
    extends: hardware.attributes.common
    attributes:
      - ref: hw.sensor_location
        requirement_level: recommended
```

```bash
weaver registry check -r v1test        # exit 0，過
weaver registry resolve -r v1test --format json -o /tmp/v1test.json
```

```text
-- metric.hw.fan.speed
     hw.id                  required
     hw.name                recommended
     hw.parent              recommended
     hw.sensor_location     recommended     ← 跟官方用轉接頭的結果完全相同
-- metric.hw.fan.speed_ratio
     hw.id                  required
     hw.name                recommended
     hw.parent              recommended
     hw.sensor_location     recommended
```

**完全合法，而且 resolve 結果跟官方那套一模一樣。**所以 `metric_attributes.hw.fan` **不是語法逼出來的，是作者為了 DRY 自己選的**——`hw.sensor_location` 少寫兩次。

那我前面說的痛點還算數嗎？算，但要講精確。

#### v1 真正的硬限制在哪

上面那條路可行，是因為 fan 只牽涉到「**一個** group ＋ 幾個零散屬性」。一旦你要的是「**兩個以上**現成 group 的組合」，v1 就真的沒轍了：

```yaml
  - id: metric.hw.fan.multi
    type: metric
    metric_name: hw.fan.multi
    instrument: gauge
    unit: "1"
    extends: [hardware.attributes.common, hardware.attributes.sensor]   # ← 試試看
```

```text
× Value ["hardware.attributes.common","hardware.attributes.sensor"] is
  not of any of the required types. Supported types: string, null (i.e. optional).
```

**`extends:` 只吃一個字串，硬錯。**要組合兩個 group，唯一的辦法就是造第三個 group 把它們合起來——這時轉接頭才是**真的無可避免**。

所以 v1 的問題精確講是這樣：**共用只有「造 group」這一條路，而這條路有三個代價**——

1. **要組合多個 group 就必須造新 group**（`extends:` 不能多重），沒得商量；
2. **就算只是為了 DRY 而選擇造 group，造出來的東西會洩漏。**它是 `type: attribute_group`，會進 resolved schema、被 policy 檢查、出現在生成的文件裡。v1 沒有任何辦法說「這只是我的內部樣板」；
3. **於是 DRY 和「registry 保持乾淨」變成二選一。**官方在 fan 上選了 DRY，代價就是 `metric_attributes.hw.fan` 這個不代表任何東西的 group 永遠留在 registry 裡。

v2 三個代價一次解掉：`ref_group:` 可以疊多個（解 1）、`visibility: internal` 讓 group 不進 resolved registry（解 2）、於是「要不要造 group」回歸單純的可讀性判斷，不再是取捨（解 3）。

這種轉接頭在 v1 的 hardware 裡有 **15 個**（`metric_attributes.hw.battery`、`.fan`、`.gpu`、`.network`、`.cpu`、`.voltage.common`、`.temperature.common`……）：

```bash
grep -h "id: metric_attributes" model/hardware/*.yaml | wc -l    # v1.37.0 → 15
grep -h "extends: hardware.attributes.common" model/hardware/*.yaml | wc -l   # → 15
```

**15 個轉接頭、15 次繼承共通屬性——一對一。**換句話說，`hardware.attributes.common` 這個真正該被大量共用的東西，**沒有任何一個 metric 直接用到它**，全部隔著一層純樣板。v2 之後同一組檔案：

```bash
grep -h "ref_group: hardware.attributes.common" model/hardware/*.yaml | wc -l  # → 30
```

**30 次，而且全部是 metric 直接引用。**共用終於發生在該發生的地方。

v2 的同一個檔案：

```yaml
file_format: definition/2
metrics:
  - name: hw.fan.speed
    instrument: gauge
    unit: "rpm"
    attributes:
      - ref_group: hardware.attributes.common   # ← metric 直接組合，不用轉接頭
      - ref: hw.sensor_location
        requirement_level: recommended

  - name: hw.fan.speed.limit
    instrument: gauge
    unit: "rpm"
    attributes:
      - ref_group: hardware.attributes.common
      - ref: hw.sensor_location
        requirement_level: recommended
      - ref: hw.limit_type
        requirement_level: recommended

  - name: hw.fan.speed_ratio
    instrument: gauge
    unit: "1"
    attributes:
      - ref_group: hardware.attributes.common
      - ref: hw.sensor_location                 # ← 第三次
        requirement_level: recommended

metric_refinements:
  - id: metric.hw.fan.status
    ref: hw.status
    attributes:
      - ref_group: hardware.attributes.common
      - ref: hw.sensor_location                 # ← 第四次
        requirement_level: recommended
      # …收窄 hw.type / hw.state，下一節講
```

**先回答一個一定會冒出來的問題：`hw.sensor_location` 在 v2 反而被寫了 4 次，v1 只寫 1 次，這不是更糟嗎？**

```bash
grep -c "ref: hw.sensor_location" model/hardware/fan-metrics.yaml   # v2 → 4
```

沒錯，就是 4 次。但這是上游**自己選的**，不是 v2 逼的。

`metric_attributes.hw.fan` **整個消失了**。`ref_group:` 是寫在 `attributes:` 列表裡的一個元素，可以跟 `ref:` 混排、也可以疊好幾個，所以 metric 自己就能組合，不需要中間人。

把現在的 main resolve 出來對照：

```bash
weaver registry resolve -r model --format json --v2 -o /tmp/rmain.json
```

```text
v2 resolved hw.fan.speed:
     hw.id                    required
     hw.name                  recommended
     hw.parent                recommended
     hw.sensor_location       recommended     ← 跟 v1.37.0 resolve 出來的一模一樣

v2 resolved attribute_groups count: 0
```

兩件事：

1. **最終結果完全相同**——這次重構沒有改變任何一個 metric 的語意，純粹是把 authoring 的方式換掉。
2. **resolved 之後 `attribute_groups` 是 0 個。**因為 v2 的 `hardware.attributes.common` 標了 `visibility: internal`，它不會進入 resolved registry。v1 的轉接頭做不到這件事——它們會實實在在留在 resolved schema 裡。這正是改變三（`visibility`）要解的問題：**讓「共用機制」留在 authoring 層，不要洩漏成 registry 的公開內容。**

同一組 16 個檔案的完整數字：

| | v1（v1.37.0） | v2（現在） |
| --- | --- | --- |
| `attribute_group` 總數 | 16 | **9** |
| 其中 `metric_attributes.*` 轉接頭 | 15 | **8** |
| 引用共通屬性組的次數 | 15（全是轉接頭） | **30（全是 metric 直接用）** |
| metric 定義 | 38 | 38 |
| `metric_refinements` | — | **21** |

metric 數量一個沒少（38 → 38），但**轉接頭從 15 掉到 8**：留下的 8 個是 battery / cpu / gpu / memory / network / physical_disk / power_supply / tape_drive，蒸發的 7 個是 fan / logical_disk / voltage / temperature / enclosure / disk_controller / `hw.attributes`。

這個「留 8 拆 7」的取捨很值得看——它正好回答「那 v2 不就要重複寫了嗎」。

#### 關鍵：v2 不是禁止共用 group，是不再**強迫**你造 group

前面說 `hw.sensor_location` 在 v2 的 fan 檔案被寫了 4 次。那為什麼上游不乾脆在 v2 也留一個 `metric_attributes.hw.fan`？

**因為 v2 的 `attribute_groups:` 還在，他們留不留是自由的——而他們對 battery 就選擇留下來。**看現在的 `battery-metrics.yaml`：

```yaml
file_format: definition/2

attribute_groups:
  - id: metric_attributes.hw.battery     # ← v2 依然保留這個 group
    visibility: internal                 # ← 但標成 internal，不會洩漏到 resolved registry
    attributes:
      - ref_group: hardware.attributes.common   # ← 用組合，不用 extends
      - ref: hw.battery.chemistry
        requirement_level: recommended
      - ref: hw.battery.capacity
        requirement_level: recommended
      - ref: hw.model
        requirement_level: recommended
      - ref: hw.vendor
        requirement_level: recommended

metrics:
  - name: hw.battery.charge
    instrument: gauge
    unit: "1"
    attributes:
      - ref_group: metric_attributes.hw.battery    # ← 照樣用

  - name: hw.battery.charge.limit
    instrument: gauge
    unit: "1"
    attributes:
      - ref_group: metric_attributes.hw.battery    # ← group ＋ 額外屬性，混排
      - ref: hw.limit_type
        requirement_level: recommended
```

同一個 domain、同一次遷移，fan 拆掉了 group、battery 留著。為什麼？**算一下就知道**：

| | 額外屬性數 | 使用點 | 不用 group 要重複幾行 |
| --- | --- | --- | --- |
| fan | 1（`hw.sensor_location`） | 4 | 4 行 → **不值得為它造一個 group** |
| battery | 4（chemistry / capacity / model / vendor） | 4 | 16 行 → **值得** |

**這就是 v1 和 v2 的真正差別。**

- **v1**：想 DRY 就只能造 group（`extends:` 又不能多重，組合兩個 group 更是非造不可），而造出來的 group **一定會洩漏到 resolved registry**。於是「少寫幾行」和「registry 保持乾淨」是二選一——fan 選了前者，代價是 `metric_attributes.hw.fan` 這個不代表任何東西的 group 永遠留著。
- **v2**：`ref_group:` 可以疊多個、可以跟 `ref:` 混排，所以 metric 自己就能組合。**group 只在「它真的代表一個有意義的集合」時才需要存在**——而且就算造了，`visibility: internal` 也能讓它不洩漏出去。

所以那 15 → 8 不是「v2 消滅了 7 個 group」，而是**在 v1 那 7 個是「為了省幾行、只好忍受它洩漏」的妥協產物；v2 拿掉了妥協，作者就按價值重新判斷了一次，留下真正值得的 8 個**。

**要誠實講的取捨**：v2 確實可能讓某些檔案的字數變多（fan 就是），但換來的是——沒有假抽象、共用機制不洩漏、改一個 metric 不會意外影響另外三個。判斷標準從「造 group 才能 DRY，但會弄髒 registry」變成單純的「這個集合有沒有意義」。

### 痛點三的另一面：v1 根本表達不出「某個 metric 在特定情境的收窄」

hardware 更嚴重的問題其實在這裡。v1 的 `common-metrics.yaml` 只定義了**一個**通用的 `hw.status`：

```yaml
  - id: metric.hw.status
    type: metric
    metric_name: hw.status
    instrument: updowncounter
    unit: "1"
    extends: metric_attributes.hw.attributes
    attributes:
      - ref: hw.state
        requirement_level: required
```

但「風扇的 `hw.status`」跟「電池的 `hw.status`」是不一樣的東西——風扇的 `hw.type` 必須是 `fan`，`hw.state` 只能是 `ok` / `degraded` / `failed`；電池的 `hw.state` 還有 `charging` / `discharging`。**v1 沒有任何語法可以講這件事**，這些規則只能寫在 markdown 散文裡，機器讀不到、policy 檢查不到、codegen 也生不出來。

v2 用 `metric_refinements` 把它變成 schema 的一部分：

```yaml
metric_refinements:
  - id: metric.hw.fan.status
    ref: hw.status                              # 指向通用定義
    attributes:
      - ref_group: hardware.attributes.common
      - ref: hw.sensor_location
        requirement_level: recommended
      - ref: hw.type
        note: "MUST be set to `fan`."           # ← 收窄，而且是機器可讀的
        examples: ["fan"]
      - ref: hw.state
        note: |
          MUST be set to one of the following values:

          * `ok`: The fan is operating normally.
          * `degraded`: The fan is operating with reduced functionality or performance.
          * `failed`: The fan has failed and is not operational.
        examples: ["ok", "degraded", "failed"]
```

現在 hardware 有 **21 個 `metric_refinements`**——14 個是 `hw.status` 的各元件版本（fan / battery / cpu / gpu / memory / network / logical_disk / physical_disk / power_supply / tape_drive / temperature / voltage / enclosure / disk_controller），7 個是 `hw.errors` 的：

```bash
grep -h -A1 "^  - id: metric\." model/hardware/*.yaml | grep "ref:" | sort | uniq -c
#   14     ref: hw.status
#    7     ref: hw.errors
```

**這 21 個定義在 v1 是零，不是因為沒人想寫，是因為寫不出來。**這才是 hardware 第一個被搬到 v2 的真正原因。

### 痛點四：Entity 的「身分 vs 描述」在 v1 是可選欄位，於是一半沒填

Entity（Resource 的新名字）是後來才加進 semconv 的概念，在 v1 裡它被塞成 `type: entity` 的一個 group，屬性全部平鋪在 `attributes:` 下。

Entity 有一個關鍵區別：**哪些屬性構成「身分」（identity），哪些只是「描述」（description）**。`k8s.pod` 的身分是 `k8s.pod.uid`；`k8s.pod.label`、`k8s.pod.ip` 只是描述。這個差異決定了後端怎麼做 entity 去重與 join——同一個 pod 換了 IP 還是同一個 pod，但換了 uid 就是另一個。

**v1 有講這件事的地方**，就是屬性上的 `role:`。看 `model/k8s/entities.yaml`（現在的 main，還是 v1 語法）：

```yaml
groups:
  - id: entity.k8s.pod
    type: entity
    stability: development
    name: k8s.pod
    brief: >
      A Kubernetes Pod object.
    attributes:
      - ref: k8s.pod.uid
        role: identifying          # ← 身分
      - ref: k8s.pod.name
        role: descriptive          # ← 描述
      - ref: k8s.pod.label
        role: descriptive
        requirement_level: opt_in
      - ref: k8s.pod.ip
        role: descriptive
        requirement_level: opt_in
```

`role:` 只有兩個合法值：

```json
// weaver/schemas/semconv.schema.json → AttributeRole
{ "enum": ["identifying", "descriptive"] }
```

k8s 這組寫得很完整。問題出在——**`role:` 是可選的，而且沒填不會有任何抱怨。**

#### 實測：漏填 `role` 完全不會被擋

```yaml
  - id: entity.k8s.pod
    type: entity
    name: k8s.pod
    stability: development
    brief: "A Kubernetes Pod object."
    attributes:
      - ref: k8s.pod.uid
        role: identifying
      - ref: k8s.pod.name
        role: descriptive
      - ref: k8s.pod.ip          # ← 故意不寫 role
```

```bash
weaver registry check -r entv1 --future
# ✔ No `after_resolution` policy violation
# exit code: 0
```

**連 `--future` 都是綠燈。**resolve 出來就是這樣：

```text
-- entity.k8s.pod  name=k8s.pod
     k8s.pod.ip      role=None            ← 既不是身分也不是描述
     k8s.pod.name    role='descriptive'
     k8s.pod.uid     role='identifying'
```

一個「既非身分也非描述」的 entity 屬性，安安靜靜地存在。

#### 上游被咬得有多慘

這不是假設性問題。把現在的 main resolve 出來數一遍：

```bash
weaver registry resolve -r model --format json -o /tmp/r.json
```

```text
entity 數 = 64
entity 屬性總數 = 224
沒有 role 的 = 100          ← 45%
```

**224 個 entity 屬性裡，100 個沒有 role。**更嚴重的是往上一層看：

```text
完全沒有任何 identifying 屬性的 entity = 28 / 64        ← 44%
   aws.ecs            (7 個屬性，全部沒身分)
   container          (7 個屬性，全部沒身分)
   cloud              (6 個屬性，全部沒身分)
   browser            (5 個屬性，全部沒身分)
   faas               (5 個屬性，全部沒身分)
   device             (4 個屬性，全部沒身分)
   container.image    (4 個屬性，全部沒身分)
   …共 28 個
```

**64 個 entity 裡有 28 個講不出自己的身分是什麼**——後端拿到這些 entity 根本無從去重、無從 join。而 `check --future` 一句話都不會說。

原因很單純：`role:` 是一個**可選的、寫在屬性上的旁註**。可選的欄位就會有人不填，尤其是在寫了幾十個 entity 之後。

#### v2 怎麼解

不是新增能力，是**把可選欄位變成強制的結構**——`identity:` 和 `description:` 兩個獨立的 list，屬性只能待在其中一個：

```yaml
entities:
  - type: k8s.pod
    identity:                  # 放這裡 = 身分
      - ref: k8s.pod.uid
    description:               # 放這裡 = 描述
      - ref: k8s.pod.name
      - ref: k8s.pod.ip
```

Entity 沒有 `attributes:` 了。**「忘了標 role」這件事在 v2 語法上不存在**——你不可能把一個屬性放進一個不存在的清單。

**要誠實講的一點**：上游到今天**沒有把任何一個 entity 搬到 v2**（24 個 v2 檔案全是 metric/span，`grep "^entities:"` 一個都沒有）。所以 v2 的 entity 語法還沒被上游 dogfood 過，只有 Part 4 我自己那份 registry 實跑驗證過。

### 還有一個外部壓力

Weaver 的 `package` / `resolve` 輸出（`resolved/2.0`）已經是「依訊號分類」的結構了。輸入是扁平的 `groups:`、輸出是分類的，中間那層轉換的複雜度全部由 weaver 吸收。把輸入格式對齊輸出格式，等於把整條 pipeline 拉直。

---

## Part 2 — v2 改了什麼

### 頂層結構：從 `groups:` 變成 11 個具名 key

```yaml
file_format: definition/2

attributes:           # 屬性「定義」，獨立的一等公民
attribute_groups:     # 可重用的屬性集合
entities:             # Entity 定義
events:               # Event 定義
metrics:              # Metric 定義
spans:                # Span 定義
entity_refinements:   # ↓ 既有定義的情境特化
event_refinements:
metric_refinements:
span_refinements:
imports:              # 跨 registry 引用
```

（這份清單是從 `schemas/semconv.schema.v2.json` 的 `properties` 直接讀出來的，不是猜的。）

### 改變一：屬性定義獨立，用 `key:` 而非 `id:`，且**不能**帶 `requirement_level`

這是最重要的一刀。v2 的 `AttributeDef` 只有這些欄位：

```
required: key, type, brief, stability
optional: examples, note, deprecated, annotations
```

**沒有 `requirement_level`。**它被徹底移到使用端：

```yaml
attributes:
  - key: payment.provider          # 定義：這個屬性是什麼
    type: string
    stability: stable
    brief: "支付服務提供商"
    examples: ["stripe", "paypal"]

metrics:
  - name: payment.errors
    attributes:
      - ref: payment.provider      # 用法：在這個 metric 裡它是必填
        requirement_level: required
```

痛點一解決了：**定義只講「是什麼」，用法只講「怎麼用」。**副作用是屬性檔會變得非常乾淨，很適合單獨拉一個 `attributes.yaml`。

### 改變二：`ref_group:` 取代 `extends:`，可以疊多層

```yaml
attribute_groups:
  - id: payment.attributes.common
    visibility: internal
    attributes:
      - ref: payment.provider
        requirement_level: required
      - ref: deployment.environment
        requirement_level: required

metrics:
  - name: payment.amount
    attributes:
      - ref_group: payment.attributes.common   # 可以有多個
      - ref: payment.currency                  # 也可以跟單獨的 ref 混排
        requirement_level: required
```

痛點三解決。注意 `ref_group` 是寫在 `attributes:` 列表裡的一個元素，跟 `ref` 平起平坐——語意上一致多了。

**實際踩到的坑**（下面遷移實作時真的撞到）：兩個 `ref_group` 如果含有同一個屬性，check 會直接擋：

```text
× The attribute id `deployment.environment` is declared multiple times in
  the following groups: ["common.resource", "payment.attributes.common"]
```

這其實是好事——v1 時代這種重複會被靜靜地 merge 掉，你不知道最後生效的是哪一個 requirement_level。

### 改變三：`visibility` 變成必填的語法宣告

v1 時代大家用命名慣例（`attributes.xxx.common`）暗示「這個 group 只是內部共用」。v2 把它變成語法，而且**是必填欄位**：

```yaml
attribute_groups:
  - id: payment.attributes.common
    visibility: internal        # 只需要 id + visibility，brief/stability 都不用
    attributes: [...]

  - id: common.resource
    visibility: public          # public 就要完整的 brief + stability
    brief: "跨服務通用的資源屬性"
    stability: stable
    attributes: [...]
```

`internal` 的實際效果（上游 stability policy README 講得很明白）：**internal groups 不會進入 resolved registry，因此也不會被 policy 檢查、不會出現在生成的文件裡、不能被下游 registry 引用。**這對企業多 registry 治理很關鍵——你終於能區分「這是我要對外承諾的 API」跟「這是我內部拿來少打字的」。

### 改變四：Span 有了 `type:`（身分）和 `name:`（命名規則）

v1 的 span group 有 `id:` 但沒有「span 名稱」的概念——span 名稱怎麼取全靠 markdown 註解描述。v2：

```yaml
spans:
  - type: payment.process          # 身分（唯一），對應 v1 的 id
    name:
      note: "使用 `payment {payment.provider}` 作為 span 名稱。"   # 命名規則，required
    kind: server                   # v1 是 span_kind
    stability: stable
    brief: "處理訂單支付流程的 Span"
```

`name.note` 是**必填**的，強迫你把命名規則寫進 schema 而不是留在 markdown 裡。`span_kind` 也改名成 `kind`。

### 改變五：Entity 分 `identity` 與 `description`

```yaml
entities:
  - type: service                  # v1 是 name:
    stability: stable
    requirement_level: recommended
    brief: "邏輯服務實體"
    identity:                      # 構成身分的屬性
      - ref: service.name
    description:                   # 只是描述的屬性
      - ref: service.team
        requirement_level: recommended
```

跟 v1 的差別整理：

| v1 | v2 |
| --- | --- |
| `name: k8s.pod` | `type: k8s.pod` |
| `attributes:` 一個 list，靠每個屬性上的 `role: identifying / descriptive` 區分 | `identity:` / `description:` 兩個 list |
| `role:` **可選**，漏填連 `--future` 都不擋（實測 exit 0） | 屬性只能待在其中一個 list，**漏標在語法上不可能** |
| entity 層級寫 `requirement_level:` 不會報錯，但 resolve 後**直接消失**（無聲的 no-op） | `recommended` / `opt_in`，是正式欄位 |
| 無法特化 | `entity_refinements:` |

痛點四解決的方式不是「新增能力」，而是**把一個可選的旁註欄位升級成強制的結構**。上游那 100 個沒有 role 的屬性、28 個沒有身分的 entity，在 v2 語法下寫不出來。

### 改變六：`*_refinements`——同一個訊號的情境特化

前面痛點三已經用 `hw.status` 完整走過一遍（v1 表達不出來，v2 用 21 個 refinement 補上）。這裡補 `metric_refinements` 的語法本身：

```yaml
metric_refinements:
  - id: metric.hw.battery.status
    ref: hw.status                          # 指向通用定義
    attributes:
      - ref_group: metric_attributes.hw.battery
      - ref: hw.type
        note: "MUST be set to `battery`."   # 收窄
        examples: ["battery"]
```

`span_refinements` 是 Kafka 那個檔案的主角——`messaging.send.producer` 是通用的 messaging span，Kafka 版本只是把 `messaging.system` 釘死成 `kafka` 再補幾個屬性。上游正是為了 messaging 和 hardware 這兩大 domain 的重複才推 v2。

### 改變七：結構化的 deprecation

v1 的 `deprecated:` 是一段自由文字。v2 是結構化的，`reason` 必填：

| reason | 額外必填 | 意思 |
| --- | --- | --- |
| `renamed` | `renamed_to` | 語意不變，只是改名 |
| `obsoleted` | — | 沒了，也沒有替代品 |
| `uncategorized` | — | 其他情況 |

`renamed` 有明確約束：**語意必須不變**，改 unit 或改 instrument type 不算 rename。這直接餵給 `weaver registry diff` 和未來的 Telemetry Schema 2.0 自動產生遷移規則——這就是「從發布 diff 改成發布完整定義，diff 由工具推導」那條路線的地基。

### 改變八：`imports` 與 `annotations`

```yaml
imports:
  metrics: ["http.server.*"]
  spans: ["http.*"]
  attribute_groups: ["server.address.*"]
```

wildcard 引用其他 registry 的定義。搭配 `annotations` 的 `dependency_resolution.exclude: true`，你可以精確控制「我引入官方 http 慣例，但不要那三個我們用不到的 metric」。

`annotations` 另一個用途是 code generation：

```yaml
annotations:
  code_generation:
    metric_value_type: double     # 生成的 Go/Python 用 double 不用 int
  naming_conventions:
    policy_exceptions:
      - metric_namespace_collision   # 明確標註「我知道這違規，這是刻意的」
```

`policy_exceptions` 很實用——v1 時代要豁免一條 policy，只能改 policy 本身；v2 可以在被檢查的對象上標註豁免，責任歸屬清楚多了。

---

## Part 3 — 目前狀態：能用，但別急

### 上游進度（2026-07-29 實測）

```bash
git clone --depth 1 https://github.com/open-telemetry/semantic-conventions.git
grep -rl "file_format: definition/2" semantic-conventions/model/ | wc -l   # → 24
find semantic-conventions/model -name "*.yaml" | wc -l                     # → 250
```

**24 / 250。**跟「從零開始」那篇寫的時候一模一樣，這幾個月沒有動。而且分佈非常集中：

```bash
grep -rl "file_format: definition/2" model/ | cut -d/ -f2 | sort | uniq -c | sort -rn
#   16 hardware
#    7 messaging
#    1 faas
```

`hardware` 和 `messaging` 正好是重複最嚴重、最需要 `*_refinements` 的兩個 domain（Part 1 已經拆過 hardware）。他們是挑痛點最大的先做，不是逐檔掃過去。

### v1 / v2 可以在同一個 registry 裡共存，而且引用是雙向的

這一節是整篇最實用的部分，因為它決定了你的遷移能不能分批做。

`model/hardware/` 18 個檔案裡有 16 個是 v2，剩下兩個還是 v1：

```bash
for f in model/hardware/*.yaml; do
  head -3 $f | grep -q "definition/2" && echo "v2 $f" || echo "v1 $f"
done
# v1 model/hardware/registry.yaml       ← 27 個 hw.* 屬性的定義，還沒搬
# v1 model/hardware/host-metrics.yaml
# v2 …其餘 16 個
```

整包 250 個檔案混著 v1 和 v2，`check` 一次過：

```bash
weaver registry check -r model --v2
# exit 0（只有 24 個 "not yet stable" 警告）
```

**能這樣混的原因是 `file_format` 是「每個檔案自己的 parser 宣告」，不是 registry 層級的設定。**weaver 的流程是「逐檔用各自的語法 parse → 全部攤平成同一個內部模型 → 才開始解 `ref:` / `extends:` / `ref_group:`」。到解引用那一步，某個定義原本寫在 v1 還是 v2 檔案裡，這個資訊已經不存在了。

#### 方向一：v2 檔 `ref:` v1 檔定義的屬性

`hw.fan.speed` 定義在 v2 的 `fan-metrics.yaml`，但它 `ref:` 的四個屬性全部定義在 v1 的 `registry.yaml`：

```bash
grep -n "hw.sensor_location" model/hardware/registry.yaml
# 213:      - id: hw.sensor_location        ← v1 語法的 id:
```

resolve 出來，`type` / `examples` 全部從 v1 檔案帶過來，一個欄位都沒掉：

```text
--- resolved hw.fan.speed（定義在 v2 檔）---
  hw.id               type=string  req=required     examples=['win32battery_battery_testsysa33_1']
  hw.name             type=string  req=recommended  examples=['eth0']
  hw.parent           type=string  req=recommended  examples=['dellStorage_perc_0']
  hw.sensor_location  type=string  req=recommended  examples=['cpu0', 'ps1', 'INLET', …]
```

#### 方向二：v1 檔 `extends:` v2 檔定義的 group——而且穿過 `internal` 邊界

反方向也通，這個比較意外。`model/hardware/common.yaml` 是 **v2**，它定義的 group 標了 `internal`：

```yaml
# yaml-language-server: $schema=…/weaver/v0.25.1/schemas/semconv.schema.v2.json
file_format: definition/2
attribute_groups:
  - id: hardware.attributes.common
    visibility: internal          # ← internal
    attributes:
      - ref: hw.id
        requirement_level: required
      # …
```

而 `model/hardware/host-metrics.yaml` 是 **v1**，它用 v1 的 `extends:` 去指這個 v2 group：

```yaml
groups:
  - id: metric.hw.host.power
    type: metric
    extends: hardware.attributes.common      # ← v1 語法指向 v2 檔的 internal group
```

resolve 出來完全正常，而那個 group 依然不進 resolved registry：

```text
--- hw.host.power（定義在 v1 檔）---
  hw.id      req=required
  hw.name    req=recommended
  hw.parent  req=recommended

resolved attribute_groups 總數: 0
裡面有 hardware.attributes.common 嗎: False
```

**兩件事同時成立**：v1 的 `extends:` 吃得下 v2 定義的 group，而 `visibility: internal` 仍然生效。所以 `internal` 的邊界是**「registry 的對外輸出」，不是「registry 內檔案之間」**——同一個 registry 裡誰都能引用它，跨 registry 才擋。這一點官方文件沒有寫，是 resolve 出來才確認的。

#### 對你的遷移的意義

**粒度是「檔案」，而且不用照拓樸順序搬。**你不必先把被依賴的定義檔搬到 v2 才能搬使用端——上游正是反過來做的：`registry.yaml`（27 個屬性定義、被所有人依賴）到今天還是 v1，上面 16 個使用端全搬了 v2。

理由也很合理：`registry.yaml` 只有屬性定義，搬到 v2 的唯一改動是 `id:` → `key:` 加拿掉 `requirement_level`，**收益接近零**；使用端搬過去能換到 `metric_refinements` 和 `visibility`，收益很大。**挑收益大的先搬就好，被依賴的那層可以永遠留在 v1。**

### weaver 的態度：三層驗證的中間層

`definition/2` 落在「預設警告、`--future` 才是錯」這一層：

```bash
weaver registry check -r model --v2
#   ⚠ File format `definition/2` is not yet stable: model/messaging/kafka.yaml
#   exit code: 0

weaver registry check -r model --v2 --future
#   × File format `definition/2` is not yet stable: model/messaging/kafka.yaml
#   exit code: 1
```

官方自己的 CI 跑 policy 時**不加** `--future`（見 Makefile 的 `check-policies`），因為他們自己就在 dogfood 這個格式。

### 沒有自動遷移工具

`weaver registry --help` 裡沒有 `migrate` 之類的指令（0.25.1 的 subcommand 是 check / generate / resolve / search / stats / update-markdown / json-schema / diff / emit / live-check / mcp / infer / package）。**遷移是純手工的。**這也是官方只搬了 24 個檔案的現實原因之一。

> 順帶一個 0.25.1 實測到的變化：`resolve` 和 `search` 在 `--help` 裡都標了 **DEPRECATED**。`resolve` 建議改用 `generate` 或 `package`，`search` 則直接寫「not compatible with V2 schema」。本文為了看 resolved 結果還是用 `resolve`（它現在仍可用），但如果你要寫進 CI，用 `package` 比較保險。

### 結論

- **不要**現在把生產 registry 搬到 `definition/2`。格式會變，而且你的 CI 一加 `--future` 就紅。
- **要**繼續用 v1 語法輸入 + `--v2` 輸出。這是上游 policy 和 template package 的前提。
- **值得**現在做一次遷移演練，因為 v2 會逼你把 registry 的結構問題暴露出來（下面就有一個真實例子）。
- **一定要**加上第一行的 `# yaml-language-server:` schema 註解，v1 v2 都適用，IDE 補全和即時驗證差很多。連上游都還沒做滿——24 個 v2 檔案裡只有 16 個有這行（hardware 全加了、messaging 7 個和 faas 1 個都沒加）：

  ```bash
  for f in $(grep -rl "file_format: definition/2" model/); do
    grep -q "yaml-language-server" $f || echo "缺: $f"
  done
  # 缺: model/faas/spans.yaml
  # 缺: model/messaging/{spans,kafka,rabbitmq,rocketmq,aws,gcp,azure}.yaml
  ```

  對照 Part 4「遷移踩到的五個坑」——其中兩個（`visibility` 必填、span 要 `type:`）的錯誤訊息都是難讀的 `oneOf` 報錯，加了這行 IDE 會直接在該行標紅。messaging 那 7 個檔案就是在沒有這層保護的情況下手工搬的。

---

## Part 4 — v1 → v2 實作遷移

拿 `examples/telemetry/registry`（電商 demo，v1）實際搬一次。

### 對照表

| v1 | v2 | 備註 |
| --- | --- | --- |
| `groups:` + `type: xxx` | 頂層 `attributes:` / `metrics:` / `spans:` / `events:` / `entities:` | |
| 屬性定義的 `id:` | `key:` | |
| 屬性定義上的 `requirement_level:` | **刪掉**，移到每個 `ref:` | v2 定義端不接受 |
| `metric_name:` | `name:` | |
| span 的 `id:` | `type:` | |
| （無） | span 的 `name.note:` | **必填** |
| `span_kind:` | `kind:` | |
| entity 的 `name:` | `type:` | |
| entity 的 `attributes:` | `identity:` + `description:` | 要自己分類 |
| `extends:` | `ref_group:`（寫進 `attributes:` 列表） | 可多個 |
| （無） | attribute_group 的 `visibility:` | **必填**，`internal` / `public` |
| 複製整個 group 做特化 | `metric_refinements:` / `span_refinements:` | |

### 遷移後的樣子

`common.yaml`（屬性 + entity + 公開屬性組）：

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/open-telemetry/weaver/v0.25.1/schemas/semconv.schema.v2.json
file_format: definition/2

attributes:
  - key: service.name
    type: string
    stability: stable
    brief: "服務名稱"
    examples: ["payment-service", "cart-service"]

  - key: git.tag
    type: string
    stability: stable
    brief: "部署的 Git 版本標籤"
    examples: ["v1.0.0", "v2.1.3-rc1"]

  - key: deployment.environment
    type: string
    stability: stable
    brief: "服務部署環境"
    examples: ["production", "staging", "development"]

  - key: service.team
    type: string
    stability: stable
    brief: "負責此服務的團隊名稱"
    examples: ["payments", "cart", "auth"]

entities:
  - type: service
    stability: stable
    requirement_level: recommended
    brief: "邏輯服務實體"
    identity:
      - ref: service.name
    description:
      - ref: service.team
        requirement_level: recommended

  - type: k8s.pod
    stability: stable
    requirement_level: recommended
    brief: "Kubernetes Pod 實體"
    identity:
      - ref: k8s.pod.name

attribute_groups:
  - id: common.resource
    visibility: public
    brief: "跨服務通用的資源屬性"
    stability: stable
    attributes:
      - ref: git.tag
        requirement_level: required
      - ref: deployment.environment
        requirement_level: required
      - ref: service.team
        requirement_level: recommended
```

`payment.yaml`（三種訊號 + 一個 internal group）：

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/open-telemetry/weaver/v0.25.1/schemas/semconv.schema.v2.json
file_format: definition/2

attributes:
  - key: payment.provider
    type: string
    stability: stable
    brief: "支付服務提供商"
    examples: ["stripe", "paypal", "bank_transfer"]

  - key: payment.audit.action
    type:
      members:
        - id: authorize
          value: "authorize"
          stability: development
          brief: "授權"
        - id: capture
          value: "capture"
          stability: development
          brief: "請款"
        - id: refund
          value: "refund"
          stability: development
          brief: "退款"
    stability: development
    brief: "稽核的支付動作"
  # …其餘屬性略

attribute_groups:
  # v1 沒有的東西：internal group 不進 resolved registry、不被 policy 檢查
  - id: payment.attributes.common
    visibility: internal
    attributes:
      - ref: payment.provider
        requirement_level: required
      - ref: deployment.environment
        requirement_level: required

metrics:
  - name: payment.amount
    instrument: histogram
    unit: "{TWD}"
    stability: stable
    requirement_level: recommended
    brief: "每筆支付的金額分佈"
    attributes:
      - ref_group: payment.attributes.common

  - name: payment.errors
    instrument: counter
    unit: "{errors}"
    stability: stable
    requirement_level: recommended
    brief: "支付失敗次數計數器"
    attributes:
      - ref: payment.provider
        requirement_level: required

spans:
  - type: payment.process
    name:
      note: "使用 `payment {payment.provider}` 作為 span 名稱。"
    kind: server
    stability: stable
    requirement_level: recommended
    brief: "處理訂單支付流程的 Span"
    entity_associations:
      - all_of: [service]
      - one_of: [k8s.pod, k8s.node]
    attributes:
      # 注意：common.resource 已含 deployment.environment，
      # 不能再疊 payment.attributes.common（同屬性重複宣告 → check 會擋）
      - ref_group: common.resource
      - ref: payment.provider
        requirement_level: required
      - ref: payment.order_id
        requirement_level: required
      - ref: error.type
        requirement_level:
          conditionally_required: "僅在支付失敗時填入"

events:
  - name: payment.audit
    stability: development
    requirement_level: recommended
    brief: "支付狀態變更的稽核 log event"
    attributes:
      - ref_group: payment.attributes.common
      - ref: payment.audit.action
        requirement_level: required
```

`entity_associations` 的 `all_of` / `one_of` 語法 v1 v2 完全一樣，不用改。

### 驗證結果

完整遷移後的 registry 放在 `examples/telemetry/registry-v2/`（`common.yaml` / `payment.yaml` / `cart.yaml`，對應 v1 版的 6 個定義檔）：

```bash
cd examples/telemetry
weaver registry check -r registry-v2 --v2
# ✔ No `after_resolution` policy violation
# exit code: 0（只有 3 個 "not yet stable" 警告）

weaver registry check -r registry-v2 --v2 --future
# × File format `definition/2` is not yet stable: registry-v2/common.yaml
# × File format `definition/2` is not yet stable: registry-v2/payment.yaml
# × File format `definition/2` is not yet stable: registry-v2/cart.yaml
# exit code: 1
```

**遷移完成後，`--future` 唯一剩下的錯誤就是「格式本身還不穩定」。**這正好說明現況：語法是對的，工具也認，只差官方蓋章。

### 遷移時實際踩到的五個坑

按撞到的順序：

1. **`visibility` 是必填的，`public` 也要寫。** 少了它錯誤訊息是那串難讀的 `does not match any schema in a 'oneOf' group`，兩個 variant 都說 `Missing required property: "visibility"`。
2. **span 要 `type:`。** 錯誤訊息只有一句 `Missing required property: "type"`，沒告訴你在哪個檔案哪一行。用 Python + `jsonschema` 對 `semconv.schema.v2.json` 跑一次就能定位：
   ```bash
   curl -sLO https://raw.githubusercontent.com/open-telemetry/weaver/main/schemas/semconv.schema.v2.json
   python3 -c "
   import json,yaml,jsonschema
   sch=json.load(open('semconv.schema.v2.json'))
   d=yaml.safe_load(open('payment.yaml'))
   for e in jsonschema.Draft202012Validator(sch).iter_errors(d):
       print(list(e.absolute_path), e.message)"
   # → ['spans', 0] 'type' is a required property
   ```
   這招在 v2 摸索期非常好用，強烈建議收進工具箱。
3. **兩個 `ref_group` 撞屬性會被擋。** 前面提過的 `deployment.environment` 重複。這是 v2 幫你抓出來的既有設計問題——v1 只是靜靜 merge 掉。
4. **屬性定義端不能寫 `requirement_level`。** 我一開始用 regex 批次加，結果加到 `attributes:` 的定義上，schema 直接拒絕（`additionalProperties: false`）。定義端和使用端要分清楚。
5. **訊號沒寫 `requirement_level` 會被 `--future` 擋。**
   ```text
   × The signal group `span.payment.process` does not set `requirement_level`.
     This will be required in the future.
   ```
   注意錯誤訊息裡的 group id `span.payment.process` 是 weaver 從 `spans[].type` **自動合成**的（加 `span.` / `metric.` / `entity.` 前綴）——v2 你不用自己取 group id 了。訊號的 `requirement_level` 只有兩個值：`recommended`（預設就該發）和 `opt_in`（要明確開啟）。

---

## Part 5 — 那 Telemetry Schema 2.0 呢

順帶把另一個「v2」講完，因為它跟 `definition/2` 是同一條路線的兩端。

目前 `schema_url` 指向的 schema 檔（file format 1.1.0）只描述**版本之間的差異**——`v1.2.0 → v1.3.0 這個屬性改名了`。後端拿到只能做欄位改名這種轉換。

Semantic Convention Tooling SIG 規劃的 2.0 要做三件事：

1. **從發布 diff 改成發布完整定義。**下游拿到的是「這個版本的完整 telemetry 定義」，不是一串 patch。
2. **diff 交給 weaver 自動推導。**前提是 deprecation 要結構化——這就是 `definition/2` 那個 `reason: renamed` + `renamed_to` 的用途。**兩個 v2 在這裡接上了。**
3. **新規則要先有實作才能進 spec。**針對 1.x 那些寫進 spec 但沒人實作的轉換規則。

配套的三個工作流：Application Telemetry OTEP（讓 vendor 之間交換定義）、Component Telemetry Schema（讓開發者更好發布 schema）、Resolved Telemetry Schema（重構 resolved 結構）。

狀態：**規劃中**，沒有 spec 草案，遷移語言還沒選定。比 `definition/2` 更早期。

---

## 給現在的你的三行結論

1. **輸入繼續用 v1，輸出一定加 `--v2`。**這兩件事無關，別搞混。
2. **現在做一次遷移演練，別上生產。**v2 的 `visibility`、屬性定義/使用分離、`ref_group` 衝突檢查會逼你把 registry 的結構問題攤開——這些發現在 v1 也修得動。
3. **`# yaml-language-server:` 那行今天就加。**這是唯一一個零成本、立即有回報的動作。

---

## 參考

- [semconv-syntax.v2.md](https://github.com/open-telemetry/weaver/blob/main/schemas/semconv-syntax.v2.md) — v2 語法權威文件（標示 Alpha）
- [semconv.schema.v2.json](https://github.com/open-telemetry/weaver/blob/main/schemas/semconv.schema.v2.json) — JSON Schema，欄位真相來源
- [semconv-syntax.md](https://github.com/open-telemetry/weaver/blob/main/schemas/semconv-syntax.md) — v1 語法
- [spec issue #4427](https://github.com/open-telemetry/opentelemetry-specification/issues/4427) — Telemetry Schema 2.0 規劃
- [Telemetry Schemas](https://opentelemetry.io/docs/specs/otel/schemas/) — 目前的 1.0.0 / 1.1.0
- 已遷移的實例：`model/messaging/kafka.yaml`（span_refinements）、`model/hardware/battery-metrics.yaml`（metric_refinements + internal group）
