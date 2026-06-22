# aiops-agent — Step 7 後半：執行平面設計（Execution Plane）

對照基準：[`agent reliability engineerin.md`](./agent%20reliability%20engineerin.md)（ARE）、[`aiops-agent-ARE-gap-analysis.md`](./aiops-agent-ARE-gap-analysis.md) §4.2 step 7
銜接現況：`service/app/actions.py` / `governance.py` / `calibration.py` / `runbook.py` / `webhook.py` / `main.py`
撰寫日期：2026-06-17

---

## 0. 範圍

前半（已完成、propose-only）給了我們三樣東西，後半要把它們接成一條**會真的改變系統狀態、但永遠安全**的迴路：

- `actions.py` — typed registry，`registry.execute()` 已是**唯一可能執行**的入口，且已雙閘（`actions_enabled` + `impl` 必須存在）。目前所有 `impl=None`，所以一律 refuse。
- `governance.py` — `decide()` 產出 `AUTO / PROPOSE / ESCALATE`，已讀 CE 收緊自主權。
- `calibration.py` — CE harness，但 `correct` 只能**離線人工**標。

後半要補的四塊（gap-analysis §4.2 step 7「⏳ 後半」）：

1. **實際執行** remediation（把 `impl` 接上，但前面再加三道安全層）
2. **dry-run / blast-radius**（執行前算清楚會動到什麼、範圍多大）
3. **circuit breaker**（自動化失控 / rollback 抖動的總開關）
4. **audit log**（每一步生命週期的不可變紀錄）
5. **Learn 閉環**（執行結果 → 自動回寫 CE → 校準未來信心）

### 設計鐵律（延續 gap-analysis §4.3）

> 全唯讀推論核心**不動**。執行平面是獨立、可關閉、帶回滾合約的外掛。
> 寫入用**獨立、權限更窄的憑證**（一個只能 patch/scale/rollout、**不能 delete** 的 write SA），與現行 read-only SA 分離。
> **三道獨立閘全過才執行**：kill switch（`actions_enabled`）× 治理裁決（AUTO 或人類核准的 PROPOSE）× 執行前風險閘（breaker 未跳 + blast-radius 在策略內 + 前提仍成立）。
> **fail-closed 無處不在**：缺憑證、前提過期、breaker 跳、無回滾合約、未知動作 → 一律 refuse。

---

## 1. 元件總覽

```
          (現有，唯讀推論核心 — 不動)
  run_headless ──► Findings(confidence) ──► governance.decide() ──► [Decision: AUTO/PROPOSE/ESCALATE]
                                                                          │
   ┌──────────────────────────────────────────────────────────────────┘
   ▼  (新增：執行平面，獨立模組)
  ActionRequest（生命週期狀態機） ──► 人類核准 (PROPOSE) 或 自主 (AUTO+kill-switch)
   │
   ▼  executor.run()  ── 每一步寫 audit log ──
   1. 前提重驗 (re-run read-only diagnostics; TOCTOU 防護)
   2. dry-run + blast-radius 閘
   3. circuit breaker + idempotency 閘
   4. registry.execute()  ← 真正的 impl（write SA，bounded timeout）
   5. 結果驗證 (re-run diagnostics 確認有效)
   6. 失敗 → 自動 rollback（reversible 才有資格走到這）/ 否則 escalate
   7. outcome → calibration.label_run(source="remediation-verified") ← Learn 閉環
```

新增檔案（每個都可獨立 review、獨立 disable）：

| 檔案 | 角色 |
|---|---|
| `app/store.py` | 持久且原子的 store（SQLite on PVC；§10）。calibration / investigations / action_requests / audit 共用，取代 ephemeral JSONL |
| `app/execution.py` | `ActionExecutor`：把 1–7 串起來的協調器；唯一呼叫 `registry.execute()` 的地方 |
| `app/action_requests.py` | `ActionRequest` 生命週期狀態機 + store（JSONL，與 calibration/investigations 同模式） |
| `app/audit.py` | append-only 結構化 audit log（含 actor 身分），每個狀態轉換一筆 |
| `app/breaker.py` | circuit breaker 狀態（global / per-action / per-target 計數與失敗窗） |
| `app/tools/k8s_write.py` | **唯一**的 mutating client；seed `k8s.rollout_undo` / `k8s.scale` 的 `impl` + `dry_run` + `rollback` |
| `runbooks/*.yaml` | remediation 步驟加 `verify`（成功判準）與 `rollback`（逆操作）欄位 |

