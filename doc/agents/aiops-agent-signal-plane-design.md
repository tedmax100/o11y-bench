# aiops-agent — Signal Plane：decision-grade telemetry 設計

對照基準：`Agentic 可靠性工程 #2`（ch2 Foundations of Agentic Observability）、`#4`（ch4.3 Signal Plane）、[`agent reliability engineerin.md`](./agent%20reliability%20engineerin.md)
銜接文件：[`aiops-agent-ARE-discrepancy-review.md`](./aiops-agent-ARE-discrepancy-review.md) §2.1 / §5（出入報告把 Signal Plane 列為**第一順位缺口**）、[`aiops-agent-ARE-gap-analysis.md`](./aiops-agent-ARE-gap-analysis.md) §4
銜接現況：`service/app/schema_catalog.md` / `capability.py` / `tools/discovery.py` / `agent.py`（RCA playbook 注入）
撰寫日期：2026-06-19

---

## 0. 範圍與根因

出入報告的翻案結論：我們蓋了 ARE 四平面裡最顯眼的三個（Reasoning / Governance / Execution 骨架），卻跳過了 ARE 認定是**一切前提**的第一個 —— Signal Plane。RCA 在 o11y-bench 只 ~2/9、confident-wrong，根因**不是 PromQL bug，是跳過了 Signal 層**。

現況的本質問題（不是「沒能力查」，是「查的東西不是 decision-grade」）：

- **topology 是散文**：`schema_catalog.md` 32–39 行用 ASCII 畫依賴圖，是手維護的 wiki page，不是 ARE ch2.4/ch4.3 要的「活的、持續對齊遙測的依賴圖」。會 drift，且 agent 只能整段讀，不能查「payment 的上游是誰」。
- **criticality / journey 不存在**：哪個服務是 revenue-critical、屬於哪條 user journey、SLO tier 多少 —— ARE 要求這些是 **schema** 而非社群知識，我們完全沒有。
- **無 signal contract**：agent 直接吃 demo-services **原始、給人看的** Prom/Loki/Tempo，自己猜「哪個 metric 是 payment 的 error SLI」。沒有 versioned 的 SLI 宣告、freshness 保證、percentile 語意、exclusion conditions。
- **capability snapshot 是繞過、不是實作**：`capability.py` 在查詢當下補「有哪些 metric/span/field」，但那是 ARE ch1.3 說的「人類補貼的程式化版本」—— 它在繞過 Signal Plane（人在旁邊看時好用），不是實作 Signal Plane（語意 + topology + criticality + contract 長在訊號裡）。

> 一句話：把目前散在 `schema_catalog.md` 散文裡、靠 LLM「讀懂」的 Signal 語意，升格成**第一級、可查詢、與遙測持續對齊、versioned** 的 artifact，餵進唯讀推論核心當 decision-grade 前提。

### 設計鐵律（延續 gap-analysis §4.3）

> 全唯讀推論核心**不動**，執行平面（step7）**不動**。Signal Plane 是**推論核心的上游**：它只生產更高品質的 context 餵給 agent，本身全唯讀、無副作用、可獨立關閉、可獨立 review。
> **artifact 與 reconciler 分離**：宣告（`topology.yaml` / `contracts.yaml`，人寫的 intent）與**對齊**（從遙測反推、diff、標 drift）是兩件事。宣告可能錯/過時，唯有「對齊到遙測」才是 decision-grade。
> **fail-open 餵 context、fail-closed 餵信心**：Signal Plane 讀不到（datastore hiccup）→ 退回現行 catalog + discover_*，不阻斷 RCA（fail-open，因為它是增強層）；但 drift / 過期 / 對不上 → 不能假裝 decision-grade，要把不確定性**顯式**標進注入的 context（fail-closed 在「宣稱可信」這件事上）。

---

## 1. 元件總覽

```
        (新增：Signal Plane，唯讀，推論核心的上游)
  topology.yaml ─┐
  contracts.yaml ─┼─► signals/registry.py（載入 + 驗證宣告）
                  │        │
  Tempo/Prom ─────┴─► signals/reconcile.py（從遙測反推 + diff + drift/DQ）
                           │
                           ▼
                signals/context.py（為 RCA 的 service 組 decision-grade 注入塊）
                           │   criticality/journey + 上下游 + SLI + drift 警示
                           ▼
        (現有，唯讀推論核心 — 強化注入，邏輯不動)
  run_headless / agent ──► capability snapshot + 【新】signal context ──► Findings
```

