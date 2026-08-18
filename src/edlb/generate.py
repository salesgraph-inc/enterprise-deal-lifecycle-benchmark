from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import shutil
import unicodedata
from collections.abc import Iterable
from datetime import date, timedelta
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DATASET_VERSION = "v1.0.0"
DATASET_SEED = 20260817
PRIVATE_CONFIG_NAME = "generation_config.json"
ARTIFACT_COUNTS = {
    "transcript": 10,
    "email": 14,
    "internal_chat": 12,
    "crm": 12,
    "calendar": 8,
    "document": 10,
    "web_news": 6,
}
SPLITS = ("train", "dev", "blind")
ROLES = ("account_executive", "domain_specialist", "sales_manager", "revops")
OUTCOME_BY_FAMILY = {
    "champion_exit": ("closed_won", "closed_lost_competitive"),
    "late_stakeholder": ("closed_won", "closed_lost_competitive"),
    "budget_shock": ("no_decision", "no_decision"),
    "requirements_change": ("closed_won", "disqualified_fit"),
    "competition": ("closed_won", "closed_lost_competitive"),
    "external_event": ("closed_lost_fit", "disqualified_fit"),
}
VARIANT_NAMES = {
    "champion_exit": ("strong_handoff", "weak_handoff"),
    "late_stakeholder": ("supportive", "blocking"),
    "budget_shock": ("reallocation", "freeze"),
    "requirements_change": ("within_fit", "out_of_fit"),
    "competition": ("transparent", "hidden_influence"),
    "external_event": ("recoverable", "terminal"),
}
FAMILIES = (
    "champion_exit",
    "late_stakeholder",
    "budget_shock",
    "requirements_change",
    "competition",
    "external_event",
)
CAUSAL_SKELETONS = {
    "champion_exit": "champion_departure",
    "late_stakeholder": "late_authority_entry",
    "budget_shock": "budget_shock",
    "requirements_change": "requirements_change",
    "competition": "competitive_pressure",
    "external_event": "external_event",
}
TERMINAL_OUTCOMES = {
    "closed_won": "closed_won",
    "closed_lost_competitive": "closed_lost",
    "closed_lost_fit": "closed_lost",
    "no_decision": "no_decision",
    "disqualified_fit": "disqualified",
}
ARTIFACT_KINDS = {
    "transcript": "call_transcript",
    "email": "email",
    "internal_chat": "internal_chat",
    "crm": "crm_record",
    "calendar": "calendar_event",
    "document": "diligence_document",
    "web_news": "news_item",
}
REQUIRED_CHANNELS = (
    "call_transcript",
    "email",
    "internal_chat",
    "crm",
    "calendar",
    "document",
    "web_signal",
)
CANONICAL_CATEGORIES = (
    "evidence_and_understanding",
    "crm_integrity",
    "stakeholder_management",
    "workflow_compliance",
    "communication_quality",
    "forecast_calibration",
    "longitudinal_recovery",
    "side_effect_discipline",
)
AMOUNT_MINOR_UNITS: dict[str, tuple[int, int]] = {
    "manufacturing": (185_000_000, 12_500_000),
    "construction": (2_800_000_000, 250_000_000),
    "commercial_insurance": (120_000_000, 10_000_000),
    "consulting": (240_000_000, 15_000_000),
    "legal_services": (95_000_000, 7_500_000),
    "corporate_banking": (7_500_000_000, 500_000_000),
}
LANE_EFFECTS = {
    "champion_exit": {
        "strong_handoff": {
            "stakeholder_consensus": {
                "delta": 10,
                "fact": "documented handoff preserved decision ownership",
            }
        },
        "weak_handoff": {
            "stakeholder_consensus": {
                "absolute": -100,
                "status": "failed",
                "sticky": True,
                "fact": "decision ownership failed after the undocumented departure",
            }
        },
    },
    "late_stakeholder": {
        "supportive": {
            "stakeholder_consensus": {
                "delta": 20,
                "fact": "executive sponsor confirmed the decision path",
            },
            "approvals": {
                "delta": 10,
                "fact": "executive sponsor clarified approval ownership",
            },
        },
        "blocking": {
            "stakeholder_consensus": {
                "absolute": -100,
                "status": "failed",
                "sticky": True,
                "fact": "executive sponsor withdrew decision-group support",
            },
            "urgency": {
                "delta": -20,
                "fact": "executive sponsor questioned the current priority",
            },
        },
    },
    "budget_shock": {
        "reallocation": {
            "commercial_terms": {
                "delta": -10,
                "fact": "reduced allocation requires a revised commercial path",
            },
            "approvals": {
                "delta": -5,
                "fact": "finance reopened the funding approval",
            },
            "urgency": {
                "delta": -10,
                "fact": "finance moved the decision into a revised review window",
            },
        },
        "freeze": {
            "commercial_terms": {
                "absolute": -35,
                "fact": "commercial scope is blocked by the spending hold",
            },
            "approvals": {
                "absolute": -25,
                "fact": "finance cannot grant funding approval during the hold",
            },
            "urgency": {
                "absolute": -60,
                "status": "failed",
                "sticky": True,
                "fact": "the current planning window closed under the spending hold",
            },
        },
    },
    "requirements_change": {
        "within_fit": {
            "business_fit": {
                "delta": 10,
                "fact": "seller evidence covers the extended requirement",
            },
            "validation": {
                "delta": 10,
                "fact": "validation scope includes the extended requirement",
            },
        },
        "out_of_fit": {
            "business_fit": {
                "absolute": -100,
                "status": "failed",
                "sticky": True,
                "fact": "mandatory requirement is outside the seller offering",
            },
            "validation": {
                "absolute": -60,
                "status": "failed",
                "sticky": True,
                "fact": "validation cannot satisfy the mandatory requirement",
            },
        },
    },
    "competition": {
        "transparent": {
            "competition": {
                "delta": -15,
                "fact": "disclosed incumbent benchmark creates addressable pressure",
            }
        },
        "hidden_influence": {
            "competition": {
                "absolute": -100,
                "status": "failed",
                "sticky": True,
                "fact": "undisclosed incumbent influence controlled the decision",
            }
        },
    },
    "external_event": {
        "recoverable": {
            "stakeholder_consensus": {
                "absolute": -100,
                "status": "failed",
                "sticky": True,
                "fact": "the current buyer decision window closed despite the workaround",
            },
            "urgency": {
                "delta": -40,
                "fact": "the disruption moved delivery beyond the current decision window",
            },
        },
        "terminal": {
            "business_fit": {
                "absolute": -100,
                "status": "failed",
                "sticky": True,
                "fact": "the paused buyer program no longer has a valid seller motion",
            },
            "urgency": {
                "absolute": -100,
                "status": "failed",
                "sticky": True,
                "fact": "the buyer program has no approved restart date",
            },
        },
    },
}
PDF_RENDERER_VERSION = "4.4.9"
XLSX_RENDERER_VERSION = "2.8.43"
PDF_MIME_TYPE = "application/pdf"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RESERVED_DOMAIN_SUFFIXES = (".example", ".invalid", ".localhost", ".test")
PRIVACY_TEXT_EXTENSIONS = frozenset({".csv", ".json", ".jsonl", ".md", ".txt"})
PRIVACY_SOURCE_KEYS = frozenset(
    {"source_url", "source_urls", "source_reference_url", "source_reference_urls"}
)
PRIVACY_SOURCE_PATH_PARTS = frozenset(
    {"source-reference", "source-references", "source_reference", "source_references"}
)
PRIVACY_PHONE_KEYS = frozenset(
    {"fax", "mobile", "phone", "phone_number", "telephone", "telephone_number"}
)
PRIVACY_DOMAIN_KEYS = frozenset(
    {"buyer_domain", "domain", "email", "location", "owner", "uri", "url"}
)
PRIVACY_COMMON_TLDS = frozenset(
    {
        "ai",
        "app",
        "au",
        "bank",
        "biz",
        "ca",
        "cloud",
        "co",
        "com",
        "company",
        "de",
        "dev",
        "edu",
        "fr",
        "gov",
        "health",
        "info",
        "int",
        "io",
        "jp",
        "law",
        "me",
        "mil",
        "net",
        "org",
        "tech",
        "uk",
        "us",
        "xyz",
    }
)
PRIVACY_DOMAIN_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9-])(?:https?://)?(?:localhost|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|localhost))(?::\d{2,5})?(?![a-z0-9-@])"
)
PRIVACY_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]?)?(?:\([2-9]\d{2}\)|[2-9]\d{2})[\s.-]*\d{3}[\s.-]+\d{4}(?!\d)"
)


VERTICALS: tuple[dict[str, Any], ...] = (
    {
        "id": "manufacturing",
        "label": "Manufacturing",
        "seller_id": "seller-northstar-fabrication",
        "seller_name": "Northstar Fabrication Cooperative",
        "domain": "northstar-fabrication.example",
        "motion": "industrial component and equipment sale",
        "gates": (
            "rfq",
            "technical_validation",
            "sample_or_pilot",
            "supplier_qualification",
            "quality_and_capacity_review",
            "commercial_terms",
            "purchase_order",
        ),
        "buyer_industry": "industrial manufacturing",
        "currency": "USD",
    },
    {
        "id": "construction",
        "label": "Construction",
        "seller_id": "seller-cinderline-builders",
        "seller_name": "Cinderline Builders Group",
        "domain": "cinderline-builders.example",
        "motion": "general contractor project pursuit",
        "gates": (
            "qualification",
            "bonding_and_safety",
            "site_walk",
            "tender",
            "bid",
            "interview",
            "value_engineering",
            "award_and_contract",
        ),
        "buyer_industry": "commercial construction",
        "currency": "USD",
    },
    {
        "id": "commercial_insurance",
        "label": "Commercial Insurance",
        "seller_id": "seller-blueharbor-risk",
        "seller_name": "Blueharbor Risk Partners",
        "domain": "blueharbor-risk.example",
        "motion": "commercial insurance placement",
        "gates": (
            "submission",
            "exposure_and_loss_data",
            "market_selection",
            "underwriting",
            "quote_comparison",
            "client_order",
            "binding",
            "policy_issuance",
        ),
        "buyer_industry": "commercial services",
        "currency": "USD",
    },
    {
        "id": "consulting",
        "label": "Consulting",
        "seller_id": "seller-fieldstone-advisory",
        "seller_name": "Fieldstone Advisory Studio",
        "domain": "fieldstone-advisory.example",
        "motion": "strategy and operations engagement",
        "gates": (
            "discovery",
            "diagnosis",
            "scope",
            "staffing",
            "commercial_model",
            "statement_of_work",
            "procurement",
            "executive_approval",
        ),
        "buyer_industry": "business services",
        "currency": "USD",
    },
    {
        "id": "legal_services",
        "label": "Legal Services",
        "seller_id": "seller-quarry-counsel",
        "seller_name": "Quarry Counsel LLP",
        "domain": "quarry-counsel.example",
        "motion": "outside counsel pursuit",
        "gates": (
            "conflicts",
            "panel_or_rfp_selection",
            "matter_scope",
            "fee_arrangement",
            "security_and_privilege_review",
            "engagement_letter",
        ),
        "buyer_industry": "regulated enterprise services",
        "currency": "USD",
    },
    {
        "id": "corporate_banking",
        "label": "Corporate Banking",
        "seller_id": "seller-emberline-bank",
        "seller_name": "Emberline Commercial Bank",
        "domain": "emberline-bank.example",
        "motion": "commercial lending and treasury sale",
        "gates": (
            "kyc_and_aml",
            "diligence",
            "underwriting",
            "credit_approval",
            "pricing",
            "committee_review",
            "documentation",
            "covenants_and_closing",
        ),
        "buyer_industry": "mid-market enterprise",
        "currency": "USD",
    },
)

FIRST_NAMES = (
    "Avery",
    "Bryn",
    "Cato",
    "Dara",
    "Emery",
    "Finley",
    "Greer",
    "Hollis",
    "Indra",
    "Jules",
    "Kiran",
    "Lior",
    "Maren",
    "Niko",
    "Orin",
    "Pax",
    "Quill",
    "Rhea",
    "Sable",
    "Tavi",
    "Uma",
    "Vale",
    "Wren",
    "Yara",
)
LAST_NAMES = (
    "Ashby",
    "Bell",
    "Cairn",
    "Dovetail",
    "Elm",
    "Fallow",
    "Grove",
    "Harbor",
    "Ives",
    "Juniper",
    "Keel",
    "Lumen",
    "Morrow",
    "North",
    "Orchard",
    "Pine",
    "Quarry",
    "Rook",
    "Solace",
    "Thorne",
    "Umber",
    "Vale",
    "Wick",
    "Yarrow",
)
COMPANY_WORDS = (
    "Alder",
    "Bracken",
    "Cairn",
    "Dovetail",
    "Elmshore",
    "Fallow",
    "Granite",
    "Harbor",
    "Ironwood",
    "Juniper",
    "Keystone",
    "Larkspur",
    "Meridian",
    "Northfield",
    "Oakline",
    "Pinecrest",
    "Quarry",
    "Redwood",
    "Stonebridge",
    "Tern",
    "Umber",
    "Vale",
    "Westmere",
    "Yarrow",
)
COMPANY_SUFFIXES = (
    "Group",
    "Holdings",
    "Industries",
    "Partners",
    "Collective",
    "Enterprises",
)

ROLE_LABELS = {
    "champion": "Champion",
    "economic_buyer": "Economic buyer",
    "procurement": "Procurement lead",
    "evaluator": "Domain evaluator",
    "finance": "Finance lead",
    "executive_sponsor": "Executive sponsor",
}

