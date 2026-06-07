package otel_weaver

import future.keywords.if
import future.keywords.in

# 受生產環境警報保護的屬性，不得被刪除
protected_required_attributes := {
    "payment.order_id",
    "payment.provider",
    "cart.item_id",
    "cart.session_id",
}

# 規則：受保護的屬性不得從 span/metric 群組中移除
deny[msg] if {
    group := input.groups[_]
    group.type in ["span", "metric"]
    attr_id := protected_required_attributes[_]

    # 找出是否在這個 group 的屬性列表或 ref 中有此 attr_id
    not any_attr_matches(group.attributes, attr_id)

    # 此 group 的 id 包含受保護屬性的命名空間
    namespace := split(attr_id, ".")[0]
    startswith(group.id, concat(".", ["span", namespace]))

    msg := sprintf(
        "❌ 受保護屬性 '%s' 不得從群組 '%s' 移除（生產警報依賴此欄位）",
        [attr_id, group.id],
    )
}

any_attr_matches(attributes, attr_id) if {
    attr := attributes[_]
    attr.id == attr_id
}

any_attr_matches(attributes, attr_id) if {
    attr := attributes[_]
    attr.ref == attr_id
}
