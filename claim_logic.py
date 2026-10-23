"""Deterministic, local-only validation helpers for ClaimGuard AI."""

import json
import hashlib
import math
from io import BytesIO
from datetime import date

from PIL import Image


MONEY_TOLERANCE = 0.02
MAX_ANALYSIS_BYTES = 20 * 1024 * 1024


def validate_policy(policy_id, record, claim_type, incident_date, claim_amount):
    """Return review findings based only on the configured policy record."""
    findings = []

    def add(status, severity, description, evidence, recommendation, confidence):
        findings.append({
            "category": "Policy validation",
            "status": status,
            "severity": severity,
            "finding": description,
            "evidence": evidence,
            "next_step": recommendation,
            "confidence": confidence,
        })

    if not policy_id:
        add(
            "Missing information", "Low", "Policy ID was not provided.",
            "No policy identifier is available.", "Request the policy number.",
            "High",
        )
        return findings

    if not record:
        add(
            "Unverified", "Info",
            "Policy ID was not found in the configured demo records.",
            f"ID checked: {policy_id}.", "Verify the policy directly with the insurer.",
            "High",
        )
        return findings

    status = record.get("status")
    if not status:
        add(
            "Missing information", "Low", "Policy status is unavailable.",
            f"Configured record for {policy_id} has no status.",
            "Confirm the current policy status with the insurer.", "High",
        )
    elif str(status).strip().casefold() == "active":
        add(
            "Verified by available rule", "Info", "Configured record is marked active.",
            f"Demo record status: {status}.",
            "Confirm status with the insurer before relying on it.", "High",
        )
    else:
        add(
            "Requires human review", "Medium",
            "Configured record is not marked active.",
            f"Demo record status: {status}.",
            "Confirm the policy status and applicable dates with the insurer.",
            "High",
        )

    expiry_value = record.get("expiry")
    if not expiry_value:
        add(
            "Missing information", "Low", "Policy expiry date is unavailable.",
            "The configured record has no expiry date.",
            "Confirm the effective dates from the policy wording.", "High",
        )
    else:
        try:
            expiry = date.fromisoformat(str(expiry_value))
            incident = (
                incident_date
                if isinstance(incident_date, date)
                else date.fromisoformat(str(incident_date))
            )
            if expiry < incident:
                add(
                    "Potential inconsistency", "Medium",
                    "Recorded policy expiry precedes the incident date.",
                    f"Expiry: {expiry.isoformat()}; incident: {incident.isoformat()}.",
                    "Verify the policy period and incident date with source records.",
                    "High",
                )
            else:
                add(
                    "Verified by available rule", "Info",
                    "Recorded policy expiry is on or after the incident date.",
                    f"Expiry: {expiry.isoformat()}; incident: {incident.isoformat()}.",
                    "Confirm the effective period and any other policy conditions.",
                    "High",
                )
        except (TypeError, ValueError):
            add(
                "Unverified", "Low", "Policy expiry or incident date is invalid.",
                f"Expiry: {expiry_value}; incident date: {incident_date}.",
                "Check the dates on the original policy and incident records.",
                "High",
            )

    policy_type = record.get("type")
    if not policy_type:
        add(
            "Missing information", "Low", "Policy type is unavailable.",
            "The configured record has no policy type.",
            "Confirm the covered policy type with the insurer.", "High",
        )
    elif str(policy_type).strip().casefold() != str(claim_type or "").strip().casefold():
        add(
            "Potential inconsistency", "Medium",
            "Selected claim type differs from the configured policy type.",
            f"Policy type: {policy_type}; selected claim type: {claim_type or 'not provided'}.",
            "Confirm the policy coverage and correct claim classification.",
            "High",
        )
    else:
        add(
            "Verified by available rule", "Info",
            "Selected claim type matches the configured policy type.",
            f"Configured policy type: {policy_type}.",
            "Confirm coverage using the policy wording.", "High",
        )

    coverage = _num(record.get("coverage"))
    amount = _num(claim_amount)
    if coverage is None:
        add(
            "Missing information", "Low", "Coverage limit is unavailable.",
            "No numeric coverage limit was present in the configured record.",
            "Check the policy schedule and applicable sublimits.", "High",
        )
    elif amount is None:
        add(
            "Missing information", "Low", "Claim amount is unavailable or invalid.",
            f"Configured coverage limit: {coverage:g}.",
            "Confirm the claimed amount and currency.", "High",
        )
    elif amount > coverage:
        add(
            "Requires human review", "Medium",
            "Claimed amount exceeds the configured coverage limit.",
            f"Claim amount: {amount:g}; demo limit: {coverage:g}.",
            "Review deductibles, exclusions, sublimits, policy wording, and currency "
            "before drawing any coverage conclusion.",
            "High",
        )
    else:
        add(
            "Verified by available rule", "Info",
            "Claimed amount does not exceed the configured coverage limit.",
            f"Claim amount: {amount:g}; demo limit: {coverage:g}.",
            "A limit comparison alone does not establish the payable amount.",
            "High",
        )
    return findings