SHARED_THEMES = (
    "company overview",
    "qualification policy",
    "pricing bands",
    "approval matrix",
    "delivery constraints",
    "security requirements",
    "quality standards",
    "commercial terms",
    "forecast policy",
    "account planning",
    "discovery guide",
    "meeting policy",
    "proposal checklist",
    "contract redlines",
    "risk escalation",
    "data handling",
    "stakeholder mapping",
    "competitive intelligence",
    "renewal policy",
    "discount policy",
    "margin guardrails",
    "capacity planning",
    "implementation guide",
    "customer references",
    "case study index",
    "legal review guide",
    "procurement guide",
    "approval request form",
    "CRM field dictionary",
    "close plan template",
)
POLICY_CONTROLS: dict[str, tuple[tuple[str, str, int, str], ...]] = {
    "manufacturing": (
        (
            "Supplier Quality Director",
            "signed supplier qualification checklist and sample inspection report",
            2_500_000,
            "sample defects exceed 100 ppm or a required PPAP element is missing",
        ),
        (
            "Commercial Director",
            "approved RFQ revision and contribution-margin worksheet",
            10_000_000,
            "discount exceeds 8 percent or projected gross margin falls below 22 percent",
        ),
        (
            "Plant Engineering Director",
            "technical validation report, tooling plan, and dated capacity model",
            7_500_000,
            "tooling lead time exceeds 12 weeks or planned utilization exceeds 85 percent",
        ),
        (
            "Supply Chain Risk Lead",
            "material traceability certificate and supplier continuity assessment",
            5_000_000,
            "a sole-source material lacks a qualified alternate",
        ),
        (
            "Revenue Operations Lead",
            "RFQ revision history, meeting evidence, and current purchase-order forecast",
            2_500_000,
            "CRM stage advances without evaluator acceptance or the close date lacks buyer evidence",
        ),
    ),
    "construction": (
        (
            "Preconstruction Director",
            "qualification form, site-walk record, and signed bid checklist",
            25_000_000,
            "scope drawings conflict or an addendum remains unacknowledged",
        ),
        (
            "Project Executive",
            "estimate reconciliation, value-engineering log, and contingency schedule",
            50_000_000,
            "bid contingency falls below 3 percent or an allowance exceeds its approved range",
        ),
        (
            "Operations Director",
            "staffing plan, critical-path schedule, and subcontractor capacity letters",
            100_000_000,
            "planned labor loading exceeds available supervision or schedule float drops below five days",
        ),
        (
            "Safety and Bonding Director",
            "current EMR, safety plan, bid bond, and surety capacity letter",
            10_000_000,
            "EMR exceeds 1.0, bond capacity is insufficient, or a reportable safety event is unresolved",
        ),
        (
            "Bid Operations Lead",
            "tender addenda log, owner correspondence, and current award forecast",
            5_000_000,
            "the bid record omits an addendum or an award date is not tied to owner evidence",
        ),
    ),
    "commercial_insurance": (
        (
            "Placement Director",
            "signed submission, exposure schedule, and five-year loss runs",
            25_000_000,
            "loss data is older than 90 days or a material exposure is unclassified",
        ),
        (
            "Broking Director",
            "quote comparison, commission disclosure, and client order",
            10_000_000,
            "premium variance exceeds 10 percent or a coverage limit differs from the client order",
        ),
        (
            "Market Relations Lead",
            "underwriter appetite confirmation and complete market-selection record",
            15_000_000,
            "fewer than two viable markets respond or an incumbent term is not comparable",
        ),
        (
            "Insurance Compliance Officer",
            "sanctions screening, surplus-lines review, and disclosure checklist",
            5_000_000,
            "a required disclosure is unsigned or a placement crosses an unapproved jurisdiction",
        ),
        (
            "Placement Operations Lead",
            "submission version history, binder checklist, and policy issuance tracker",
            5_000_000,
            "the CRM stage precedes client order or a binder term lacks source evidence",
        ),
    ),
    "consulting": (
        (
            "Engagement Partner",
            "signed discovery summary, scope assumptions, and executive sponsor confirmation",
            25_000_000,
            "a workstream lacks an accountable buyer or success measure",
        ),
        (
            "Commercial Partner",
            "pricing model, margin worksheet, and approved statement of work",
            15_000_000,
            "discount exceeds 10 percent or engagement margin falls below 35 percent",
        ),
        (
            "Resourcing Director",
            "named staffing plan, availability checks, and delivery calendar",
            10_000_000,
            "a critical role is unstaffed within 30 days of the proposed start",
        ),
        (
            "Risk and Security Lead",
            "data-flow assessment, security responses, and subcontractor disclosure",
            5_000_000,
            "restricted data leaves an approved environment or a subcontractor is undisclosed",
        ),
        (
            "Sales Operations Lead",
            "scope version history, procurement correspondence, and dated approval forecast",
            5_000_000,
            "forecast stage advances before scope and procurement gates are evidenced",
        ),
    ),
    "legal_services": (
        (
            "Conflicts Counsel",
            "completed conflicts search, affiliate list, and written clearance",
            5_000_000,
            "a potential conflict lacks a documented waiver or clearance",
        ),
        (
            "Relationship Partner",
            "matter scope, client decision record, and signed engagement letter",
            10_000_000,
            "work begins before engagement terms and responsible partner approval",
        ),
        (
            "Pricing Committee Chair",
            "fee proposal, staffing pyramid, and realization analysis",
            7_500_000,
            "discount exceeds 12 percent or expected realization falls below 80 percent",
        ),
        (
            "Information Governance Counsel",
            "security questionnaire, privilege protocol, and data-retention schedule",
            5_000_000,
            "privileged material is routed through an unapproved system",
        ),
        (
            "Legal Sales Operations Lead",
            "panel status, RFP version history, and matter-opening forecast",
            2_500_000,
            "CRM stage advances before conflicts clearance or panel eligibility is confirmed",
        ),
    ),
    "corporate_banking": (
        (
            "Relationship Credit Officer",
            "borrower request, ownership chart, and current financial statements",
            500_000_000,
            "leverage exceeds the approved screen or financials are older than 120 days",
        ),
        (
            "Credit Committee Chair",
            "underwriting memorandum, risk rating, and repayment sensitivity",
            1_000_000_000,
            "any exception breaches delegated credit authority or debt-service coverage falls below 1.25",
        ),
        (
            "Treasury Product Director",
            "cash-flow analysis, implementation plan, and pricing schedule",
            250_000_000,
            "fee waiver exceeds 15 percent or implementation depends on an unsupported jurisdiction",
        ),
        (
            "KYC and AML Officer",
            "beneficial ownership certification, sanctions results, and source-of-funds review",
            100_000_000,
            "a beneficial owner is unverified or a screening alert remains open",
        ),
        (
            "Banking Sales Operations Lead",
            "committee calendar, covenant tracker, and dated closing forecast",
            100_000_000,
            "forecast stage advances before KYC, credit, or documentation evidence is recorded",
        ),
    ),
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_json(row) + "\n" for row in rows))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n")


def _normalize_privacy_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[^\W_]+", normalized, re.UNICODE))


def _normalized_privacy_contains(value: str, needle: str) -> bool:
    return f" {needle} " in f" {value} "


def _privacy_phone_digits(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def _is_reserved_privacy_phone(value: str) -> bool:
    digits = _privacy_phone_digits(value)
    if len(digits) != 10:
        return False
    exchange, subscriber = digits[3:6], digits[6:]
    return exchange == "555" and 100 <= int(subscriber) <= 199


def _privacy_domain_host(value: str) -> str | None:
    candidate = value.rstrip(".,;:!?)]}")
    parsed = urlsplit(candidate if "://" in candidate else f"//{candidate}")
    return parsed.hostname.casefold() if parsed.hostname else None


def _is_reserved_privacy_domain(host: str) -> bool:
    normalized = host.rstrip(".").casefold()
    return any(
        normalized == suffix[1:] or normalized.endswith(suffix)
        for suffix in RESERVED_DOMAIN_SUFFIXES
    )


def _privacy_text_errors(
    value: str,
    location: str,
    forbidden_phrases: tuple[tuple[str, str], ...],
    forbidden_entities: tuple[tuple[str, str], ...],
    field_name: str = "",
) -> list[str]:
    errors: list[str] = []
    normalized = _normalize_privacy_text(value)
    for original, phrase in forbidden_phrases:
        if _normalized_privacy_contains(normalized, phrase):
            errors.append(f"privacy_forbidden_phrase={location}:{original}")
    for original, entity in forbidden_entities:
        if _normalized_privacy_contains(normalized, entity):
            errors.append(f"privacy_entity_collision={location}:{original}")
    for match in PRIVACY_DOMAIN_PATTERN.finditer(value):
        host = _privacy_domain_host(match.group())
        tld = host.rsplit(".", 1)[-1] if host and "." in host else host
        explicit = (
            "://" in match.group()
            or (match.start() > 0 and value[match.start() - 1] == "@")
            or field_name in PRIVACY_DOMAIN_KEYS
        )
        if (
            host
            and (explicit or tld in PRIVACY_COMMON_TLDS)
            and not _is_reserved_privacy_domain(host)
        ):
            errors.append(f"privacy_non_reserved_domain={location}:{host}")
    for match in PRIVACY_PHONE_PATTERN.finditer(value):
        if not _is_reserved_privacy_phone(match.group()):
            errors.append(f"privacy_non_reserved_phone={location}:{match.group()}")
    if field_name in PRIVACY_PHONE_KEYS:
        digits = _privacy_phone_digits(value)
        if len(digits) == 10 and not _is_reserved_privacy_phone(value):
            errors.append(f"privacy_non_reserved_phone={location}:{value}")
    return errors


def _privacy_value_errors(
    value: Any,
    location: str,
    forbidden_phrases: tuple[tuple[str, str], ...],
    forbidden_entities: tuple[tuple[str, str], ...],
    source_reference: bool = False,
    field_name: str = "",
) -> list[str]:
    if source_reference:
        return []
    if isinstance(value, dict):
        errors: list[str] = []
        for key, item in value.items():
            key_text = str(key)
            errors.extend(
                _privacy_value_errors(
                    item,
                    f"{location}.{key_text}",
                    forbidden_phrases,
                    forbidden_entities,
                    key_text.casefold() in PRIVACY_SOURCE_KEYS,
                    key_text.casefold(),
                )
            )
        return errors
    if isinstance(value, (list, tuple)):
        nested_errors: list[str] = []
        for index, item in enumerate(value):
            nested_errors.extend(
                _privacy_value_errors(
                    item,
                    f"{location}[{index}]",
                    forbidden_phrases,
                    forbidden_entities,
                    False,
                    field_name,
                )
            )
        return nested_errors
    if isinstance(value, str):
        return _privacy_text_errors(
            value,
            location,
            forbidden_phrases,
            forbidden_entities,
            field_name,
        )
    return []


def _privacy_file_values(root: Path) -> Iterable[tuple[str, Any]]:
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or path.suffix.casefold() not in PRIVACY_TEXT_EXTENSIONS
            or "reviews" in path.parts
            or any(part.casefold() in PRIVACY_SOURCE_PATH_PARTS for part in path.parts)
        ):
            continue
        location = path.relative_to(root).as_posix()
        text = path.read_text(errors="ignore")
        if path.suffix.casefold() == ".json":
            try:
                yield location, json.loads(text)
            except json.JSONDecodeError:
                yield location, text
            continue
        if path.suffix.casefold() == ".jsonl":
            for line_number, line in enumerate(text.splitlines(), 1):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    value = line
                yield f"{location}:{line_number}", value
            continue
        yield location, text


def _privacy_identity_errors(
    worlds: Iterable[dict[str, Any]], shared_seller_actor_ids: Iterable[str]
) -> list[str]:
    errors: list[str] = []
    shared_ids = set(shared_seller_actor_ids)
    for world in worlds:
        identities: dict[str, list[dict[str, Any]]] = {}
        for actor in world.get("actors", ()):
            if not isinstance(actor, dict) or not isinstance(
                actor.get("display_name"), str
            ):
                continue
            name = _normalize_privacy_text(actor["display_name"])
            if name:
                identities.setdefault(name, []).append(actor)
        for name, actors in identities.items():
            if len(actors) < 2:
                continue
            actor_ids = {actor.get("actor_id") for actor in actors}
            if len(actor_ids) == 1 and all(
                actor.get("kind") == "seller" for actor in actors
            ):
                continue
            if all(
                actor.get("kind") == "seller" and actor.get("actor_id") in shared_ids
                for actor in actors
            ):
                continue
            errors.append(
                f"privacy_duplicate_person={world.get('world_id', 'unknown')}:{name}"
            )
    return errors


def _validate_synthetic_privacy(
    root: Path,
    worlds: Iterable[dict[str, Any]],
    shared_documents: Iterable[dict[str, Any]] = (),
    *,
    forbidden_phrases: Iterable[str] = (),
    forbidden_entities: Iterable[str] = (),
    shared_seller_actor_ids: Iterable[str] = (),
) -> list[str]:
    phrase_pairs = tuple(
        (value, normalized)
        for value in forbidden_phrases
        if (normalized := _normalize_privacy_text(value))
    )
    entity_pairs = tuple(
        (value, normalized)
        for value in forbidden_entities
        if (normalized := _normalize_privacy_text(value))
    )
    world_rows = list(worlds)
    errors = _privacy_identity_errors(world_rows, shared_seller_actor_ids)
    for index, world in enumerate(world_rows):
        errors.extend(
            _privacy_value_errors(
                world,
                f"worlds[{index}]",
                phrase_pairs,
                entity_pairs,
            )
        )
    for index, document in enumerate(shared_documents):
        errors.extend(
            _privacy_value_errors(
                document,
                f"shared_documents[{index}]",
                phrase_pairs,
                entity_pairs,
            )
        )
    for location, value in _privacy_file_values(root):
        errors.extend(
            _privacy_value_errors(value, location, phrase_pairs, entity_pairs)
        )
    return list(dict.fromkeys(errors))


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _date_text(start: date, offset: int) -> str:
    return (start + timedelta(days=offset)).isoformat()


def _stable_seed(*parts: object) -> int:
    payload = "|".join(str(part) for part in parts).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _opaque_id(prefix: str, *parts: object) -> str:
    payload = "|".join(str(part) for part in parts).encode()
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def _side_effect_id(prefix: str, world_id: str, key: str) -> str:
    payload = f"{world_id}:{key}".encode()
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def _timestamp(day: date | str, hour: int = 9) -> str:
    value = date.fromisoformat(day) if isinstance(day, str) else day
    return f"{value.isoformat()}T{hour:02d}:00:00Z"


def _checksum(body: str) -> str:
    return f"sha256:{hashlib.sha256(body.encode()).hexdigest()}"


def _file_checksum(path: Path | Traversable) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _money(minor_units: int, currency: str) -> str:
    major, minor = divmod(minor_units, 100)
    return f"{currency} {major:,}.{minor:02d}"


def _actor_role(actor: dict[str, Any]) -> str:
    return actor["role_tags"][0]


def _actor_label(actor: dict[str, Any]) -> str:
    return actor["attributes"]["job_title"]


def _person_name(
    dataset_seed: int, vertical_index: int, family_index: int, role_index: int
) -> str:
    seed = _stable_seed(dataset_seed, vertical_index, family_index, role_index)
    middle = chr(ord("A") + role_index % 26)
    return f"{FIRST_NAMES[seed % len(FIRST_NAMES)]} {middle}. {LAST_NAMES[(seed // len(FIRST_NAMES)) % len(LAST_NAMES)]}"