---

## 2. 資料模型

### 2.1 `ActionRequest`（生命週期狀態機）

每個被 governance 判為 AUTO/PROPOSE 的 remediation 動作，落地成一筆 `ActionRequest`。狀態機：

```
proposed ──(governance=AUTO + actions_enabled)──► approved ──► executing ──► succeeded
   │                                                  ▲            │
   │                                                  │            ├─► failed ──► rolling_back ──► rolled_back
   ├──(governance=PROPOSE)── 等待人類 ─(approve)──────┘            │                              └─► rollback_failed(→escalate)
   │                         └────────(reject)──► rejected         └─► verify_failed ──► rolling_back ─► …
   └──(preconditions 過期 / TTL 到)──► expired
```

```python
class ActionRequest(BaseModel):
    request_id: str          # uuid
    fp: str                  # 來源 investigation fingerprint（接得回 RCA / CE record）
    action: str              # registry 名稱，如 k8s.rollout_undo
    args: dict               # 已 substitute 的具體參數（deployment/namespace…）
    autonomy: str            # 來自 Decision：auto/propose/escalate
    status: str              # 上面狀態機
    reversible: bool
    rollback: dict | None    # 逆操作合約（action + args），無則不得執行
    blast_radius: dict | None  # dry-run 算出來的範圍
    created_ts: str
    expires_ts: str          # 前提會過期，核准也有 TTL（預設 15 min）
    actor: str | None        # 誰核准的（UI 帶來的 user，或 "system" for AUTO）
    outcome: str = ""        # verify 結論
```

**為什麼要 expires_ts**：proposal 算出來的前提（「payment 還在 v2.5.0」）在人類按按鈕時可能已不成立（TOCTOU）。核准有 TTL，且執行前 step 1 一定**重驗**前提，雙保險。

### 2.2 Runbook remediation 擴充 `verify` + `rollback`

`runbook.py` 的 `Step` 已有 `reversible` / `requires_approval`。後半加兩個：

```yaml
remediation:
  - desc: Roll back payment-service to the previous version
    action: k8s.rollout_undo
    args: {deployment: payment-service, namespace: demo}
    reversible: true
    requires_approval: true
    verify:                       # 執行後重驗，判定是否真的修好
      action: query_prometheus
      args: {expr: 'sum(rate(payment_charges_total{status="declined"}[2m]))', queryType: instant}
      check: {max_value: 0.01}    # decline rate 掉下來才算成功
    rollback:                     # 逆操作合約；沒有它就不准執行
      action: k8s.rollout_undo    # rollout undo 的逆操作還是 rollout undo（回到剛剛那版）
      args: {deployment: payment-service, namespace: demo}
```

無 `rollback` 的 remediation → executor **拒絕執行**（fail-closed）。`verify` 重用 Tier 1 的 `_evaluate_check` 機制（擴 `max_value` 判準）。

---

## 3. 執行管線（`ActionExecutor.run()`）逐步

每一步進入/離開都寫 audit log（§5）。任一步 fail → 停在當步、寫 audit、狀態轉 failed/expired/escalate，**絕不**往下做。

1. **前提重驗（TOCTOU 防護）**
   重跑來源 runbook 的 read-only `diagnostics`（`runbook.run_diagnostics`，已存在、結構性唯讀）。任一 precondition 由 pass→fail，或 `ActionRequest` 已 `expired` → abort。確保「決策當下成立的世界」現在還成立。

