"""Whether replies and the system prompt may name the host, the install and its settings.

``gateway.hide_operator_details`` is for a profile whose people are not its operator. Off keeps the stock
wording, so the stock prompt and replies do not change for anyone who has not set it.
"""


def operator_details_hidden() -> bool:
    from hermes_cli.config import load_config_readonly

    return bool((load_config_readonly().get("gateway") or {}).get("hide_operator_details"))
