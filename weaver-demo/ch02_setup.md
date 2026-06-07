# 第二章：環境建置與核心工具

> 本章介紹 Weaver 的三種安裝方式、所有核心指令的完整用法，以及初始化專案結構的最佳實踐。

---

## 2.1 安裝 Weaver

### 方式一：直接下載二進位檔（推薦生產環境）

```bash
# Linux x86_64
curl -Lo weaver \
  https://github.com/open-telemetry/weaver/releases/latest/download/weaver-linux-amd64
chmod +x weaver
sudo mv weaver /usr/local/bin/

# Linux ARM64
curl -Lo weaver \
  https://github.com/open-telemetry/weaver/releases/latest/download/weaver-linux-arm64
chmod +x weaver
sudo mv weaver /usr/local/bin/

# macOS x86_64 (Intel)
curl -Lo weaver \
  https://github.com/open-telemetry/weaver/releases/latest/download/weaver-macos-amd64
chmod +x weaver
sudo mv weaver /usr/local/bin/

# macOS ARM64 (Apple Silicon)
curl -Lo weaver \
  https://github.com/open-telemetry/weaver/releases/latest/download/weaver-macos-arm64
chmod +x weaver
sudo mv weaver /usr/local/bin/

# 驗證安裝
weaver --version
```

預期輸出：
```
weaver 0.13.0
```

### 方式二：Docker（推薦初學者）

Docker 方式不需要在本機安裝依賴，適合快速試用。

```bash
# 拉取最新版本
docker pull otel/weaver:latest

# 設定 alias（讓 docker 版本與本機版本的指令格式一致）
alias weaver='docker run --rm \
  -v $(pwd):/workspace \
  -w /workspace \
  otel/weaver:latest'

# 驗證
weaver --version

# 指定版本（生產建議固定版本，不用 latest）
alias weaver='docker run --rm \
  -v $(pwd):/workspace \
  -w /workspace \
  otel/weaver:0.13.0'
```

**注意**：使用 Docker alias 時，路徑必須相對於 `$(pwd)`，因為只有當前目錄被掛載到容器中。

### 方式三：GitHub Actions（CI/CD）

```yaml
# .github/workflows/weaver.yml
- name: Setup Weaver
  uses: open-telemetry/weaver-action/setup@v1
  with:
    version: 'latest'    # 或指定版本：'0.13.0'

# 驗證安裝
- run: weaver --version
```

### 從原始碼編譯（開發者用）

```bash
# 需要 Rust 工具鏈
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env

git clone https://github.com/open-telemetry/weaver.git
cd weaver
cargo build --release
sudo cp target/release/weaver /usr/local/bin/
```

---

## 2.2 核心指令速查表

| 指令 | 用途 | 常用參數 |
|------|------|---------|
| `weaver registry check` | 靜態驗證 YAML 語法與語義、執行 Policy | `--registry`, `--policy` |
| `weaver registry generate` | 根據模板生成程式碼或文件 | `--registry`, `--templates`, target, output |
| `weaver registry diff` | 比較兩個版本的 Schema 差異 | `--registry-old`, `--registry-new` |
| `weaver registry resolve` | 打包 Schema 與依賴為單一工件 | `--registry`, `--output`, `--format` |
| `weaver registry live-check` | 即時驗證 OTLP 訊號合規性 | `--registry`, `--policy`, `--otlp-grpc-port` |
| `weaver registry emit` | 根據 Schema 產生範例遙測訊號 | `--registry`, `--group`, `--otlp-endpoint` |
| `weaver registry mcp` | 啟動 MCP 伺服器供 AI 工具使用 | `--registry` |
| `weaver registry update-markdown` | 更新 Markdown 文件中的遙測規格表 | `--registry`, `--templates` |

---

## 2.3 各指令詳細說明

### `weaver registry check` — 靜態驗證

驗證 YAML 語法、引用的完整性（ref 指向的 id 存在）、必填欄位，以及自訂 Rego Policy。

```bash
# 基本驗證（只驗語法）
weaver registry check \
  --registry ./telemetry/registry

# 加上自訂 Policy 驗證
weaver registry check \
  --registry ./telemetry/registry \
  --policy ./policies

# 同時驗證多個 Registry（企業場景：OTel 官方 + 公司擴充）
weaver registry check \
  --registry ./opentelemetry-semantic-conventions/model \
  --registry ./my-company-extensions/model \
  --policy ./policies

# 輸出詳細資訊（除錯用）
weaver registry check \
  --registry ./telemetry/registry \
  --debug
```