2. **dry-run + blast-radius 閘**
   呼叫該 action 的 `dry_run(args)`（read-only / `--dry-run=server`）算出：目標物件、`current_rev → target_rev`、受影響 pod 數、是否 singleton、是否跨 namespace、是否在 protected set（如 `kube-system`、無 rollback 的物件）。
   策略（config）：`max_blast_pods`、`namespace_allowlist`（預設只 `demo`）、`deny_singletons`。超標 → abort（fail-closed）。blast_radius 寫回 `ActionRequest` 給 UI 顯示。

3. **circuit breaker + idempotency 閘**
   - **idempotency key** = `(action, target, fp)`。同一事件同一目標已執行過 → 短路（不重複 rollback）。防 alert storm 把同個 deployment rollback N 次。
   - **breaker**（`breaker.py`）：
     - global：滑動窗內執行次數上限（`breaker_max_actions_per_window`）。
     - per-action / per-target：連續失敗 ≥ `breaker_fail_threshold` → 跳閘（open），該動作/目標進冷卻。
     - **跳閘即全面拒絕自主執行**，只能人工 reset（`POST /actions/breaker/reset`）。
     防 rollback→verify_fail→rollback 的抖動迴圈，與自動化失控。

4. **執行**
   `registry.execute(action, args)` → 命中 `tools/k8s_write.py` 的 `impl`，bounded timeout（`action_timeout_seconds`）。**這是唯一真正 mutate 的地方**，仍受 `actions_enabled` kill switch（registry 內既有檢查）。write SA 權限：patch/scale/rollout deployments，**no delete**（deploy.sh 驗證讀寫分離，比照現行 read SA）。

5. **結果驗證（closed-loop）**
   等一個 settle 窗（`verify_delay_seconds`）後跑 remediation 的 `verify` step，要求**持續改善**才算 pass（降噪，§6.2）。pass → `succeeded`；fail → 進 step 6。這把 ARE 的「採取旨在恢復穩定的行動」真正閉環：不只是做了，而是**確認有效**。
   **CE 回寫的分界就在這裡**（§6.2 約束 2）：唯有「`execute` 乾淨成功、但 `verify` 仍 fail」才是「RCA 大概率錯」的證據，可回寫 `remediation-failed`；反之 step 4 的 `execute` 例外只是動作沒跑成，**不碰 CE**，只進 breaker。

6. **失敗 → 自動 rollback**
   `verify_failed` 或 `execute` 例外 → 因為只有 reversible + 有 `rollback` 合約的動作能走到這，執行 `rollback`。rollback 成功 → `rolled_back` + escalate 給人類（「我試了、沒用、已還原，換你」）。rollback 也失敗 → `rollback_failed` → 最高優先級 escalate（呼叫人 + 跳 breaker）。
   區分兩種來源寫進 audit：`execute` 失敗 → breaker 計數，CE 不動；`verify` 失敗 → breaker 計數 +（若 `learn_remediation_into_ce`）`remediation-failed` 自產 stream。

7. **Learn 閉環**（§6）
   把 outcome 餵回 CE。

---

## 4. 核准路徑（人類在迴路）

完全沿用 `/alerts/provision` 的 human-in-the-loop pattern（`main.py:113`）：

- **新 endpoint**（fail-closed，比照 alerts）：
  - `GET /actions/requests` — 列 pending `ActionRequest`（plugin Investigations 頁顯示）。
  - `POST /actions/requests/{id}/approve` — 人類按鈕（帶 actor），轉 approved → 觸發 executor。
  - `POST /actions/requests/{id}/reject` — 轉 rejected，寫 audit。
  - `POST /actions/{id}/dry-run` — 回 blast-radius preview（不執行；類似 `/alerts/preview`）。
  - `POST /actions/breaker/reset` — 人工重置 breaker。
- **plugin**：在 Investigations 頁的 governance decision 旁，PROPOSE 顯示「Dry-run」＋「Approve & execute」＋「Reject」按鈕（沿用 `AlertProposalCard.tsx` 卡片模式）。ESCALATE 無按鈕。
- **AUTO**：`actions_enabled=True` 且 governance=AUTO 時，`webhook._investigate_and_sink` 直接建 approved 的 `ActionRequest` 交 executor（actor="system"），但仍走完整 1–7 管線。

三種觸發都收斂到同一條 `ActionExecutor.run()`，差別只在誰把狀態推到 `approved`。