def _company_name(world_id: str) -> str:
    seed = _stable_seed(world_id, "company")
    first = COMPANY_WORDS[seed % len(COMPANY_WORDS)]
    second = COMPANY_WORDS[(seed // len(COMPANY_WORDS)) % len(COMPANY_WORDS)]
    if first == second:
        second = COMPANY_WORDS[(COMPANY_WORDS.index(second) + 1) % len(COMPANY_WORDS)]
    return f"{first} {second} {COMPANY_SUFFIXES[(seed // (len(COMPANY_WORDS) ** 2)) % len(COMPANY_SUFFIXES)]}"


def _actor_id(role: str, vertical_index: int, family_index: int) -> str:
    return f"{role}-{vertical_index:02d}-{family_index:02d}"


def _world_split(vertical_index: int, family_index: int) -> str:
    return SPLITS[(vertical_index + family_index) % len(SPLITS)]


def _checkpoint_days(duration: int, count: int, seed: int) -> list[int]:
    randomizer = random.Random(seed)
    values = [0]
    for index in range(1, count - 1):
        base = round(index * duration / (count - 1))
        values.append(max(values[-1] + 1, base + randomizer.randint(-4, 4)))
    values.append(duration)
    return values


def _outcome(family: str, variant: int) -> str:
    return OUTCOME_BY_FAMILY[family][variant]


def _variant_name(family: str, variant: int) -> str:
    return VARIANT_NAMES[family][variant]


def _family_description(family: str, variant_name: str) -> str:
    descriptions = {
        "champion_exit": f"The initial champion leaves during the cycle, with a {variant_name.replace('_', ' ')}.",
        "late_stakeholder": f"A high-authority stakeholder enters late and is {variant_name}.",
        "budget_shock": f"The buyer's budget changes through {variant_name.replace('_', ' ')}.",
        "requirements_change": f"The requirements change to remain {variant_name.replace('_', ' ')}.",
        "competition": f"An incumbent or competitor moves in a {variant_name.replace('_', ' ')} way.",
        "external_event": f"An external event is {variant_name.replace('_', ' ')}.",
    }
    return descriptions[family]


def _outcome_reason(family: str, variant: str) -> str:
    return {
        "champion_exit": {
            "strong_handoff": "The named handoff preserved consensus and the buyer completed its approval path.",
            "weak_handoff": "The champion departed without a replacement and the incumbent retained the account.",
        },
        "late_stakeholder": {
            "supportive": "The late executive sponsor clarified the approval path and supported the purchase.",
            "blocking": "The late executive sponsor rejected the current priority and selected another approach.",
        },
        "budget_shock": {
            "reallocation": "A reduced allocation remained, but the buyer deferred a decision beyond the cycle.",
            "freeze": "The spending freeze moved consideration into a future planning cycle.",
        },
        "requirements_change": {
            "within_fit": "The revised requirement remained within the approved delivery plan and the buyer proceeded.",
            "out_of_fit": "The revised requirement exceeded the seller's supported scope and required disqualification.",
        },
        "competition": {
            "transparent": "The disclosed benchmark let the seller address the comparison and earn the award.",
            "hidden_influence": "Undisclosed incumbent influence redirected the award before a fair comparison occurred.",
        },
        "external_event": {
            "recoverable": "The disruption was recoverable, but its timing moved the decision beyond the buyer's required window.",
            "terminal": "The external event ended the buyer program with no approved restart date.",
        },
    }[family][variant]


def _actors(
    vertical: dict[str, Any],
    vertical_index: int,
    family_index: int,
    identity_seed: int,
    buyer_name: str,
    buyer_domain: str,
    buyer_org_id: str,
    seller_org_id: str,
    start_at: str,
) -> list[dict[str, Any]]:
    roles = (
        ("champion", "Champion", "contributor", "Business unit"),
        ("economic_buyer", "Economic buyer", "final_decider", "Executive office"),
        ("procurement", "Procurement lead", "approver", "Procurement"),
        ("technical_evaluator", "Domain evaluator", "contributor", "Operations"),
        ("finance", "Finance lead", "approver", "Finance"),
        ("executive_sponsor", "Executive sponsor", "final_decider", "Executive office"),
    )
    result: list[dict[str, Any]] = []
    for role_index, (role, job_title, authority, department) in enumerate(roles):
        name = _person_name(identity_seed, vertical_index, family_index, role_index)
        result.append(
            {
                "actor_id": _opaque_id("act", identity_seed, role),
                "kind": "buyer",
                "display_name": name,
                "organization_id": buyer_org_id,
                "role_tags": [role],
                "active_from": start_at,
                "email": f"{_slug(name)}@{buyer_domain}",
                "visibility": "public",
                "attributes": {
                    "job_title": job_title,
                    "authority_level": authority,
                    "department": department,
                },
            }
        )
    for role_index, role in enumerate(ROLES, start=10):
        name = _person_name(
            identity_seed, vertical_index + 10, family_index, role_index
        )
        result.append(
            {
                "actor_id": _opaque_id("act", identity_seed, role),
                "kind": "seller",
                "display_name": name,
                "organization_id": seller_org_id,
                "role_tags": [role],
                "active_from": start_at,
                "email": f"{role.replace('_', '.')}@{vertical['domain']}",
                "visibility": "internal_role_scoped",
                "visible_roles": list(ROLES),
                "attributes": {
                    "job_title": role.replace("_", " ").title(),
                    "authority_level": "approver"
                    if role == "sales_manager"
                    else "contributor",
                    "department": "Revenue operations" if role == "revops" else "Sales",
                },
            }
        )
    return result


def _causal_evidence(
    world: dict[str, Any],
    checkpoint: dict[str, Any],
    artifact_type: str,
    source_actor: dict[str, Any],
    recipient: dict[str, Any],
) -> str:
    if checkpoint["sequence"] <= world["intervention_sequence"]:
        return ""
    actors = {
        _actor_role(actor): actor
        for actor in world["actors"]
        if actor["kind"] == "buyer"
    }
    champion = actors["champion"]
    executive = actors["executive_sponsor"]
    evaluator = actors["technical_evaluator"]
    finance = actors["finance"]
    evidence = {
        "champion_exit": {
            "strong_handoff": f"An automated reply from {champion['display_name']} directs the {checkpoint['label'].replace('_', ' ')} questions to {actors['economic_buyer']['display_name']}, who confirms ownership.",
            "weak_handoff": f"An automated reply says {champion['display_name']} is no longer with {world['buyer_name']}; no replacement owner is named for the {checkpoint['label'].replace('_', ' ')} work.",
        },
        "late_stakeholder": {
            "supportive": f"{executive['display_name']}, the executive sponsor, joined the {checkpoint['label'].replace('_', ' ')} review and asked for a path to approval.",
            "blocking": f"{executive['display_name']}, the executive sponsor, joined the {checkpoint['label'].replace('_', ' ')} review and questioned whether the proposal fits the current priority.",
        },
        "budget_shock": {
            "reallocation": f"{finance['display_name']} confirmed that funds moved to another program, but a smaller allocation remains for the {checkpoint['label'].replace('_', ' ')} gate.",
            "freeze": f"{finance['display_name']} confirmed a spending hold through the next planning cycle, so the {checkpoint['label'].replace('_', ' ')} gate cannot receive approval now.",
        },
        "requirements_change": {
            "within_fit": f"{evaluator['display_name']} added a traceability requirement for the {checkpoint['label'].replace('_', ' ')} gate, and the seller's current delivery plan covers it.",
            "out_of_fit": f"{evaluator['display_name']} added a requirement for the {checkpoint['label'].replace('_', ' ')} gate that the seller's current delivery plan does not cover.",
        },
        "competition": {
            "transparent": f"{recipient['display_name']} disclosed that the incumbent is being benchmarked at the {checkpoint['label'].replace('_', ' ')} gate and requested a comparable response.",
            "hidden_influence": f"A buyer note references an incumbent offer affecting the {checkpoint['label'].replace('_', ' ')} decision, but the external meeting invite does not name the competitor.",
        },
        "external_event": {
            "recoverable": f"A synthetic industry bulletin reports a temporary disruption, and {recipient['display_name']} confirmed a workaround for the {checkpoint['label'].replace('_', ' ')} gate.",
            "terminal": f"A synthetic industry bulletin reports an event that pauses the buyer's {checkpoint['label'].replace('_', ' ')} program with no approved restart date.",
        },
    }
    return evidence[world["causal_family"]][world["variant"]]


def _artifact_body(
    artifact_type: str,
    vertical: dict[str, Any],
    world: dict[str, Any],
    index: int,
    channel_index: int,
    checkpoint: dict[str, Any],
    source_actor: dict[str, Any],
    recipient: dict[str, Any],
    evidence: str,
) -> str:
    template = channel_index % 3
    title = f"{vertical['label']} {checkpoint['label'].replace('_', ' ')} update"
    gate = checkpoint["label"].replace("_", " ")
    previous_gate = world["gates"][
        max(0, min(checkpoint["sequence"] - 1, len(world["gates"]) - 1))
    ].replace("_", " ")
    subject = f"{world['deal_name']} | {gate} review"
    source_name = source_actor["display_name"]
    source_role = _actor_label(source_actor)
    recipient_name = recipient["display_name"]
    recipient_role = _actor_label(recipient)
    amount = _money(world["amount_minor_units"], world["currency"])
    evidence_line = (
        evidence
        or f"The {previous_gate} record remains the latest confirmed source for this thread."
    )
    if artifact_type == "transcript":
        exchanges = (
            (
                f"{source_name} ({source_role}): Which evidence clears {gate}, and who accepts it?",
                f"{recipient_name} ({recipient_role}): I will confirm the owner after our {vertical['motion']} review.",
                f"{source_name} ({source_role}): We will carry the open item from {previous_gate} into the next meeting.",
            ),
            (
                f"{recipient_name} ({recipient_role}): The decision group needs the {gate} package tied to {amount}.",
                f"{source_name} ({source_role}): I will separate confirmed facts from assumptions and name each approval dependency.",
                f"{recipient_name} ({recipient_role}): Keep the {previous_gate} evidence linked so reviewers can trace the change.",
            ),
            (
                f"{source_name} ({source_role}): The {vertical['label']} sequence now moves from {previous_gate} to {gate}.",
                f"{recipient_name} ({recipient_role}): The date holds only if the listed owner closes the open evidence request.",
                f"{source_name} ({source_role}): I will record the commitment without advancing the CRM stage early.",
            ),
        )[template]
        return "\n".join(
            (
                f"# {title}",
                "",
                "- Channel: call transcript",
                f"- Date: {checkpoint['available_at']}",
                f"- Subject: {subject}",
                "",
                *exchanges,
                f"{recipient_name} ({recipient_role}): {evidence_line}",
            )
        )
    if artifact_type == "email":
        paragraphs = (
            f"I captured the {gate} owner, the remaining evidence request, and the next step for the {vertical['motion']}. Please confirm the decision group and target date.",
            f"The attached thread carries forward the {previous_gate} facts. For {gate}, please identify the approver for the {amount} request and any condition still open.",
            f"Today we agreed not to advance beyond {gate} until the source record is reconciled. Please reply with corrections to the owner, amount, or timing.",
        )
        return "\n".join(
            (
                f"# {title}",
                "",
                f"- From: {source_actor['email']}",
                f"- To: {recipient['email']}",
                f"- Date: {checkpoint['available_at']}",
                f"- Subject: {subject}",
                "",
                f"Hi {recipient_name},",
                "",
                paragraphs[template],
                evidence_line,
                "",
                f"Regards,\n{source_name}",
            )
        )
    if artifact_type == "internal_chat":
        chat_exchanges = (
            (
                f"{source_name}: Reconcile the {gate} stage before forecast review.",
                f"{recipient_name}: I will trace the owner and source history, then preserve the prior values.",
            ),
            (
                f"{source_name}: The {amount} amount is unchanged, but {gate} still has an open dependency.",
                f"{recipient_name}: I will flag the stale projection and avoid overwriting the audit history.",
            ),
            (
                f"{source_name}: Carry the {previous_gate} commitment into the {gate} checkpoint.",
                f"{recipient_name}: I will attach the evidence and route any exception to the authorized approver.",
            ),
        )[template]
        return "\n".join(
            (
                f"# {title}",
                "",
                "- Channel: internal chat",
                f"- Date: {checkpoint['available_at']}",
                f"- Participants: {source_name}, {recipient_name}",
                "",
                *chat_exchanges,
                f"{source_name}: {evidence_line}",
            )
        )
    if artifact_type == "document":
        headings = ("Decision record", "Review package", "Approval brief")
        focuses = (
            f"Document the accountable owner and acceptance evidence for {gate}.",
            f"Reconcile the {previous_gate} source record before presenting the {amount} package.",
            f"Separate committed terms, unresolved questions, and approval conditions for {gate}.",
        )
        lines = [
            f"# {title}",
            "",
            f"- Owner: {source_name}",
            f"- Audience: {recipient_name}",
            f"- Effective date: {checkpoint['available_at']}",
            f"- Motion: {vertical['motion']}",
            "",
            f"## {headings[template]}",
            "",
            focuses[template],
            "",
            "## Open items",
            "",
            f"- Confirm the accountable owner for {gate}.",
            f"- Link the current evidence to the {vertical['label']} gate record.",
            "- Keep any commercial exception within the approval matrix.",
            f"- Evidence update: {evidence_line}",
        ]
        if channel_index % 4 == 1:
            primary = world["amount_minor_units"] * 82 // 100
            secondary = world["amount_minor_units"] - primary
            lines.extend(
                (
                    "",
                    "## Pricing",
                    "",
                    f"- Primary scope: {_money(primary, world['currency'])} ({primary} minor units)",
                    f"- Delivery and contingency: {_money(secondary, world['currency'])} ({secondary} minor units)",
                    f"- Total: {amount} ({world['amount_minor_units']} minor units)",
                )
            )
        return "\n".join(lines)
    if artifact_type == "web_news":
        leads = (
            f"A synthetic market brief tracks capacity and approval conditions relevant to {vertical['buyer_industry']}.",
            f"A synthetic trade bulletin reviews timing pressure around the {gate} stage of a {vertical['motion']}.",
            f"A synthetic company notice reports a planning update that may affect the {amount} initiative.",
        )
        artifact_id = _opaque_id("artifact", world["world_id"], index)
        return "\n".join(
            (
                f"# {title}",
                "",
                "- Publisher: EDLB Synthetic Wire",
                f"- Published: {checkpoint['available_at']}",
                f"- URL: https://edlb.example/signals/{artifact_id}",
                "",
                leads[template],
                evidence_line,
                "Confirm the signal against buyer evidence before changing the forecast.",
            )
        )
    raise ValueError(artifact_type)


def _artifact_record(
    artifact_type: str,
    index: int,
    channel_index: int,
    vertical: dict[str, Any],
    world: dict[str, Any],
    checkpoint: dict[str, Any],
    source_actor: dict[str, Any],
    recipient: dict[str, Any],
    defect: dict[str, Any] | None,
) -> dict[str, Any]:
    artifact_id = _opaque_id("artifact", world["world_id"], index)
    extension = "json" if artifact_type in {"crm", "calendar"} else "md"
    path = f"artifacts/{artifact_type}/{artifact_id}.{extension}"
    evidence = _causal_evidence(
        world, checkpoint, artifact_type, source_actor, recipient
    )
    gate_index = min(checkpoint["sequence"], len(world["gates"]) - 1)
    gate = world["gates"][gate_index]
    if artifact_type == "crm":
        field = (
            defect["field"]
            if defect
            else ("next_step", "stage", "close_date")[channel_index % 3]
        )
        observed = (
            defect["observed_value"]
            if defect
            else (f"confirm {gate} owner", gate, world["forecast_close_date"])[
                channel_index % 3
            ]
        )
        body_value = {
            "object": "opportunity",
            "record_id": world["deal_id"],
            "account": world["buyer_name"],
            "stage": gate,
            "amount_minor_units": world["amount_minor_units"],
            "currency": world["currency"],
            "close_date": world["forecast_close_date"],
            "next_step": f"confirm {gate} evidence and owner",
            "owner": source_actor["email"],
            "owner_role": _actor_label(source_actor),
            "observed_field": field,
            "observed_value": observed,
            "last_modified": checkpoint["available_at"],
            "checkpoint_sequence": checkpoint["sequence"],
            "verification_basis": evidence
            or f"Reconcile against the {gate} meeting and buyer reply before advancing.",
        }
        body = json.dumps(body_value, ensure_ascii=False, sort_keys=True, indent=2)
    elif artifact_type == "calendar":
        agendas = (
            f"Review {gate} evidence, owner, and next decision.",
            f"Carry forward the prior commitment and resolve the {_money(world['amount_minor_units'], world['currency'])} approval path.",
            f"Confirm the {vertical['label']} gate sequence, stakeholder attendance, and source record.",
        )
        body_value = {
            "subject": subject_for_calendar(world, checkpoint),
            "start": checkpoint["available_at"],
            "end": _timestamp(checkpoint["date"], 10),
            "organizer": source_actor["email"],
            "attendees": [source_actor["email"], recipient["email"]],
            "location": f"https://edlb.example/meetings/{artifact_id}",
            "status": "tentative" if channel_index % 4 == 0 else "confirmed",
            "agenda": f"{agendas[channel_index % 3]} Evidence update: {evidence or 'none confirmed'}",
        }
        body = json.dumps(body_value, ensure_ascii=False, sort_keys=True, indent=2)
    else:
        body = _artifact_body(
            artifact_type,
            vertical,
            world,
            index,
            channel_index,
            checkpoint,
            source_actor,
            recipient,
            evidence,
        )
    if artifact_type == "crm":
        kind = "crm_history" if channel_index % 3 == 2 else "crm_record"
    elif artifact_type == "document":
        kind = ("proposal", "quote", "contract", "diligence_document")[
            channel_index % 4
        ]
    elif artifact_type == "web_news":
        kind = "web_page" if channel_index % 2 == 0 else "news_item"
    else:
        kind = ARTIFACT_KINDS[artifact_type]
    record: dict[str, Any] = {
        "artifact_id": artifact_id,
        "world_id": world["world_id"],
        "kind": kind,
        "title": f"{vertical['label']} {gate.replace('_', ' ')} update {channel_index + 1}",
        "created_at": checkpoint["available_at"],
        "available_at": checkpoint["available_at"],
        "visibility": "public"
        if artifact_type == "web_news"
        else "role_scoped"
        if artifact_type in {"internal_chat", "crm"}
        else "agent_visible",
        "source_actor_ids": [source_actor["actor_id"]],
        "recipient_actor_ids": [recipient["actor_id"]],
        "thread_id": _opaque_id(
            "thread", world["world_id"], artifact_type, channel_index % 3
        ),
        "version": 1 + channel_index // 4 if artifact_type == "document" else 1,
        "content": {
            "mime_type": "application/json" if extension == "json" else "text/markdown",
            "body": body,
            "language": "en",
            "source_uri": path,
        },
        "checksum": _checksum(body),
        "provenance": {
            "synthetic_only": True,
            "source_type": "derived_projection"
            if artifact_type == "crm"
            else "generated_template",
            "generator": "edlb.generate",
            "generator_version": DATASET_VERSION,
            "license": "CC-BY-4.0",
        },
    }
    if artifact_type == "internal_chat":
        record["visible_roles"] = list(
            dict.fromkeys((_actor_role(source_actor), _actor_role(recipient)))
        )
    elif artifact_type == "crm":
        record["visible_roles"] = list(ROLES)
    if artifact_type == "crm":
        record["record_id"] = world["deal_id"]
    return record


def subject_for_calendar(world: dict[str, Any], checkpoint: dict[str, Any]) -> str:
    return f"{world['deal_name']} checkpoint, {checkpoint['label']}"


def _build_world(
    vertical_index: int, family_index: int, variant: int, dataset_seed: int
) -> dict[str, Any]:
    vertical = VERTICALS[vertical_index]
    family = FAMILIES[family_index]
    variant_name = _variant_name(family, variant)
    duration = 210 + vertical_index * 11 + family_index * 17
    checkpoint_count = 8 + ((vertical_index + family_index) % 5)
    start = date(2025, 1, 6) + timedelta(days=vertical_index * 13 + family_index * 7)
    pair_seed = _stable_seed(dataset_seed, vertical_index, family_index) % (2**63 - 1)
    pair_id = _opaque_id("pair", pair_seed, "pair")
    world_id = _opaque_id("world", pair_seed, variant)
    checkpoint_days = _checkpoint_days(duration, checkpoint_count, pair_seed)
    checkpoints = [
        {
            "checkpoint_id": _opaque_id("checkpoint", world_id, index),
            "sequence": index,
            "day": day,
            "date": _date_text(start, day),
            "available_at": _timestamp(_date_text(start, day)),
            "label": vertical["gates"][min(index, len(vertical["gates"]) - 1)],
            "status": "pending" if index else "active",
        }
        for index, day in enumerate(checkpoint_days)
    ]
    buyer_name = _company_name(world_id)
    buyer_domain = f"{_slug(buyer_name)}.example"
    buyer_org_id = _opaque_id("org", world_id, "buyer")
    seller_org_id = _opaque_id("org", DATASET_SEED, "seller", vertical_index)
    identity_seed = _stable_seed(world_id, "identities")
    actors = _actors(
        vertical,
        vertical_index,
        family_index,
        identity_seed,
        buyer_name,
        buyer_domain,
        buyer_org_id,
        seller_org_id,
        _timestamp(start),
    )
    base_amount, family_increment = AMOUNT_MINOR_UNITS[vertical["id"]]
    amount_minor_units = base_amount + family_index * family_increment
    forecast_close_date = _date_text(start, min(duration, checkpoint_days[-2]))
    intervention_sequence = max(1, checkpoint_count // 2)
    defects = [
        {
            "defect_id": _opaque_id("defect", pair_seed, 1),
            "field": "stage",
            "observed_value": "proposal",
            "truth_value": "discovery",
            "origin": "manual update entered before the evaluator confirmed fit",
            "evidence_role": "technical_evaluator",
        },
        {
            "defect_id": _opaque_id("defect", pair_seed, 2),
            "field": "close_date",
            "observed_value": forecast_close_date,
            "truth_value": _date_text(start, checkpoint_days[-1]),
            "origin": "copied forecast from an earlier quarter",
            "evidence_role": "champion",
        },
        {
            "defect_id": _opaque_id("defect", pair_seed, 3),
            "field": "next_step",
            "observed_value": "send proposal",
            "truth_value": "confirm decision group and approval path",
            "origin": "stale next-step text left after a meeting moved",
            "evidence_role": "economic_buyer",
        },
    ]
    return {
        "world_id": world_id,
        "pair_id": pair_id,
        "deal_id": _opaque_id("deal", world_id),
        "vertical": vertical["id"],
        "seller_id": vertical["seller_id"],
        "seller_org_id": seller_org_id,
        "seller_name": vertical["seller_name"],
        "buyer_org_id": buyer_org_id,
        "buyer_name": buyer_name,
        "buyer_domain": buyer_domain,
        "buyer_industry": vertical["buyer_industry"],
        "deal_name": f"{buyer_name} {vertical['motion']}",
        "motion": vertical["motion"],
        "currency": vertical["currency"],
        "amount_minor_units": amount_minor_units,
        "forecast_close_date": forecast_close_date,
        "causal_family": family,
        "variant": variant_name,
        "variant_index": variant,
        "split": _world_split(vertical_index, family_index),
        "seed": pair_seed,
        "start_date": start.isoformat(),
        "start_at": _timestamp(start),
        "end_at": _timestamp(_date_text(start, duration), 17),
        "duration_days": duration,
        "checkpoint_count": checkpoint_count,
        "checkpoint_ids": [checkpoint["checkpoint_id"] for checkpoint in checkpoints],
        "intervention_checkpoint_id": checkpoints[intervention_sequence][
            "checkpoint_id"
        ],
        "intervention_sequence": intervention_sequence,
        "checkpoints": checkpoints,
        "actors": actors,
        "defects": defects,
        "reference_outcome": _outcome(family, variant),
        "outcome_reason": _outcome_reason(family, variant_name),
        "family_description": _family_description(family, variant_name),
        "gates": list(vertical["gates"]),
        "artifact_counts": ARTIFACT_COUNTS.copy(),
    }


def _build_events(
    world: dict[str, Any], artifacts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    visible: list[dict[str, Any]] = [
        {
            "event_id": _opaque_id("event", world["world_id"], "meeting-booked"),
            "world_id": world["world_id"],
            "sequence": 0,
            "kind": "meeting_booked",
            "effective_at": world["start_at"],
            "recorded_at": world["start_at"],
            "available_at": world["start_at"],
            "actor_ids": [
                world["actors"][0]["actor_id"],
                world["actors"][6]["actor_id"],
            ],
            "visibility": "agent_visible",
            "channel": "calendar",
            "payload": {
                "checkpoint_id": world["checkpoint_ids"][0],
                "subject": "first meeting booked",
            },
        }
    ]
    kind_by_artifact = {
        "call_transcript": ("meeting_held", "call_transcript"),
        "email": ("message_sent", "email"),
        "internal_chat": ("message_sent", "internal_chat"),
        "crm_record": ("crm_projection_changed", "crm"),
        "crm_history": ("crm_projection_changed", "crm"),
        "calendar_event": ("meeting_booked", "calendar"),
        "proposal": ("document_created", "document"),
        "quote": ("document_created", "document"),
        "contract": ("document_revised", "document"),
        "diligence_document": ("document_revised", "document"),
        "web_page": ("external_signal_published", "web_signal"),
        "news_item": ("external_signal_published", "web_signal"),
    }
    for sequence, artifact in enumerate(
        sorted(artifacts, key=lambda item: (item["available_at"], item["artifact_id"])),
        1,
    ):
        kind, channel = kind_by_artifact[artifact["kind"]]
        event = {
            "event_id": _opaque_id("event", world["world_id"], "artifact", sequence),
            "world_id": world["world_id"],
            "sequence": sequence,
            "kind": kind,
            "effective_at": artifact["created_at"],
            "recorded_at": artifact["created_at"],
            "available_at": artifact["available_at"],
            "actor_ids": list(
                dict.fromkeys(
                    artifact.get("source_actor_ids", [])
                    + artifact.get("recipient_actor_ids", [])
                )
            ),
            "artifact_ids": [artifact["artifact_id"]],
            "visibility": "role_scoped"
            if artifact["visibility"] == "role_scoped"
            else "agent_visible",
            "channel": channel,
            "payload": {
                "title": artifact["title"],
                "source_uri": artifact["content"]["source_uri"],
            },
        }
        if event["visibility"] == "role_scoped":
            event["visible_roles"] = artifact["visible_roles"]
        visible.append(event)
    family_event_day = world["checkpoints"][world["intervention_sequence"]]["day"]
    release_sequence = min(
        world["intervention_sequence"] + 1, world["checkpoint_count"] - 1
    )
    release_checkpoint = world["checkpoints"][release_sequence]
    family_event_id = _opaque_id("event", world["world_id"], "causal-intervention")
    event_kind = {
        "champion_exit": "stakeholder_departed",
        "late_stakeholder": "stakeholder_joined",
        "budget_shock": "budget_changed",
        "requirements_change": "requirement_changed",
        "competition": "external_signal_published",
        "external_event": "external_signal_published",
    }[world["causal_family"]]
    buyer = {
        _actor_role(actor): actor
        for actor in world["actors"]
        if actor["kind"] == "buyer"
    }
    observable = {
        "champion_exit": {
            "strong_handoff": {
                "stakeholder_actor_id": buyer["champion"]["actor_id"],
                "change": "departed",
                "handoff_actor_ids": [buyer["economic_buyer"]["actor_id"]],
                "source": "buyer_automatic_reply",
            },
            "weak_handoff": {
                "stakeholder_actor_id": buyer["champion"]["actor_id"],
                "change": "departed",
                "handoff_actor_ids": [],
                "source": "buyer_automatic_reply",
            },
        },
        "late_stakeholder": {
            "supportive": {
                "stakeholder_actor_id": buyer["executive_sponsor"]["actor_id"],
                "change": "joined_decision_group",
                "stated_position": "requested_approval_path",
                "source": "meeting_record",
            },
            "blocking": {
                "stakeholder_actor_id": buyer["executive_sponsor"]["actor_id"],
                "change": "joined_decision_group",
                "stated_position": "questioned_current_priority",
                "source": "meeting_record",
            },
        },
        "budget_shock": {
            "reallocation": {
                "stakeholder_actor_id": buyer["finance"]["actor_id"],
                "budget_status": "reduced_allocation_available",
                "review_window": "current_cycle",
                "source": "finance_reply",
            },
            "freeze": {
                "stakeholder_actor_id": buyer["finance"]["actor_id"],
                "budget_status": "spending_hold",
                "review_window": "next_planning_cycle",
                "source": "finance_reply",
            },
        },
        "requirements_change": {
            "within_fit": {
                "stakeholder_actor_id": buyer["technical_evaluator"]["actor_id"],
                "requirement": "extended_traceability",
                "seller_coverage": "available_in_current_plan",
                "source": "validation_record",
            },
            "out_of_fit": {
                "stakeholder_actor_id": buyer["technical_evaluator"]["actor_id"],
                "requirement": "extended_traceability",
                "seller_coverage": "not_in_current_plan",
                "source": "validation_record",
            },
        },
        "competition": {
            "transparent": {
                "stakeholder_actor_id": buyer["procurement"]["actor_id"],
                "signal": "incumbent_benchmark_disclosed",
                "disclosure_channel": "buyer_meeting",
                "source": "buyer_record",
            },
            "hidden_influence": {
                "stakeholder_actor_id": buyer["procurement"]["actor_id"],
                "signal": "incumbent_offer_referenced",
                "disclosure_channel": "internal_buyer_note",
                "source": "buyer_record",
            },
        },
        "external_event": {
            "recoverable": {
                "stakeholder_actor_id": buyer["executive_sponsor"]["actor_id"],
                "signal": "temporary_industry_disruption",
                "restart_status": "workaround_confirmed",
                "source": "synthetic_bulletin",
            },
            "terminal": {
                "stakeholder_actor_id": buyer["executive_sponsor"]["actor_id"],
                "signal": "buyer_program_paused",
                "restart_status": "no_approved_date",
                "source": "synthetic_bulletin",
            },
        },
    }[world["causal_family"]][world["variant"]]
    visible.append(
        {
            "event_id": _opaque_id(
                "event", world["world_id"], "observable-intervention"
            ),
            "world_id": world["world_id"],
            "sequence": 0,
            "kind": event_kind,
            "effective_at": _timestamp(
                _date_text(date.fromisoformat(world["start_date"]), family_event_day)
            ),
            "recorded_at": _timestamp(
                _date_text(
                    date.fromisoformat(world["start_date"]), family_event_day + 1
                )
            ),
            "available_at": release_checkpoint["available_at"],
            "actor_ids": [observable["stakeholder_actor_id"]],
            "visibility": "agent_visible",
            "channel": "web_signal"
            if world["causal_family"] in {"competition", "external_event"}
            else "email",
            "payload": {
                **observable,
                "checkpoint_id": release_checkpoint["checkpoint_id"],
                "lane_effects": LANE_EFFECTS[world["causal_family"]][world["variant"]],
            },
        }
    )
    visible.append(
        {
            "event_id": _opaque_id("event", world["world_id"], "cycle-horizon"),
            "world_id": world["world_id"],
            "sequence": 0,
            "kind": "workflow_gate_completed",
            "effective_at": world["end_at"],
            "recorded_at": world["end_at"],
            "available_at": world["end_at"],
            "actor_ids": [],
            "visibility": "agent_visible",
            "channel": "system",
            "payload": {
                "checkpoint_id": world["checkpoint_ids"][-1],
                "status": "cycle_horizon_reached",
            },
        }
    )
    hidden: list[dict[str, Any]] = []
    for sequence, defect in enumerate(world["defects"]):
        hidden.append(
            {
                "event_id": _opaque_id("event", world["world_id"], "defect", sequence),
                "world_id": world["world_id"],
                "sequence": sequence,
                "kind": "crm_projection_changed",
                "effective_at": world["start_at"],
                "recorded_at": world["start_at"],
                "available_at": world["start_at"],
                "actor_ids": [],
                "visibility": "oracle_only",
                "channel": "crm",
                "payload": defect,
            }
        )
    hidden.append(
        {
            "event_id": family_event_id,
            "world_id": world["world_id"],
            "sequence": len(hidden),
            "kind": event_kind,
            "effective_at": _timestamp(
                _date_text(date.fromisoformat(world["start_date"]), family_event_day)
            ),
            "recorded_at": _timestamp(
                _date_text(
                    date.fromisoformat(world["start_date"]), family_event_day + 1
                )
            ),
            "available_at": release_checkpoint["available_at"],
            "actor_ids": [],
            "visibility": "oracle_only",
            "channel": "system",
            "payload": {
                "family": world["causal_family"],
                "variant": world["variant"],
                "description": world["family_description"],
                "checkpoint_id": world["intervention_checkpoint_id"],
            },
        }
    )
    hidden.append(
        {
            "event_id": _opaque_id("event", world["world_id"], "terminal-outcome"),
            "world_id": world["world_id"],
            "sequence": len(hidden),
            "kind": "terminal_outcome",
            "effective_at": world["end_at"],
            "recorded_at": world["end_at"],
            "available_at": world["end_at"],
            "actor_ids": [],
            "visibility": "oracle_only",
            "channel": "system",
            "causal_parent_ids": [family_event_id],
            "payload": {
                "outcome": world["reference_outcome"],
                "reason": world["outcome_reason"],
            },
        }
    )
    visible.sort(key=lambda event: (event["available_at"], event["event_id"]))
    for sequence, event in enumerate(visible):
        event["sequence"] = sequence
    return visible, hidden


def _build_artifacts(world: dict[str, Any]) -> list[dict[str, Any]]:
    vertical = next(item for item in VERTICALS if item["id"] == world["vertical"])
    actors = world["actors"]
    buyer = {_actor_role(actor): actor for actor in actors if actor["kind"] == "buyer"}
    seller = {
        _actor_role(actor): actor for actor in actors if actor["kind"] == "seller"
    }
    participant_roles = {
        "transcript": (
            ("account_executive", "champion"),
            ("domain_specialist", "technical_evaluator"),
            ("account_executive", "economic_buyer"),
        ),
        "email": (
            ("account_executive", "champion"),
            ("domain_specialist", "technical_evaluator"),
            ("account_executive", "procurement"),
        ),
        "internal_chat": (
            ("sales_manager", "revops"),
            ("account_executive", "sales_manager"),
            ("domain_specialist", "account_executive"),
        ),
        "crm": (
            ("revops", "economic_buyer"),
            ("account_executive", "champion"),
            ("sales_manager", "finance"),
        ),
        "calendar": (
            ("account_executive", "champion"),
            ("domain_specialist", "technical_evaluator"),
            ("account_executive", "procurement"),
        ),
        "document": (
            ("domain_specialist", "technical_evaluator"),
            ("account_executive", "procurement"),
            ("sales_manager", "economic_buyer"),
        ),
        "web_news": (
            ("revops", "executive_sponsor"),
            ("revops", "finance"),
            ("account_executive", "champion"),
        ),
    }
    result: list[dict[str, Any]] = []
    index = 1
    for artifact_type, count in ARTIFACT_COUNTS.items():
        for channel_index in range(count):
            checkpoint = world["checkpoints"][channel_index % len(world["checkpoints"])]
            source_role, recipient_role = participant_roles[artifact_type][
                channel_index % 3
            ]
            source = seller[source_role]
            recipient = (
                seller[recipient_role]
                if recipient_role in seller
                else buyer[recipient_role]
            )
            defect = (
                world["defects"][channel_index]
                if artifact_type == "crm" and channel_index < len(world["defects"])
                else None
            )
            result.append(
                _artifact_record(
                    artifact_type,
                    index,
                    channel_index,
                    vertical,
                    world,
                    checkpoint,
                    source,
                    recipient,
                    defect,
                )
            )
            index += 1
    return result


def _artifact_content(record: dict[str, Any], world: dict[str, Any]) -> str:
    return record["content"]["body"]


_PACKAGE_RESOURCES = files("edlb").joinpath("resources")


def _renderer_script(name: str) -> Traversable:
    return _PACKAGE_RESOURCES.joinpath("renderers", name)


def _rendering_asset(artifact_id: str, extension: str) -> Traversable:
    return _PACKAGE_RESOURCES.joinpath("rendering_assets", f"{artifact_id}.{extension}")


def _copy_rendering_asset(artifact_id: str, extension: str, output: Path) -> None:
    source = _rendering_asset(artifact_id, extension)
    if not source.is_file():
        raise FileNotFoundError(f"required rendering asset is missing: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(source.read_bytes())


def _rendering_record(
    path: Path,
    base: Path,
    mime_type: str,
    normalized_source_uri: str,
    name: str,
    version: str,
    configuration: str,
    implementation: Path | Traversable,
) -> dict[str, Any]:
    return {
        "path": path.relative_to(base).as_posix(),
        "mime_type": mime_type,
        "checksum": _file_checksum(path),
        "normalized_source_uri": normalized_source_uri,
        "renderer": {
            "name": name,
            "version": version,
            "configuration_hash": _checksum(configuration),
            "implementation_hash": _file_checksum(implementation),
        },
    }


def _render_rich_files(
    base: Path, world: dict[str, Any], artifacts: list[dict[str, Any]]
) -> None:
    if not (
        world["split"] == "train"
        and world["vertical"] == "manufacturing"
        and world["causal_family"] == "champion_exit"
    ):
        return
    proposal = next(
        artifact for artifact in artifacts if artifact["kind"] == "proposal"
    )
    quote = next(artifact for artifact in artifacts if artifact["kind"] == "quote")
    pdf_path = base / f"artifacts/rendered/{proposal['artifact_id']}.pdf"
    pdf_script = _renderer_script("render_pdf.source")
    _copy_rendering_asset(proposal["artifact_id"], "pdf", pdf_path)
    proposal["content"]["renderings"] = [
        _rendering_record(
            pdf_path,
            base,
            PDF_MIME_TYPE,
            proposal["content"]["source_uri"],
            "reportlab",
            PDF_RENDERER_VERSION,
            "edlb-reportlab-proposal-v1",
            pdf_script,
        )
    ]
    xlsx_path = base / f"artifacts/rendered/{quote['artifact_id']}.xlsx"
    xlsx_script = _renderer_script("render_xlsx.source")
    _copy_rendering_asset(quote["artifact_id"], "xlsx", xlsx_path)
    quote["content"]["renderings"] = [
        _rendering_record(
            xlsx_path,
            base,
            XLSX_MIME_TYPE,
            quote["content"]["source_uri"],
            "@oai/artifact-tool",
            XLSX_RENDERER_VERSION,
            "edlb-artifact-tool-pricing-v1",
            xlsx_script,
        )
    ]


def _build_rubric(
    world: dict[str, Any], artifacts: list[dict[str, Any]]
) -> dict[str, Any]:
    evidence_refs = [artifact["artifact_id"] for artifact in artifacts[:3]]
    first_checkpoint = world["checkpoints"][0]["available_at"]
    initial_communications = sum(
        artifact["kind"] in {"call_transcript", "email"}
        and artifact["available_at"] <= first_checkpoint
        for artifact in artifacts
    )
    specifications = (
        (
            "evidence_and_understanding",
            "state.crm_records[0].data.evidence_refs",
            "count",
            2,
        ),
        (
            "crm_integrity",
            "state.crm_records[0].data.record_integrity_status",
            "equals",
            "reconciled",
        ),
        (
            "stakeholder_management",
            "state.communications",
            "count",
            24 + 2 * world["checkpoint_count"],
        ),
        (
            "workflow_compliance",
            "state.checkpoint_completions",
            "count",
            world["checkpoint_count"] * len(ROLES),
        ),
        (
            "communication_quality",
            f"state.communications[{initial_communications}].metadata.semantic_envelope.attachments",
            "count",
            1,
        ),
        (
            "forecast_calibration",
            "state.crm_records[0].data.forecast_probability",
            "exists",
            True,
        ),
        (
            "longitudinal_recovery",
            "state.crm_records[0].data.post_intervention_evidence_ref",
            "exists",
            True,
        ),
        (
            "side_effect_discipline",
            "state.crm_records[0].data.side_effect_review",
            "equals",
            "completed_without_unrelated_changes",
        ),
    )
    assertions: list[dict[str, Any]] = []
    for index, (category, path, operator, expected) in enumerate(specifications):
        assertion: dict[str, Any] = {
            "assertion_id": _opaque_id("assertion", world["world_id"], index),
            "world_id": world["world_id"],
            "scope": "world",
            "category": category,
            "kind": "deterministic",
            "target": {"path": path, "operator": operator, "expected": expected},
            "required": True,
            "critical": False,
            "controllability": "controllable",
            "weight": 0.125,
            "evidence_refs": evidence_refs,
            "provenance": {"source": "synthetic_blueprint", "license": "CC-BY-4.0"},
        }
        assertions.append(assertion)
    assertions.append(
        {
            "assertion_id": _opaque_id(
                "assertion", world["world_id"], "communication-diagnostic"
            ),
            "world_id": world["world_id"],
            "scope": "world",
            "category": "communication_quality",
            "kind": "llm_judge",
            "target": {
                "path": f"state.communications[{initial_communications}].body",
                "operator": "exists",
                "expected": True,
            },
            "required": False,
            "critical": False,
            "controllability": "partially_controllable",
            "weight": 0.01,
            "evidence_refs": evidence_refs,
            "judge": {
                "criterion": "Diagnostic only until calibrated: assess grounding, clarity, tone, and unauthorized claims against the cited evidence.",
                "judge_version": DATASET_VERSION,
                "prompt_hash": _checksum("edlb communication diagnostic v1"),
            },
            "provenance": {"source": "synthetic_blueprint", "license": "CC-BY-4.0"},
        }
    )
    return {
        "rubric_version": DATASET_VERSION,
        "world_id": world["world_id"],
        "deterministic_weight": 1.0,
        "categories": list(CANONICAL_CATEGORIES),
        "assertions": assertions,
    }


def _build_reference_trace(
    world: dict[str, Any], artifacts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    run_id = _opaque_id("reference-run", world["world_id"])
    sequence = 0
    observation_token: str | None = None

    def append(kind: str, role: str, occurred_at: str, **values: Any) -> str:
        nonlocal sequence
        if kind in {
            "tool_call",
            "team_message",
            "yield",
            "checkpoint_complete",
            "run_end",
        }:
            values["observation_token"] = observation_token
        message_id = _opaque_id("message", world["world_id"], sequence, kind, role)
        trace.append(
            {
                "protocol_version": "v1.0.0",
                "run_id": run_id,
                "sequence": sequence,
                "message_id": message_id,
                "occurred_at": occurred_at,
                "kind": kind,
                "role": role,
                **values,
            }
        )
        sequence += 1
        return message_id

    append(
        "start",
        "system",
        world["start_at"],
        payload={"world_id": world["world_id"], "track": "open_team"},
    )
    buyers = {
        _actor_role(actor): actor
        for actor in world["actors"]
        if actor["kind"] == "buyer"
    }
    recovery_role = {
        "champion_exit": "economic_buyer",
        "late_stakeholder": "executive_sponsor",
        "budget_shock": "finance",
        "requirements_change": "technical_evaluator",
        "competition": "procurement",
        "external_event": "executive_sponsor",
    }[world["causal_family"]]
    for checkpoint in world["checkpoints"]:
        observation_token = _opaque_id(
            "observation-token", world["world_id"], checkpoint["sequence"]
        )
        visible = [
            artifact["artifact_id"]
            for artifact in artifacts
            if artifact["available_at"] <= checkpoint["available_at"]
        ]
        recipient_role = (
            recovery_role
            if checkpoint["sequence"] > world["intervention_sequence"]
            else "champion"
        )
        recipient = buyers[recipient_role]
        gate = checkpoint["label"].replace("_", " ")
        arguments = {
            "channel": "email",
            "recipients": [recipient["email"]],
            "subject": f"{gate} evidence confirmation",
            "body": f"Please confirm the attached {gate} evidence, decision owner, and approval condition before the team advances the opportunity.",
            "semantic_envelope": {
                "purpose": f"confirm {gate} evidence and decision ownership",
                "related_records": [world["deal_id"]],
                "requested_decisions": [f"confirm the accountable owner for {gate}"],
                "commitments": ["record corrections before advancing"],
                "attachments": [visible[-1]],
            },
        }
        call_id = append(
            "tool_call",
            "account_executive",
            checkpoint["available_at"],
            tool_name="communications.send",
            arguments=arguments,
            idempotency_key=_opaque_id(
                "reference", world["world_id"], checkpoint["sequence"], "evidence"
            ),
        )
        append(
            "tool_result",
            "account_executive",
            checkpoint["available_at"],
            call_id=call_id,
            ok=True,
            result={"status": "sent_with_grounding", "recipient_role": recipient_role},
        )
        next_checkpoint = world["checkpoints"][
            min(checkpoint["sequence"] + 1, world["checkpoint_count"] - 1)
        ]
        changes = {
            "record_integrity_status": "reconciled",
            "evidence_refs": visible[-2:],
            "next_step": f"confirm {next_checkpoint['label'].replace('_', ' ')} owner and approval evidence",
            "close_date": world["forecast_close_date"],
            "forecast_probability": round(
                0.3
                + 0.5 * checkpoint["sequence"] / max(1, world["checkpoint_count"] - 1),
                3,
            ),
            "amount_minor_units": world["amount_minor_units"],
            "currency": world["currency"],
            "side_effect_review": "completed_without_unrelated_changes",
        }
        if checkpoint["sequence"] > world["intervention_sequence"]:
            changes["post_intervention_evidence_ref"] = visible[-1]
        call_id = append(
            "tool_call",
            "revops",
            checkpoint["available_at"],
            tool_name="crm.update",
            arguments={"record_id": world["deal_id"], "changes": changes},
            idempotency_key=_opaque_id(
                "reference", world["world_id"], checkpoint["sequence"], "crm-repair"
            ),
        )
        append(
            "tool_result",
            "revops",
            checkpoint["available_at"],
            call_id=call_id,
            ok=True,
            result={"status": "reconciled", "record_id": world["deal_id"]},
        )
        document_key = _opaque_id(
            "reference", world["world_id"], checkpoint["sequence"], "gate-document"
        )
        document_id = _side_effect_id("document", world["world_id"], document_key)
        call_id = append(
            "tool_call",
            "domain_specialist",
            checkpoint["available_at"],
            tool_name="documents.create",
            arguments={
                "title": f"{gate} evidence and decision record",
                "content": f"Gate: {gate}\nEvidence: {', '.join(visible[-2:])}\nOwner: domain specialist\nDecision condition: confirm evidence before advancement.",
                "kind": "gate_evidence",
            },
            idempotency_key=document_key,
        )
        append(
            "tool_result",
            "domain_specialist",
            checkpoint["available_at"],
            call_id=call_id,
            ok=True,
            result={"document_id": document_id, "version": 1},
        )
        attach_key = _opaque_id(
            "reference", world["world_id"], checkpoint["sequence"], "attach-document"
        )
        call_id = append(
            "tool_call",
            "domain_specialist",
            checkpoint["available_at"],
            tool_name="documents.attach",
            arguments={
                "document_id": document_id,
                "related_type": "opportunity",
                "related_id": world["deal_id"],
            },
            idempotency_key=attach_key,
        )
        append(
            "tool_result",
            "domain_specialist",
            checkpoint["available_at"],
            call_id=call_id,
            ok=True,
            result={
                "document_id": document_id,
                "related_type": "opportunity",
                "related_id": world["deal_id"],
            },
        )
        approval_sequence = min(
            world["intervention_sequence"] + 1, world["checkpoint_count"] - 1
        )
        if checkpoint["sequence"] == approval_sequence:
            approval_key = _opaque_id(
                "reference", world["world_id"], "bounded-gate-approval"
            )
            approval_id = _side_effect_id("approval", world["world_id"], approval_key)
            approval_details = {
                "amount_minor_units": 100_000,
                "currency": world["currency"],
                "document_id": document_id,
                "deal_id": world["deal_id"],
                "gate": gate,
                "required_for_close": True,
            }
            call_id = append(
                "tool_call",
                "account_executive",
                checkpoint["available_at"],
                tool_name="approvals.request",
                arguments={
                    "approver_role": "sales_manager",
                    "purpose": f"approve bounded {gate} commercial exception",
                    "details": approval_details,
                },
                idempotency_key=approval_key,
            )
            append(
                "tool_result",
                "account_executive",
                checkpoint["available_at"],
                call_id=call_id,
                ok=True,
                result={
                    "approval_id": approval_id,
                    "status": "pending",
                    "details": approval_details,
                },
            )
            if world["causal_family"] != "budget_shock":
                decision_key = _opaque_id(
                    "reference", world["world_id"], "bounded-gate-decision"
                )
                call_id = append(
                    "tool_call",
                    "sales_manager",
                    checkpoint["available_at"],
                    tool_name="approvals.approve",
                    arguments={
                        "approval_id": approval_id,
                        "note": "Approved within the delegated limit against the attached gate evidence.",
                    },
                    idempotency_key=decision_key,
                )
                append(
                    "tool_result",
                    "sales_manager",
                    checkpoint["available_at"],
                    call_id=call_id,
                    ok=True,
                    result={
                        "approval_id": approval_id,
                        "status": "approved",
                        "details": approval_details,
                    },
                )
        for role in ROLES:
            summary = f"Reconciled available evidence and recorded the {checkpoint['label'].replace('_', ' ')} next step."
            call_id = append(
                "tool_call",
                role,
                checkpoint["available_at"],
                tool_name="run.complete_checkpoint",
                arguments={
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "summary": summary,
                },
                idempotency_key=_opaque_id(
                    "reference", world["world_id"], checkpoint["sequence"], role
                ),
            )
            result = {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "role": role,
                "status": "complete",
            }
            if (
                checkpoint["sequence"] == world["checkpoint_count"] - 1
                and role == ROLES[-1]
            ):
                result["outcome"] = TERMINAL_OUTCOMES[world["reference_outcome"]]
            append(
                "tool_result",
                role,
                checkpoint["available_at"],
                call_id=call_id,
                ok=True,
                result=result,
            )
        if checkpoint["sequence"] + 1 < world["checkpoint_count"]:
            next_observation_token = _opaque_id(
                "observation-token",
                world["world_id"],
                checkpoint["sequence"] + 1,
            )
            append(
                "observation",
                "account_executive",
                world["checkpoints"][checkpoint["sequence"] + 1]["available_at"],
                payload={
                    "checkpoint_advanced": {
                        "checkpoint": {"sequence": checkpoint["sequence"] + 1},
                        "budget_exhausted": False,
                    }
                },
                observation_token=next_observation_token,
            )
    append("run_end", "system", world["end_at"], status="completed")
    return trace


def _checkpoint_records(
    world: dict[str, Any], artifacts: list[dict[str, Any]], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, checkpoint in enumerate(world["checkpoints"]):
        available_at = checkpoint["available_at"]
        records.append(
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "world_id": world["world_id"],
                "sequence": index,
                "available_at": available_at,
                "window_start": available_at,
                "window_end": world["checkpoints"][index + 1]["available_at"]
                if index + 1 < len(world["checkpoints"])
                else world["end_at"],
                "status": "active" if index == 0 else "pending",
                "objective_ids": [_opaque_id("objective", world["world_id"], index)],
                "visible_artifact_ids": [
                    artifact["artifact_id"]
                    for artifact in artifacts
                    if artifact["available_at"] <= available_at
                ],
                "released_event_ids": [
                    event["event_id"]
                    for event in events
                    if event["available_at"] == available_at
                ],
                "required_roles": list(ROLES),
                "max_tool_calls": 32,
                "max_turns": 64,
                "terminal": index == len(world["checkpoints"]) - 1,
            }
        )
    return records


def _source_policy_ids(world: dict[str, Any]) -> list[str]:
    vertical_index = next(
        index
        for index, vertical in enumerate(VERTICALS)
        if vertical["id"] == world["vertical"]
    )
    return [
        _opaque_id("policy", DATASET_SEED, vertical_index, index) for index in range(30)
    ]


def _manifest(
    world: dict[str, Any],
    events: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    include_truth: bool,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": DATASET_VERSION,
        "world_id": world["world_id"],
        "split": world["split"],
        "vertical": world["vertical"],
        "seller_org_id": world["seller_org_id"],
        "buyer_org_id": world["buyer_org_id"],
        "title": world["deal_name"],
        "description": f"Synthetic {world['vertical'].replace('_', ' ')} opportunity from first meeting through a final decision window.",
        "start_at": world["start_at"],
        "end_at": world["end_at"],
        "duration_days": world["duration_days"],
        "checkpoint_ids": world["checkpoint_ids"],
        "actor_ids": [actor["actor_id"] for actor in world["actors"]],
        "event_ids": [event["event_id"] for event in events],
        "artifact_ids": [artifact["artifact_id"] for artifact in artifacts],
        "required_channels": list(REQUIRED_CHANNELS),
        "license": {"code": "MIT", "data": "CC-BY-4.0"},
        "provenance": {
            "synthetic_only": True,
            "generator": "edlb.generate",
            "generator_version": DATASET_VERSION,
            "created_at": "2026-08-17T00:00:00Z",
            "source_policy_ids": _source_policy_ids(world),
        },
    }
    if include_truth:
        manifest.update(
            {
                "pair_id": world["pair_id"],
                "counterfactual_variant": "a" if world["variant_index"] == 0 else "b",
                "causal_skeleton": CAUSAL_SKELETONS[world["causal_family"]],
                "terminal_outcome": TERMINAL_OUTCOMES[world["reference_outcome"]],
                "outcome_reason": world["outcome_reason"],
                "seed": world["seed"],
            }
        )
    return manifest


def _summary_markdown(world: dict[str, Any]) -> str:
    lines = [
        f"# {world['world_id']}",
        "",
        f"- Split: {world['split']}",
        f"- Vertical: {world['vertical']}",
        f"- Seller: {world['seller_name']}",
        f"- Buyer: {world['buyer_name']} ({world['buyer_domain']})",
        f"- Simulated duration: {world['duration_days']} days",
        f"- Checkpoints: {world['checkpoint_count']}",
        f"- Canonical artifacts: {sum(world['artifact_counts'].values())}",
        "- Shared seller documents: 30",
        "",
        "## Channels",
        "",
    ]
    lines.extend(
        f"- {kind}: {count}" for kind, count in world["artifact_counts"].items()
    )
    lines.extend(
        (
            "",
            "All identities, domains, records, messages, and external signals are synthetic. No real PII is included.",
        )
    )
    return "\n".join(lines)


def _write_world(root: Path, world: dict[str, Any]) -> None:
    is_private = world["split"] == "blind"
    include_oracle = world["split"] == "train" or is_private
    base = (
        root
        / ("private/blind" if is_private else f"output/public/{world['split']}")
        / world["world_id"]
    )
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)
    artifacts = _build_artifacts(world)
    _render_rich_files(base, world, artifacts)
    visible_events, hidden_events = _build_events(world, artifacts)
    checkpoints = _checkpoint_records(world, artifacts, visible_events)
    _write_json(
        base / "manifest.json", _manifest(world, visible_events, artifacts, is_private)
    )
    _write_jsonl(base / "actors.jsonl", world["actors"])
    _write_jsonl(base / "checkpoints.jsonl", checkpoints)
    _write_jsonl(base / "events.jsonl", visible_events)
    _write_jsonl(base / "artifacts.jsonl", artifacts)
    with (base / "artifacts.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "artifact_id",
                "kind",
                "source_uri",
                "created_at",
                "available_at",
                "checksum",
            ],
        )
        writer.writeheader()
        writer.writerows(
            {
                field: artifact["content"]["source_uri"]
                if field == "source_uri"
                else artifact[field]
                for field in writer.fieldnames
            }
            for artifact in artifacts
        )
    rubric = _build_rubric(world, artifacts)
    _write_json(base / "rubric.json", rubric)
    _write_jsonl(base / "assertions.jsonl", rubric["assertions"])
    _write_text(base / "content_summary.md", _summary_markdown(world))
    for artifact in artifacts:
        _write_text(
            base / artifact["content"]["source_uri"], _artifact_content(artifact, world)
        )
    if include_oracle:
        _write_json(
            base / "oracle.json",
            {
                "world_id": world["world_id"],
                "scenario_manifest": _manifest(world, visible_events, artifacts, True),
                "reference_outcome": world["reference_outcome"],
                "causal_family": world["causal_family"],
                "variant": world["variant"],
                "intervention_checkpoint_id": world["intervention_checkpoint_id"],
                "outcome_reason": world["outcome_reason"],
                "duration_days": world["duration_days"],
                "expected_lanes": {
                    "business_fit": "within_fit"
                    if world["reference_outcome"] != "disqualified_fit"
                    else "out_of_fit",
                    "terminal_state": world["reference_outcome"],
                    "crm_defects": world["defects"],
                },
                "hidden_events": hidden_events,
            },
        )
        _write_jsonl(
            base / "reference_trace.jsonl", _build_reference_trace(world, artifacts)
        )
    if is_private:
        _write_jsonl(base / "hidden_events.jsonl", hidden_events)


def _policy_control_index(theme: str) -> int:
    if any(
        token in theme
        for token in (
            "pricing",
            "discount",
            "margin",
            "commercial",
            "proposal",
            "contract",
            "approval",
        )
    ):
        return 1
    if any(
        token in theme
        for token in ("delivery", "capacity", "implementation", "staffing", "planning")
    ):
        return 2
    if any(
        token in theme
        for token in ("security", "quality", "risk", "data", "legal", "procurement")
    ):
        return 3
    if any(
        token in theme
        for token in (
            "forecast",
            "crm",
            "account",
            "meeting",
            "stakeholder",
            "competitive",
            "close plan",
        )
    ):
        return 4
    return 0


def _write_shared_documents(root: Path) -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    for vertical_index, vertical in enumerate(VERTICALS):
        seller_org_id = _opaque_id("org", DATASET_SEED, "seller", vertical_index)
        base = root / "output/public/shared" / seller_org_id
        if base.exists():
            shutil.rmtree(base)
        for doc_index, theme in enumerate(SHARED_THEMES, start=1):
            document_id = _opaque_id(
                "policy", DATASET_SEED, vertical_index, doc_index - 1
            )
            path = f"documents/{document_id}.md"
            owner, evidence, threshold_minor_units, escalation = POLICY_CONTROLS[
                vertical["id"]
            ][_policy_control_index(theme)]
            gate = vertical["gates"][(doc_index - 1) % len(vertical["gates"])].replace(
                "_", " "
            )
            effective_at = f"2025-01-{((doc_index - 1) % 27) + 1:02d}T00:00:00Z"
            text = "\n".join(
                (
                    f"# {vertical['seller_name']} {theme}",
                    "",
                    f"- Document ID: {document_id}",
                    f"- Seller: {seller_org_id}",
                    f"- Vertical: {vertical['id']}",
                    f"- Effective date: {effective_at}",
                    "- Synthetic: true",
                    "- Provenance: EDLB deterministic policy template v1.0.0, CC-BY-4.0",
                    "",
                    "## Control",
                    "",
                    f"Rule: For {theme}, {vertical['seller_name']} may not mark {gate} complete until {owner} verifies {evidence}.",
                    f"Owner: {owner}",
                    f"Required evidence: {evidence}.",
                    f"Approval threshold: Exposure above {_money(threshold_minor_units, vertical['currency'])} ({threshold_minor_units} minor units) requires written approval from {owner}.",
                    f"Escalation trigger: Escalate when {escalation}.",
                    "",
                    "## Recordkeeping",
                    "",
                    f"Attach the evidence to the {gate} record, preserve prior versions, and record the decision date before advancing the {vertical['motion']}.",
                )
            )
            _write_text(base / path, text)
            index.append(
                {
                    "document_id": document_id,
                    "seller_org_id": seller_org_id,
                    "vertical": vertical["id"],
                    "theme": theme,
                    "path": f"output/public/shared/{seller_org_id}/{path}",
                    "effective_at": effective_at,
                    "owner": owner,
                    "required_evidence": evidence,
                    "approval_threshold_minor_units": threshold_minor_units,
                    "currency": vertical["currency"],
                    "escalation_trigger": escalation,
                    "checksum": _checksum(text.rstrip() + "\n"),
                    "provenance": {
                        "synthetic_only": True,
                        "generator": "edlb.generate",
                        "generator_version": DATASET_VERSION,
                        "license": "CC-BY-4.0",
                    },
                    "synthetic": True,
                    "contains_real_pii": False,
                }
            )
        _write_jsonl(
            base / "documents.jsonl",
            [row for row in index if row["seller_org_id"] == seller_org_id],
        )
    _write_jsonl(root / "authoring/shared_documents.jsonl", index)
    return index


def _write_authoring(
    root: Path, worlds: list[dict[str, Any]], shared_documents: list[dict[str, Any]]
) -> None:
    authoring = root / "authoring"
    authoring.mkdir(parents=True, exist_ok=True)
    (authoring / "schema_projection_gaps.json").unlink(missing_ok=True)
    with (authoring / "verticals.csv").open("w", newline="") as handle:
        fields = [
            "vertical",
            "seller_org_id",
            "seller_name",
            "domain",
            "motion",
            "gates",
            "shared_document_count",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for vertical_index, vertical in enumerate(VERTICALS):
            writer.writerow(
                {
                    "vertical": vertical["id"],
                    "seller_org_id": _opaque_id(
                        "org", DATASET_SEED, "seller", vertical_index
                    ),
                    "seller_name": vertical["seller_name"],
                    "domain": vertical["domain"],
                    "motion": vertical["motion"],
                    "gates": ";".join(vertical["gates"]),
                    "shared_document_count": 30,
                }
            )
    _write_jsonl(
        authoring / "worlds.jsonl",
        [
            (
                {
                    "world_id": world["world_id"],
                    "pair_id": world["pair_id"],
                    "vertical": world["vertical"],
                    "seller_org_id": world["seller_org_id"],
                    "buyer_org_id": world["buyer_org_id"],
                    "split": world["split"],
                    "seed": world["seed"],
                    "duration_days": world["duration_days"],
                    "checkpoint_count": world["checkpoint_count"],
                    "intervention_checkpoint_id": world["intervention_checkpoint_id"],
                    "intervention_sequence": world["intervention_sequence"],
                    "causal_family": world["causal_family"],
                    "variant": world["variant"],
                    "reference_outcome": world["reference_outcome"],
                    "defects": world["defects"],
                    "actors": world["actors"],
                    "checkpoints": world["checkpoints"],
                }
                if world["split"] == "train"
                else {
                    "world_id": world["world_id"],
                    "vertical": world["vertical"],
                    "seller_org_id": world["seller_org_id"],
                    "buyer_org_id": world["buyer_org_id"],
                    "split": world["split"],
                    "duration_days": world["duration_days"],
                    "checkpoint_count": world["checkpoint_count"],
                }
            )
            for world in worlds
        ],
    )
    _write_text(
        authoring / "README.md",
        "\n".join(
            (
                "# EDLB v1 authoring",
                "",
                f"This directory contains deterministic synthetic blueprints for six verticals, six causal families, two variants per family, and {len(worlds)} public deal worlds.",
                "",
                "Train truth is available for authoring and reference use. Dev authoring contains only the public projection. Blind blueprints and outputs are withheld under benchmarks/v1/private.",
                "",
                "Public dataset seed: deterministic and versioned in the generator.",
                f"Dataset version: {DATASET_VERSION}",
                f"Shared documents: {len(shared_documents)}",
            )
        ),
    )


PAIR_ALLOWED_DIFFERENCES = (
    "opaque_public_identity_projection",
    "declared_intervention",
    "causal_descendants_after_intervention",
)


def _alpha_replacements(
    world: dict[str, Any], artifacts: list[dict[str, Any]]
) -> list[tuple[str, str]]:
    replacements: dict[str, str] = {
        world["world_id"]: "world-local",
        world["deal_id"]: "deal-local",
        world["buyer_org_id"]: "buyer-org-local",
        world["buyer_name"]: "Buyer Organization",
        world["buyer_domain"]: "buyer-organization.example",
        world["deal_name"]: "Buyer Organization Deal",
    }
    for checkpoint in world["checkpoints"]:
        replacements[checkpoint["checkpoint_id"]] = (
            f"checkpoint-{checkpoint['sequence']}"
        )
    for actor in world["actors"]:
        label = f"{actor['kind']}-{_actor_role(actor)}"
        replacements[actor["actor_id"]] = f"actor-{label}"
        replacements[actor["display_name"]] = label.replace("_", " ").title()
        if actor.get("email"):
            replacements[actor["email"]] = f"{label}@synthetic.example"
    for index, artifact in enumerate(artifacts):
        replacements[artifact["artifact_id"]] = f"artifact-{index:03d}"
    thread_ids = list(dict.fromkeys(artifact["thread_id"] for artifact in artifacts))
    for index, thread_id in enumerate(thread_ids):
        replacements[thread_id] = f"thread-{index:02d}"
    for index, defect in enumerate(world["defects"]):
        replacements[defect["defect_id"]] = f"defect-{index}"
    return sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True)


def _alpha_normalize(value: Any, replacements: list[tuple[str, str]]) -> Any:
    if isinstance(value, dict):
        return {
            key: _alpha_normalize(item, replacements)
            for key, item in value.items()
            if key != "checksum"
        }
    if isinstance(value, list):
        return [_alpha_normalize(item, replacements) for item in value]
    if isinstance(value, str):
        result = value.replace("\r\n", "\n")
        for source, target in replacements:
            result = result.replace(source, target)
        return result
    return value


def _normalized_artifact(
    record: dict[str, Any], replacements: list[tuple[str, str]]
) -> dict[str, Any]:
    return _alpha_normalize(record, replacements)


def _pair_base_facts(world: dict[str, Any]) -> dict[str, Any]:
    return {
        "vertical": world["vertical"],
        "seller_id": world["seller_id"],
        "seller_org_id": world["seller_org_id"],
        "seller_name": world["seller_name"],
        "buyer_industry": world["buyer_industry"],
        "motion": world["motion"],
        "currency": world["currency"],
        "amount_minor_units": world["amount_minor_units"],
        "forecast_close_date": world["forecast_close_date"],
        "causal_family": world["causal_family"],
        "split": world["split"],
        "seed": world["seed"],
        "start_date": world["start_date"],
        "start_at": world["start_at"],
        "end_at": world["end_at"],
        "duration_days": world["duration_days"],
        "checkpoint_count": world["checkpoint_count"],
        "intervention_sequence": world["intervention_sequence"],
        "checkpoints": [
            {
                key: checkpoint[key]
                for key in (
                    "sequence",
                    "day",
                    "date",
                    "available_at",
                    "label",
                    "status",
                )
            }
            for checkpoint in world["checkpoints"]
        ],
        "defects": [
            {key: value for key, value in defect.items() if key != "defect_id"}
            for defect in world["defects"]
        ],
        "gates": world["gates"],
        "artifact_counts": world["artifact_counts"],
    }


def pair_diff(world_a: dict[str, Any], world_b: dict[str, Any]) -> dict[str, Any]:
    artifacts_a = _build_artifacts(world_a)
    artifacts_b = _build_artifacts(world_b)
    replacements_a = _alpha_replacements(world_a, artifacts_a)
    replacements_b = _alpha_replacements(world_b, artifacts_b)
    pre_equal = True
    post_differences = 0
    post_total = 0
    for record_a, record_b in zip(artifacts_a, artifacts_b, strict=True):
        checkpoint = next(
            item
            for item in world_a["checkpoints"]
            if item["available_at"] == record_a["available_at"]
        )
        normalized_a = _normalized_artifact(record_a, replacements_a)
        normalized_b = _normalized_artifact(record_b, replacements_b)
        if checkpoint["sequence"] <= world_a["intervention_sequence"]:
            if normalized_a != normalized_b:
                pre_equal = False
        else:
            post_total += 1
            if normalized_a != normalized_b:
                post_differences += 1
    return {
        "pair_id": world_a["pair_id"],
        "world_ids": [world_a["world_id"], world_b["world_id"]],
        "base_facts_equal": _pair_base_facts(world_a) == _pair_base_facts(world_b),
        "pre_intervention_artifacts_equal": pre_equal,
        "post_intervention_artifact_differences": post_differences,
        "post_intervention_artifact_total": post_total,
        "allowed_differences": list(PAIR_ALLOWED_DIFFERENCES),
    }


def _validate(
    root: Path,
    worlds: list[dict[str, Any]],
    shared_documents: list[dict[str, Any]],
    include_blind: bool,
    *,
    forbidden_phrases: Iterable[str] = (),
    forbidden_entities: Iterable[str] = (),
    shared_seller_actor_ids: Iterable[str] = (),
) -> dict[str, Any]:
    errors: list[str] = []
    id_pattern = re.compile(r"^(world|pair)-[0-9a-f]{20}$")
    timestamp_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    artifact_channels = {
        "call_transcript": "transcript",
        "email": "email",
        "internal_chat": "internal_chat",
        "crm_record": "crm",
        "crm_history": "crm",
        "calendar_event": "calendar",
        "proposal": "document",
        "quote": "document",
        "contract": "document",
        "diligence_document": "document",
        "web_page": "web_news",
        "news_item": "web_news",
    }
    expected_world_count = 72 if include_blind else 48
    if len(worlds) != expected_world_count:
        errors.append(f"world_count={len(worlds)}")
    if len(shared_documents) != 180:
        errors.append(f"shared_document_count={len(shared_documents)}")
    for vertical in VERTICALS:
        vertical_worlds = [
            world for world in worlds if world["vertical"] == vertical["id"]
        ]
        expected_vertical_count = 12 if include_blind else 8
        if len(vertical_worlds) != expected_vertical_count:
            errors.append(f"{vertical['id']}_world_count={len(vertical_worlds)}")
        outcome_names = (
            "closed_won",
            "closed_lost_competitive",
            "closed_lost_fit",
            "no_decision",
            "disqualified_fit",
        )
        outcomes = {
            outcome: sum(
                world["reference_outcome"] == outcome for world in vertical_worlds
            )
            for outcome in outcome_names
        }
        closed_lost = outcomes.get("closed_lost_competitive", 0) + outcomes.get(
            "closed_lost_fit", 0
        )
        if include_blind and (
            outcomes.get("closed_won", 0) != 4
            or closed_lost != 4
            or outcomes.get("no_decision", 0) != 2
            or outcomes.get("disqualified_fit", 0) != 2
        ):
            errors.append(f"{vertical['id']}_outcomes={outcomes}")
    for world in worlds:
        if not id_pattern.fullmatch(world["world_id"]) or not id_pattern.fullmatch(
            world["pair_id"]
        ):
            errors.append(f"semantic_id={world['world_id']}")
        if world["split"] not in SPLITS:
            errors.append(f"bad_split={world['world_id']}")
        if not 180 <= world["duration_days"] <= 365:
            errors.append(f"bad_duration={world['world_id']}")
        if not 8 <= world["checkpoint_count"] <= 12:
            errors.append(f"bad_checkpoints={world['world_id']}")
        pair = [
            item
            for item in worlds
            if item["vertical"] == world["vertical"]
            and item["causal_family"] == world["causal_family"]
        ]
        if len(pair) != 2 or len({item["split"] for item in pair}) != 1:
            errors.append(f"pair_split={world['world_id']}")
        if (
            sum(world["artifact_counts"].values()) < 60
            or sum(world["artifact_counts"].values()) > 120
        ):
            errors.append(f"bad_artifacts={world['world_id']}")
        if set(world["artifact_counts"]) != set(ARTIFACT_COUNTS):
            errors.append(f"bad_channels={world['world_id']}")
        if not world["buyer_domain"].endswith(".example"):
            errors.append(f"non_example_domain={world['world_id']}")
        for actor in world["actors"]:
            if actor["visibility"] in {"internal_role_scoped", "restricted"} and (
                not actor.get("visible_roles")
                or not set(actor["visible_roles"]) <= set(ROLES)
            ):
                errors.append(
                    f"actor_role_scope={world['world_id']}:{actor['actor_id']}"
                )
            if actor["kind"] == "buyer":
                if actor["organization_id"] != world["buyer_org_id"]:
                    errors.append(
                        f"buyer_organization={world['world_id']}:{actor['actor_id']}"
                    )
                if not actor["email"].endswith(f"@{world['buyer_domain']}"):
                    errors.append(
                        f"buyer_email={world['world_id']}:{actor['actor_id']}"
                    )
                if not timestamp_pattern.fullmatch(actor["active_from"]):
                    errors.append(
                        f"actor_timestamp={world['world_id']}:{actor['actor_id']}"
                    )
        artifacts = _build_artifacts(world)
        if len(artifacts) != sum(ARTIFACT_COUNTS.values()):
            errors.append(f"artifact_count={world['world_id']}")
        counts = {channel: 0 for channel in ARTIFACT_COUNTS}
        for artifact in artifacts:
            channel = artifact_channels.get(artifact["kind"])
            if channel is None:
                errors.append(
                    f"artifact_kind={world['world_id']}:{artifact['artifact_id']}"
                )
                continue
            counts[channel] += 1
            if not timestamp_pattern.fullmatch(
                artifact["created_at"]
            ) or not timestamp_pattern.fullmatch(artifact["available_at"]):
                errors.append(
                    f"artifact_timestamp={world['world_id']}:{artifact['artifact_id']}"
                )
            if artifact["checksum"] != _checksum(artifact["content"]["body"]):
                errors.append(
                    f"artifact_checksum={world['world_id']}:{artifact['artifact_id']}"
                )
            if artifact["visibility"] == "role_scoped" and (
                not artifact.get("visible_roles")
                or not set(artifact["visible_roles"]) <= set(ROLES)
            ):
                errors.append(
                    f"artifact_role_scope={world['world_id']}:{artifact['artifact_id']}"
                )
            if not artifact["content"]["source_uri"].startswith("artifacts/"):
                errors.append(
                    f"artifact_uri={world['world_id']}:{artifact['artifact_id']}"
                )
        if counts != ARTIFACT_COUNTS:
            errors.append(f"artifact_channels={world['world_id']}:{counts}")
        visible_events, hidden_events = _build_events(world, artifacts)
        for event in visible_events + hidden_events:
            if not all(
                timestamp_pattern.fullmatch(event[field])
                for field in ("effective_at", "recorded_at", "available_at")
            ):
                errors.append(
                    f"event_timestamp={world['world_id']}:{event['event_id']}"
                )
            if event["visibility"] == "role_scoped" and (
                not event.get("visible_roles")
                or not set(event["visible_roles"]) <= set(ROLES)
            ):
                errors.append(
                    f"event_role_scope={world['world_id']}:{event['event_id']}"
                )
        material_keys = {
            "change",
            "budget_status",
            "requirement",
            "stated_position",
            "disclosure_channel",
            "restart_status",
        }
        material_events = [
            event for event in visible_events if material_keys & event["payload"].keys()
        ]
        if len(material_events) != 1:
            errors.append(
                f"material_event_count={world['world_id']}:{len(material_events)}"
            )
        else:
            material_event = material_events[0]
            if {
                "family",
                "variant",
                "outcome",
                "reference_outcome",
                "pair_id",
            } & material_event["payload"].keys():
                errors.append(f"material_event_truth={world['world_id']}")
            checkpoint = next(
                (
                    item
                    for item in world["checkpoints"]
                    if item["checkpoint_id"]
                    == material_event["payload"].get("checkpoint_id")
                ),
                None,
            )
            if (
                checkpoint is None
                or checkpoint["available_at"] != material_event["available_at"]
            ):
                errors.append(f"material_event_release={world['world_id']}")
        intervention_available = next(
            event["available_at"]
            for event in hidden_events
            if event["event_id"]
            == _opaque_id("event", world["world_id"], "causal-intervention")
        )
        actors = {actor["actor_id"]: actor for actor in world["actors"]}
        for artifact in artifacts:
            checkpoint = next(
                item
                for item in world["checkpoints"]
                if item["available_at"] == artifact["available_at"]
            )
            channel = artifact["content"]["source_uri"].split("/")[1]
            evidence = _causal_evidence(
                world,
                checkpoint,
                channel,
                actors[artifact["source_actor_ids"][0]],
                actors[artifact["recipient_actor_ids"][0]],
            )
            if evidence and artifact["available_at"] < intervention_available:
                errors.append(
                    f"temporal_leak={world['world_id']}:{artifact['artifact_id']}"
                )
        rubric = _build_rubric(world, artifacts)
        rubric_categories = set(rubric["categories"])
        assertion_categories = {
            assertion["category"] for assertion in rubric["assertions"]
        }
        if rubric_categories != assertion_categories:
            errors.append(f"rubric_categories={world['world_id']}")
        if (
            sum(
                assertion["weight"]
                for assertion in rubric["assertions"]
                if assertion["kind"] == "deterministic"
            )
            < 0.75
        ):
            errors.append(f"deterministic_weight={world['world_id']}")
    public_root = root / "output/public"
    for split in ("train", "dev"):
        expected = sum(world["split"] == split for world in worlds)
        actual = (
            len(list((public_root / split).glob("*/manifest.json")))
            if (public_root / split).exists()
            else 0
        )
        if actual != expected:
            errors.append(f"{split}_bundle_count={actual}")
    blind_root = root / "private/blind"
    if include_blind:
        actual_blind = (
            len(list(blind_root.glob("*/manifest.json"))) if blind_root.exists() else 0
        )
        if actual_blind != sum(world["split"] == "blind" for world in worlds):
            errors.append(f"blind_bundle_count={actual_blind}")
    prohibited_manifest_keys = {
        "pair_id",
        "counterfactual_variant",
        "causal_skeleton",
        "terminal_outcome",
        "seed",
        "outcome_reason",
    }
    prohibited_authoring_keys = prohibited_manifest_keys | {
        "causal_family",
        "variant",
        "reference_outcome",
        "intervention_checkpoint_id",
        "intervention_sequence",
        "defects",
        "checkpoints",
        "actors",
    }
    public_worlds = [world for world in worlds if world["split"] != "blind"]
    for world in public_worlds:
        bundle = public_root / world["split"] / world["world_id"]
        manifest = json.loads((bundle / "manifest.json").read_text())
        if prohibited_manifest_keys & manifest.keys():
            errors.append(f"public_manifest_truth={world['world_id']}")
        if bundle.name != world["world_id"] or not id_pattern.fullmatch(bundle.name):
            errors.append(f"public_bundle_id={bundle}")
        if world["split"] == "dev":
            summary_text = (bundle / "content_summary.md").read_text()
            truth_values = (
                world["pair_id"],
                world["causal_family"],
                world["variant"],
                world["reference_outcome"],
            )
            if any(
                value in summary_text or value in _json(manifest)
                for value in truth_values
            ):
                errors.append(f"dev_summary_truth={world['world_id']}")
    authoring_rows = [
        json.loads(line)
        for line in (root / "authoring/worlds.jsonl").read_text().splitlines()
    ]
    for row in authoring_rows:
        if row["split"] == "dev" and prohibited_authoring_keys & row.keys():
            errors.append(f"dev_authoring_truth={row['world_id']}")
    pair_diffs: list[dict[str, Any]] = []
    seen_pairs: set[str] = set()
    for world in worlds:
        if world["pair_id"] in seen_pairs:
            continue
        pair = [item for item in worlds if item["pair_id"] == world["pair_id"]]
        if len(pair) != 2:
            continue
        seen_pairs.add(world["pair_id"])
        diff = pair_diff(pair[0], pair[1])
        pair_diffs.append(diff)
        if not diff["base_facts_equal"]:
            errors.append(f"pair_base_facts={world['pair_id']}")
        if not diff["pre_intervention_artifacts_equal"]:
            errors.append(f"pair_pre_intervention={world['pair_id']}")
        if diff["post_intervention_artifact_differences"] == 0:
            errors.append(f"pair_no_causal_evidence={world['pair_id']}")
    for path in root.rglob("*"):
        if path.is_file() and "reviews" not in path.parts:
            text = path.read_text(errors="ignore")
            if "Scenario note:" in text:
                errors.append(f"scenario_note_leak={path}")
    errors.extend(
        _validate_synthetic_privacy(
            root,
            worlds,
            shared_documents,
            forbidden_phrases=forbidden_phrases,
            forbidden_entities=forbidden_entities,
            shared_seller_actor_ids=shared_seller_actor_ids,
        )
    )
    return {
        "valid": not errors,
        "errors": errors,
        "world_count": len(worlds),
        "shared_document_count": len(shared_documents),
        "split_counts": {
            split: sum(world["split"] == split for world in worlds) for split in SPLITS
        },
        "vertical_counts": {
            vertical["id"]: sum(world["vertical"] == vertical["id"] for world in worlds)
            for vertical in VERTICALS
        },
        "outcome_counts": {
            vertical["id"]: {
                outcome: sum(
                    world["vertical"] == vertical["id"]
                    and world["reference_outcome"] == outcome
                    for world in worlds
                )
                for outcome in (
                    "closed_won",
                    "closed_lost_competitive",
                    "closed_lost_fit",
                    "no_decision",
                    "disqualified_fit",
                )
            }
            for vertical in VERTICALS
        },
        "artifact_count_per_world": sum(ARTIFACT_COUNTS.values()),
        "artifact_total": sum(
            sum(world["artifact_counts"].values()) for world in worlds
        ),
        "checkpoint_min": min(world["checkpoint_count"] for world in worlds),
        "checkpoint_max": max(world["checkpoint_count"] for world in worlds),
        "duration_min": min(world["duration_days"] for world in worlds),
        "duration_max": max(world["duration_days"] for world in worlds),
        "pair_count": len(pair_diffs),
        "pair_diffs": pair_diffs,
        "blind_included": include_blind,
    }


def _load_private_config(
    root: Path, private_config: Path | str | None, official: bool
) -> int | None:
    if not official:
        return None
    config_path = (
        Path(private_config)
        if private_config is not None
        else root / "private" / PRIVATE_CONFIG_NAME
    )
    if not config_path.exists():
        raise FileNotFoundError(
            f"private generation config required for official generation: {config_path}"
        )
    config = json.loads(config_path.read_text())
    if config.get("dataset_version") != DATASET_VERSION:
        raise ValueError("private generation config dataset version mismatch")
    blind_seed = config.get("blind_seed")
    if not isinstance(blind_seed, int) or isinstance(blind_seed, bool):
        raise TypeError("private generation config must contain an integer blind_seed")
    return blind_seed


def generate_dataset(
    root: Path | str | None = None,
    private_config: Path | str | None = None,
    official: bool = False,
    *,
    forbidden_phrases: Iterable[str] = (),
    forbidden_entities: Iterable[str] = (),
    shared_seller_actor_ids: Iterable[str] = (),
) -> dict[str, Any]:
    target = (
        Path(root)
        if root is not None
        else Path(__file__).resolve().parents[2] / "benchmarks/v1"
    )
    target.mkdir(parents=True, exist_ok=True)
    blind_seed = _load_private_config(target, private_config, official)
    output_path = target / "output"
    if output_path.exists():
        shutil.rmtree(output_path)
    if official:
        blind_path = target / "private" / "blind"
        if blind_path.exists():
            shutil.rmtree(blind_path)
    worlds: list[dict[str, Any]] = []
    for vertical_index in range(len(VERTICALS)):
        for family_index in range(len(FAMILIES)):
            for variant in range(2):
                split = _world_split(vertical_index, family_index)
                if split == "blind" and not official:
                    continue
                seed = (
                    blind_seed
                    if split == "blind" and blind_seed is not None
                    else DATASET_SEED
                )
                worlds.append(_build_world(vertical_index, family_index, variant, seed))
    shared_documents = _write_shared_documents(target)
    _write_authoring(
        target,
        [world for world in worlds if world["split"] != "blind"],
        shared_documents,
    )
    for world in worlds:
        _write_world(target, world)
    summary = _validate(
        target,
        worlds,
        shared_documents,
        official,
        forbidden_phrases=forbidden_phrases,
        forbidden_entities=forbidden_entities,
        shared_seller_actor_ids=shared_seller_actor_ids,
    )
    public_worlds = [world for world in worlds if world["split"] != "blind"]
    public_summary = _validate(
        target,
        public_worlds,
        shared_documents,
        False,
        forbidden_phrases=forbidden_phrases,
        forbidden_entities=forbidden_entities,
        shared_seller_actor_ids=shared_seller_actor_ids,
    )
    published_validation = {
        key: value
        for key, value in public_summary.items()
        if key not in {"pair_diffs", "outcome_counts"}
    }
    _write_json(
        target / "output/manifest.json",
        {
            "dataset_version": DATASET_VERSION,
            "seed": DATASET_SEED,
            "world_count": len(public_worlds),
            "verticals": [vertical["id"] for vertical in VERTICALS],
            "splits": {
                split: public_summary["split_counts"][split]
                for split in ("train", "dev")
            },
            "shared_documents": public_summary["shared_document_count"],
            "artifact_count_per_world": public_summary["artifact_count_per_world"],
            "artifact_total": public_summary["artifact_total"],
            "validation": published_validation,
        },
    )
    _write_json(target / "authoring/validation.json", published_validation)
    if official:
        _write_json(target / "private/validation.json", summary)
    if not summary["valid"]:
        raise ValueError(_json(summary))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--official", action="store_true")
    parser.add_argument("--private-config", type=Path)
    args = parser.parse_args()
    summary = generate_dataset(args.root, args.private_config, args.official)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
