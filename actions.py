"""Validate model output before applying a whole voice note atomically."""

from dataclasses import dataclass

from db import Item, PendingAdd, PendingRemoval, Store


@dataclass(frozen=True)
class ActionResult:
    items: list[Item]
    pending_adds: list[PendingAdd]
    pending_removals: list[PendingRemoval]


def _validate_item(value: object) -> None:
    if not isinstance(value, dict) or set(value) - {"item", "quantity"}:
        raise ValueError("An item must contain only item and optional quantity")
    if not isinstance(value.get("item"), str) or not value["item"].strip():
        raise ValueError("Item name must be a nonempty string")
    if value.get("quantity") is not None and (
        not isinstance(value["quantity"], str) or not value["quantity"].strip()
    ):
        raise ValueError("Quantity must be a nonempty string or null")


def validate_actions(payload: object) -> list[dict]:
    """Accept the plan's JSON envelope, rejecting malformed or extra fields."""
    if not isinstance(payload, dict) or set(payload) != {"actions"}:
        raise ValueError("Expected an actions envelope")
    actions = payload["actions"]
    if not isinstance(actions, list):
        raise ValueError("Actions must be a list")
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("Each action must be an object")
        kind = action.get("type")
        if kind in ("add", "remove"):
            _validate_item({key: value for key, value in action.items() if key != "type"})
        elif kind == "clear":
            if set(action) != {"type"}:
                raise ValueError("Clear accepts no extra fields")
        elif kind == "bought_all_except":
            if set(action) != {"type", "items"} or not isinstance(action["items"], list):
                raise ValueError("Bought-all-except requires an items list")
            for item in action["items"]:
                _validate_item(item)
        else:
            raise ValueError("Unknown action type")
    return actions


def apply_actions(store: Store, chat_id: int, payload: object) -> ActionResult:
    """Apply in spoken order; pending confirmations do not block later actions."""
    actions = validate_actions(payload)
    with store.transaction():
        for action in actions:
            kind = action["type"]
            if kind == "add":
                store.add_item(chat_id, action["item"], action.get("quantity"))
            elif kind == "remove":
                store.remove_item(chat_id, action["item"], action.get("quantity"))
            elif kind == "clear":
                store.clear(chat_id)
            else:
                store.bought_all_except(chat_id, [
                    (item["item"], item.get("quantity")) for item in action["items"]
                ])
        return ActionResult(store.list_items(chat_id), store.pending_adds(chat_id),
                            store.pending_removals(chat_id))