---

## 5. Audit log（`audit.py`）

**insert-only** 表（持久化層見 §10——不是 ephemeral JSONL）。**每個生命週期轉換一筆**，不可變：

```python
class AuditEntry(BaseModel):
    ts: str
    request_id: str
    fp: str
    phase: str        # proposed/approved/precond_revalidate/dry_run/breaker/execute/verify/rollback/...
    verdict: str      # ok / abort / refuse / success / fail
    actor: str        # 人類 user 或 "system"
    detail: dict      # blast_radius / breaker 狀態 / diagnostics 結果 / 例外
```

audit 是**安全與事後可究責的核心**：誰、何時、核准了什麼、前提是什麼、實際做了什麼、結果如何、有沒有還原。CLI：`python -m app.audit tail <fp>`。

---

## 6. Learn 閉環（執行結果 → CE → 自主權）

這是把 DRAL 的 **Learn** 補上的關鍵。機制上重用 `calibration.label_run` 省下數學，**但省的是數學、不是統計效度**——remediation outcome 當 label 會破壞 ECE 背後的抽樣假設，效度要靠下面三個約束買回來。

### 6.1 為什麼不能把 verify outcome 直接當 RCA correctness

CE 的意義是「stated confidence 是否追得上 **RCA 因果正確性**」。但 remediation `verify`（decline rate 掉了）量的是 **修好症狀**，是 RCA 正確的 proxy，兩個方向都會壞：

- **偽陽性**：rollback 順帶重啟 pod、清掉一個與版本無關的暫態 → verify pass，卻把錯誤假說標成對 → 強化對錯誤假設的信心。
- **偽陰性**：verify 失敗可能是 action 沒跑成功、第二個併發成因、verify 窗太短、metric noise → 把對的 RCA 標成錯。

而且 remediation 只在**通過 governance 的子集**（高信心 AUTO / 人類核准的 PROPOSE）上執行 → 自動 label 系統性偏向高信心、人類認可的 run；低信心 / 無 runbook / novel 事件永遠拿不到 label。**autonomy 餵養它自己用來解鎖 autonomy 的指標**，沒有外生、無偏的制衡；且 CE 在 playbook 覆蓋區量得漂亮、在 novel 事件（過度自信最危險處）全盲。

### 6.2 三個約束（把效度買回來）

1. **兩條 label 流分開，gate 數字不混入自產 label。**
   `remediation-verified` / `remediation-failed` 自成一條 CE stream（`source` 區分）。**gate AUTO 的 headline overconfidence 仍以人工 / grader label 為主。** 新增 `governance_min_human_labeled_runs`——`governance._calibration_verdict` 要求「人工 / grader label 數」達標才認 calibration proven-good；autonomy **不能**只靠自產 label 解鎖。自產 stream 另算，只供觀測與 per-action 成功率，**不直接**鬆動 AUTO。

2. **執行失敗絕不回寫 RCA correctness。**
   executor 已區分 `execute`（動作本身失敗）vs `verify`（動作成功、症狀仍在）。**只有後者**是「RCA 大概率錯」的證據，才 `label_run(correct=False, source="remediation-failed")`；`execute` 失敗只進 breaker（§3 step 4/6），**完全不碰 CE**。直接修掉偽陰性的一半。

3. **verify 當較弱訊號，且要持續。**
   不用單點 instant check；要求 settle 窗內**持續改善**才算 pass（降噪）。remediation outcome 預設主要餵「fix efficacy」指標（per-action 成功率 + breaker），是否升格成 CE correctness label 由 `learn_remediation_into_ce`（預設 False）控制——預設只有人工 / grader label 進 headline CE。

> 人工標（`source="ui"`）與自動標的優先序：人工為準。`label_run` 取最新一筆，UI 人工標在後即覆蓋；audit 兩者都留痕。

### 6.3 複利仍然成立（只是更慢、更可信）

