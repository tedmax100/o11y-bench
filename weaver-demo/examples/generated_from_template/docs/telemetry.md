# 遙測規格文件

> 本文件由 Weaver 自動生成，請勿手動修改。
> 指令：`weaver registry generate --registry ./telemetry/registry --templates ./templates go ./generated_from_template`

---

## 📊 Metric: `metric.cart.active_sessions`

**目前活躍的購物車 Session 數量**

| 屬性 | 值 |
|------|-----|
| Metric Name | `cart.active_sessions` |
| Instrument | `updowncounter` |
| Unit | `{sessions}` |
| Stability | `stable` |

### 屬性列表

| 屬性名稱 | Go 常數 | 類型 | 必填 | 說明 |
|---------|---------|------|------|------|
| `deployment.environment` | `DEPLOYMENT_ENVIRONMENT` | `string` | ✅ 必填 | 服務部署環境 |

---
## 📊 Metric: `metric.cart.add_item.count`

**加入購物車的商品件數**

| 屬性 | 值 |
|------|-----|
| Metric Name | `cart.add_item.count` |
| Instrument | `counter` |
| Unit | `{items}` |
| Stability | `stable` |

### 屬性列表

| 屬性名稱 | Go 常數 | 類型 | 必填 | 說明 |
|---------|---------|------|------|------|
| `cart.item_id` | `CART_ITEM_ID` | `string` | ✅ 必填 | 加入購物車的商品 SKU |
| `deployment.environment` | `DEPLOYMENT_ENVIRONMENT` | `string` | ✅ 必填 | 服務部署環境 |

---
## 📊 Metric: `metric.cart.checkout.total`

**每次結帳金額分佈（TWD）**

| 屬性 | 值 |
|------|-----|
| Metric Name | `cart.checkout.total` |
| Instrument | `histogram` |
| Unit | `{TWD}` |
| Stability | `stable` |

### 屬性列表

| 屬性名稱 | Go 常數 | 類型 | 必填 | 說明 |
|---------|---------|------|------|------|
| `deployment.environment` | `DEPLOYMENT_ENVIRONMENT` | `string` | ✅ 必填 | 服務部署環境 |

---
## 📊 Metric: `metric.payment.amount`

**每筆支付的金額分佈**

| 屬性 | 值 |
|------|-----|
| Metric Name | `payment.amount` |
| Instrument | `histogram` |
| Unit | `{TWD}` |
| Stability | `stable` |

### 屬性列表

| 屬性名稱 | Go 常數 | 類型 | 必填 | 說明 |
|---------|---------|------|------|------|
| `deployment.environment` | `DEPLOYMENT_ENVIRONMENT` | `string` | ✅ 必填 | 服務部署環境 |
| `payment.provider` | `PAYMENT_PROVIDER` | `string` | ✅ 必填 | 支付服務提供商 |

---
## 📊 Metric: `metric.payment.duration`

**支付處理耗時（毫秒）**

| 屬性 | 值 |
|------|-----|
| Metric Name | `payment.duration` |
| Instrument | `histogram` |
| Unit | `ms` |
| Stability | `stable` |

### 屬性列表

| 屬性名稱 | Go 常數 | 類型 | 必填 | 說明 |
|---------|---------|------|------|------|
| `deployment.environment` | `DEPLOYMENT_ENVIRONMENT` | `string` | ✅ 必填 | 服務部署環境 |
| `payment.provider` | `PAYMENT_PROVIDER` | `string` | ✅ 必填 | 支付服務提供商 |

---
## 📊 Metric: `metric.payment.errors`

**支付失敗次數計數器**

| 屬性 | 值 |
|------|-----|
| Metric Name | `payment.errors` |
| Instrument | `counter` |
| Unit | `{errors}` |
| Stability | `stable` |

### 屬性列表

| 屬性名稱 | Go 常數 | 類型 | 必填 | 說明 |
|---------|---------|------|------|------|
| `payment.provider` | `PAYMENT_PROVIDER` | `string` | ✅ 必填 | 支付服務提供商 |

---
## 📡 Span: `span.cart.add_item`

**將商品加入購物車的 Span**

| 屬性 | 值 |
|------|-----|
| Span Kind | `server` |
| Stability | `stable` |

### 屬性列表

| 屬性名稱 | Go 常數 | 類型 | 必填 | 說明 |
|---------|---------|------|------|------|
| `cart.item_id` | `CART_ITEM_ID` | `string` | ✅ 必填 | 加入購物車的商品 SKU |
| `cart.item_price` | `CART_ITEM_PRICE` | `double` | ✅ 必填 | 商品單價（TWD） |
| `cart.item_quantity` | `CART_ITEM_QUANTITY` | `int` | ✅ 必填 | 加入的商品數量 |
| `cart.session_id` | `CART_SESSION_ID` | `string` | ✅ 必填 | 購物車 Session 識別碼 |
| `deployment.environment` | `DEPLOYMENT_ENVIRONMENT` | `string` | ✅ 必填 | 服務部署環境 |
| `git.tag` | `GIT_TAG` | `string` | ✅ 必填 | 部署的 Git 版本標籤 |

---
## 📡 Span: `span.cart.checkout`

**購物車結帳 Span**

| 屬性 | 值 |
|------|-----|
| Span Kind | `server` |
| Stability | `stable` |

### 屬性列表

| 屬性名稱 | Go 常數 | 類型 | 必填 | 說明 |
|---------|---------|------|------|------|
| `cart.item_count` | `CART_ITEM_COUNT` | `int` | ✅ 必填 | 購物車中商品總件數 |
| `cart.session_id` | `CART_SESSION_ID` | `string` | ✅ 必填 | 購物車 Session 識別碼 |
| `cart.total_amount` | `CART_TOTAL_AMOUNT` | `double` | ✅ 必填 | 結帳總金額（TWD） |
| `deployment.environment` | `DEPLOYMENT_ENVIRONMENT` | `string` | ✅ 必填 | 服務部署環境 |
| `git.tag` | `GIT_TAG` | `string` | ✅ 必填 | 部署的 Git 版本標籤 |

---
## 📡 Span: `span.payment.process`

**處理訂單支付流程的 Span**

| 屬性 | 值 |
|------|-----|
| Span Kind | `server` |
| Stability | `stable` |

### 屬性列表

| 屬性名稱 | Go 常數 | 類型 | 必填 | 說明 |
|---------|---------|------|------|------|
| `deployment.environment` | `DEPLOYMENT_ENVIRONMENT` | `string` | ✅ 必填 | 服務部署環境 |
| `error.type` | `ERROR_TYPE` | `string` | ⬜ 選填 | 失敗時的錯誤類型 |
| `git.tag` | `GIT_TAG` | `string` | ✅ 必填 | 部署的 Git 版本標籤 |
| `payment.currency` | `PAYMENT_CURRENCY` | `string` | ✅ 必填 | 貨幣代碼（ISO 4217） |
| `payment.order_id` | `PAYMENT_ORDER_ID` | `string` | ✅ 必填 | 唯一的訂單識別碼 |
| `payment.provider` | `PAYMENT_PROVIDER` | `string` | ✅ 必填 | 支付服務提供商 |
| `payment.status` | `PAYMENT_STATUS` | `string` | ✅ 必填 | 支付結果狀態 |

---
