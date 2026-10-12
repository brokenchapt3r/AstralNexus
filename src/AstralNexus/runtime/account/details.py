"""Platform Web profile and wallet queries projected for account UI consumers.

Port of the details layer of ``@deepseek-ai/dsh-deepseek-account-platform``
(commit 5badb15, lib/types/details.js). Contract kept:

- ``profile`` projects Platform user data without retaining credentials or
  unneeded fields (id / avatarUrl / name / contact with the mobile →
  mobile_number → email fallback); a malformed payload is a protocol failure.
- Two fixed queries (``/auth-api/v0/users/current`` and
  ``/api/v0/users/get_user_summary``) with their own parse projections.
- ``read_account_detail`` never turns a failure into a zero balance: any
  non-unauthorized failure becomes ``{"status": "failed"}``, while
  ``AccountUnauthorizedError`` propagates so the caller can expire the
  credential.
- Bonus rows validate as UUID orders with decimal-string amounts and
  CNY/USD currencies, then project snake → camel.
"""
from __future__ import annotations

import re
import uuid as uuid_module
from typing import Any, Dict, List, Optional

from .protocol import (
    AccountUnauthorizedError, PlatformAuthError, issue, log_payload_rejection,
    request_account, request_bonus_notified, request_unnotified_bonuses)

DECIMAL = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$", re.IGNORECASE)
CURRENCIES = ("CNY", "USD")


def parse_user(value: Any) -> tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """``{id?, email, mobile?, mobile_number?, id_profile?{name,picture?}}``."""
    issues: List[Dict[str, Any]] = []
    if not isinstance(value, dict):
        return None, [issue([], "invalid_type", value)]
    email = value.get("email")
    if not isinstance(email, str):
        issues.append(issue(["email"], "invalid_type", email, missing="email" not in value))
    ident = value.get("id")
    if ident is not None and not isinstance(ident, str):
        issues.append(issue(["id"], "invalid_type", ident))
    for name in ("mobile", "mobile_number"):
        field = value.get(name)
        if field is not None and not isinstance(field, str):
            issues.append(issue([name], "invalid_type", field))
    id_profile = value.get("id_profile")
    if id_profile is not None:
        if not isinstance(id_profile, dict):
            issues.append(issue(["id_profile"], "invalid_type", id_profile))
        else:
            inner_name = id_profile.get("name")
            if inner_name is not None and not isinstance(inner_name, str):
                issues.append(issue(["id_profile", "name"], "invalid_type", inner_name))
            picture = id_profile.get("picture")
            if picture is not None and not isinstance(picture, str):
                issues.append(issue(["id_profile", "picture"], "invalid_type", picture))
    if issues:
        return None, issues
    return {"id": ident, "email": email, "mobile": value.get("mobile"),
            "mobile_number": value.get("mobile_number"), "id_profile": id_profile}, []