正常輸出：
```
✔ No `after_resolution` policy violation
```

錯誤輸出範例：
```
✗ Error in group 'metric.payment.amount':
  missing required field 'unit'

✗ Policy violation in group 'metric.cart.value':
  指標 'cart.value' 不符合命名規範，必須以 ["payment.", "cart.", "auth."] 其中之一開頭
```

---

### `weaver registry generate` — 程式碼/文件生成

```bash
# 完整格式
weaver registry generate \
  --registry ./telemetry/registry \
  --templates ./templates \
  <target>          # 對應 templates/<target>/ 目錄名稱
  [output_dir]      # 輸出目錄，預設為 output/

# 生成 Go 程式碼（讀取 templates/go/ 目錄）
weaver registry generate \
  --registry ./telemetry/registry \
  --templates ./templates \
  go \
  ./generated_from_template

# 生成 Python 程式碼
weaver registry generate \
  --registry ./telemetry/registry \
  --templates ./templates \
  python \
  ./generated_from_template

# 生成 Markdown 文件
weaver registry generate \
  --registry ./telemetry/registry \
  --templates ./templates \
  docs \
  ./docs/telemetry

# 加上 --debug 查看 filter 執行結果
weaver registry generate \
  --registry ./telemetry/registry \
  --templates ./templates \
  --debug \
  go ./generated_from_template
```

正常輸出：
```
✔ Generated file "./generated_from_template/semconv_attrs.go"
✔ Generated file "./generated_from_template/semconv_metrics.go"
✔ Artifacts generated successfully
```

---

### `weaver registry resolve` — 打包 Schema

將分散的 YAML 檔案與 `ref` 依賴全部展開，打包成單一工件。適合用於：
- 發布 Schema 給其他團隊使用
- 除錯：確認 `ref` 展開後的實際結構

```bash
# 輸出 JSON（預設）
weaver registry resolve \
  --registry ./telemetry/registry \
  --format json

# 輸出 YAML
weaver registry resolve \
  --registry ./telemetry/registry \
  --format yaml \
  --output ./dist/schema-v2.0.0.yaml

# 配合 jq 查看特定 group
weaver registry resolve \
  --registry ./telemetry/registry \
  --format json | jq '.groups[] | select(.id == "span.payment.process")'
```

輸出片段（JSON）：
```json
{
  "groups": [
    {
      "id": "span.payment.process",
      "type": "span",
      "span_kind": "server",
      "brief": "處理訂單支付流程的 Span",
      "stability": "stable",
      "attributes": [
        {
          "name": "git.tag",
          "type": "string",
          "brief": "部署的 Git 版本標籤",
          "requirement_level": "required",
          "stability": "stable"
        }
      ]
    }
  ]
}
```

---

### `weaver registry diff` — 版本差異比較

```bash
# 比較本地 Schema 與特定 Git tag
weaver registry diff \
  --registry-old https://github.com/myorg/schemas/tree/v1.0.0 \
  --registry-new ./telemetry/registry

# 比較兩個本地目錄
weaver registry diff \
  --registry-old ./backup/registry-v1 \
  --registry-new ./telemetry/registry
```

輸出範例：
```
+ 新增: payment.transaction_id (required, stable)
~ 修改: payment.order_id → deprecated
  before: stability=stable, requirement_level=required
  after:  stability=stable, deprecated=true
- 警告: 不允許直接移除 required 屬性 (payment.order_id)
```

---

### `weaver registry live-check` — 動態驗證

啟動一個 OTLP 接收端，攔截程式發出的遙測訊號並即時驗證。

```bash
weaver registry live-check \
  --registry ./telemetry/registry \
  --policy ./policies \
  --input-source otlp \           # 目前只支援 otlp
  --format yaml \                 # 報告格式：yaml 或 json
  --output ./reports/weaver-report.yaml \
  --otlp-grpc-address 0.0.0.0 \  # 監聽地址
  --otlp-grpc-port 4318           # 監聽埠
```

**注意**：`--otlp-grpc-port 4318` 是 HTTP/Protobuf 埠，gRPC 標準埠是 4317。確認你的 OTLP exporter 設定與此一致。

---

### `weaver registry emit` — 發送模擬訊號

根據 Schema 自動產生範例遙測訊號並發送到 OTLP 端點，適合：
- 在應用程式開發完成前，讓 SRE 先建立 Grafana 儀表板
- 測試 OTLP pipeline 是否正常

