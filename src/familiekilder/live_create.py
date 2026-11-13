"""Single-send actions with minimal readback evidence, never a message archive."""

import json

from .core import FamiliekilderError
from .action_ledger import now


def perform(
    ledger, operation_id, source, action, payload, target, observe, send, validate=None
):
    """observe returns IDs of exact matching source objects, from complete reads.

    The caller holds the source lock across this entire operation. A retry only
    observes; it never sends again after the sending marker has been committed.
    Only matching pre-existing IDs, not their content, survive a crash.
    """
    prior = ledger.reserve_operation(operation_id, source, action, payload)
    if prior and prior["state"] == "verified":
        return ledger.operation_result(operation_id, "verified")
    if prior and prior["state"] in {"sending", "unknown"}:
        receipt = json.loads(prior["receipt_json"] or "null")
        if not isinstance(receipt, dict) or not isinstance(
            receipt.get("baseline_ids"), list
        ):
            return ledger.operation_result(operation_id, "unknown")
    else:
        if validate:
            validate()
        baseline = sorted(set(observe()))
        if len(baseline) > 50:
            raise FamiliekilderError(
                "For mange identiske kilderegistreringer til entydig resultatkontroll"
            )
        receipt = dict(
            target,
            source=source,
            action=action,
            started_at=now(),
            baseline_ids=baseline,
        )
        ledger.operation_result(operation_id, "sending", receipt)
        try:
            send()
        except Exception as exc:
            # A transport, HTTP or decoding failure does not prove non-delivery.
            # Stop here on explicit provider throttling/auth rejection. Readback
            # can be requested later with the same operation id.
            from .api_transport import ApiHTTPError

            if isinstance(exc, ApiHTTPError) and exc.status_code in {401, 403, 429}:
                return ledger.operation_result(operation_id, "unknown", receipt)
    try:
        new_ids = set(observe()) - set(receipt["baseline_ids"])
    except Exception:
        return ledger.operation_result(operation_id, "unknown", receipt)
    if len(new_ids) != 1:
        return ledger.operation_result(operation_id, "unknown", receipt)
    receipt.pop("baseline_ids")
    receipt.update(
        reference=next(iter(new_ids)),
        verified_at=now(),
        verification="fresh_exact_match",
    )
    return ledger.operation_result(operation_id, "verified", receipt)