每筆**人工確認**的 outcome 仍推著 `governance_min_human_labeled_runs` 達標 → calibration proven-good → 同類故障升 AUTO；remediation 失敗推高 overconfidence → AUTO 降回 PROPOSE。差別是：解鎖速度由**外生 label** 決定；自產 stream 只加速「我們的修復有沒有效」這個獨立判斷，不污染「我們的診斷準不準」。

---

## 7. 新增 config（全部 fail-closed 預設）

```python
# --- Execution plane (step 7 後半) — 預設全部關 / 保守 ---
actions_enabled: bool = False            # 既有總開關；後半完成、累積標記前不開
execution_namespace_allowlist: list[str] = ["demo"]
max_blast_pods: int = 5                  # 超過受影響 pod 數 → abort
deny_singletons: bool = True             # 單副本服務 rollback 風險高 → 預設拒
action_timeout_seconds: int = 30
verify_delay_seconds: int = 60           # 執行到驗證的 settle 窗
approval_ttl_seconds: int = 900          # 核准 15 min 過期，逼重驗
breaker_max_actions_per_window: int = 3
breaker_window_seconds: int = 3600
breaker_fail_threshold: int = 2          # 連 2 次失敗跳閘
require_rollback_contract: bool = True   # 無 rollback 的動作不得執行

# --- Learn 閉環效度約束（§6.2）---
# gate AUTO 只認外生 label：人工 / grader 標的 run 數須達標，自產 label 不算。
governance_min_human_labeled_runs: int = 20
# 是否讓 remediation outcome 升格成 headline CE 的 correctness label。
# 預設 False：remediation 只餵 fix-efficacy 指標 + breaker，不污染診斷校準。
learn_remediation_into_ce: bool = False
```

---

## 8. 分階段實作計畫（安全序 = ROI 序；每階段可獨立 ship，執行到最後才開）

> 原則：**mutate 的能力最後才接、且只在 demo namespace、且先只走人類核准路徑**。前面每一階段都是唯讀或無副作用，可安心合併。

