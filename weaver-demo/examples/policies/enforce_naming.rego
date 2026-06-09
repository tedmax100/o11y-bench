package after_resolution

import rego.v1

# 允許的指標命名前綴
allowed_metric_prefixes := ["payment.", "cart.", "auth.", "inventory."]

# 規則 1：指標必須以允許的前綴開頭
deny contains violation if {
    group := input.groups[_]
    group.type == "metric"
    metric_name := group.metric_name
    not starts_with_any(metric_name, allowed_metric_prefixes)
    violation := {
        "id": "metric_naming_prefix",
        "level": "violation",
        "message": sprintf("指標 '%s' 不符合命名規範，必須以 %v 其中之一開頭", [metric_name, allowed_metric_prefixes]),
        "context": {"group": group.id, "metric_name": metric_name},
    }
}

# 規則 2：所有屬性必須有非空的 brief
deny contains violation if {
    group := input.groups[_]
    attr := group.attributes[_]
    trim_space(attr.brief) == ""
    violation := {
        "id": "missing_attr_brief",
        "level": "violation",
        "message": sprintf("屬性 '%s' 在群組 '%s' 中缺少 brief 說明", [attr.name, group.id]),
        "context": {"group": group.id, "attr": attr.name},
    }
}

starts_with_any(str, prefixes) if {
    prefix := prefixes[_]
    startswith(str, prefix)
}