def validate_upload(data, mime_type):
    """Validate upload size, emptiness and basic format signatures locally."""
    if not data:
        return "The uploaded file is empty."
    if len(data) > MAX_ANALYSIS_BYTES:
        return "File exceeds the configured 20 MB limit."

    signatures = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/webp": (
            len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
        ),
        "application/pdf": b"%PDF-" in data[:1024],
    }
    if mime_type not in signatures:
        return "The declared file type is not supported."
    if not signatures[mime_type]:
        return "File contents do not match the declared file type."
    if mime_type.startswith("image/"):
        try:
            with Image.open(BytesIO(data)) as image:
                image.verify()
        except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
            return "Image file is unreadable or corrupted."
    elif b"%%EOF" not in data[-2048:]:
        return "PDF file appears incomplete or corrupted."
    return None


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def validate_invoice_totals(result, filename):
    """Compare only extracted numeric amounts; missing values are not zero."""
    findings = []
    if not result.get("ok"):
        return findings
    fields = result.get("fields") or {}
    items = result.get("line_items") or []
    calculated_lines = []
    incomplete_lines = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            incomplete_lines.append(f"Line {index} is malformed.")
            continue
        quantity, unit_price, line_total = (
            _num(item.get(key)) for key in ("quantity", "unit_price", "line_total")
        )
        if quantity is None or unit_price is None or line_total is None:
            incomplete_lines.append(f"Line {index} has missing amounts.")
        else:
            calculated_lines.append(quantity * unit_price)
            if abs(quantity * unit_price - line_total) > MONEY_TOLERANCE:
                findings.append({
                    "document_name": filename,
                    "category": "Invoice arithmetic",
                    "status": "Potential inconsistency",
                    "severity": "Medium",
                    "finding": f"Line {index} total differs from quantity × unit price.",
                    "evidence": (
                        f"Quantity={quantity:g}; unit price={unit_price:.2f}; "
                        f"calculated={quantity * unit_price:.2f}; stated={line_total:.2f}."
                    ),
                    "why_it_matters": "The extracted line amount may need reconciliation.",
                    "next_step": "Compare the original line and any line-specific adjustments.",
                    "limitations": "Extraction errors and invoice-specific rules may explain differences.",
                })

    subtotal, taxes, discounts, total = (
        _num(fields.get(key))
        for key in ("declared_subtotal", "taxes", "discounts", "grand_total")
    )
    if items and len(calculated_lines) == len(items) and subtotal is not None:
        line_sum = sum(calculated_lines)
        if abs(line_sum - subtotal) > MONEY_TOLERANCE:
            findings.append({
                "document_name": filename,
                "category": "Invoice arithmetic",
                "status": "Potential inconsistency",
                "severity": "Medium",
                "finding": "Complete extracted line-item sum differs from declared subtotal.",
                "evidence": f"Line sum={line_sum:.2f}; declared subtotal={subtotal:.2f}.",
                "why_it_matters": "The subtotal may need reconciliation.",
                "next_step": "Check omitted lines, adjustments and the printed subtotal.",
                "limitations": "This comparison requires every listed line item to be extracted.",
            })

    if None not in (subtotal, taxes, discounts, total):
        expected = subtotal + taxes - discounts
        if abs(expected - total) > MONEY_TOLERANCE:
            findings.append({
                "document_name": filename,
                "category": "Invoice arithmetic",
                "status": "Potential inconsistency",
                "severity": "Medium",
                "finding": "Subtotal plus taxes minus discounts differs from grand total.",
                "evidence": (
                    f"Subtotal={subtotal:.2f}; taxes={taxes:.2f}; "
                    f"discounts={discounts:.2f}; calculated={expected:.2f}; "
                    f"grand total={total:.2f}; currency={fields.get('currency') or 'not stated'}."
                ),
                "why_it_matters": "Invoice amounts may need reconciliation.",
                "next_step": "Check for omitted fees, credits, rounding or extraction errors.",
                "limitations": "AI extraction can be wrong and other invoice components may apply.",
            })
    elif any(value is not None for value in (subtotal, taxes, discounts, total)):
        incomplete_lines.append("Subtotal, tax, discount or grand total is missing.")

    if incomplete_lines:
        findings.append({
            "document_name": filename,
            "category": "Invoice arithmetic",
            "status": "Unverified",
            "severity": "Low",
            "finding": "Some line items could not be recalculated.",
            "evidence": "; ".join(incomplete_lines),
            "why_it_matters": "The extraction is incomplete for a full arithmetic review.",
            "next_step": "Manually verify unclear quantities, prices and totals.",
            "limitations": "Missing values are not treated as zero.",
        })

    if not findings:
        complete = None not in (subtotal, taxes, discounts, total)
        findings.append({
            "document_name": filename,
            "category": "Invoice arithmetic",
            "status": "Verified by available rule" if complete else "Unverified",
            "severity": "Info",
            "finding": (
                "Available invoice arithmetic checks found no discrepancy."
                if complete else "Insufficient extracted values to complete invoice arithmetic checks."
            ),
            "evidence": f"Subtotal={fields.get('declared_subtotal')}; total={fields.get('grand_total')}.",
            "why_it_matters": "Arithmetic consistency is not proof of authenticity.",
            "next_step": "Verify key values against the original invoice.",
            "limitations": "No external issuer verification was performed.",
        })
    return findings


def serialize_report(report):
    """Serialize report data without exposing runtime secrets."""
    return json.dumps(report, ensure_ascii=False, indent=2, default=str)