新增檔案（每個可獨立 review、獨立 disable）：

| 檔案 | 角色 | 階段 |
|---|---|---|
| `app/signals/topology.yaml` | 宣告式服務圖：node（criticality tier / owner / journey）+ edge（caller→callee）。取代 `schema_catalog.md` 的散文依賴圖 | s1 |
| `app/signals/__init__.py` + `topology.py` | pydantic 模型（`ServiceNode` / `Edge` / `Topology`）+ loader + 對 live service set 的存在性驗證 + 查詢 API（upstream/downstream/journey） | s1 |
| `app/signals/context.py` | 把 topology（+後續 contract / health）組成注入 RCA 的 decision-grade context 塊 | s1→s4 |
| `app/signals/reconcile.py` | 從 Tempo parent→child 反推實際邊，與宣告 diff，標 drift（餵 DQ-SLO） | s2 |
| `app/signals/contracts.yaml` + contract 模型 | per-service signal contract：SLI 宣告 / freshness / percentile 語意 / 支援決策 / exclusion | s3 |
| `app/signals/health.py` | RCA 時評估該 service 的上下游 SLI 健康度（blame propagation） | s4 |

> `schema_catalog.md` 不刪：它保留「跨訊號慣例 / 查詢風格 / incident 劇本」這些**手冊知識**。但「topology / criticality / SLI」這三塊從散文遷出，改由 Signal Plane artifact 當權威來源，catalog 對應段落改成「見 signal context」。

---

## 2. 資料模型

### 2.1 `Topology`（s1）

```python
class ServiceNode(BaseModel):
    name: str                       # canonical service_name（須在 live set 內）
    role: str                       # 一句話角色（從 catalog 遷入）
    tier: int                       # criticality：1=revenue/edge-critical … 3=best-effort
    journeys: list[str]             # 屬於哪些 user journey，如 ["checkout"]
    owner: str = ""                 # ownership 當 schema（ARE 要求），demo 可留空
    repo: str = "tedmax100/o11y-bench"
    git_version: str = ""           # 當前已知部署版本（catalog 已有）

class Edge(BaseModel):
    caller: str
    callee: str

class Topology(BaseModel):
    version: str                    # signal contract versioning（ARE 要求 versioned）
    nodes: list[ServiceNode]
    edges: list[Edge]               # 宣告的依賴邊
    journeys: dict[str, list[str]]  # journey -> 有序服務鏈（checkout: webapp→…→payment）
```

查詢 API（取代「LLM 讀散文」）：`upstream(svc)` / `downstream(svc)` / `journey_of(svc)` / `tier_of(svc)` / `blast_path(svc)`（沿 edge 找出下游受影響集合）。

demo 初值（從 `schema_catalog.md` 既有事實搬，**不新編**）：

```yaml
version: "1.0.0"
journeys:
  checkout: [webapp, api-gateway, order-service, payment-service]
nodes:
  - {name: webapp,          role: "public edge", tier: 1, journeys: [checkout]}
  - {name: api-gateway,     role: "proxy router", tier: 1, journeys: [checkout]}
  - {name: order-service,   role: "products/cart/orders", tier: 1, journeys: [checkout]}
  - {name: payment-service, role: "charges; payment_use_new_validator flag", tier: 1, journeys: [checkout], git_version: v2.4.1}
  - {name: user-service,    role: "user lookup + auth", tier: 2, journeys: []}
edges:
  - {caller: webapp,        callee: api-gateway}
  - {caller: api-gateway,   callee: user-service}
  - {caller: api-gateway,   callee: order-service}
  - {caller: api-gateway,   callee: payment-service}
  - {caller: order-service, callee: user-service}
  - {caller: order-service, callee: payment-service}
```

### 2.2 `SignalContract`（s3）

```python
class SLI(BaseModel):
    kind: str                       # latency | error | throughput | saturation
    promql: str                     # 該 SLI 的權威 PromQL（含正確 aggregation）
    objective: str = ""             # 如 "p99 < 200ms" / "error_rate < 1%"
    unit: str = ""                  # ms / ratio / rps —— 直接打掉「秒進 ms bucket」類 bug

class SignalContract(BaseModel):
    service: str
    version: str
    freshness_seconds: int          # 訊號新鮮度保證；超過 → context 標「stale」
    slis: list[SLI]
    supported_decisions: list[str]  # 這份訊號支援哪些決策（rca / deploy-correlation…）
    exclusions: list[str] = []      # 何時不該信（如 "no up{} for app services"）
```