```bash
# 發送所有訊號
weaver registry emit \
  --registry ./telemetry/registry \
  --otlp-endpoint http://localhost:4317

# 只發送特定 group
weaver registry emit \
  --registry ./telemetry/registry \
  --group metric.payment.amount \
  --otlp-endpoint http://localhost:4317

# 發送多次（壓測用）
weaver registry emit \
  --registry ./telemetry/registry \
  --count 100 \
  --otlp-endpoint http://localhost:4317
```

---

### `weaver registry mcp` — 啟動 MCP 伺服器

讓 AI 工具（Claude Code、GitHub Copilot）能夠存取你的 Schema。

```bash
weaver registry mcp \
  --registry ./telemetry/registry

# 輸出：
# ✓ MCP Server started at localhost:3000
# ✓ Registry loaded: 15 groups, 42 attributes, 8 metrics
```

---

## 2.4 全域選項

所有指令都支援以下全域選項：

| 選項 | 說明 | 預設值 |
|------|------|--------|
| `--debug` | 輸出除錯資訊（包含 jq filter 執行結果）| false |
| `--quiet` | 只輸出錯誤訊息 | false |
| `--log-format` | 日誌格式：`text` 或 `json` | `text` |
| `--future` | 啟用實驗性功能 | false |

---

## 2.5 初始化專案結構

### 推薦目錄結構

```
my-service/
├── telemetry/
│   └── registry/
│       ├── common.yaml          # 共用屬性（跨服務 ref 用）
│       ├── payment-spans.yaml   # 訂單支付 Span 定義
│       ├── payment-metrics.yaml # 訂單支付 Metric 定義
│       └── cart-metrics.yaml    # 購物車 Metric 定義
├── templates/
│   ├── go/
│   │   ├── weaver.yaml          # 模板設定（必要）
│   │   ├── semconv_attrs.go.j2  # 屬性常數模板
│   │   └── semconv_metrics.go.j2# 指標常數模板
│   ├── python/
│   │   ├── weaver.yaml
│   │   ├── semconv_attrs.py.j2
│   │   └── semconv_metrics.py.j2
│   └── docs/
│       ├── weaver.yaml
│       └── telemetry.md.j2
├── policies/
│   ├── enforce_naming.rego      # 命名規則
│   ├── enforce_git_tag.rego     # 必填屬性規則
│   └── no_breaking_changes.rego # 破壞性變更防護
└── generated_from_template/     # 自動生成（版本控制，但不手改）
    ├── semconv_attrs.go
    └── semconv_metrics.go
```

### 為什麼要將生成的程式碼納入版本控制？

許多團隊傾向把生成的程式碼加入 `.gitignore`，但這有缺點：

| 策略 | 優點 | 缺點 |
|------|------|------|
| 納入版本控制 | PR diff 可見「Schema 變更對程式碼的影響」；不依賴 CI 環境有 Weaver | 多出 commit；有時 diff 雜訊較多 |
| 加入 .gitignore | 乾淨的 repo | CI 必須每次重新生成；本地開發需要先手動執行 generate |

**推薦做法**：納入版本控制，在 CI 加入 drift detection（Schema 改了但忘記重新 generate 就會被擋下）。

---

## 2.6 常見安裝問題

### 問題：`weaver: command not found`

```bash
# 確認二進位檔在 PATH 中
which weaver
echo $PATH

# 若不在 PATH 中，加到 ~/.bashrc 或 ~/.zshrc
export PATH="$PATH:/usr/local/bin"
source ~/.bashrc
```

### 問題：Docker alias 路徑錯誤

```bash
# ❌ 錯誤：使用絕對路徑，容器內找不到
weaver registry check --registry /home/user/my-project/telemetry/registry

# ✓ 正確：使用相對路徑（相對於 $(pwd)，也就是掛載點）
weaver registry check --registry ./telemetry/registry
```

### 問題：SSL 憑證驗證失敗（企業內網環境）

```bash
# 使用 --no-verify-ssl 跳過 SSL 驗證（僅限內網環境）
docker pull otel/weaver:latest --tls-verify=false

# 或設定環境變數
export SSL_CERT_FILE=/path/to/company-ca.crt
```

---

## 延伸閱讀

- [Weaver Release Notes](https://github.com/open-telemetry/weaver/releases)
- [Weaver GitHub Actions](https://github.com/open-telemetry/weaver-action)
- [Docker Hub: otel/weaver](https://hub.docker.com/r/otel/weaver)