def _parse_wallet(value: Any, path: List[Any]) -> tuple[Optional[Dict[str, Any]],
                                                        List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    if not isinstance(value, dict):
        return None, [issue(path, "invalid_type", value)]
    currency = value.get("currency")
    if not isinstance(currency, str) or currency not in CURRENCIES:
        issues.append(issue(path + ["currency"], "invalid_enum", currency,
                            missing="currency" not in value))
    balance = value.get("balance")
    if not isinstance(balance, str) or not DECIMAL.match(balance):
        issues.append(issue(path + ["balance"], "invalid_string_format", balance,
                            missing="balance" not in value))
    if issues:
        return None, issues
    return {"currency": currency, "balance": balance}, []


def parse_summary(value: Any) -> tuple[Optional[Dict[str, List[Dict[str, Any]]]],
                                       List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    normal: List[Dict[str, str]] = []
    bonus: List[Dict[str, str]] = []
    if not isinstance(value, dict):
        return None, [issue([], "invalid_type", value)]
    for field, target in (("normal_wallets", normal), ("bonus_wallets", bonus)):
        rows = value.get(field)
        if not isinstance(rows, list):
            issues.append(issue([field], "invalid_type", rows, missing=field not in value))
            continue
        for index, row in enumerate(rows):
            parsed, row_issues = _parse_wallet(row, [field, index])
            if row_issues:
                issues.extend(row_issues)
            else:
                target.append(parsed)  # type: ignore[arg-type]
    if issues:
        return None, issues
    return {"normal_wallets": normal, "bonus_wallets": bonus}, []


def parse_bonus(value: Any, path: List[Any]) -> tuple[Optional[Dict[str, Any]],
                                                      List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    if not isinstance(value, dict):
        return None, [issue(path, "invalid_type", value)]
    order_id = value.get("order_id")
    ok = isinstance(order_id, str)
    if ok:
        try:
            uuid_module.UUID(order_id)
        except ValueError:
            ok = False
    if not ok:
        issues.append(issue(path + ["order_id"], "invalid_string_format", order_id,
                            missing="order_id" not in value))
    campaign = value.get("campaign")
    if not isinstance(campaign, str):
        issues.append(issue(path + ["campaign"], "invalid_type", campaign,
                            missing="campaign" not in value))
    amount = value.get("amount")
    if not isinstance(amount, str) or not DECIMAL.match(amount or ""):
        issues.append(issue(path + ["amount"], "invalid_string_format", amount,
                            missing="amount" not in value))
    currency = value.get("currency")
    if not isinstance(currency, str) or currency not in CURRENCIES:
        issues.append(issue(path + ["currency"], "invalid_enum", currency,
                            missing="currency" not in value))
    for field in ("granted_at", "expires_at", "msg"):
        item = value.get(field)
        if not isinstance(item, str):
            issues.append(issue(path + [field], "invalid_type", item,
                                missing=field not in value))
    if issues:
        return None, issues
    return {"order_id": order_id, "campaign": campaign, "amount": amount,
            "currency": currency, "granted_at": value.get("granted_at"),
            "expires_at": value.get("expires_at"), "msg": value.get("msg")}, []


def profile(value: Any) -> Dict[str, Any]:
    """Project Platform user data for the UI; malformed payloads are protocol failures."""
    parsed, issues = parse_user(value)
    if issues:
        log_payload_rejection("profile", issues)
        raise PlatformAuthError("protocol")
    identity = parsed["id_profile"]  # type: ignore[index]
    return {
        "id": parsed["id"] if parsed["id"] is not None else None,  # type: ignore[index]
        "avatarUrl": (identity or {}).get("picture") or None,
        "name": (identity or {}).get("name") or None,
        "contact": (parsed["mobile"] or parsed["mobile_number"]  # type: ignore[index]
                    or parsed["email"] or None),  # type: ignore[index]
    }


def _parse_balance(value: Any) -> Dict[str, Any]:
    parsed, issues = parse_summary(value)
    if issues or parsed is None:
        log_payload_rejection("balance", issues)
        raise PlatformAuthError("protocol")
    return {"status": "ready", "value": parsed["normal_wallets"],
            "bonusWallets": parsed["bonus_wallets"]}


QUERIES: Dict[str, Dict[str, Any]] = {
    "profile": {
        "path": "/auth-api/v0/users/current",
        "parse": lambda value: {"status": "ready", "value": profile(value)},
    },
    "balance": {
        "path": "/api/v0/users/get_user_summary",
        "parse": _parse_balance,
    },
}


def read_account_detail(field: str, origin: str, token: str, headers: Dict[str, str],
                        timeout: float) -> Dict[str, Any]:
    """Read one Platform account field; failure never becomes a zero balance."""
    query = QUERIES[field]
    try:
        return query["parse"](request_account(origin, query["path"], token, headers, timeout))
    except AccountUnauthorizedError:
        raise
    except Exception:  # noqa: BLE001 - upstream returns {status:"failed"} for everything else
        return {"status": "failed"}


def read_unnotified_bonuses(origin: str, token: str, headers: Dict[str, str],
                            timeout: float) -> List[Dict[str, str]]:
    """The granted bonuses Platform has not yet recorded as displayed."""
    payload = request_unnotified_bonuses(origin, token, headers, timeout)
    if not isinstance(payload, list):
        log_payload_rejection("unnotified", [issue([], "invalid_type", payload)])
        raise PlatformAuthError("protocol")
    rows: List[Dict[str, str]] = []
    issues: List[Dict[str, Any]] = []
    for index, row in enumerate(payload):
        parsed, row_issues = parse_bonus(row, [index])
        if row_issues:
            issues.extend(row_issues)
        else:
            rows.append(parsed)  # type: ignore[arg-type]
    if issues:
        log_payload_rejection("unnotified", issues)
        raise PlatformAuthError("protocol")
    return [{"orderId": row["order_id"], "campaign": row["campaign"], "amount": row["amount"],
             "currency": row["currency"], "grantedAt": row["granted_at"],
             "expiresAt": row["expires_at"], "message": row["msg"]} for row in rows]


def send_bonus_notified(origin: str, token: str, order_id: str, headers: Dict[str, str],
                        timeout: float) -> None:
    """Record one displayed bonus as notified; HTTP and business failures throw."""
    request_bonus_notified(origin, token, order_id, headers, timeout)