> contract 的 `promql` 是「該服務 error/latency SLI 的權威寫法」——把目前 agent 每次自己重推、常推錯的查詢，變成**宣告好、對齊過、可直接 cite** 的合約。這是把 histogram-seconds-in-ms-buckets、count-vs-rate 這類反覆踩的坑從根上關掉。

---

## 3. 注入：decision-grade context（`signals/context.py`）

現行 `capability_for_services()` 注入「有哪些 metric/span/field」（inventory）。Signal Plane 在它**之上**加一塊 **decision-grade signal context**，為 RCA 命中的 service 給：

```
## Signal context — payment-service (topology v1.0.0)
- criticality: tier-1 (revenue-critical); journey: checkout (pos 4/4, leaf)
- upstream (callers): api-gateway, order-service   ← 它壞，這些會跟著壞
- downstream (deps): (none, leaf)                   ← 不是被下游拖累
- SLI (authoritative):
    error: sum by (git_version,reason) (rate(payment_charges_total{status="declined"}[5m]))  [ratio]  obj: declined_rate < 1%
    latency: histogram_quantile(0.95, sum by (le)(rate(payment_request_duration_ms_bucket[5m]))) [ms] obj: p95<200ms
- freshness: ok (last sample 14s ago)               ← 或 ⚠ stale / ⚠ topology drift（見 s2）
```

效益鏈（直接對應 ~2/9 的失分模式）：
- **upstream/downstream 明確** → agent 不再把「order 失敗」誤判成 order 的 code bug（其實是 payment 拖累）—— s4 把健康度也填進來後閉環。
- **SLI 權威寫法** → 不再自己推 PromQL/aggregation 推錯。
- **criticality/journey** → 多服務同時告警時，agent 知道先追 tier-1 / journey 關鍵節點。
- **freshness/drift 顯式** → confident-wrong 收斂：訊號對不上時 context 直說「別太信」，不再悄悄補貼。

注入點：`run_headless`（webhook RCA）與互動 chat 都走 `context.build_signal_context(services)`，拼在現有 capability snapshot 後面。fail-open：組不出來就跳過，不阻斷。

---

## 4. 分階段實作計畫（安全序 = ROI 序；每階段全唯讀、可獨立 ship）

> 原則：**先把散文搬成 artifact（立即有 ROI、零風險）→ 再讓它對齊遙測（decision-grade 的關鍵）→ 再加 contract / health（打 confident-wrong 的主力）**。

| 階段 | 內容 | 副作用 | 測試 |
|---|---|---|---|
| **s1 topology first-class** | `topology.yaml` + `signals/topology.py`（模型 + loader + 查詢 API + 對 live service set 存在性驗證）+ `context.py` 注入 criticality/journey/上下游。`schema_catalog.md` 散文依賴圖改指向 signal context。 | 無（唯讀，純增強注入） | 單元：yaml 載入/驗證、upstream/downstream/journey/blast_path、未知 service 拒；live 對齊 smoke |
| **s2 活的對齊 + drift** | `reconcile.py`：Tempo `{}` 取 root→leaf，反推實際 caller→callee（parent span 的 `resource.service.name` → child 的），與宣告 edge diff。對不上 → context 標 `⚠ topology drift: api-gateway→X observed but not declared`。產出**第一個 DQ-SLO 資料點**（declared∩observed / observed）。 | 無（唯讀） | 單元：faked trace tree → 反推邊、diff 算 drift；k3d 實機反推 demo 依賴圖 |
| **s3 signal contract** | `contracts.yaml` + 模型；`context.py` 注入權威 SLI 寫法 + objective + unit + freshness。先做 payment（有真 incident）+ 補齊 5 服務的 error/latency SLI。 | 無（唯讀） | 單元：contract 載入/驗證、SLI promql 形狀、freshness 判定（stale 標記） |
| **s4 dependency-health / blame propagation** | `health.py`：RCA 時對命中 service 的**上下游各跑一次 SLI**（唯讀、不計 agent budget，比照 runbook diagnostics），把「上游 healthy / 下游 payment error-rate 12%」填進 context。agent 據此把根因歸到正確節點。 | 無（唯讀） | 單元：health 評估 + 注入；k3d：payment incident 下 order 的 context 顯示「下游 payment unhealthy」 |
| **s5（接續）DQ-SLO → governance ✅ 已做** | 把 s2 的 drift / s3 的 freshness 匯成 DQ-SLO 量測，餵 `governance.decide()`：訊號非 decision-grade（drift 高 / stale）時收緊自主權。補出入報告 §2.3 五大 SLO 的第二個。`signals/dq.py` + `tests/test_dq.py`（6 tests pass）。 | 無 | 單元：DQ 低 → 自主收緊 |

