---
title: 'OpenTelemetry Weaver #10：拆解官方 semantic-conventions 與 weaver-packages'
tags: [' OpenTelemetry']

---

# OpenTelemetry Weaver — 第十篇：拆解官方 semantic-conventions 與 weaver-packages

> 前九篇我們用一個自己捏的電商 demo registry，把 schema、Merge Gate、發布演進、企業治理、MCP、Signal Plane 走了一遍。
> 但每次分享完，最常被問的都是同一句：「這是你自己玩的規模，真的有團隊這樣用嗎？registry 大到幾百個 attribute 之後會長什麼樣？」
>
> 這個問題其實有標準答案，而且答案就在眼前——**OpenTelemetry 自己的 semantic-conventions repo 就是全世界最大的 weaver registry**，而且它的 CI 從頭到尾都是 weaver 在把關。
> 這篇我們就把 [`open-telemetry/semantic-conventions`](https://github.com/open-telemetry/semantic-conventions) 和它 2026 年初分出去的 [`open-telemetry/opentelemetry-weaver-packages`](https://github.com/open-telemetry/opentelemetry-weaver-packages) 拆開看，然後把上游那套 policy **實際套到我們的 demo registry 上跑一次**——結果比我預期的有趣很多，六個 violation、一個誤判、一個 template 直接爆掉。
>
> 環境：weaver **0.25.0**、semantic-conventions main（2026-07 當下）。所有輸出原樣貼上。

---

## 0. 為什麼要拆這兩個 repo

老實說我一開始只是想找「一份寫得夠好的 registry」來當範例。點進去之後才發現，這個 repo 遠不只是 registry：

- `model/` 是 registry 本體，**76 個 area 目錄**（android、aws、db、gen-ai、http、k8s、mcp、messaging、openai…），每個 area 一個資料夾。
- `policies/` 只剩**一支** rego。
- `policies_test/` 用 OPA 跑 rego 的單元測試。
- `schemas/` 存了 **1.4.0 一路到 1.43.0** 的 schema 檔。
- `templates/registry/markdown/` 是產生官方文件網站的模板。
- `.github/workflows/checks.yml` 把上面全部串成 PR gate。

換句話說：**我們前九篇講的每一件事，這個 repo 都在用，而且是在 76 個 area、上千個 attribute 的規模下用。**

而 `policies/` 只剩一支 rego 這件事，就是最值得講的伏筆——policy 搬去哪了？

---

## 1. 先看目錄，對照前面篇章

```text
semantic-conventions/
├── model/                      # registry 本體（#2 講的 YAML）
│   ├── manifest.yaml
│   ├── http/ db/ k8s/ gen-ai/ … （76 個 area）
│   └── version.properties
├── policies/                   # 本地 policy（#3 講的 Merge Gate）
│   ├── README.md
│   └── brief.rego              # ← 只剩這一支
├── policies_test/
│   └── brief_test.rego         # OPA 單元測試
├── schemas/                    # #4 講的版本演進
│   ├── 1.4.0 … 1.43.0
├── templates/registry/markdown/  # #5 講的 template
├── docs/                       # 由 templates + model 生成的文件
├── dependencies.Dockerfile     # 工具版本釘選
├── Makefile                    # 所有指令的單一入口
└── .github/workflows/checks.yml
```

一個對照表，把官方做法跟我們前面幾篇對起來：

| 我們的篇章 | semantic-conventions 的對應 |
| --- | --- |
| #2 registry 結構 | `model/` + `manifest.yaml`，一個 area 一個目錄 |
| #3 Merge Gate | `make check-policies` + `make table-check` |
| #4 版本演進 | `schemas/1.x.y` + `make schema-check` |
| #5 多 registry 治理 | 上游 policy package + 本地 policy 分層 |
| #9 0.24/0.25 新功能 | `dependencies.Dockerfile` 釘在 `otel/weaver:v0.25.0` |

---

## 2. `dependencies.Dockerfile`：一個我立刻抄走的小技巧

這個檔案很妙，它**不是拿來 build image 的**：

```dockerfile
# DO NOT BUILD
# This file is just for tracking dependencies of the semantic convention build.
# Dependabot can keep this file up to date with latest containers.

# Weaver is used to generate markdown docs, and enforce policies on the model.
FROM otel/weaver:v0.25.0@sha256:bef6000b4a4be46f81242f9ee785e0ebf0604606c15f92cb54a59893a741ec0c AS weaver

# OPA is used to test policies enforced by weaver.
FROM openpolicyagent/opa:1.18.2@sha256:cba27d3c6af2feba1e4d6e6b5e24df5b53db332420d4148a90acccd12efae6ed AS opa

# Lychee is used for checking links in documentation.
FROM lycheeverse/lychee:sha-0a96dc2@sha256:2d397eb32e4add073deb5af328f7d644538cd62c007892c57b57551b073b6a12 AS lychee
```

它存在的唯一理由是：**Dependabot 認得 Dockerfile 語法，會自動幫你發 PR 升級這些 image**。工具版本因此有單一事實來源，而且是釘到 digest 的。Makefile 再把版本讀回來：

```makefile
VERSIONED_WEAVER_CONTAINER_NO_REPO=$(shell cat dependencies.Dockerfile | awk '$$4=="weaver" {print $$2}')
WEAVER_CONTAINER=$(WEAVER_CONTAINER_REPOSITORY)/$(VERSIONED_WEAVER_CONTAINER_NO_REPO)
```

痛點很實際：如果 weaver 版本散在 CI workflow、Makefile、README 三個地方，某天升級一定會漏掉一個，然後你會遇到「本機綠、CI 紅」——第九篇那條 `resolved_schema_uri` → `resolved_registry_uri` 的破壞性改名，就是這種情境的完美地雷。這個檔案花五分鐘加，之後省下的時間不只五分鐘。

---

## 3. CI 全貌：`checks.yml` 每個 job 在檢查什麼

`.github/workflows/checks.yml` 把每項檢查拆成**獨立 job**（失敗時一眼看出是哪一類問題，不用翻 log）。跟 weaver 有關的是這幾個：

| Job | 指令 | 在防什麼 |
| --- | --- | --- |
| 表格檢查 | `make table-check` | markdown 內嵌的 semconv 表格跟 YAML 不同步 |
| registry 文件 | `make registry-generation` + `git diff --exit-code` | 生成的 `docs/registry/*.md` 沒 commit |
| policy 檢查 | `make check-policies` | 命名、穩定性、破壞性變更 |
| policy 測試 | `make test-policies` | rego policy 自己壞掉 |
| schema | `make schema-check` | `schemas/` 版本檔錯誤 |
| dead yaml | `make check-dead-yaml` | YAML 定義了 signal 但沒有任何 markdown 用到 |
| issue 模板 | `make generate-gh-issue-templates` + `git diff` | issue 下拉選單的 area 清單過期 |

有兩個設計我覺得特別值得抄。

### 3.1 「生成物必須已 commit」的 gate

```yaml
- name: verify registry tables
  run: |
    make registry-generation
    git diff --exit-code './docs/registry/*.md' || (echo 'Attribute registry markdown is out of date, please run "make registry-generation" and commit the changes in this PR.' && exit 1)
```

這是 codegen 專案的經典手法：**CI 重跑生成，然後要求 diff 是空的**。它同時擋掉兩種錯誤——「改了 YAML 但忘記重跑生成」以及「手改了生成檔案」。錯誤訊息還直接告訴你該跑哪個指令，這種對貢獻者的體貼是 OTel repo 的一貫水準。

我們第三篇的 Merge Gate 只擋了 `registry check`，這條其實更該補上——因為忘記重跑生成的機率，遠比寫壞 YAML 高。

### 3.2 `table-check` 用 dry-run，不用 git diff

同樣的目的，表格檢查換了個做法：

```makefile
table-check:
	$(DOCKER_RUN) ... $(WEAVER_CONTAINER) registry update-markdown \
		--registry=/home/weaver/source \
		--param registry_base_url=/docs/registry/ \
		--templates=/home/weaver/templates \
		--dry-run \
		/home/weaver/target
```

`update-markdown --dry-run` 是 weaver 內建的「檢查模式」：它會算出應該生成什麼，跟現有檔案比對，不一致就 exit 非零，但**不寫檔**。`docs/` 是唯讀掛載，連寫都寫不進去。

什麼時候用哪個？`--dry-run` 適合 weaver 原生支援的 markdown 更新；`git diff --exit-code` 是萬用解，任何生成物都能套。

### 3.3 `check-dead-yaml`：反向找孤兒

這個檢查我第一次看到，思路很有意思——它用一個自訂 template target 把 registry 裡所有 signal 名稱倒成一份清單，再去 grep `docs/` 有沒有被引用：

```makefile
check-dead-yaml:
	... registry generate --registry=/home/weaver/source --templates=... --v2 \
		signal-groups /home/weaver/target
	$(TOOLS_DIR)/scripts/find-dead-yaml.sh $(PWD)/internal/tools/bin/signal-groups.txt $(PWD)/docs
```

**template 不一定要拿來產程式碼**，它也可以只是「把 registry 查詢出一份清單」的工具。這個角度可以延伸出很多自訂檢查：找沒人用的 attribute、找沒有 examples 的欄位、產 owner 對照表。

---

## 4. 伏筆揭曉：policy 搬去 weaver-packages 了

回到開頭那個問題——為什麼 `policies/` 只剩一支 `brief.rego`？

看 `make check-policies` 就懂了：

```makefile
LATEST_RELEASED_SEMCONV_VERSION := $(shell git ls-remote --tags https://github.com/open-telemetry/semantic-conventions.git \
	| cut -f 2 | sort --reverse | head -n 1 | tr '/' ' ' | cut -d ' ' -f 3 | $(SED) 's/v//g')

WEAVER_PACKAGES_REPO=https://github.com/open-telemetry/opentelemetry-weaver-packages.git

check-policies:
	$(DOCKER_RUN) --rm ... ${WEAVER_CONTAINER} registry check \
		--v2 \
		--registry=/home/weaver/source \
		--baseline-registry=https://github.com/open-telemetry/semantic-conventions/archive/refs/tags/v$(LATEST_RELEASED_SEMCONV_VERSION).zip[model] \
		--policy=/home/weaver/policies \
		--policy="$(WEAVER_PACKAGES_REPO)[policies/check/naming_conventions]" \
		--policy="$(WEAVER_PACKAGES_REPO)[policies/check/stability]" \
		--policy="$(WEAVER_PACKAGES_REPO)[policies/check/entity_associations]" \
		--policy="$(WEAVER_PACKAGES_REPO)[policies/check/backwards-compatibility]"
```

這一段指令資訊量很大，拆三個點來看。

### 4.1 `--policy` 可以直接指向遠端 repo 的子目錄

`repo-url[subdir]` 這個語法（`--registry`、`--templates`、`--baseline-registry` 也通用）是 weaver 的 virtual directory 機制：weaver 會 clone 到 `~/.weaver/vdir_cache/` 再取子目錄。

意思是 **policy 可以像 library 一樣被共用**。你不用把 rego 複製到每個 repo，也不用自己包 artifact。

### 4.2 `--baseline-registry` 動態抓最新 release tag

`git ls-remote --tags | sort -r | head -1` 撈出最新 tag，組出 GitHub 的 archive zip URL，加上 `[model]` 指定 zip 內的子目錄。

這正是第三篇「破壞性變更該拿什麼當基準」的正解：**基準不是上一個 commit，是上一個 release**。開發中的 main 本來就允許改來改去，只有跨 release 才需要相容性保證。而且它是動態的，發新版之後不用改 Makefile。

### 4.3 本地 policy 和遠端 policy 可以疊加

`--policy` 可以給多次，本地目錄和遠端 package 混用。`policies/README.md` 把分工寫得很清楚：

> Most semantic-convention policy checks are provided by the shared opentelemetry-weaver-packages repository (…). Only checks that are **not** available upstream are kept here:
>
> - `brief.rego` — requires a non-empty `brief` on every attribute and signal. This is a semantic-conventions **editorial requirement** rather than a general registry rule.

這句話就是第五篇「多 registry 與企業治理」的實務版分層：

- **通用規則**（命名、穩定性、相容性）→ 用上游共用 package
- **組織自己的編輯規範**（例如「brief 不能空白」）→ 留在自己 repo

套到公司內部就是：`platform-team/telemetry-policies` 放全公司共用的，各 BU 的 registry 只留自己的特殊規範。

---

## 5. weaver-packages 這個 repo 長什麼樣

[`opentelemetry-weaver-packages`](https://github.com/open-telemetry/opentelemetry-weaver-packages) 是 2026 年 1 月成立的共享套件庫，README 開宗明義：

> Weaver packages come in two primary forms:
> - `templates`: Code generation, Documentation generation, etc.
> - `policies`: Verification and validation rules that can be applied to a repository.

```text
opentelemetry-weaver-packages/
├── policies/check/
│   ├── naming_conventions/       # 7 支 rego + 9 個測試案例
│   ├── stability/                # deprecation.rego, stability.rego
│   ├── entity_associations/
│   └── backwards-compatibility/  # compat.rego
├── templates/docs/markdown/      # 產 registry 文件的完整模板組
├── diagnostic_templates/json/    # 把 weaver 診斷輸出成 JSON
├── buildscripts/
│   ├── test_weaver_policies.sh
│   └── test_weaver_templates.sh
└── skills/prepare-release.md
```

四個 policy package 的職責：

| Package | 檢查什麼 |
| --- | --- |
| `naming_conventions` | 名稱格式（regex）、常數名衝突、metric/attribute 命名空間衝突、enum member 唯一性、複雜型別限制、metric brief 格式 |
| `stability` | `renamed_to` 指向有效目標、stable entity 必須有 identifying attribute、**signal 穩定度不得高於它引用的 attribute** |
| `entity_associations` | `entity_associations` 參照的 entity 要真的存在 |
| `backwards-compatibility` | 對照 baseline，signal 不得消失 / 改 unit / 改型別 |

其中 `stability` 那條「穩定度排序」規則設計得很細，README 寫：

> Stability levels are ordered `development`/`experimental` < `alpha` < `beta` < `release_candidate` < `stable`, so this catches both a stable metric referencing a development attribute and, e.g., a `release_candidate` span referencing a `development` attribute.

這是真實會踩的坑：你把 metric 標成 stable 對外承諾了，但它引用的 attribute 還是 development、隨時可能改名——等於承諾跳票。除非該 attribute 是 `opt_in`（使用者主動開啟，不算隱含承諾）。

---

## 6. 實跑：把上游 policy 套到我們的電商 demo

理論看完，直接套到前面幾篇那個電商 registry（`examples/telemetry/registry`）上。這才是這篇最有意思的部分。

### 6.1 naming_conventions：六個 violation，而且不是我寫錯

```bash
weaver registry check --v2 \
  -r ./telemetry/registry \
  -p 'https://github.com/open-telemetry/opentelemetry-weaver-packages.git[policies/check/naming_conventions]'
```

> 注意 shell quoting：`[...]` 在 zsh 是萬用字元，整個參數一定要用單引號包起來，否則會看到莫名其妙的 `division by zero`。

```text
Weaver Registry Check
Checking registry `./telemetry/registry`
ℹ Found registry manifest: ./telemetry/registry/manifest.yaml
✔ All `after_resolution` policies checked (6 violations found)

Diagnostic report:

Violation: naming_convention_metric_brief_period
  - Message   : Non-empty metric brief '加入購物車的商品件數' must end with a period (.).
  - Level     : violation
  - Context   :
    - brief : 加入購物車的商品件數
  - Provenance: ./telemetry/registry

Violation: naming_convention_metric_brief_period
  - Message   : Non-empty metric brief '支付失敗次數計數器' must end with a period (.).
...（共 6 筆，每個 metric 一筆）

Total execution time: 1.373902911s
```

第一次跑出來我愣了一下，然後笑出來——**六個 violation 全部是「brief 沒有以句號結尾」**。

這是個很好的教材。我們的 registry 用中文寫 brief，中文不會在句尾加半形句號；但上游這條規則來自 OTel 的英文文件編輯規範。看一下 rego 就知道它完全沒有轉圜空間：

```rego
deny contains finding if {
    some metric in input.registry.metrics
    trimmed_brief := trim(metric.brief, " \n")
    trimmed_brief != ""
    not endswith(trimmed_brief, ".")
    finding := { "id": "naming_convention_metric_brief_period", ... }
}
```

**沒有讀 `policy_exceptions`。**這條規則無法豁免。

這帶出一個實務結論：**上游 package 不是「全套照收」，要挑著用**。`naming_conventions` 這個 package 混了兩類規則——真正的通用規則（名稱衝突、regex 格式）和 OTel 自家的編輯規範（brief 句號）。對非英文 registry 來說，後者不適用。

三個選擇：
1. 接受規範，brief 一律加句號（最省事，但中文讀起來怪）。
2. 不用整個 `naming_conventions`，改抄它的 rego 挑需要的規則放自己 repo。
3. 用 `diagnostic_templates/json` 把輸出轉 JSON，在 CI 裡過濾掉特定 finding id。

我選 2。這也剛好呼應第五篇的分層原則——**共用的是「規則庫」，不是「規則集」**。

### 6.2 stability：三個 entity 沒有身分

```bash
weaver registry check --v2 -r ./telemetry/registry \
  -p 'https://github.com/open-telemetry/opentelemetry-weaver-packages.git[policies/check/stability]'
```

```text
✔ All `after_resolution` policies checked (3 violations found)

Violation: stability_entity_no_identity
  - Message   : Stable entity 'k8s.node' has no identifying attributes
Violation: stability_entity_no_identity
  - Message   : Stable entity 'k8s.pod' has no identifying attributes
Violation: stability_entity_no_identity
  - Message   : Stable entity 'service' has no identifying attributes
```

這個就是**我真的寫錯了**。我的 entity 定義長這樣：

```yaml
- id: entity.service
  type: entity
  name: service
  stability: stable
  brief: "邏輯服務實體"
  attributes:
    - id: service.name
      type: string
      stability: stable
      brief: "服務名稱"
      requirement_level: required      # ← 我以為 required 就夠了
```

`requirement_level: required` 表達的是「這個 attribute 一定要有值」，但 entity 需要的是**哪些 attribute 構成它的身分**（identity）——也就是拿哪幾個欄位去判斷「這兩筆資料是不是同一個 service」。這兩件事不一樣，要用 `role: identifying` 標。

一個 stable entity 沒有 identity，下游就無法做 entity 去重與關聯——這正是 Signal Plane（第八篇）最需要的東西。**上游 policy 幫我抓到一個我自己讀三遍都沒發現的設計錯誤，這一條就值回票價。**

### 6.3 entity_associations：一個誤判（要誠實講）

```bash
weaver registry check --v2 -r ./telemetry/registry \
  -p '...[policies/check/entity_associations]'
```

```text
Violation: entity_association_unknown_entity
  - Message   : Unknown entity '{"all_of": ["service"]}' associated with span 'payment.process'
Violation: entity_association_unknown_entity
  - Message   : Unknown entity '{"one_of": ["k8s.pod", "k8s.node"]}' associated with span 'payment.process'
```

但 `service`、`k8s.pod`、`k8s.node` 三個 entity 我都定義了（上一節才被檢查過），怎麼會 unknown？

看 rego 就懂了：

```rego
known_entities := {entity.type | some entity in input.registry.entities}

deny contains finding if {
    some span in input.registry.spans
    some association in span.entity_associations
    not known_entities[association]        # ← 假設 association 是字串
    ...
}
```

它假設 `entity_associations` 的每個元素都是**字串**。但我們用的是複合語法：

```yaml
entity_associations:
  - all_of: [service]            # payment-service 一定要帶
  - one_of: [k8s.pod, k8s.node]  # k8s 帶 pod，裸機帶 node，擇一
```

複合形式進到 rego 是 object 不是 string，`known_entities[{...}]` 當然找不到，於是誤報。這不是我寫錯，是 **policy 還沒跟上 weaver 的語法演進**。

這也是為什麼每個 package 的 README 都標著：

```text
Stability: Development
Owners: @open-telemetry/specs-semconv-maintainers
```

**用上游 package 前先確認你的 registry 用到的語法有沒有被涵蓋。**這種誤判如果直接接上 Merge Gate，會變成擋所有 PR 的假警報，然後團隊就會養成 `--no-verify` 的壞習慣——比沒有 gate 更糟。

---

## 7. backwards-compatibility 實跑：完整的破壞性變更演練

這個 package 需要 `--baseline-registry`，所以來做一次完整演練。複製一份 registry 當基準，另一份故意做兩個破壞性變更：

```bash
cp -r telemetry/registry /tmp/base
cp -r telemetry/registry /tmp/cur

# 變更 1：改 unit（{errors} → {failures}）
# 變更 2：改名（payment.amount → payment.amount.total）
```

```bash
weaver registry check --v2 \
  -r /tmp/cur \
  --baseline-registry /tmp/base \
  -p 'https://github.com/open-telemetry/opentelemetry-weaver-packages.git[policies/check/backwards-compatibility]'
```

```text
Checking registry `/tmp/cur`
ℹ Found registry manifest: /tmp/cur/manifest.yaml
ℹ Found registry manifest: /tmp/base/manifest.yaml
✔ No `after_resolution` policy violation
✔ All `comparison_after_resolution` policies checked (2 violations found)

Diagnostic report:

Violation: compatibility_metric_missing
  - Message   : Metric 'payment.amount' no longer exists in semantic conventions
  - Level     : violation
  - Provenance: /tmp/cur

Violation: compatibility_metric_changed_unit
  - Message   : Metric 'payment.errors' cannot change unit (was '{errors}', now: '{failures}')
  - Level     : violation
  - Context   :
    - current_unit : {failures}
    - previous_unit : {errors}
  - Provenance: /tmp/cur

Total execution time: 3.5306805150000002s
```

兩個都抓到了，而且注意輸出裡的兩行：

```text
✔ No `after_resolution` policy violation
✔ All `comparison_after_resolution` policies checked (2 violations found)
```

weaver 的 policy 有**兩個執行階段**：`after_resolution`（只看當前 registry）和 `comparison_after_resolution`（比對 baseline）。前面幾個 package 都是前者，只有 backwards-compatibility 是後者。這也解釋了為什麼它一定要 `--baseline-registry`——沒有基準，第二階段根本不會執行，你的相容性檢查會靜靜地什麼都不做（綠燈，但沒檢查到東西）。

**這是最危險的一種 CI 假象。**如果你的 Merge Gate 有 backwards-compatibility 卻忘了給 baseline，它會永遠是綠的。

那個「改名成 `payment.amount.total`」的案例也值得注意：從 policy 的角度，改名 = 舊的消失了。要合法改名，得用第四篇講的 `deprecated.renamed_to` 保留舊定義，而不是直接改字。

---

## 8. 上游的 template package 也能直接用

weaver-packages 除了 policy 還有 `templates/docs/markdown`——就是產生 OTel 官方文件那套模板。它也能直接消費：

```bash
weaver registry generate --v2 \
  --registry ./telemetry/registry \
  -t 'https://github.com/open-telemetry/opentelemetry-weaver-packages.git[templates/docs]' \
  markdown \
  /tmp/docsout
```

兩個容易踩的點，我兩個都踩了：

1. **路徑要指到 target 的上一層。**weaver 的 `-t` 給的是「模板根目錄」，最後那個 `markdown` 是 target 名稱，會被接成 `templates/docs/markdown`。我一開始寫 `[templates/docs/markdown] markdown`，得到 `Failed to canonicalize the path '.../markdown/markdown'`。
2. **一定要加 `--v2`。**這套模板是為 v2 resolved schema 寫的，少了會爆 `Filter 'semconv_grouped_metrics(...)' failed: cannot use null as iterable`。

跑起來的結果：

```text
✔ Generated file "/tmp/docsout/payment/README.md"
✔ Generated file "/tmp/docsout/README.md"
...
/tmp/docsout/
├── README.md
├── cart/{README.md, metrics.md, spans.md}
├── payment/{README.md, metrics.md, events.md}
├── k8s/{README.md, entities.md}
├── service/{README.md, entities.md}
├── deployment/README.md
├── git/README.md
└── error/README.md
```

`payment/metrics.md` 的內容（原樣）：

```markdown
<!-- NOTE: THIS FILE IS AUTOGENERATED. DO NOT EDIT BY HAND. -->
<!-- see templates/docs/markdown/metric_namespace.md.j2 -->

# Payment metrics

| Name | Stability | Description |
| --- | --- | --- |
| [`payment.amount`](#paymentamount) | ![Stable](https://img.shields.io/badge/-stable-lightgreen) | 每筆支付的金額分佈 |
| [`payment.duration`](#paymentduration) | ![Stable](https://img.shields.io/badge/-stable-lightgreen) | 支付處理耗時（毫秒） |
| [`payment.errors`](#paymenterrors) | ![Stable](https://img.shields.io/badge/-stable-lightgreen) | 支付失敗次數計數器 |

## `payment.amount`

| Name | Instrument Type | Unit (UCUM) | Description | Stability | Entity Associations |
| -------- | --------------- | ----------- | -------------- | --------- | ------ |
| `payment.amount` | Histogram | `{TWD}` | 每筆支付的金額分佈 | ![Stable](...) | |

**Attributes:**

| Key | Stability | Requirement Level | Value Type | Description | Example Values |
| --- | --- | --- | --- | --- | --- |
| [`deployment.environment`](...) | ![Stable](...) | `Required` | string | 服務部署環境 | `production`; `staging`; `development` |
| [`payment.provider`](...) | ![Stable](...) | `Required` | string | 支付服務提供商 | `stripe`; `paypal`; `bank_transfer` |
```

**沒寫半行模板，就得到跟 OTel 官網同一個格式的文件網站**，namespace 分頁、stability 徽章、attribute 表格、跨頁連結全都有。第五篇我們花了整篇在刻自己的模板，現在有現成的可以直接用。

不過同一個坑又出現了——最後有個錯誤：

```text
× Template evaluation error -> invalid operation: Expected string, found:
│ {"all_of": ["service"]} (in entity_macros.j2:13)
```

跟 6.3 的誤判**同一個根因**：複合式 `entity_associations` 還沒被支援，policy 誤判、template 直接爆。所以 `payment/spans.md` 沒產出來（`cart/spans.md` 有，因為 cart span 沒用複合語法）。

這條線索串起來就是一個完整的訊號：**`all_of` / `one_of` 是 weaver 較新的語法，生態系（policy、template）還在追趕。**如果你要在生產環境用複合 entity association，得有心情自己補 template。或者反過來——在生態系跟上之前，先用單純的字串列表。

---

## 9. 例外機制：`policy_exceptions` 與它的邊界

上游 package 提供的豁免機制不是 CI 裡的 skip list，而是**寫回 model 裡**：

```yaml
metrics:
  - name: hw.battery.charge
    brief: A colliding metric.
    stability: stable
    instrument: counter
    unit: "1"
    annotations:
      naming_conventions:
        policy_exceptions:
          # I should document why this exception is allowed.
          - metric_namespace_collision
```

規則：key 是 `<package_name>.policy_exceptions`，值是 **finding id 去掉 package 前綴**。例如 `stability_metric_lower_stability_attribute` → `metric_lower_stability_attribute`。

我很喜歡這個設計，理由有三個：

1. **例外跟著定義走。**不是散在 CI 設定裡，而是就寫在那個 metric 旁邊。誰改到這個 metric，就會看到例外。
2. **例外會進 code review。**改 model 一定要開 PR，例外因此自動被 owner 審。
3. **例外可以留註解說明理由。**上游 README 那句 `# I should document why this exception is allowed.` 本身就是在示範這個習慣。

對照我們第三篇 Merge Gate 那種「CI 裡 grep 掉某些訊息」的做法，這個乾淨太多——CI 過濾是隱形的技術債，半年後沒人記得為什麼要濾。

但**邊界要講清楚**：不是每條規則都吃例外。`naming_conventions` README 只列出一個：

> Supported policy_exception strings:
> - `metric_namespace_collision`: For metrics which have namespace conflicts.

我們 6.1 遇到的 `metric_brief_period` 就在支援清單外——rego 裡根本沒讀 annotation。要判斷某條規則能不能豁免，最可靠的方法是**去看那支 rego 有沒有 `exceptions` 那段**：

```rego
exceptions := { policy | some policy in metric.annotations.naming_conventions.policy_exceptions } | ...
not exceptions["metric_namespace_collision"]
```

有這兩行才吃例外。

另外 `attribute_constant_collision` 是特例，它不用 `policy_exceptions`，而是叫你用另一個 annotation 讓 codegen 跳過該欄位：

```yaml
attributes:
- key: my.attribute
  annotations:
    code_generation:
      exclude: true   # 提示所有 codegen 不要使用這個 attribute
```

---

## 10. policy 也要有測試：一個可以照抄的框架

第三篇我們寫 rego 的時候，驗證方式是「改壞 YAML，看它有沒有叫」——手動、不可重複、改 rego 之後也不知道有沒有回歸。weaver-packages 的做法完全不同。

`policies/check/AGENTS.md` 定義了套件規格：

> Each policy package must include:
> - `*.rego`: Policy logic.
> - `README.md`: Documentation for the package.
> - `tests/`: Directory containing test cases.
>
> **Test Structure**: `tests/<test_name>/base/` (baseline registry), `tests/<test_name>/current/` (current registry), and `expected-diagnostic-output.json` (expected findings).

實際的目錄：

```text
policies/check/backwards-compatibility/
├── compat.rego
├── README.md
└── tests/
    ├── metrics/
    │   ├── base/model.yaml                    # 基準版
    │   ├── current/model.yaml                 # 改壞的版本
    │   └── expected-diagnostic-output.json    # 預期產出的 finding
    ├── spans/…  events/…  entities/…  attribute/…  attribute_groups/…
```

`naming_conventions` 有九個測試案例，命名直接對應 finding id：`attribute_constant_collision`、`enum_member_value_collision`、`metric_brief_period`、`metric_namespace_collision`、`names_violations`，還有一個 `all_valid` 當 negative case（確認乾淨的 registry 不會誤報）。`stability` 甚至有 `metric_experimental_attribute_exception` 專門測**豁免機制本身有沒有生效**。

跑測試：

```bash
./buildscripts/test_weaver_policies.sh                          # 全部
./buildscripts/test_weaver_policies.sh --test metric_brief_period  # 單一
./buildscripts/test_weaver_policies.sh --test xxx --coverage       # 看 rego 覆蓋率
```

`--coverage` 那個特別實用。寫 rego 最常見的失敗模式不是「規則寫錯」，而是「規則根本沒被執行」——某個條件永遠不成立，policy 靜靜地什麼都不做，CI 一片綠。coverage report 會告訴你哪幾行 rego 真的跑過。

失敗時實際輸出在 `observed-output/tests/<test_name>/diagnostic-output.raw`，腳本會用 `jq` 排版好方便對照。

AGENTS.md 還教了一招 rego 除錯——**用暫時的 deny 規則把狀態 dump 出來**：

```rego
deny contains finding if {
    some entity in input.registry.entities
    finding := {
        "id": "debug_entity",
        "message": sprintf("entity: %s", [entity]),
        "level": "violation",
        ...
    }
}
```

rego 沒有 debugger、也沒有 print，這個「把變數塞進 message」的土法是最實用的。搭配另一招查 input 結構：

```bash
weaver registry json-schema -j forge-registry-v2
```

會印出 policy input 的完整 schema（`input.registry.attributes` / `.metrics` / `.spans` / `.events` / `.entities` / `.attribute_groups`），不用再靠猜。

還有一個容易卡住的細節：v2 schema 檔會固定噴 `Version '2' schema file format is not yet stable` 警告，測試腳本會自動濾掉，所以 `expected-diagnostic-output.json` **不要**把它寫進去。

---

## 11. `schemas/`：版本演進長期跑下來的樣子

第四篇講 schema 演進的時候，我們的例子只有兩三個版本。官方的 `schemas/` 有 **1.4.0 到 1.43.0 共 40 個檔案**，可以看到這套機制跑五年之後的樣子。

`schemas/1.43.0` 開頭：

```yaml
file_format: 1.1.0
schema_url: https://opentelemetry.io/schemas/1.43.0
versions:
  1.43.0:
  1.42.0:
    metrics:
      changes:
        - rename_metrics:
            v8js.memory.heap.limit: v8js.memory.heap.space.size
  1.41.1:
  1.41.0:
    metrics:
      changes:
        - rename_metrics:
            k8s.container.cpu.limit: k8s.container.cpu.limit.desired
            k8s.container.cpu.limit_utilization: k8s.container.cpu.limit.utilization
            k8s.container.cpu.request: k8s.container.cpu.request.desired
            k8s.container.cpu.request_utilization: k8s.container.cpu.request.utilization
            k8s.container.memory.limit: k8s.container.memory.limit.desired
            k8s.container.memory.request: k8s.container.memory.request.desired
  1.40.0:
    all:
      changes:
        - rename_attributes:
            attribute_map:
              feature_flag.evaluation.error.message: feature_flag.error.message
```

有幾個觀察：

- **每個 schema 檔都含完整歷史**，不是只有 delta。所以任何一個版本的檔案都足以做任意兩版之間的轉換。
- **很多版本是空的**（`1.43.0:`、`1.41.1:`）——那一版沒有 rename。空條目仍然要在，代表「這個版本存在且無變更」。
- `all:` 區塊套用到所有 signal 型別，`metrics:` / `spans:` 只套用到特定型別。
- 這些檔案就是 collector `schemaprocessor` 吃的東西，也是第四篇說的「讓舊 telemetry 自動翻譯成新命名」的資料來源。

看 1.41.0 那批 k8s 改名（`cpu.limit` → `cpu.limit.desired`）就懂為什麼需要這個機制：這種大批改名如果沒有 schema 檔記錄，所有下游 dashboard 和 alert 都會無聲斷掉。

---

## 12. 帶回自己的專案：一份可以抄的最小組合

把上面所有東西壓縮成能立刻用的版本。

**`dependencies.Dockerfile`**（讓 Dependabot 幫你顧版本）：

```dockerfile
# DO NOT BUILD — dependency tracking only.
FROM otel/weaver:v0.25.0 AS weaver
FROM openpolicyagent/opa:1.18.2 AS opa
```

**`Makefile`**：

```makefile
WEAVER_VERSION=$(shell awk '$$4=="weaver" {print $$2}' dependencies.Dockerfile)
WEAVER_PACKAGES=https://github.com/open-telemetry/opentelemetry-weaver-packages.git
LATEST_TAG := $(shell git describe --tags --abbrev=0 2>/dev/null)

.PHONY: check
check:
	weaver registry check --v2 \
	  --registry ./telemetry/registry \
	  --baseline-registry "https://github.com/your-org/your-repo/archive/refs/tags/$(LATEST_TAG).zip[telemetry/registry]" \
	  --policy ./policies \
	  --policy "$(WEAVER_PACKAGES)[policies/check/stability]" \
	  --policy "$(WEAVER_PACKAGES)[policies/check/backwards-compatibility]"

.PHONY: docs
docs:
	weaver registry generate --v2 \
	  --registry ./telemetry/registry \
	  -t "$(WEAVER_PACKAGES)[templates/docs]" \
	  markdown ./docs/registry/

.PHONY: docs-check
docs-check: docs
	git diff --exit-code './docs/registry/*' \
	  || (echo 'docs out of date — run "make docs" and commit' && exit 1)
```

注意我**沒有**收 `naming_conventions`（brief 句號規則不適用中文 registry）也**沒有**收 `entity_associations`（複合語法誤判）。挑著用，不是全套照收。

**`.github/workflows/checks.yml`**——照官方拆成獨立 job：

```yaml
jobs:
  policies:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }   # baseline 要抓 tag，不能用 shallow clone
      - run: make check

  docs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make docs-check
```

`fetch-depth: 0` 那行是血淚——預設的 shallow clone 抓不到 tag，`git describe` 會回空字串，於是 `--baseline-registry` 變成一個壞掉的 URL，或更糟：整個相容性檢查靜悄悄地沒跑。

---

## 13. 幾個沒解決的問題

誠實記錄，這些是我目前還沒有好答案的：

**weaver-packages 還沒有 release。**semantic-conventions 的 Makefile 裡留著這條 TODO：

```makefile
# TODO: pin commit or tag of opentelemetry-weaver-packages and add it to renovate
# once weaver-packages is released.
```

也就是說**連官方自己現在都是抓 main 分支**。上游 policy 改一行，你的 CI 明天就可能紅。在 release 出來之前，比較保險的做法是 fork 一份或把 rego 複製進自己 repo；至少要有心理準備 CI 會因為別人的 commit 而變色。

**每個 package 都標 `Stability: Development`。**這不是客套話，6.3 那個誤判就是實例。

**複合 `entity_associations` 的生態系支援還沒到位。**policy 誤判、官方 markdown template 直接爆。我還沒去確認 upstream 有沒有對應的 issue，這會是下一步。

---

## 小結

繞了一圈，最有價值的三個收穫：

1. **官方 repo 是最好的參考實作。**`table-check` 用 `--dry-run`、生成物用 `git diff --exit-code` 把關、`dependencies.Dockerfile` 釘版本、CI 拆成獨立 job——這些都是規模化之後才學得到的教訓，直接抄就好。

2. **policy 已經在變成可共用的 library。**`--policy 'repo.git[subdir]'` 這個語法把 rego 從「每個 repo 各自複製」變成「共享套件」，配上「上游通用規則 + 本地編輯規範」的分層，第五篇講的企業治理有了具體形狀。

3. **但共用不等於照單全收。**把上游 policy 套到自己的 registry 上，六個 violation 裡有六個是英文編輯規範（不適用）、三個 stability violation 是真的設計錯誤（超值）、兩個 entity 誤判是 policy 沒跟上語法（要避開）。**跑一次才知道哪些適合你**——這件事花不到五分鐘，但沒跑過就接上 Merge Gate，換來的會是一個團隊集體無視的假警報產生器。

下一篇我想把 6.2 抓到的 entity identity 問題補完，順便看看 `role: identifying` 修好之後，Signal Plane 的 entity 關聯能做到什麼程度。

---

## 參考

- [open-telemetry/semantic-conventions](https://github.com/open-telemetry/semantic-conventions)
- [open-telemetry/opentelemetry-weaver-packages](https://github.com/open-telemetry/opentelemetry-weaver-packages)
- [semantic-conventions `policies/README.md`](https://github.com/open-telemetry/semantic-conventions/blob/main/policies/README.md)
- [weaver-packages `policies/check/AGENTS.md`](https://github.com/open-telemetry/opentelemetry-weaver-packages/blob/main/policies/check/AGENTS.md)
- [naming_conventions README](https://github.com/open-telemetry/opentelemetry-weaver-packages/blob/main/policies/check/naming_conventions/README.md) ／ [stability README](https://github.com/open-telemetry/opentelemetry-weaver-packages/blob/main/policies/check/stability/README.md)
