package otel_weaver

import future.keywords.if
import future.keywords.in

# 允許的指標命名前綴
allowed_metric_prefixes := ["payment.", "cart.", "auth.", "inventory."]

# 規則 1：指標必須以允許的前綴開頭
deny[msg] if {
    group := input.groups[_]
    group.type == "metric"
    metric_name := group.metric_name
    not starts_with_any(metric_name, allowed_metric_prefixes)
    msg := sprintf(
        "指標 '%s' 不符合命名規範，必須以 %v 其中之一開頭",
        [metric_name, allowed_metric_prefixes],
    )
}

# 規則 2：所有 group 必須有非空的 brief
deny[msg] if {
    group := input.groups[_]
    trim_space(group.brief) == ""
    msg := sprintf("群組 '%s' 缺少 brief 說明欄位", [group.id])
}

# 規則 3：所有屬性必須有非空的 brief
deny[msg] if {
    group := input.groups[_]
    attr := group.attributes[_]
    not attr.ref  # 跳過 ref 引用（ref 不需要 brief）
    trim_space(attr.brief) == ""
    msg := sprintf("屬性 '%s' 在群組 '%s' 中缺少 brief 說明", [attr.id, group.id])
}

starts_with_any(str, prefixes) if {
    prefix := prefixes[_]
    startswith(str, prefix)
}