| 階段 | 內容 | 副作用？ | `actions_enabled` |
|---|---|---|---|
| **7b-0 持久化層（前置）✅** | 已完成。`app/store.py`（SQLite，WAL + busy_timeout + 寫鎖）；calibration / investigations 改走它（pydantic 模型 + 公開 API + CLI 介面不變，只換底層，`label_run` 變原子 UPDATE）；k8s 加 PVC `aiops-agent-data` + `/data` mount + `STORE_PATH` + `strategy: Recreate`；lifespan 啟動時 `store.init()` 跑 schema + 一次性 JSONL 遷移（idempotent）。測試：`test_store.py`（原子 label / 重連存活 / 遷移 idempotent）+ 既有 calibration/investigations 測試改走 unified db，全 79 passed。 | 無 | False |
| **7b-1 生命週期 + audit 骨架 ✅（後端）** | 已完成後端。`app/action_requests.py`（`Status` 狀態機 + `ActionRequest`；transition 走 store 原子 CAS → 雙重核准/核准撞 AUTO 不可能雙跑）；`app/audit.py`（insert-only 稽核，每個轉換一筆）；`app/execution.py`（executor **已接線但被 kill switch 退回** → 誠實 no-op，非假 stub，7b-4 只需接 impl + 開關）；store 加 `action_requests` / `audit` 兩表 + 原子 helper；endpoints `GET/POST /actions/requests…` + `/actions/audit`；`run_headless` 把 AUTO/PROPOSE decision 落成 ActionRequest（帶 substitute 後的 args + rollback 合約）。測試：`test_action_requests`(7) / `test_execution`(4) / `test_audit`(2)，全 92 passed；E2E：create→approve→executor REFUSED→完整 audit trail。**剩**：plugin 卡片按鈕（前端）。 | 無 | False |
| **7b-2 dry-run + blast-radius ✅** | 已完成。`app/blast_radius.py`：`BlastRadius` 模型 + 唯讀 `dry_run_rollout_undo`/`dry_run_scale`（重用 `tools/k8s` 的 read SA，算 current→target revision、affected pods、singleton、namespace）+ `evaluate_policy`（fail-closed：dry-run 讀不到 / protected ns / 不在 allowlist / 跨 ns / singleton / 超 `max_blast_pods` / 無前一版 → 拒）。`actions.py` ActionSpec 加唯讀 `dry_run` 欄位並接上兩個 action。executor 加 step 1 前提重驗（重用 `run_diagnostics`，只在 `fail` 才 abort）+ step 2 dry-run 閘，失敗 → 新狀態 `ABORTED`；blast_radius 存回 request 給 UI。store 加 `runbook_id`/`params` 欄位 + `ar_update`。config 加 `execution_namespace_allowlist`/`max_blast_pods`/`deny_singletons`/`protected_namespaces`。測試：`test_blast_radius`(12，policy + faked-client dry-run) + executor 閘測試；全 108 passed。**實機驗證**：對 live k3d 跑，dry-run 讀到 payment-service 真實狀態（rev 8→7、1 replica）並被 singleton 策略正確 abort。 | 無（全唯讀） | False |
| **7b-3 circuit breaker + idempotency ✅** | 已完成。`app/breaker.py`：global 滑動窗 rate-limit + per-(action,target) 連續失敗 ≥ `breaker_fail_threshold` → 跳閘 open，**open 後只能人工 reset**（success 不自動關）。store 加 `executions` ledger（算窗計數 / 連續失敗）+ `breaker` 狀態表（durable，重啟存活）。idempotency：`idem_key = action|target|fp` 存在 request 上，`ar_find_ran` 查同 key 且已 ran/running 的 request → 短路（防 alert storm 對同目標重複動作）。executor step 3 接上 idempotency + breaker 閘（任一拒 → ABORTED）；`record_outcome` 接在 execute 的 success/fail 分支（refuse 不計，nothing ran）。endpoints `GET /actions/breaker` + `POST /actions/breaker/reset`。config 加 `breaker_*`。測試：`test_breaker`(8) + executor idempotency/breaker-open(2)；全 117 passed。 | 無 | False |
| **7b-4 接真實 impl（僅人類核准路徑）✅ 已做** | `tools/k8s_write.py`：write SA bound AppsV1Api（separate from read SA）；`impl_rollout_undo` 找 previous RS patch template；`impl_scale` patch replicas。`actions.py` 接 impl。`execution.py` 補 step 5 `_verify_outcome`（settle window + query_prometheus + max_value check）+ step 6 `_auto_rollback`（讀 rollback contract impl）；狀態機補 VERIFY_FAILED→ROLLING_BACK→ROLLED_BACK/ROLLBACK_FAILED。`runbook.py` 加 `max_value`/`verify` 欄位。`payment-bad-deploy.yaml` 加 verify spec（decline rate < 0.01）。k8s manifest 加 write SA + Role（patch/update，no delete）+ token Secret + projected volume；manifest env `ACTIONS_ENABLED=true`（`requires_approval` 仍保證人在迴路）。deploy.sh 驗證 write RBAC。180 tests pass。 | **有**（人類核准） | True（demo） |
| **7b-5 Learn 閉環** | verify outcome → `label_run(source="remediation-verified/-failed")`；per-action 成功率指標。 | 無新增 | True（demo） |
| **7b-6（最後、可選）真 AUTO** | 累積足夠 verified labeled runs、calibration proven-good 後，才允許 reversible 動作**無人**自主執行——仍受 breaker + blast-radius + auto-rollback。ARE「earned autonomy」在此真正開啟。 | **有**（自主） | True |

每階段測試延續現有風格（pure 邏輯 pin 單元測試 + k3d 實機 smoke）：狀態機轉換、blast-radius 策略邊界、breaker 跳/冷卻/reset、precondition 過期、rollback 路徑、Learn 自動標的覆蓋序。

---

## 9. 與 ARE 對齊小結

| ARE 維度 | 後半如何補上 |
|---|---|
| DRAL：Act | executor 真正執行 reversible remediation（7b-4） |
| DRAL：Learn | verify outcome 自動回寫 CE（7b-5），複利收斂 |
| 平面：Execution | Action Contract 完整（dry-run/blast-radius/verify/rollback/timeout/audit） |
| 平面：Governance（運行時） | 三道閘 + breaker；CE 升高自動收緊 AUTO→PROPOSE |
| 紀律：CE 驅動自主權 | 7b-6 才開 AUTO，且須 calibration proven-good；失敗自動降級 |
| 工作流：Incident Response | 補上「遏制性操作 + 升級」的另一半 |