> s1–s4 全唯讀、無副作用，可安心連續合併；任一階段都能讓 RCA 立刻吃到更好的 context。step7 後半（真 mutate）刻意排在 Signal 穩固之後。

---

## 5. 與 ARE 對齊小結

| ARE 要求（出處） | 本設計如何補上 |
|---|---|
| Signal Plane = decision-grade telemetry（ch2 / ch4.3） | s1–s3：topology + criticality + SLI contract 升為第一級 artifact |
| topology graph 當 first-class、活的、對齊遙測（ch2.4 / ch4.3） | s1 宣告 + s2 從 Tempo 反推對齊 + drift 標記 |
| 語意標註長在訊號裡（criticality/journey/percentile/dependency health，ch2.2） | s1 criticality/journey + s3 percentile/unit + s4 dependency health |
| signal contract：versioned / freshness / 支援決策 / exclusion（ch2.3） | s3 `SignalContract` |
| ownership/criticality 當 schema 而非社群知識（ch4.3） | s1 `ServiceNode.tier` / `owner` / `journeys` |
| DQ-SLO（五旗艦之一，ch3.6） | s2 drift + s3 freshness → s5 匯成 DQ-SLO 餵 governance |
| confident-wrong 是最危險失敗（ch3.6 / ch5.9） | s4 blame propagation + freshness/drift 顯式 → 從根上收斂 |

> 一句話：把目前靠 LLM「讀懂 `schema_catalog.md` 散文」的 Signal 補貼，換成**宣告 → 對齊遙測 → 帶 drift/freshness 標記**的 decision-grade artifact，餵進不動的唯讀推論核心；RCA 的 confident-wrong 從地基收斂，後面 step7 的 Act 才站得穩。

---

## 6. Backlog / 已知缺口（k3d live 驗收 2026-06-19 揪出）

s1–s4 + A/B + s4.1 在 live k3d incident 下驗收：payment 根因 RCA、deploy
correlation、v2.4.1↔v2.5.0 code diff 全正確（confident-wrong 被 A/B 扳回），
order 被拖累的過度宣稱被 s4.1 收掉。仍掛三項：

- **s4.1 — blame-confirm（Q2 過度宣稱）✅ 已做（commit 2ec0dda，live 驗證）**：
  self 健康 + 下游不健康時，verdict 改口「下游不健康但本服務 SLI 健康，先驗本服務是否
  真的吃到該依賴的失敗再斷言症狀」。live 驗證 agent 確實去查了 `cancelled{reason=payment}`
  才下結論、不再過度宣稱。**剩進階 s4.2（下）。**
- **s4.2 — edge-attributed impact metric ✅ 已做（commit 102c8e3）**：topology edge
  宣告 `attribution` PromQL（caller 歸因 callee 失敗的指標，如 order 的
  `orders_total{reason=~"payment|payment_upstream"}`），s4 量 current vs baseline-offset
  的 delta：rising→確認 materially impacted（genuine symptom）/ flat→只是 topologically
  adjacent、未受實質影響（不報為症狀）/ 無 attribution→退回 s4.1 措辭。解掉 order「把
  baseline 取消當 incident 影響」的精度點。**剩 live 重驗（需重新觸發 incident）。**
- **log signal contract（Loki 查詢生成 bug）✅ 已做（commit 6d1b1b7）**：`SignalContract.logs`
  （`LogSignal`）宣告權威 stream selector（`{service_name=...}`）+ 真實 failure `event=`
  值（payment.declined/gateway_error、order.cancelled、user.auth_failed、http.request_failed，
  對 schema_catalog BizEvent enum 驗證）+ 代表性 error_query。context 注入「Logs (authoritative)」
  區塊，明確指示用此 selector/events、**不要** `{service=...}` 或捏造 `event="error"`。
  **剩 live 重驗**（看 agent 是否改用正確 LogQL）。
- **contract ↔ Weaver registry 對齊（schema 單一真相來源）◾ schema 半邊已做（commit c6f601e）**：
  `app/signals/weaver.py` 從 registry note 抽 Prom 名 + `validate_against_weaver()` dev/CI 守門，
  確認 contract SLI 只引用 registry 宣告的 metric（6/6 對齊）。**剩 ownership 重構（下方）。**
  原始說明：`contracts.yaml` 曾**硬寫** metric 名（`payment_charges_total` 等），踩到「hardcoded demo schema」風險。
  repo 已有 Weaver semconv registry（`demo-services/weaver/registry/model/metrics.yaml`
  宣告 `metric.app.payment.charges.count`，註解 `Current code metric: payment_charges_total`）。
  ARE 的 Signal Plane 分兩層：**Weaver registry = schema/semantic 半邊**（名字/語意/型別/
  versioned/`live-check` 驗證）；**signal-contract = 決策/SLO 半邊**（哪個 metric 是 SLI、
  objective、topology、criticality）。改進方向：signal-contract 的 SLI metric 名**衍生/
  驗證自 Weaver registry**，而非各寫一份；需處理 OTel 點號名 `app.payment.charges.count`
  → Prometheus `payment_charges_total`（`.`→`_` + `_total`）的 mapping。模式同構：Weaver
  `live-check`（遙測 vs schema）≈ s2 reconcile（trace vs 宣告 topology）。

  **ownership 重構（同項，更根本）✅ 已做（commit 見下）**：每服務在
  `demo-services/services/<svc>/signal.yaml` **擁有自己的宣告**（tier/journey 成員/slis/
  logs/exclusions/`depends_on`+attribution）。`app/signals/compile.py` 仿 Weaver 模式
  （去中心化貢獻 → 編譯產物）把 fragments 聚合成 agent 出貨的 `topology.yaml`/
  `contracts.yaml`（標 GENERATED、DO NOT EDIT）。dev/CI 編譯（agent image 無 demo-services），
  runtime loader 不變。**跨服務 topology = 各服務出邊的聯集（caller 擁有）**；**journey 鏈從邊
  topo-sort 推導**（不再中央宣告）。回歸測試 `test_compile` pin fragments↔生成檔同步。
  剩可選：把 compile 接進 CI、journey 成員/順序的更嚴格 drift 告警（與 s2 reconcile 合流）。

- **polyrepo 演進 — manifest-driven 聚合（fragment 跨 repo 時）◻ 未做**：目前
  `compile.py:_fragments_dir()` 用 `glob("*/signal.yaml")` **發現** fragment，只在 monorepo
  成立（檔案同樹）。Weaver 第四/五篇明示這只是「單 repo 內部合約」階段；跨 repo 後 Weaver
  **不靠 discovery，改用 manifest 宣告依賴**（`manifest.yaml` 的 `dependencies`/`import`/
  `extends`，pin 版本）→ `weaver registry package` 把依賴圖 resolve 壓平成自包含 artifact →
  消費者只拿這份、看不到 N 個 repo。同構搬到 Signal Plane，解掉「agent 很難知道有多少 service
  repo」——**它本來就不該知道，discovery 由 pin 版本的依賴 manifest 取代**。三步：
  1. **加 `signal-manifest.yaml`**（平台團隊 own，CODEOWNERS 把關）列出納入的服務 + 各自 pin 的
     版本/URI；`compile.py` 的 `_fragments_dir()` glob 換成「讀 manifest → 拉齊 pinned fragment」
     的可插拔 collector（保留 monorepo glob 當預設來源）。
  2. **各服務 CI 把 `signal.yaml` 發布成 versioned artifact**（綁 git tag、帶 `git_version`/
     `owner`，對映 #4 publish 的 provenance「金線索」）；中央聚合 job 照 manifest 拉齊 → 跑 compile
     → 一份 `topology.yaml`/`contracts.yaml`（agent image 仍只帶 compiled aggregates，runtime 不變）。
  3. **compile 加 fail-fast gate**：manifest 列了卻拉不到、或 edge 的 `callee` 不在 manifest 內
     （懸空邊）→ 直接紅，不靜默（現 `_topo_sort` 會把 leftover 補進 order，monorepo 下無妨，
     polyrepo 下會變「漏拉一片、圖悄悄殘缺」的隱患）。對映 weaver resolve 對缺 ref 報錯。
  取捨：`signal.yaml` 內容（SLI/LogQL/拓樸歸因）**不是 semconv attribute schema**，不能直接餵
  `weaver registry package`。選 (a) **只借模式**：自寫 manifest + collector + 壓平（即上三步，
  推薦）；或 (b) **真用 weaver**：把 signal 建模成 registry 擴充 group 靠 weaver 解析依賴，省 collector
  但要把健康訊號硬塞進 attribute schema 形狀，彆扭。建議 (a)——壓平語意（union of caller-owned
  edges → topo-sort 推導 journey）weaver 不懂，留在自家 `compile.py`。