> 一句話：後半把前半的 propose-only 裁決，包進一條「**重驗前提 → 算清範圍 → breaker 守門 → 有界執行 → 驗證有效 → 沒效就還原 → 結果回饋校準**」的合約化管線；mutate 能力獨立、可關、帶回滾，唯讀推論核心一行不動。

---

## 10. 持久化層（7b-0；後半的硬前置）

### 10.1 現況（repo 實際樣子，非設計）

- `calibration_log_path="calibration.jsonl"` / `investigations_log_path="investigations.jsonl"` 是相對路徑 → 容器 `WORKDIR /app/` 內。
- Deployment `replicas: 1`，**無 PVC / volume mount**。checkpointer 是 `MemorySaver()`（in-memory），`webhook._last_run` dedup 也 in-memory。
- ⇒ **CE 與 investigations 寫在 ephemeral pod 磁碟，每次重啟清空。** calibration.py 註解的「move to Postgres」目前**尚未存在**。

### 10.2 為什麼這對後半是阻斷級

- Learn 閉環的全部價值是**跨事件長期累積 labeled run** 去掙得 autonomy（`governance_min_human_labeled_runs`）。重啟即清 → 永遠累積不起來。
- 諷刺點：step 7 executor 自己會 rollout / 重啟（含 `deploy.sh` 滾動重啟 agent），**正在累積 autonomy 證據的系統會被它自己的動作清空證據**。
- JSONL 本身在後半才踩到的兩個問題：
  - `label_run` 整檔 rewrite（`calibration._write_all`）→ 非原子；中途 crash 截斷；併發背景 task race。
  - `action_requests` 是**併發狀態機**（webhook 背景 task vs 人類 approve endpoint）→ JSONL 無 atomic compare-and-set → **重複執行**風險。

### 10.3 各 store 的需求

| store | 需求 | JSONL |
|---|---|---|
| audit | append-only、不可變、可究責、永久 | insert-only 還行，但 ephemeral 不行 |
| calibration（CE） | 持久、低量、**read-modify-write**（label） | RMW 非原子 → 不安全 |
| action_requests | 持久、**併發狀態轉換**、UI 查詢 | 狀態機無原子轉換 → 不安全 |
| investigations | 持久、append + list | 功能上可，但仍 ephemeral |

### 10.4 決定（兩檔，與副本數綁定）

- **demo / 現在（`replicas: 1`）→ SQLite on a PVC**，mount 在固定路徑（如 `/data`），四張表（calibration / investigations / action_requests / audit）收在一個 `app/store.py`。單副本讓 SQLite 完全夠：ACID 給原子狀態轉換與安全 `label_run`，重啟存活，**不必多跑一個服務**。audit 表 **insert-only**（可選 hash chain 做 tamper-evidence）。
- **要多副本、或要把 checkpointer 一起持久化 → Postgres**（LangGraph 有 Postgres checkpointer，saver + 四張表收同一 DB）。

> **關鍵耦合**：`replicas: 1` 正是現在 MemorySaver + in-memory dedup + 檔案 store「看起來能用」的原因，也是 SQLite 可行的前提。一旦多副本，這些**會一起壞**——儲存決策與副本數必須一起決定。

### 10.5 落地清單（7b-0）

- 新增 `app/store.py`：SQLite 連線 + schema（四表）+ 原子 helper；calibration / investigations 改走它（保留現有 pydantic 模型與 CLI 介面，只換底層）。
- k8s：加 PVC + volumeMount（`/data`），`*_log_path` / 新 `store_path` 指向 `/data`。
- 遷移：啟動時若偵測到舊 JSONL 且 DB 空，匯入一次（best-effort）。
- 測試：原子 `label_run` 併發、狀態轉換 compare-and-set、重啟後資料存活（k3d 實機 restart smoke）。
