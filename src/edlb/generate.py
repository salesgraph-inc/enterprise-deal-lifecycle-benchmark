from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import shutil
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from .models import stable_hash
from .tools import WRITE_TOOLS

DATASET_VERSION = "v1.0.0"
DATASET_SEED = 20260817
ARTIFACT_COUNTS = {
    "transcript": 10,
    "email": 14,
    "internal_chat": 12,
    "crm": 12,
    "calendar": 8,
    "document": 10,
    "web_news": 6,
}
ARTIFACT_RANGES = {
    "transcript": (8, 14),
    "email": (14, 24),
    "internal_chat": (8, 16),
    "crm": (8, 14),
    "calendar": (6, 10),
    "document": (10, 18),
    "web_news": (4, 8),
}
SPLITS = ("train", "dev", "blind")
ROLES = ("account_executive", "domain_specialist", "sales_manager", "revops")
PROSE_TRANSITIONS = (
    "Reconcile any disagreement before the next buyer decision.",
    "Keep unresolved ownership visible until the buyer responds.",
    "Separate the dated buyer record from the current forecast assumption.",
    "Carry forward only commitments that the accountable owner confirmed.",
    "Treat timing, ownership, and acceptance as separate open questions.",
    "Preserve the prior record while the decision group reviews the change.",
    "Use the latest dated buyer statement when the records conflict.",
    "Leave the forecast unchanged until the accountable owner decides.",
)
MANDATORY_GATES = {
    "manufacturing": {
        "supplier_qualification",
        "quality_and_capacity_review",
        "purchase_order",
    },
    "construction": {"tender", "bid"},
    "commercial_insurance": {
        "quotation_request",
        "quotation",
        "client_order",
        "binding",
    },
    "consulting": {"scope", "procurement"},
    "legal_services": {
        "conflicts",
        "matter_scope",
        "fee_arrangement",
        "confidentiality",
        "leadership_approval",
    },
    "corporate_banking": {
        "beneficial_ownership",
        "customer_due_diligence",
        "underwriting",
        "credit_approval",
        "documentation",
    },
}
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
ADDITIONAL_AUTHORITIES = {
    "manufacturing": {
        "purchase_order": (
            (
                "manufacturing.seller_acceptance_authority",
                "seller",
                "Order acceptance manager",
                "accept_purchase_order",
            ),
        )
    },
    "construction": {
        "bonding_capacity": (
            (
                "construction.surety_authority",
                "third_party",
                "Surety bond underwriter",
                "confirm_bonding_capacity",
            ),
        ),
        "award_and_contract": (
            (
                "construction.seller_execution_authority",
                "seller",
                "Contract execution officer",
                "execute_contract",
            ),
        ),
    },
    "consulting": {
        "delivery_resourcing": (
            (
                "consulting.staffing_authority",
                "seller",
                "Staffing and capacity lead",
                "commit_delivery_capacity",
            ),
        )
    },
    "legal_services": {
        "conflicts": (
            (
                "legal.affected_client_authority",
                "third_party",
                "Affected client authorized counsel",
                "grant_conflict_waiver",
            ),
        )
    },
    "corporate_banking": {
        "closing": (
            (
                "bank.third_party_condition_authority",
                "third_party",
                "Third-party condition provider",
                "confirm_closing_condition",
            ),
        )
    },
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
    "forecast_discipline",
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
    {
        "source-reference",
        "source-references",
        "source_reference",
        "source_references",
        "source_registry.json",
        "attributions.json",
        "baselines",
    }
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
            "supplier_qualification",
            "sample_or_pilot",
            "apqp_review",
            "quality_and_capacity_review",
            "purchase_order",
            "technical_revalidation",
        ),
        "buyer_industry": "industrial manufacturing",
        "currency": "USD",
        "jurisdiction": "Synthetic supplier lifecycle modeled on Neapco's company-specific process, not an industry requirement",
    },
    {
        "id": "construction",
        "label": "Construction",
        "seller_id": "seller-cinderline-builders",
        "seller_name": "Cinderline Builders Group",
        "domain": "cinderline-builders.example",
        "motion": "federal public transportation CM/GC project pursuit",
        "gates": (
            "qualification",
            "bonding_capacity",
            "site_walk",
            "tender",
            "bid",
            "interview",
            "value_engineering",
            "award_and_contract",
        ),
        "buyer_industry": "public transportation infrastructure",
        "currency": "USD",
        "delivery_method": "cm_gc",
        "project_sector": "public_transportation",
        "procurement_scope": "federal",
        "jurisdiction": "Synthetic composite of a direct United States federal FAR construction acquisition and nonbinding public-transportation CM/GC practice, not an FTA grant-recipient regime or unified legal framework",
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
            "additional_information",
            "quotation_request",
            "quotation",
            "client_order",
            "binding",
            "contract_data_validation",
            "post_placement",
        ),
        "buyer_industry": "commercial services",
        "currency": "USD",
        "jurisdiction": "Synthetic London Market placement workflow modeled on Lloyd's January 2023 digital placement journey, not universal insurance law",
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
            "delivery_resourcing",
            "commercial_model",
            "service_specification",
            "procurement",
            "knowledge_transfer",
        ),
        "buyer_industry": "business services",
        "currency": "USD",
        "jurisdiction": "Synthetic United Kingdom central-government consultancy procurement workflow under Playbook guidance",
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
            "uniform_assumptions",
            "matter_scope",
            "fee_arrangement",
            "confidentiality",
            "leadership_approval",
            "engagement_letter",
        ),
        "buyer_industry": "regulated enterprise services",
        "currency": "USD",
        "jurisdiction": "Synthetic California engagement workflow combining California professional-conduct rules with GSK-specific sourcing practice",
    },
    {
        "id": "corporate_banking",
        "label": "Corporate Banking",
        "seller_id": "seller-emberline-bank",
        "seller_name": "Emberline Commercial Bank",
        "domain": "emberline-bank.example",
        "motion": "commercial lending and treasury sale",
        "gates": (
            "customer_identification",
            "beneficial_ownership",
            "customer_due_diligence",
            "underwriting",
            "pricing",
            "credit_approval",
            "documentation",
            "closing",
        ),
        "buyer_industry": "mid-market enterprise",
        "currency": "USD",
        "jurisdiction": "Synthetic OCC-supervised United States national-bank lending workflow using contemporaneous OCC and BSA/AML guidance",
    },
)

VERTICAL_BLUEPRINTS: dict[str, Any] = json.loads(
    files("edlb").joinpath("resources", "vertical_blueprints.json").read_text()
)

VERTICAL_FACTS: dict[str, dict[str, Any]] = {
    "manufacturing": {
        "source_ids": ("source-neapco-supplier-requirements",),
        "fact_ids": (
            "manufacturing-rfq-response",
            "manufacturing-supplier-qualification",
            "manufacturing-award-authorization",
            "manufacturing-apqp-ppap",
            "manufacturing-capacity-validation",
        ),
        "evidence_by_gate": {
            "rfq": "RFQ response, supporting material, and cost breakdown",
            "technical_validation": "technical feasibility response",
            "supplier_qualification": "supplier self-assessment and qualification assessment",
            "sample_or_pilot": "PPAP sample parts and inspection evidence",
            "apqp_review": "APQP actions and PPAP status",
            "quality_and_capacity_review": "process capability, capacity, run-at-rate, and launch-readiness evidence",
            "purchase_order": "Project Authorization Letter or purchase order with written acceptance",
            "technical_revalidation": "current requalification, capacity, and run-at-rate evidence",
        },
        "crm_origins": (
            "RFQ revision imported before engineering disposition",
            "launch forecast copied from an obsolete capacity plan",
            "sample action remained after the PPAP requirement changed",
        ),
    },
    "construction": {
        "source_ids": (
            "source-far-fac-2025-03",
            "source-agc-cmgc-value-engineering",
        ),
        "fact_ids": (
            "construction-qualification",
            "construction-site-visit",
            "construction-bonding-capacity",
            "construction-performance-payment-bonds",
            "construction-solicitation-addenda",
            "construction-best-value-award",
            "construction-cmgc-interview",
            "construction-cmgc-value-engineering",
        ),
        "evidence_by_gate": {
            "qualification": "CM/GC qualifications and best-value selection evidence",
            "bonding_capacity": "surety and available bonding-capacity evidence",
            "site_walk": "site-visit questions and field-condition record",
            "tender": "formal solicitation amendments and any revised proposal deadline",
            "bid": "responsive proposal, estimate, allowances, and contingency",
            "interview": "CM/GC interview and selection record",
            "value_engineering": "preconstruction value-engineering log and GMP reconciliation",
            "award_and_contract": "documented federal award decision and executed contract",
        },
        "crm_origins": (
            "bid stage advanced when an unacknowledged addendum arrived",
            "award date copied before the owner's evaluation calendar changed",
            "site-walk action remained after the tender clarification deadline moved",
        ),
    },
    "commercial_insurance": {
        "source_ids": ("source-lloyds-digital-placement",),
        "fact_ids": (
            "insurance-submission-quote",
            "insurance-firm-order-bind",
            "insurance-contract-data-validation",
            "insurance-post-placement",
            "insurance-structured-placement-data",
            "insurance-client-order",
        ),
        "evidence_by_gate": {
            "submission": "structured submission and coverage request",
            "additional_information": "additional-information request and verified exposure data",
            "quotation_request": "quotation request with structured placement data",
            "quotation": "quotation with limits, premiums, commissions, and exclusions",
            "client_order": "client order and selected terms",
            "binding": "firm order, bind confirmation, and signed lines",
            "contract_data_validation": "validated contract data and tax allocation",
            "post_placement": "post-placement reconciliation and structured data",
        },
        "crm_origins": (
            "placement stage advanced when a market response was still indicative",
            "inception forecast copied before the insured changed the firm-order date",
            "quote-comparison action remained after an underwriter added a subjectivity",
        ),
    },
    "consulting": {
        "source_ids": ("source-consultancy-playbook",),
        "fact_ids": (
            "consulting-make-buy-business-case",
            "consulting-outcome-specification",
            "consulting-market-engagement",
            "consulting-evaluation-pricing",
            "consulting-delivery-resourcing",
            "consulting-knowledge-transfer",
        ),
        "evidence_by_gate": {
            "discovery": "make-or-buy rationale and business-case evidence",
            "diagnosis": "diagnosis tied to measurable outcomes",
            "scope": "outcome specification and scope boundaries",
            "delivery_resourcing": "delivery approach and resource-availability evidence",
            "commercial_model": "pricing mechanism, risk allocation, and payment basis",
            "service_specification": "outcome specification, deliverables, and dependencies",
            "procurement": "market-engagement, evaluation, and clarification record",
            "knowledge_transfer": "knowledge-transfer responsibilities and handoff plan",
        },
        "crm_origins": (
            "proposal stage copied from early market engagement before scope approval",
            "start date retained after buyer data access slipped",
            "staffing action remained after the named team availability changed",
        ),
    },
    "legal_services": {
        "source_ids": (
            "source-gsk-ocsi",
            "source-calbar-rules-2018",
        ),
        "fact_ids": (
            "legal-mini-rfi",
            "legal-uniform-assumptions",
            "legal-selection-scorecard",
            "legal-leadership-approval",
            "legal-value-engagement",
            "legal-conflicts",
            "legal-confidentiality",
        ),
        "evidence_by_gate": {
            "conflicts": "conflicts screen and any required informed written consent",
            "panel_or_rfp_selection": "mini-RFI and sourcing-room comparison",
            "uniform_assumptions": "common matter assumptions",
            "matter_scope": "matter scope, staffing, and quality comparison",
            "fee_arrangement": "fee and value commitments",
            "confidentiality": "permitted disclosure and informed-consent handling",
            "leadership_approval": "practice-group and legal-leadership approval",
            "engagement_letter": "value-based fee engagement letter",
        },
        "crm_origins": (
            "selection stage advanced before the conflicts clearance was recorded",
            "matter start date copied before security review completion",
            "engagement-letter action remained after the fee assumption changed",
        ),
    },
    "corporate_banking": {
        "source_ids": (
            "source-occ-loan-portfolio-management",
            "source-ffiec-cip",
            "source-ffiec-cdd",
            "source-ffiec-beneficial-ownership",
        ),
        "fact_ids": (
            "banking-lending-authority",
            "banking-underwriting-repayment",
            "banking-credit-separation",
            "banking-exception-approval",
            "banking-preclosing-documentation",
            "banking-customer-identification",
            "banking-risk-profile-monitoring",
            "banking-beneficial-owner-verification",
        ),
        "evidence_by_gate": {
            "customer_identification": "customer identity collection and verification",
            "beneficial_ownership": "ownership-prong and control-prong identification and verification",
            "customer_due_diligence": "customer risk profile and ongoing-monitoring record",
            "underwriting": "repayment, capacity, collateral, guarantor, and sensitivity analysis",
            "pricing": "risk-based pricing within documented lending authority",
            "credit_approval": "separated origination and approval with routed exceptions when required",
            "documentation": "complete loan documentation and pre-closing conditions",
            "closing": "executed documents and satisfied closing controls",
        },
        "crm_origins": (
            "credit stage advanced when beneficial-owner verification was still open",
            "closing date copied before committee conditions were documented",
            "documentation action remained after a covenant exception was escalated",
        ),
    },
}

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
            "submission version history, binder checklist, and post-placement reconciliation",
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
            "pricing model, margin worksheet, and approved service specification",
            15_000_000,
            "discount exceeds 10 percent or engagement margin falls below 35 percent",
        ),
        (
            "Resourcing Director",
            "delivery approach, resource availability, and delivery calendar",
            10_000_000,
            "required delivery capacity is unavailable within 30 days of the proposed start",
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
            "Senior Credit Approver",
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
            "credit approval record, covenant tracker, and dated closing forecast",
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


def _timestamp(day: date | str, hour: int = 9, minute: int = 0) -> str:
    value = date.fromisoformat(day) if isinstance(day, str) else day
    return f"{value.isoformat()}T{hour:02d}:{minute:02d}:00Z"


def _checksum(body: str) -> str:
    return f"sha256:{hashlib.sha256(body.encode()).hexdigest()}"


REFERENCE_AGENT_MANIFEST = {
    "resolved": True,
    "roles": {role: "edlb-reference-fixture" for role in ROLES},
    "models": {
        "edlb-reference-fixture": {
            "model_id": "edlb-reference-fixture",
            "model_digest": stable_hash({"model": "edlb-reference-fixture"}),
            "prompt_hash": stable_hash({"prompt": "edlb-reference-fixture"}),
            "provider_settings": {},
            "provider_defaults": False,
        }
    },
}
REFERENCE_TRACE_LIMITS = {
    "tool_calls_per_checkpoint": None,
    "turns_per_checkpoint": None,
    "timeout_seconds": None,
    "retries": 0,
}


def _reference_configuration_hash() -> str:
    return stable_hash(
        {
            "agent_manifest": REFERENCE_AGENT_MANIFEST,
            "limits": REFERENCE_TRACE_LIMITS,
        }
    )


def _file_checksum(path: Path | Traversable) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _money(minor_units: int, currency: str) -> str:
    major, minor = divmod(minor_units, 100)
    return f"{currency} {major:,}.{minor:02d}"


def _actor_role(actor: dict[str, Any]) -> str:
    known = set(ROLES) | {
        "champion",
        "economic_buyer",
        "procurement",
        "legal",
        "risk",
        "finance",
        "technical_evaluator",
        "executive_sponsor",
        "gatekeeper",
        "competitor",
        "analyst",
        "internal_approver",
    }
    return next(
        (str(role) for role in actor["role_tags"] if role in known),
        str(actor["role_tags"][0]),
    )


def _actor_active_at(actor: dict[str, Any], available_at: str) -> bool:
    return actor["active_from"] <= available_at and (
        actor.get("active_until") is None or available_at < actor["active_until"]
    )


def _actor_active_during(actor: dict[str, Any], start_at: str, end_at: str) -> bool:
    return actor["active_from"] <= start_at and (
        actor.get("active_until") is None or end_at < actor["active_until"]
    )


def _active_buyer(world: dict[str, Any], available_at: str) -> dict[str, Any]:
    return min(
        (
            actor
            for actor in world["actors"]
            if actor["kind"] == "buyer" and _actor_active_at(actor, available_at)
        ),
        key=lambda actor: actor["authority"]["role_id"],
    )


def _actor_label(actor: dict[str, Any]) -> str:
    return actor["attributes"]["job_title"]


def _person_name(
    dataset_seed: int, vertical_index: int, family_index: int, role_index: int
) -> str:
    seed = _stable_seed(dataset_seed, vertical_index, family_index, role_index)
    middle = chr(ord("A") + seed % 26)
    first = FIRST_NAMES[seed % len(FIRST_NAMES)]
    first_last = LAST_NAMES[(seed // len(FIRST_NAMES)) % len(LAST_NAMES)]
    second_last = LAST_NAMES[
        (seed // (len(FIRST_NAMES) * len(LAST_NAMES))) % len(LAST_NAMES)
    ]
    return f"{first} {middle}. {first_last}-{second_last}-{seed & 0xFFFF:04x}"


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


def _next_business_day(value: date) -> date:
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value


def _business_date_after(value: date, days: int) -> date:
    result = value
    remaining = days
    while remaining:
        result += timedelta(days=1)
        if result.weekday() < 5:
            remaining -= 1
    return result


def _business_checkpoint_days(
    start: date, duration: int, count: int, seed: int
) -> list[int]:
    result: list[int] = []
    for day in _checkpoint_days(duration, count, seed):
        business_day = _next_business_day(start + timedelta(days=day))
        offset = (business_day - start).days
        if result and offset <= result[-1]:
            business_day = _next_business_day(start + timedelta(days=result[-1] + 1))
            offset = (business_day - start).days
        result.append(offset)
    return result


def _checkpoint_time(pair_seed: int, gate: str, day: str) -> str:
    seed = _stable_seed(pair_seed, gate, day, "checkpoint-time")
    return _timestamp(day, 15 + seed % 3, (seed // 3) % 4 * 15)


def _artifact_counts(seed: int, vertical: dict[str, Any]) -> dict[str, int]:
    counts = {
        channel: minimum
        + _stable_seed(seed, "count", channel) % (maximum - minimum + 1)
        for channel, (minimum, maximum) in ARTIFACT_RANGES.items()
    }
    if sum(counts.values()) < 60:
        counts["email"] += 60 - sum(counts.values())
    for gate in vertical["gates"]:
        blueprint = _blueprint_gate(vertical["id"], gate)
        for spec in blueprint["required_artifacts"]:
            counts[
                {
                    "call_transcript": "transcript",
                    "email": "email",
                    "crm": "crm",
                    "document": "document",
                }[spec["channel"]]
            ] += 1
    minimums = {channel: bounds[0] for channel, bounds in ARTIFACT_RANGES.items()}
    while sum(counts.values()) > 120:
        channel = max(
            counts,
            key=lambda item: counts[item] - minimums[item],
        )
        if counts[channel] <= minimums[channel]:
            break
        counts[channel] -= 1
    return counts


def _artifact_timestamp(
    world: dict[str, Any],
    artifact_type: str,
    channel_index: int,
    checkpoint: dict[str, Any],
) -> str:
    seed = _stable_seed(world["seed"], artifact_type, channel_index, "time")
    available = datetime.fromisoformat(checkpoint["available_at"]).astimezone(UTC)
    return _format_datetime(available - timedelta(minutes=30 + seed % 241))


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _artifact_times(
    world: dict[str, Any],
    artifact_type: str,
    channel_index: int,
    checkpoint: dict[str, Any],
) -> tuple[str, str]:
    available = datetime.fromisoformat(
        _artifact_timestamp(world, artifact_type, channel_index, checkpoint)
    ).astimezone(UTC)
    lower = int(checkpoint["availability_delay_bounds"]["min_minutes"])
    upper = int(checkpoint["availability_delay_bounds"]["max_minutes"])
    delay = lower + _stable_seed(
        world["seed"], artifact_type, channel_index, "delay"
    ) % (upper - lower + 1)
    created = max(
        available - timedelta(minutes=delay),
        datetime.fromisoformat(world["start_at"]).astimezone(UTC),
    )
    return _format_datetime(created), _format_datetime(available)


def _causal_event_times(checkpoint: dict[str, Any]) -> tuple[str, str, str]:
    seed = _stable_seed(checkpoint["gate_id"], checkpoint["date"], "causal-time")
    effective = datetime.fromisoformat(_timestamp(checkpoint["date"], 8, seed % 4 * 10))
    return (
        _format_datetime(effective),
        _format_datetime(effective + timedelta(minutes=15)),
        _format_datetime(effective + timedelta(minutes=30)),
    )


def _structured_times(
    world: dict[str, Any],
    checkpoint: dict[str, Any],
    artifact_index: int,
    artifact_count: int,
    parent_available_at: Sequence[str] = (),
) -> tuple[str, str]:
    checkpoint_available = datetime.fromisoformat(
        checkpoint["available_at"]
    ).astimezone(UTC)
    lower = int(checkpoint["availability_delay_bounds"]["min_minutes"])
    upper = int(checkpoint["availability_delay_bounds"]["max_minutes"])
    delay_range = min(upper - lower, 60)
    delay = lower + _stable_seed(
        world["seed"], checkpoint["gate_id"], artifact_index, "structured-delay"
    ) % (delay_range + 1)
    gap = (
        lower
        + _stable_seed(
            world["seed"], checkpoint["gate_id"], checkpoint["date"], "structured-gap"
        )
        % 31
    )
    available = checkpoint_available - timedelta(
        minutes=(artifact_count - artifact_index - 1) * gap
    )
    parent_available = max(
        (
            datetime.fromisoformat(value).astimezone(UTC)
            for value in parent_available_at
        ),
        default=None,
    )
    if parent_available is not None:
        available = max(available, parent_available + timedelta(minutes=lower))
        if available > checkpoint_available:
            raise ValueError(
                f"structured artifact lineage exceeds checkpoint window: {checkpoint['gate_id']}"
            )
        delay = min(delay, int((available - parent_available).total_seconds() // 60))
    return _format_datetime(available - timedelta(minutes=delay)), _format_datetime(
        available
    )


def _vertical_facts(vertical: str) -> dict[str, Any]:
    return VERTICAL_FACTS[vertical]


def _blueprint_gate(vertical: str, gate_id: str) -> dict[str, Any]:
    for gate in VERTICAL_BLUEPRINTS["verticals"][vertical]["gates"]:
        if gate["gate_id"] == gate_id:
            return gate
    raise ValueError(f"blueprint gate is missing: {vertical}/{gate_id}")


def _causal_window(vertical: str, family: str) -> dict[str, Any]:
    window = VERTICAL_BLUEPRINTS["verticals"][vertical]["causal_windows"][family]
    intervention_gate = str(window["intervention_gate"])
    resolution_gate = str(window["resolution_gate"])
    gates = [
        gate["gate_id"] for gate in VERTICAL_BLUEPRINTS["verticals"][vertical]["gates"]
    ]
    if intervention_gate not in gates or resolution_gate not in gates:
        raise ValueError(f"causal window gate is missing: {vertical}/{family}")
    if gates.index(intervention_gate) >= gates.index(resolution_gate):
        raise ValueError(f"causal window order is invalid: {vertical}/{family}")
    return dict(window)


def _world_gate_route(
    vertical: dict[str, Any], family: str, pair_seed: int
) -> tuple[str, ...]:
    gates = tuple(str(gate) for gate in vertical["gates"])
    window = _causal_window(str(vertical["id"]), family)
    required = {
        gates[0],
        gates[-1],
        str(window["intervention_gate"]),
        str(window["resolution_gate"]),
        *MANDATORY_GATES[str(vertical["id"])],
    }
    target = max(6 + _stable_seed(pair_seed, "route-length", 0) % 3, len(required))
    candidates = sorted(
        (gate for gate in gates if gate not in required),
        key=lambda gate: _stable_seed(pair_seed, gate, "route"),
    )
    selected = required | set(candidates[: target - len(required)])
    return tuple(gate for gate in gates if gate in selected)


def _vertical_causal_facts(
    vertical: str, family: str, variant: str, *, include_source: bool = False
) -> dict[str, Any]:
    requirement = {
        "manufacturing": (
            "extended_traceability",
            "available_in_current_plan",
            "not_in_current_plan",
        ),
        "construction": (
            "revised_scope_addendum",
            "within_tender_scope",
            "requires_rebid",
        ),
        "commercial_insurance": (
            "coverage_and_contract_data_revision",
            "supported_by_quoted_coverage",
            "not_supported_by_quoted_coverage",
        ),
        "consulting": (
            "outcome_and_scope_revision",
            "within_delivery_plan",
            "outside_delivery_plan",
        ),
        "legal_services": (
            "confidentiality_and_fee_assumption_revision",
            "compatible_with_clearance_and_fee_terms",
            "conflicts_with_clearance_or_fee_terms",
        ),
        "corporate_banking": (
            "covenant_and_closing_condition_revision",
            "within_underwriting_policy",
            "outside_underwriting_policy",
        ),
    }[vertical]
    competition = {
        "manufacturing": (
            "rfq_factor_comparison_disclosed",
            "incumbent_supplier_offer_referenced",
        ),
        "construction": (
            "stated_evaluation_factor_comparison",
            "unstated_incumbent_influence",
        ),
        "commercial_insurance": (
            "client_order_market_comparison",
            "incumbent_terms_influence",
        ),
        "consulting": (
            "evaluation_matrix_disclosed",
            "incumbent_preference",
        ),
        "legal_services": (
            "panel_scorecard_disclosed",
            "incumbent_relationship_influence",
        ),
        "corporate_banking": (
            "authorized_financing_term_comparison",
            "incumbent_bank_influence",
        ),
    }[vertical]
    external = {
        "manufacturing": (
            {
                "capacity_status": "capacity_revalidated",
                "purchase_order_restart_status": "purchase_order_restart_confirmed",
                "source": "capacity_revalidation_record",
            },
            {
                "capacity_status": "program_paused",
                "purchase_order_restart_status": "no_restart_date",
                "source": "capacity_interruption_notice",
            },
        ),
        "construction": (
            {
                "solicitation_status": "solicitation_amended",
                "award_schedule_status": "award_rescheduled",
                "source": "solicitation_amendment",
            },
            {
                "solicitation_status": "procurement_cancelled",
                "award_schedule_status": "no_award_date",
                "source": "cancellation_notice",
            },
        ),
        "commercial_insurance": (
            {
                "contract_data_status": "post_bind_contract_data_corrected",
                "tax_data_status": "tax_data_validated",
                "source": "post_bind_correction_record",
            },
            {
                "contract_data_status": "placement_paused",
                "tax_data_status": "no_correction_date",
                "source": "post_bind_discrepancy_record",
            },
        ),
        "consulting": (
            {
                "delivery_baseline_status": "delivery_rebaselined",
                "knowledge_transfer_status": "knowledge_transfer_plan_confirmed",
                "source": "delivery_rebaseline_record",
            },
            {
                "delivery_baseline_status": "program_paused",
                "knowledge_transfer_status": "no_restart_date",
                "source": "delivery_dependency_notice",
            },
        ),
        "legal_services": (
            {
                "conflicts_status": "conflicts_clearance_renewed",
                "confidentiality_status": "confidentiality_clearance_renewed",
                "source": "clearance_record",
            },
            {
                "conflicts_status": "matter_paused",
                "confidentiality_status": "no_clearance_date",
                "source": "new_conflicts_notice",
            },
        ),
        "corporate_banking": (
            {
                "closing_exception_status": "closing_exception_approved",
                "closing_schedule_status": "close_rescheduled",
                "source": "closing_exception_record",
            },
            {
                "closing_exception_status": "facility_paused",
                "closing_schedule_status": "no_close_date",
                "source": "closing_condition_notice",
            },
        ),
    }[vertical]
    values = {
        "requirements_change": {
            "requirement": requirement[0],
            "requirement_version": "revision_2",
            "seller_coverage": requirement[1 if variant == "within_fit" else 2],
        },
        "competition": {
            "evaluation_record_owner": "buyer",
            "signal": competition[0 if variant == "transparent" else 1],
        },
        "external_event": {
            key: value
            for key, value in external[0 if variant == "recoverable" else 1].items()
            if key != "source"
        },
    }
    result = values[family]
    if family == "external_event" and include_source:
        result = dict(external[0 if variant == "recoverable" else 1])
    return result


def _causal_cure_data(world: dict[str, Any]) -> dict[str, Any]:
    actors = {_actor_role(actor): actor for actor in world["actors"]}
    values = {
        "champion_exit": {
            "stakeholder_actor_id": actors["champion"]["actor_id"],
            "handoff_actor_ids": (
                [actors["economic_buyer"]["actor_id"]]
                if world["variant"] == "strong_handoff"
                else []
            ),
        },
        "late_stakeholder": {
            "stakeholder_actor_id": actors["executive_sponsor"]["actor_id"],
            "active_from": world["late_activation_at"],
            "stated_position": (
                "requested_approval_path"
                if world["variant"] == "supportive"
                else "questioned_current_priority"
            ),
        },
        "budget_shock": {
            "budget_status": (
                "reduced_allocation_available"
                if world["variant"] == "reallocation"
                else "spending_hold"
            ),
            "review_window": (
                "current_cycle"
                if world["variant"] == "reallocation"
                else "next_planning_cycle"
            ),
        },
        "requirements_change": _vertical_causal_facts(
            world["vertical"], "requirements_change", world["variant"]
        ),
        "competition": _vertical_causal_facts(
            world["vertical"], "competition", world["variant"]
        ),
        "external_event": _vertical_causal_facts(
            world["vertical"], "external_event", world["variant"]
        ),
    }
    return values[world["causal_family"]]


def _causal_artifact_channel(world: Mapping[str, Any]) -> str:
    if world["causal_family"] == "external_event":
        return str(
            VERTICAL_BLUEPRINTS["verticals"][world["vertical"]]["external_observation"][
                "channel"
            ]
        )
    return {
        "champion_exit": "email",
        "late_stakeholder": "transcript",
        "budget_shock": "email",
        "requirements_change": "transcript",
        "competition": "internal_chat",
    }[str(world["causal_family"])]


def _prose_variant(
    world: Mapping[str, Any],
    artifact_type: str,
    channel_index: int,
    checkpoint: Mapping[str, Any],
) -> int:
    return _stable_seed(
        world["seed"],
        checkpoint["gate_id"],
        artifact_type,
        channel_index,
        "prose-variant",
    ) % len(PROSE_TRANSITIONS)


def _normalized_prose_lines(world: Mapping[str, Any], body: str) -> tuple[str, ...]:
    value = body.casefold()
    replacements = {
        str(world[key]).casefold()
        for key in ("buyer_name", "deal_name", "seller_name", "motion")
        if world.get(key)
    }
    replacements.update(
        str(actor[field]).casefold()
        for actor in world["actors"]
        for field in ("display_name", "email")
    )
    replacements.update(
        {str(gate).replace("_", " ").casefold() for gate in world["gates"]}
    )
    for replacement in sorted(replacements, key=len, reverse=True):
        value = value.replace(replacement, "<value>")
    value = re.sub(r"https?://\S+", "<value>", value)
    value = re.sub(
        r"\b(?:artifact|world|pair|deal|org|act)-[0-9a-f-]+\b", "<value>", value
    )
    value = re.sub(r"\b\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}z\b", "<value>", value)
    value = re.sub(r"\b\d+(?:[.,]\d+)?\b", "<value>", value)
    lines = tuple(
        normalized
        for line in value.splitlines()
        if len(normalized := re.sub(r"\s+", " ", line.strip(" #-*\t")).strip()) >= 24
        and not normalized.startswith(
            (
                "channel:",
                "date:",
                "from:",
                "participants:",
                "published:",
                "publisher:",
                "subject:",
                "to:",
                "url:",
            )
        )
        and not normalized.endswith(" update")
    )
    return lines


def _prose_skeleton(world: Mapping[str, Any], body: str) -> tuple[str, ...]:
    return _normalized_prose_lines(world, body)


def _prose_metrics(
    worlds_and_artifacts: Iterable[
        tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]
    ],
) -> dict[str, dict[str, float | int]]:
    skeletons: dict[str, Counter[tuple[str, ...]]] = {}
    vertical_skeletons: dict[str, Counter[tuple[str, ...]]] = {}
    lines: dict[str, Counter[str]] = {}
    for world, artifacts in worlds_and_artifacts:
        for artifact in artifacts:
            source_uri = str(artifact["content"]["source_uri"])
            channel = next(
                (
                    value
                    for value in (
                        "transcript",
                        "email",
                        "internal_chat",
                        "document",
                        "web_news",
                    )
                    if source_uri.startswith(f"artifacts/{value}/")
                ),
                None,
            )
            if channel is None:
                continue
            body = str(artifact["content"]["body"])
            skeleton = _prose_skeleton(world, body)
            skeletons.setdefault(channel, Counter())[skeleton] += 1
            vertical_skeletons.setdefault(f"{world['vertical']}:{channel}", Counter())[
                skeleton
            ] += 1
            lines.setdefault(channel, Counter()).update(
                _normalized_prose_lines(world, body)
            )

    def summarize(counter: Counter[Any]) -> dict[str, float | int]:
        total = sum(counter.values())
        return {
            "total": total,
            "unique": len(counter),
            "modal_share": max(counter.values(), default=0) / total if total else 0.0,
            "duplicate_share": sum(count - 1 for count in counter.values()) / total
            if total
            else 0.0,
        }

    result = {channel: summarize(counter) for channel, counter in skeletons.items()}
    result.update(
        {
            f"vertical:{key}": summarize(counter)
            for key, counter in vertical_skeletons.items()
        }
    )
    result.update(
        {f"lines:{channel}": summarize(counter) for channel, counter in lines.items()}
    )
    return result


def _structured_causal_source_keys(
    world: dict[str, Any], checkpoint: dict[str, Any]
) -> tuple[str, str]:
    required = _blueprint_gate(world["vertical"], checkpoint["gate_id"])[
        "required_artifacts"
    ]
    source_specs = [item for item in required if item["artifact_role"] == "evidence"]
    source_specs.extend(
        item for item in required if item["artifact_role"] == "supporting"
    )
    if len(source_specs) < 2:
        source_specs.extend(
            item for item in required if item["artifact_role"] == "decision"
        )
    return str(source_specs[0]["artifact_key"]), str(source_specs[1]["artifact_key"])


def _structured_causal_payload(
    world: dict[str, Any], checkpoint: dict[str, Any], spec: dict[str, Any]
) -> dict[str, Any]:
    if int(checkpoint["sequence"]) != int(world["intervention_sequence"]):
        return {}
    source_keys = _structured_causal_source_keys(world, checkpoint)
    artifact_key = str(spec["artifact_key"])
    if artifact_key not in source_keys:
        return {}
    if (
        world["causal_family"] == "competition"
        and world["variant"] == "hidden_influence"
    ):
        return {
            (
                "evaluation_status"
                if source_keys.index(artifact_key) == 0
                else "criteria_change_status"
            ): (
                "ranking_changed"
                if source_keys.index(artifact_key) == 0
                else "no_disclosed_change"
            )
        }
    cure = _causal_cure_data(world)
    position = source_keys.index(artifact_key)
    return {
        key: value
        for index, (key, value) in enumerate(cure.items())
        if index % len(source_keys) == position
    }


def _crm_current_state(
    world: dict[str, Any], checkpoint: dict[str, Any]
) -> dict[str, str]:
    sequence = int(checkpoint["sequence"])
    next_step = (
        f"account executive to confirm the {world['checkpoints'][sequence + 1]['visible_gate']} decision with buyer authority"
        if sequence + 1 < len(world["checkpoints"])
        else f"confirm the final {checkpoint['visible_gate']} disposition"
    )
    return {
        "stage": checkpoint["gate_id"],
        "close_date": world["forecast_close_date"],
        "next_step": next_step,
    }


def _sync_structured_artifact(record: dict[str, Any]) -> None:
    if "/structured/" not in str(record["content"]["source_uri"]):
        return
    record["content"]["body"] = json.dumps(
        {
            "artifact_key": record["artifact_key"],
            "gate_id": record["gate_id"],
            "structured_payload": record["structured_payload"],
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    record["checksum"] = _checksum(record["content"]["body"])


def _assign_crm_authorities(
    world: dict[str, Any], artifacts: list[dict[str, Any]]
) -> None:
    fields = ("stage", "close_date", "next_step")
    field_labels = {
        "stage": "Current buyer stage",
        "close_date": "Current buyer decision date",
        "next_step": "Current buyer next step",
    }
    for artifact in artifacts:
        artifact["authoritative_for"] = [
            value
            for value in artifact["authoritative_for"]
            if value not in {f"crm.{field}" for field in fields}
        ]
        payload = artifact["structured_payload"]
        payload["authoritative_for"] = [
            value
            for value in payload.get("authoritative_for", ())
            if value not in {f"crm.{field}" for field in fields}
        ]
        payload.pop("current_state", None)
        if artifact.get("projection_origin") is None:
            for field in fields:
                payload.pop(field, None)
        _sync_structured_artifact(artifact)

    for checkpoint in world["checkpoints"]:
        required = _blueprint_gate(world["vertical"], checkpoint["gate_id"])[
            "required_artifacts"
        ]
        roles = {
            str(item["artifact_key"]): str(item["artifact_role"]) for item in required
        }
        direct = [
            artifact
            for artifact in artifacts
            if artifact["gate_id"] == checkpoint["gate_id"]
            and artifact.get("projection_origin") is None
            and artifact["kind"] not in {"crm_record", "crm_history"}
        ]
        evidence = next(
            artifact
            for artifact in direct
            if roles.get(artifact["artifact_key"]) == "evidence"
        )
        decision = next(
            artifact
            for artifact in direct
            if roles.get(artifact["artifact_key"]) == "decision"
        )
        projection = next(
            artifact
            for artifact in artifacts
            if artifact["gate_id"] == checkpoint["gate_id"]
            and (artifact.get("projection_origin") or {}).get("transformation")
            == "structured_authority_projection"
        )
        used_source_ids = {evidence["artifact_id"], decision["artifact_id"]}
        eligible = [
            artifact
            for artifact in direct
            if artifact["artifact_id"] not in used_source_ids
            and artifact["available_at"] <= projection["created_at"]
        ]
        supporting = next(
            (
                artifact
                for artifact in eligible
                if roles.get(artifact["artifact_key"]) == "supporting"
            ),
            None,
        )
        if supporting is None:
            supporting = next(
                artifact
                for artifact in eligible
                if "/structured/" not in artifact["content"]["source_uri"]
            )
        sources = {
            "stage": evidence,
            "close_date": supporting,
            "next_step": decision,
        }
        if len({source["artifact_id"] for source in sources.values()}) != len(fields):
            raise ValueError("CRM authority sources must be distinct")
        state = _crm_current_state(world, checkpoint)
        for field, source in sources.items():
            value = state[field]
            source["authoritative_for"].append(f"crm.{field}")
            source["structured_payload"].setdefault("authoritative_for", []).append(
                f"crm.{field}"
            )
            source["structured_payload"][field] = value
            if "/structured/" in source["content"]["source_uri"]:
                _sync_structured_artifact(source)
            else:
                source["content"]["body"] += (
                    f"\n\n{field_labels[field]}: {str(value).replace('_', ' ')}."
                )
                source["checksum"] = _checksum(source["content"]["body"])

        source_ids = [sources[field]["artifact_id"] for field in fields]
        primary = sources["stage"]
        origin = {
            "source_artifact_id": primary["artifact_id"],
            "source_actor_id": primary["source_actor_ids"][0],
            "source_time": primary["available_at"],
            "transformation": "structured_authority_projection",
            "visible": True,
        }
        projection["derived_from_artifact_ids"] = source_ids
        projection["projection_origin"] = origin
        projection["structured_payload"].update(state)
        projection["structured_payload"]["projection_origin"] = origin
        _sync_structured_artifact(projection)

        for artifact in artifacts:
            if artifact["gate_id"] != checkpoint["gate_id"] or artifact["kind"] not in {
                "crm_record",
                "crm_history",
            }:
                continue
            if artifact["artifact_id"] == projection["artifact_id"]:
                continue
            field = str(artifact["structured_payload"].get("observed_field", ""))
            if field not in sources:
                continue
            source = sources[field]
            origin = {
                "source_artifact_id": source["artifact_id"],
                "source_actor_id": source["source_actor_ids"][0],
                "source_time": source["available_at"],
                "transformation": (
                    "crm_projection_from_stale_state"
                    if any(
                        defect["field"] == field
                        and defect["checkpoint_sequence"] == checkpoint["sequence"]
                        for defect in world["defects"]
                    )
                    else "crm_projection_from_gate_evidence"
                ),
                "visible": True,
            }
            artifact["derived_from_artifact_ids"] = [source["artifact_id"]]
            artifact["projection_origin"] = origin
            artifact["structured_payload"]["projection_origin"] = origin

    by_id = {artifact["artifact_id"]: artifact for artifact in artifacts}
    for artifact in artifacts:
        projection_origin = artifact.get("projection_origin")
        if not isinstance(projection_origin, dict):
            continue
        source_id = projection_origin.get("source_artifact_id")
        origin_source = by_id.get(source_id)
        if (
            origin_source is None
            or projection_origin.get("source_time") != origin_source["available_at"]
        ):
            raise ValueError("projection origin must identify its current source")
        parent_ids = list(
            dict.fromkeys([*artifact.get("derived_from_artifact_ids", ()), source_id])
        )
        parents = [by_id.get(parent_id) for parent_id in parent_ids]
        if any(parent is None for parent in parents):
            raise ValueError("projection lineage must identify existing parents")
        artifact["derived_from_artifact_ids"] = parent_ids
        artifact["created_at"] = max(
            artifact["created_at"],
            *(parent["available_at"] for parent in parents if parent is not None),
        )
        if artifact["created_at"] > artifact["available_at"]:
            raise ValueError("projection cannot predate its sources")
        if artifact["structured_payload"].get("projection_origin") != projection_origin:
            artifact["structured_payload"]["projection_origin"] = projection_origin
            _sync_structured_artifact(artifact)


def _crm_authority_errors(
    world: dict[str, Any], artifacts: Sequence[dict[str, Any]]
) -> list[str]:
    errors: list[str] = []
    fields = ("stage", "close_date", "next_step")
    if any("current_state" in artifact["structured_payload"] for artifact in artifacts):
        errors.append(f"current_state_map={world['world_id']}")
    for checkpoint in world["checkpoints"]:
        state = _crm_current_state(world, checkpoint)
        sources: dict[str, dict[str, Any]] = {}
        for field in fields:
            matches = [
                artifact
                for artifact in artifacts
                if artifact["gate_id"] == checkpoint["gate_id"]
                and artifact.get("projection_origin") is None
                and f"crm.{field}" in artifact["authoritative_for"]
            ]
            if len(matches) != 1:
                errors.append(
                    f"current_crm_authority={world['world_id']}:{checkpoint['gate_id']}:{field}"
                )
                continue
            source = matches[0]
            sources[field] = source
            if source["structured_payload"].get(field) != state[field]:
                errors.append(
                    f"current_crm_value={world['world_id']}:{checkpoint['gate_id']}:{field}"
                )
            if (
                len(
                    {
                        value
                        for value in source["authoritative_for"]
                        if value.startswith("crm.")
                    }
                )
                != 1
            ):
                errors.append(
                    f"current_crm_scope={world['world_id']}:{checkpoint['gate_id']}:{field}"
                )
        if len(sources) != len(fields):
            continue
        if len({source["artifact_id"] for source in sources.values()}) != len(fields):
            errors.append(
                f"current_crm_distinct={world['world_id']}:{checkpoint['gate_id']}"
            )
        projections = [
            artifact
            for artifact in artifacts
            if artifact["gate_id"] == checkpoint["gate_id"]
            and (artifact.get("projection_origin") or {}).get("transformation")
            == "structured_authority_projection"
        ]
        if len(projections) != 1:
            errors.append(
                f"current_crm_projection={world['world_id']}:{checkpoint['gate_id']}"
            )
            continue
        projection = projections[0]
        source_ids = {source["artifact_id"] for source in sources.values()}
        if (
            set(projection["derived_from_artifact_ids"]) != source_ids
            or projection["projection_origin"]["source_artifact_id"] not in source_ids
            or any(
                projection["structured_payload"].get(field) != state[field]
                for field in fields
            )
        ):
            errors.append(
                f"current_crm_origin={world['world_id']}:{checkpoint['gate_id']}"
            )
    return errors


def _fact_source_ids(fact_ids: Iterable[str]) -> list[str]:
    wanted = set(fact_ids)
    return sorted(
        str(source["source_id"])
        for source in _source_registry()["sources"]
        if wanted.intersection(source.get("fact_ids", ()))
    )


def _checkpoint_contract(
    vertical: dict[str, Any], gate: str, sequence: int
) -> dict[str, Any]:
    blueprint = _blueprint_gate(vertical["id"], gate)
    fact_ids = tuple(blueprint["source_fact_ids"])
    visible_gate = gate.replace("_", " ")
    conditional_on = blueprint.get("conditional_on")
    conditional_route = blueprint.get("conditional_route")
    condition_text = ""
    if conditional_on:
        condition_text = (
            f" This checkpoint applies only when {conditional_on['field']} is "
            f"{conditional_on['equals']}."
        )
    elif conditional_route:
        condition_text = f" This checkpoint is conditional on the {conditional_route['route']} route."
    return {
        "gate_id": gate,
        "source_fact_ids": fact_ids,
        "required_artifact_keys": tuple(
            item["artifact_key"] for item in blueprint["required_artifacts"]
        ),
        "required_artifact_roles": {
            item["artifact_key"]: item["artifact_role"]
            for item in blueprint["required_artifacts"]
        },
        "label": gate,
        "authority_role_ids": tuple(blueprint["authority_role_ids"]),
        "authority_rights": tuple(blueprint["authority_rights"]),
        "required_payload_fields": tuple(blueprint["required_payload_fields"]),
        "decision_route": blueprint["decision_route"],
        "recovery_decisions": tuple(blueprint["recovery_decisions"]),
        "availability_delay_bounds": blueprint["availability_delay_bounds"],
        "visible_gate": visible_gate,
        "business_objective": f"Determine whether the buyer can complete {visible_gate} with an accountable owner and a dated decision.",
        "decision_condition": f"Proceed when the accountable buyer has made the {visible_gate} decision and the remaining uncertainty is understood.{condition_text}",
        "role_deliverables": {
            "account_executive": f"Confirm the buyer owner, decision date, and open commitment for {visible_gate}.",
            "domain_specialist": "Review the buyer's current source records and report material limits.",
            "sales_manager": "Review forecast support and resolve any commercial or policy exception.",
            "revops": "Keep the forecast aligned with the latest dated buyer record.",
        },
        "completion_conditions": [
            f"The accountable buyer's position on {visible_gate} is clear.",
            "Ownership and timing for the next business decision are clear.",
        ],
        "policy_entrypoints": [
            _opaque_id(
                "policy",
                DATASET_SEED,
                VERTICALS.index(vertical),
                sequence % len(SHARED_THEMES),
            )
        ],
    }


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
    checkpoint_activation: dict[str, str],
    late_activation_at: str | None,
    late_actor_role_id: str | None,
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
    all_gate_ids = list(vertical["gates"])
    all_authority_rights = sorted(
        {
            right
            for gate_id in all_gate_ids
            for right in _blueprint_gate(vertical["id"], gate_id)["authority_rights"]
        }
    )
    for role_index, (role, job_title, authority_level, department) in enumerate(roles):
        name = _person_name(identity_seed, vertical_index, family_index, role_index)
        active_from = start_at
        if f"buyer.{role}" == late_actor_role_id and late_activation_at:
            active_from = late_activation_at
        result.append(
            {
                "actor_id": _opaque_id("act", identity_seed, role),
                "kind": "buyer",
                "display_name": name,
                "organization_id": buyer_org_id,
                "role_tags": [role],
                "active_from": active_from,
                "email": f"{_slug(name)}@{buyer_domain}",
                "visibility": "public",
                "synthetic": True,
                "authority": {
                    "role_id": f"buyer.{role}",
                    "rights": ["provide_buyer_input", *all_authority_rights],
                    "gate_ids": all_gate_ids,
                },
                "attributes": {
                    "job_title": job_title,
                    "authority_level": authority_level,
                    "department": department,
                },
            }
        )
    authority_roles: dict[str, dict[str, Any]] = {}
    authority_profiles = VERTICAL_BLUEPRINTS["verticals"][vertical["id"]][
        "authority_actor_profiles"
    ]
    for gate_id in all_gate_ids:
        blueprint = _blueprint_gate(vertical["id"], gate_id)
        for role_id in blueprint["authority_role_ids"]:
            entry = authority_roles.setdefault(
                role_id,
                {
                    "rights": [],
                    "gate_ids": [],
                    "active_from": start_at,
                },
            )
            entry["rights"].extend(
                str(right) for right in blueprint["authority_rights"]
            )
            entry["gate_ids"].append(gate_id)
        for role_id, scope, job_title, right in ADDITIONAL_AUTHORITIES.get(
            vertical["id"], {}
        ).get(gate_id, ()):
            authority_roles[role_id] = {
                "rights": [right],
                "gate_ids": [gate_id],
                "active_from": start_at,
            }
    window = _causal_window(vertical["id"], FAMILIES[family_index])
    resolution_blueprint = _blueprint_gate(vertical["id"], window["resolution_gate"])
    for role_id in window["authority_role_ids"]:
        if str(role_id).startswith("buyer."):
            continue
        entry = authority_roles.setdefault(
            role_id,
            {"rights": [], "gate_ids": [], "active_from": start_at},
        )
        entry["rights"].extend(resolution_blueprint["authority_rights"])
        entry["gate_ids"].append(window["resolution_gate"])
    for gate_id in all_gate_ids:
        blueprint = _blueprint_gate(vertical["id"], gate_id)
        checkpoint = {
            "gate_id": gate_id,
            "authority_role_ids": blueprint["authority_role_ids"],
        }
        for role_id in _checkpoint_authority_role_ids(
            {"vertical": vertical["id"]}, checkpoint
        ):
            if role_id in authority_roles:
                authority_roles[role_id]["gate_ids"].append(gate_id)
    for role_index, (role_id, authority) in enumerate(
        sorted(authority_roles.items()), start=100
    ):
        short_role = role_id.rsplit(".", 1)[-1].removesuffix("_authority")
        extra_profile = next(
            (
                (scope, job_title)
                for values in ADDITIONAL_AUTHORITIES.get(vertical["id"], {}).values()
                for candidate, scope, job_title, _ in values
                if candidate == role_id
            ),
            None,
        )
        profile = (
            {
                "organization_scope": extra_profile[0],
                "kind": "internal"
                if extra_profile[0] == "seller"
                else "external"
                if extra_profile[0] == "third_party"
                else "buyer",
                "job_title": extra_profile[1],
            }
            if extra_profile is not None
            else authority_profiles[role_id]
        )
        if role_id == "legal.fee_authority":
            profile = {
                **profile,
                "organization_scope": "buyer",
                "kind": "buyer",
            }
        job_title = str(profile["job_title"])
        name = _person_name(
            identity_seed, vertical_index + 20, family_index, role_index
        )
        active_from = authority["active_from"]
        if role_id == late_actor_role_id and late_activation_at:
            active_from = late_activation_at
        scope = str(profile["organization_scope"])
        organization_id = {
            "buyer": buyer_org_id,
            "seller": seller_org_id,
            "third_party": _opaque_id("org", identity_seed, role_id),
        }[scope]
        domain = {
            "buyer": buyer_domain,
            "seller": vertical["domain"],
            "third_party": f"{_slug(short_role)}.example",
        }[scope]
        result.append(
            {
                "actor_id": _opaque_id("act", identity_seed, "authority", role_id),
                "kind": str(profile["kind"]),
                "display_name": name,
                "organization_id": organization_id,
                "role_tags": ["authority", "gatekeeper"],
                "active_from": active_from,
                "email": f"{_slug(name)}@{domain}",
                "visibility": "public",
                "synthetic": True,
                "authority": {
                    "role_id": role_id,
                    "rights": sorted(set(authority["rights"])),
                    "gate_ids": sorted(set(authority["gate_ids"])),
                },
                "attributes": {
                    "job_title": job_title,
                    "authority_level": "final_decider"
                    if any(token in role_id for token in ("executive", "committee"))
                    else "approver",
                    "department": scope.replace("_", " ").title(),
                    "organization_scope": scope,
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
                "synthetic": True,
                "authority": {
                    "role_id": f"seller.{role}",
                    "rights": ["manage_seller_workflow"],
                    "gate_ids": all_gate_ids,
                },
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
    evidence_channels = {_causal_artifact_channel(world)}
    if (
        checkpoint["sequence"] != world["intervention_sequence"]
        or artifact_type not in evidence_channels
    ):
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
    cure_data = _causal_cure_data(world)
    requirement = str(cure_data.get("requirement", "")).replace("_", " ")
    coverage = str(cure_data.get("seller_coverage", "")).replace("_", " ")
    signal = str(cure_data.get("signal", "")).replace("_", " ")
    external_facts = _vertical_causal_facts(
        world["vertical"],
        "external_event",
        world["variant"],
        include_source=True,
    )
    external_source = str(external_facts["source"]).replace("_", " ")
    external_details = ", and ".join(
        f"{key.replace('_', ' ')} is {str(value).replace('_', ' ')}"
        for key, value in external_facts.items()
        if key != "source"
    )
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
            "within_fit": f"{evaluator['display_name']} recorded the {requirement} in revision 2 for the {checkpoint['label'].replace('_', ' ')} gate; seller coverage is {coverage}.",
            "out_of_fit": f"{evaluator['display_name']} recorded the {requirement} in revision 2 for the {checkpoint['label'].replace('_', ' ')} gate; seller coverage is {coverage}.",
        },
        "competition": {
            "transparent": f"{recipient['display_name']} recorded {signal} for the {checkpoint['label'].replace('_', ' ')} gate in the buyer's evaluation record.",
            "hidden_influence": f"The buyer evaluation ranking changed for the {checkpoint['label'].replace('_', ' ')} gate without a disclosed criteria change.",
        },
        "external_event": {
            "recoverable": f"The {external_source} reports that {external_details}; {recipient['display_name']} recorded the update for the {checkpoint['label'].replace('_', ' ')} gate.",
            "terminal": f"The {external_source} reports that {external_details} for the {checkpoint['label'].replace('_', ' ')} gate.",
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
    prose_variant = _prose_variant(world, artifact_type, channel_index, checkpoint)
    template = prose_variant
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
    evidence_requirement = _vertical_facts(world["vertical"])["evidence_by_gate"][
        checkpoint["label"]
    ]
    evidence_line = (
        evidence
        or (
            f"The {previous_gate} record is the dated basis for {evidence_requirement}.",
            f"The open {evidence_requirement} question still points back to the {previous_gate} record.",
            f"No later buyer source displaces the {previous_gate} record on {evidence_requirement}.",
            f"Reviewers still cite the {previous_gate} record for {evidence_requirement}.",
            f"The latest buyer-backed entry for {evidence_requirement} remains at {previous_gate}.",
            f"The {previous_gate} source still governs the unresolved {evidence_requirement} item.",
            f"The dated {previous_gate} note remains the traceable source for {evidence_requirement}.",
            f"The current record for {evidence_requirement} has not moved beyond {previous_gate}.",
        )[prose_variant]
    )
    transition = PROSE_TRANSITIONS[prose_variant]
    record_line = (
        f"The next review should test {evidence_requirement} against a dated buyer reply.",
        f"Keep the owner of {evidence_requirement} distinct from the person recording it.",
        f"Any change to {evidence_requirement} needs its own buyer-backed date and source.",
        f"Preserve the open limit on {evidence_requirement} until the buyer decides.",
        f"Treat the current {evidence_requirement} entry as a source claim, not a forecast fact.",
        f"Carry the unresolved {evidence_requirement} item with its accountable buyer role.",
        f"Compare any new {evidence_requirement} statement with the dated record before replacing it.",
        f"Keep the {evidence_requirement} decision open until its buyer source is current.",
    )[
        _stable_seed(
            world["seed"],
            checkpoint["gate_id"],
            artifact_type,
            channel_index,
            "record-line",
        )
        % 8
    ]
    context_lines = (
        (transition, evidence_line, record_line),
        (evidence_line, record_line, transition),
        (record_line, transition, evidence_line),
        (evidence_line, transition, record_line),
    )[prose_variant % 4]
    if artifact_type == "transcript":
        exchanges = (
            (
                f"{source_name} ({source_role}): Who accepts the {evidence_requirement} needed to clear {gate}?",
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
            (
                f"{recipient_name} ({recipient_role}): The {gate} review is waiting on a dated buyer position.",
                f"{source_name} ({source_role}): I will identify who can decide and keep assumptions outside the forecast.",
                f"{recipient_name} ({recipient_role}): Bring the unresolved {previous_gate} item into that discussion.",
            ),
            (
                f"{source_name} ({source_role}): Which part of {evidence_requirement} remains disputed for {gate}?",
                f"{recipient_name} ({recipient_role}): The decision owner needs a concise comparison before responding.",
                f"{source_name} ({source_role}): I will preserve both the current position and the open question.",
            ),
            (
                f"{recipient_name} ({recipient_role}): The {amount} request has not cleared the {gate} decision group.",
                f"{source_name} ({source_role}): I will separate timing risk from the substantive decision.",
                f"{recipient_name} ({recipient_role}): Keep the next discussion tied to the accountable buyer.",
            ),
            (
                f"{source_name} ({source_role}): The latest {gate} note conflicts with the carried {previous_gate} assumption.",
                f"{recipient_name} ({recipient_role}): Use the dated buyer statement and mark the older projection as stale.",
                f"{source_name} ({source_role}): I will leave the decision open until the owner responds.",
            ),
            (
                f"{recipient_name} ({recipient_role}): Before {gate} moves, we need the owner, decision date, and remaining limit.",
                f"{source_name} ({source_role}): I will return with those three facts from the buyer record.",
                f"{recipient_name} ({recipient_role}): Do not infer acceptance from meeting attendance.",
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
                *(
                    f"{recipient_name} ({recipient_role}): {line}"
                    for line in context_lines
                ),
            )
        )
    if artifact_type == "email":
        paragraphs = (
            f"I captured the {gate} owner, the open {evidence_requirement}, and the next step for the {vertical['motion']}. Please confirm the decision group and target date.",
            f"The attached thread carries forward the {previous_gate} facts. For {gate}, please identify the approver for the {amount} request and any condition still open.",
            f"Today we agreed not to advance beyond {gate} until the source record is reconciled. Please reply with corrections to the owner, amount, or timing.",
            f"The {gate} discussion left one buyer decision open. Please confirm who owns it, when they expect to decide, and which assumption should remain outside the forecast.",
            f"Our records disagree on the current {gate} position. Please point me to the latest dated buyer statement and identify any older projection we should preserve as stale.",
            f"Before the {amount} request moves, the {gate} group needs a clear decision and accountable owner. Please reply with the remaining limit and expected decision date.",
            f"I separated the confirmed {previous_gate} facts from the open {gate} questions. Please correct the buyer position, ownership, or timing if any item has changed.",
            f"The next {vertical['motion']} discussion should focus on the unresolved {evidence_requirement}. Please name the buyer decision maker and the date that now governs the plan.",
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
                *context_lines,
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
            (
                f"{source_name}: The buyer position for {gate} is still open.",
                f"{recipient_name}: I will keep the decision, owner, and timing separate until we have a dated response.",
            ),
            (
                f"{source_name}: The current {gate} note conflicts with the older projection.",
                f"{recipient_name}: I will preserve both records and identify which buyer source is current.",
            ),
            (
                f"{source_name}: The {amount} request needs a named decision owner.",
                f"{recipient_name}: I will confirm the authority path without treating attendance as approval.",
            ),
            (
                f"{source_name}: Separate the {previous_gate} carryover from the new {gate} question.",
                f"{recipient_name}: I will update only the field supported by the latest source.",
            ),
            (
                f"{source_name}: We need the buyer's dated {gate} position before the next review.",
                f"{recipient_name}: I will return with the owner, date, and unresolved business limit.",
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
                *(f"{source_name}: {line}" for line in context_lines),
            )
        )
    if artifact_type == "document":
        headings = (
            "Decision record",
            "Review package",
            "Approval brief",
            "Buyer position",
            "Open question log",
            "Dated source review",
            "Ownership summary",
            "Decision timing note",
        )
        focuses = (
            f"Document the accountable owner and acceptance of {evidence_requirement} for {gate}.",
            f"Reconcile the {previous_gate} source record before presenting the {amount} package.",
            f"Separate committed terms, unresolved questions, and approval conditions for {gate}.",
            f"State the buyer's current {gate} position without inferring acceptance from activity.",
            f"List the unresolved {evidence_requirement} question and the buyer role expected to decide it.",
            f"Compare the dated {previous_gate} source with the current {gate} statement and preserve both.",
            f"Identify the accountable buyer, the next decision, and any internal owner for the {amount} request.",
            f"Record the expected {gate} decision date and distinguish it from the forecast assumption.",
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
            f"- Confirm the accountable owner for {gate} and {evidence_requirement}.",
            f"- Link the current {evidence_requirement} source to the {vertical['label']} gate record.",
            f"- Keep any commercial exception affecting {evidence_requirement} within the approval matrix.",
            *(f"- {line}" for line in context_lines),
        ]
        if channel_index % 4 == 1:
            primary = world["amount_minor_units"] * 82 // 100
            secondary = world["amount_minor_units"] - primary
            lines.extend(
                (
                    "",
                    "## Pricing",
                    "",
                    f"- Primary scope for {evidence_requirement}: {_money(primary, world['currency'])} ({primary} minor units)",
                    f"- Delivery and contingency for {evidence_requirement}: {_money(secondary, world['currency'])} ({secondary} minor units)",
                    f"- Total tied to {evidence_requirement}: {amount} ({world['amount_minor_units']} minor units)",
                )
            )
        return "\n".join(lines)
    if artifact_type == "web_news":
        leads = (
            f"A synthetic market brief tracks capacity and approval conditions relevant to {vertical['buyer_industry']}.",
            f"A synthetic trade bulletin reviews timing pressure around the {gate} stage of a {vertical['motion']}.",
            f"A synthetic company notice reports a planning update that may affect the {amount} initiative.",
            f"A synthetic sector report compares current {gate} conditions with the prior planning cycle.",
            f"A synthetic regulatory notice describes a dated change relevant to the {vertical['motion']} decision.",
            f"A synthetic operations bulletin flags a new timing constraint for organizations in {vertical['buyer_industry']}.",
            f"A synthetic issuer update reports a business condition that may alter the {gate} review.",
            f"A synthetic market notice separates confirmed facts from unresolved implications for the {amount} initiative.",
        )
        artifact_id = _opaque_id(
            "artifact", world["world_id"], artifact_type, channel_index
        )
        confirmations = (
            f"Confirm the signal against buyer evidence for {evidence_requirement} before changing the forecast.",
            f"Keep the {evidence_requirement} forecast unchanged until a dated buyer source confirms the signal.",
            f"Reconcile this signal with the current {evidence_requirement} record before revising timing.",
            f"Treat the signal as context for {evidence_requirement}, not as buyer acceptance.",
            f"Verify the signal with the accountable buyer for {evidence_requirement} before acting.",
            f"Preserve the prior {evidence_requirement} record until this signal has buyer support.",
            f"Record any effect on {evidence_requirement} separately from the external signal.",
            f"Do not replace the dated {evidence_requirement} source with this market notice alone.",
        )
        return "\n".join(
            (
                f"# {title}",
                "",
                "- Publisher: EDLB Synthetic Wire",
                f"- Published: {checkpoint['available_at']}",
                f"- URL: https://edlb.example/signals/{artifact_id}",
                "",
                leads[template],
                *context_lines,
                confirmations[template],
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
    if (
        artifact_type == "document"
        and channel_index < 2
        and world["vertical"] == "manufacturing"
        and world["causal_family"] == "champion_exit"
    ):
        artifact_id = _opaque_id("artifact", world["world_id"], 57 + channel_index)
    else:
        artifact_id = _opaque_id(
            "artifact", world["world_id"], artifact_type, channel_index
        )
    extension = "json" if artifact_type in {"crm", "calendar"} else "md"
    path = f"artifacts/{artifact_type}/{artifact_id}.{extension}"
    evidence = _causal_evidence(
        world, checkpoint, artifact_type, source_actor, recipient
    )
    created_at, available_at = _artifact_times(
        world, artifact_type, channel_index, checkpoint
    )
    if artifact_type == "crm":
        created_at, available_at = _structured_times(world, checkpoint, 0, 1)
    artifact_checkpoint = {**checkpoint, "available_at": available_at}
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
            "next_step": _crm_current_state(world, checkpoint)["next_step"],
            "owner": source_actor["email"],
            "owner_role": _actor_label(source_actor),
            "observed_field": field,
            "observed_value": observed,
            "last_modified": available_at,
            "checkpoint_sequence": checkpoint["sequence"],
            "projection_sequence": checkpoint["sequence"],
            "verification_basis": evidence
            or f"Reconcile against the {gate} meeting and buyer reply before advancing.",
        }
        body_value[field] = observed
        body = json.dumps(body_value, ensure_ascii=False, sort_keys=True, indent=2)
    elif artifact_type == "calendar":
        agendas = (
            f"Review {gate} evidence, owner, and next decision.",
            f"Carry forward the prior commitment and resolve the {_money(world['amount_minor_units'], world['currency'])} approval path.",
            f"Confirm the {vertical['label']} gate sequence, stakeholder attendance, and source record.",
        )
        body_value = {
            "subject": subject_for_calendar(world, checkpoint),
            "start": available_at,
            "end": _timestamp(
                checkpoint["date"],
                min(17, int(available_at[11:13]) + 1),
                int(available_at[14:16]),
            ),
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
            artifact_checkpoint,
            source_actor,
            recipient,
            evidence,
        )
    if artifact_type == "crm":
        kind = "crm_history" if channel_index % 3 == 2 else "crm_record"
    elif artifact_type == "document":
        kind = ("proposal", "quote", "diligence_document")[channel_index % 3]
    elif artifact_type == "web_news":
        kind = "web_page" if channel_index % 2 == 0 else "news_item"
    else:
        kind = ARTIFACT_KINDS[artifact_type]
    record: dict[str, Any] = {
        "artifact_id": artifact_id,
        "world_id": world["world_id"],
        "kind": kind,
        "title": f"{vertical['label']} {gate.replace('_', ' ')} update {channel_index + 1}",
        "created_at": created_at,
        "available_at": available_at,
        "visibility": "public"
        if artifact_type == "web_news"
        else "role_scoped"
        if artifact_type in {"internal_chat", "crm"}
        else "agent_visible",
        "synthetic": True,
        "source_actor_ids": [source_actor["actor_id"]],
        "recipient_actor_ids": [recipient["actor_id"]],
        "thread_id": _opaque_id(
            "thread", world["world_id"], artifact_type, channel_index % 3
        ),
        "version": 1 if artifact_type == "document" else None,
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
            "source_ids": _fact_source_ids(checkpoint["source_fact_ids"]),
            "fact_ids": list(checkpoint["source_fact_ids"]),
            "license": "CC-BY-4.0",
        },
        "gate_id": checkpoint["gate_id"],
        "artifact_key": f"{checkpoint['gate_id']}_{artifact_type}_{channel_index}",
        "structured_payload": {
            "gate_id": checkpoint["gate_id"],
            "source_fact_ids": list(checkpoint["source_fact_ids"]),
            "authority_role_id": None,
            "decision_state": "unverified",
            "decision_owner_actor_id": None,
            "projection_sequence": checkpoint["sequence"]
            if artifact_type == "crm"
            else None,
        },
        "authoritative_for": [],
        "recipient_role_ids": [_actor_role(recipient)],
        "projection_origin": {
            "source_artifact_id": _opaque_id(
                "artifact",
                world["world_id"],
                "structured",
                checkpoint["sequence"],
                "evidence",
            ),
            "source_actor_id": source_actor["actor_id"],
            "source_time": available_at,
            "transformation": "crm_projection_from_gate_evidence",
            "visible": True,
        }
        if artifact_type == "crm"
        else None,
        "logical_document_id": _opaque_id(
            "logical-document", world["world_id"], checkpoint["gate_id"], channel_index
        )
        if artifact_type == "document"
        else None,
        "supersedes_artifact_id": None,
        "derived_from_artifact_ids": [],
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
    start = _next_business_day(
        date(2025, 1, 6) + timedelta(days=vertical_index * 13 + family_index * 7)
    )
    pair_seed = _stable_seed(dataset_seed, vertical_index, family_index) % (2**63 - 1)
    gate_route = _world_gate_route(vertical, family, pair_seed)
    checkpoint_count = len(gate_route)
    pair_id = _opaque_id("pair", pair_seed, "pair")
    world_id = _opaque_id("world", pair_seed, variant)
    checkpoint_days = _business_checkpoint_days(
        start, duration, checkpoint_count, pair_seed
    )
    duration = checkpoint_days[-1]
    checkpoints = []
    for index, day in enumerate(checkpoint_days):
        gate = gate_route[index]
        checkpoints.append(
            {
                "checkpoint_id": _opaque_id("checkpoint", world_id, index),
                "sequence": index,
                "day": day,
                "date": _date_text(start, day),
                "available_at": _checkpoint_time(
                    pair_seed, gate, _date_text(start, day)
                ),
                "label": gate,
                "status": "pending" if index else "active",
                **_checkpoint_contract(vertical, gate, index),
            }
        )
    buyer_name = _company_name(pair_id)
    buyer_domain = f"{_slug(buyer_name)}.example"
    buyer_org_id = _opaque_id("org", pair_id, "buyer")
    seller_org_id = _opaque_id("org", DATASET_SEED, "seller", vertical_index)
    identity_seed = _stable_seed(pair_id, "identities")
    checkpoint_activation = {
        checkpoint["label"]: checkpoint["available_at"] for checkpoint in checkpoints
    }
    causal_window = _causal_window(vertical["id"], family)
    sequence_by_gate = {
        checkpoint["gate_id"]: int(checkpoint["sequence"]) for checkpoint in checkpoints
    }
    intervention_sequence = sequence_by_gate[causal_window["intervention_gate"]]
    resolution_sequence = sequence_by_gate[causal_window["resolution_gate"]]
    late_actor_role_id = (
        "buyer.executive_sponsor" if family == "late_stakeholder" else None
    )
    late_activation_at = (
        _causal_event_times(checkpoints[intervention_sequence])[0]
        if late_actor_role_id
        else None
    )
    actors = _actors(
        vertical,
        vertical_index,
        family_index,
        identity_seed,
        buyer_name,
        buyer_domain,
        buyer_org_id,
        seller_org_id,
        _timestamp(start, 8),
        checkpoint_activation,
        late_activation_at,
        late_actor_role_id,
    )
    base_amount, family_increment = AMOUNT_MINOR_UNITS[vertical["id"]]
    amount_minor_units = base_amount + family_index * family_increment
    forecast_close_date = _date_text(start, min(duration, checkpoint_days[-2]))
    if family == "champion_exit":
        champion = next(actor for actor in actors if actor["role_tags"] == ["champion"])
        champion["active_until"] = _causal_event_times(
            checkpoints[intervention_sequence]
        )[0]
    origins = _vertical_facts(vertical["id"])["crm_origins"]
    defect_sequences = tuple(
        min(index, max(0, resolution_sequence - 1)) for index in range(3)
    )
    next_step_sequence = defect_sequences[2]
    next_step_checkpoint = checkpoints[next_step_sequence]
    next_step_target = checkpoints[next_step_sequence + 1]
    defects = [
        {
            "defect_id": _opaque_id("defect", pair_seed, 1),
            "checkpoint_sequence": defect_sequences[0],
            "field": "stage",
            "observed_value": "prospecting",
            "truth_value": checkpoints[defect_sequences[0]]["gate_id"],
            "origin": origins[0],
            "evidence_role": "technical_evaluator",
        },
        {
            "defect_id": _opaque_id("defect", pair_seed, 2),
            "checkpoint_sequence": defect_sequences[1],
            "field": "close_date",
            "observed_value": checkpoints[defect_sequences[1]]["date"],
            "truth_value": forecast_close_date,
            "origin": origins[1],
            "evidence_role": "champion",
        },
        {
            "defect_id": _opaque_id("defect", pair_seed, 3),
            "checkpoint_sequence": next_step_sequence,
            "field": "next_step",
            "observed_value": f"confirm {next_step_checkpoint['visible_gate']} evidence and owner",
            "truth_value": f"account executive to confirm the {next_step_target['visible_gate']} decision with buyer authority",
            "origin": origins[2],
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
        "jurisdiction": vertical.get("jurisdiction"),
        "deal_name": f"{buyer_name} {vertical['motion']}",
        "motion": vertical["motion"],
        "currency": vertical["currency"],
        "amount_minor_units": amount_minor_units,
        "delivery_method": vertical.get("delivery_method"),
        "project_sector": vertical.get("project_sector"),
        "forecast_close_date": forecast_close_date,
        "causal_family": family,
        "variant": variant_name,
        "variant_index": variant,
        "release_visibility": "public",
        "split": _world_split(vertical_index, family_index),
        "seed": pair_seed,
        "start_date": start.isoformat(),
        "start_at": _timestamp(start, 8),
        "end_at": _timestamp(_date_text(start, duration), 18),
        "duration_days": duration,
        "checkpoint_count": checkpoint_count,
        "checkpoint_ids": [checkpoint["checkpoint_id"] for checkpoint in checkpoints],
        "intervention_checkpoint_id": checkpoints[intervention_sequence][
            "checkpoint_id"
        ],
        "intervention_sequence": intervention_sequence,
        "intervention_gate": causal_window["intervention_gate"],
        "resolution_checkpoint_id": checkpoints[resolution_sequence]["checkpoint_id"],
        "resolution_sequence": resolution_sequence,
        "resolution_gate": causal_window["resolution_gate"],
        "causal_action_code": causal_window["action_code"],
        "observable_cure": causal_window["observable_cure"],
        "causal_owner_role": causal_window["owner_role"],
        "causal_authority_role_ids": list(causal_window["authority_role_ids"]),
        "late_actor_role_id": late_actor_role_id,
        "late_activation_at": late_activation_at,
        "checkpoints": checkpoints,
        "actors": actors,
        "defects": defects,
        "reference_outcome": _outcome(family, variant),
        "outcome_reason": _outcome_reason(family, variant_name),
        "family_description": _family_description(family, variant_name),
        "gates": list(gate_route),
        "artifact_counts": _artifact_counts(pair_seed, vertical),
    }


def _build_events(
    world: dict[str, Any], artifacts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    observable_event_id = _opaque_id(
        "event", world["world_id"], "observable-intervention"
    )
    visible: list[dict[str, Any]] = [
        {
            "event_id": _opaque_id("event", world["world_id"], "meeting-booked"),
            "world_id": world["world_id"],
            "kind": "meeting_booked",
            "effective_at": world["start_at"],
            "recorded_at": world["start_at"],
            "available_at": world["start_at"],
            "actor_ids": [
                _active_buyer(world, world["start_at"])["actor_id"],
                next(
                    actor["actor_id"]
                    for actor in world["actors"]
                    if actor["authority"]["role_id"] == "seller.account_executive"
                ),
            ],
            "artifact_ids": [],
            "visibility": "agent_visible",
            "channel": "calendar",
            "causal_parent_ids": [],
            "visible_roles": [],
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
        "contract": ("document_created", "document"),
        "diligence_document": ("document_created", "document"),
        "policy_document": ("document_created", "document"),
        "web_page": ("external_signal_published", "web_signal"),
        "news_item": ("external_signal_published", "web_signal"),
    }
    for sequence, artifact in enumerate(
        sorted(artifacts, key=lambda item: (item["available_at"], item["artifact_id"])),
        1,
    ):
        kind, channel = kind_by_artifact[artifact["kind"]]
        checkpoint = _artifact_checkpoint(world, artifact)
        event = {
            "event_id": _opaque_id("event", world["world_id"], "artifact", sequence),
            "world_id": world["world_id"],
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
            "causal_parent_ids": (
                [observable_event_id]
                if checkpoint["sequence"] >= world["intervention_sequence"]
                else []
            ),
            "visible_roles": artifact.get("visible_roles", []),
            "payload": {
                "title": artifact["title"],
                "source_uri": artifact["content"]["source_uri"],
            },
        }
        if event["visibility"] == "role_scoped":
            event["visible_roles"] = artifact["visible_roles"]
        visible.append(event)
    release_checkpoint = world["checkpoints"][world["intervention_sequence"]]
    (
        family_event_effective_at,
        family_event_recorded_at,
        family_event_available_at,
    ) = _causal_event_times(release_checkpoint)
    family_event_id = _opaque_id("event", world["world_id"], "causal-intervention")
    external_profile = VERTICAL_BLUEPRINTS["verticals"][world["vertical"]][
        "external_observation"
    ]
    event_kind = {
        "champion_exit": "stakeholder_departed",
        "late_stakeholder": "stakeholder_joined",
        "budget_shock": "budget_changed",
        "requirements_change": "requirement_changed",
        "competition": "message_sent",
        "external_event": (
            "message_sent"
            if external_profile["channel"] == "email"
            else "document_revised"
        ),
    }[world["causal_family"]]
    buyer = {
        _actor_role(actor): actor
        for actor in world["actors"]
        if actor["kind"] == "buyer"
    }
    actors_by_role = {
        actor["authority"]["role_id"]: actor
        for actor in world["actors"]
        if actor["authority"].get("role_id")
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
                "active_from": world["late_activation_at"],
                "stated_position": "requested_approval_path",
                "source": "meeting_record",
            },
            "blocking": {
                "stakeholder_actor_id": buyer["executive_sponsor"]["actor_id"],
                "change": "joined_decision_group",
                "active_from": world["late_activation_at"],
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
                **_vertical_causal_facts(
                    world["vertical"], "requirements_change", "within_fit"
                ),
                "source": "validation_record",
            },
            "out_of_fit": {
                "stakeholder_actor_id": buyer["technical_evaluator"]["actor_id"],
                **_vertical_causal_facts(
                    world["vertical"], "requirements_change", "out_of_fit"
                ),
                "source": "validation_record",
            },
        },
        "competition": {
            "transparent": {
                "stakeholder_actor_id": buyer["procurement"]["actor_id"],
                **_vertical_causal_facts(
                    world["vertical"], "competition", "transparent"
                ),
                "disclosure_channel": "buyer_meeting",
                "source": "buyer_record",
            },
            "hidden_influence": {
                "stakeholder_actor_id": buyer["procurement"]["actor_id"],
                "evaluation_status": "ranking_changed",
                "criteria_change_status": "no_disclosed_change",
                "disclosure_channel": "buyer_scorecard",
                "source": "buyer_evaluation_record",
            },
        },
        "external_event": {
            "recoverable": {
                "stakeholder_actor_id": actors_by_role[
                    external_profile["actor_role_id"]
                ]["actor_id"],
                **_vertical_causal_facts(
                    world["vertical"],
                    "external_event",
                    "recoverable",
                    include_source=True,
                ),
            },
            "terminal": {
                "stakeholder_actor_id": actors_by_role[
                    external_profile["actor_role_id"]
                ]["actor_id"],
                **_vertical_causal_facts(
                    world["vertical"],
                    "external_event",
                    "terminal",
                    include_source=True,
                ),
            },
        },
    }[world["causal_family"]][world["variant"]]
    visible.append(
        {
            "event_id": observable_event_id,
            "world_id": world["world_id"],
            "kind": event_kind,
            "effective_at": family_event_effective_at,
            "recorded_at": family_event_recorded_at,
            "available_at": family_event_available_at,
            "actor_ids": [observable["stakeholder_actor_id"]],
            "artifact_ids": [],
            "visibility": (
                external_profile["visibility"]
                if world["causal_family"] == "external_event"
                else "agent_visible"
            ),
            "channel": (
                external_profile["channel"]
                if world["causal_family"] == "external_event"
                else "internal_chat"
                if world["causal_family"] == "competition"
                else "email"
            ),
            "causal_parent_ids": [],
            "visible_roles": (
                list(dict.fromkeys((world["causal_owner_role"], "account_executive")))
                if world["causal_family"] == "external_event"
                and external_profile["visibility"] == "role_scoped"
                else []
            ),
            "payload": {
                **observable,
                "checkpoint_id": release_checkpoint["checkpoint_id"],
            },
        }
    )
    visible.append(
        {
            "event_id": _opaque_id("event", world["world_id"], "cycle-horizon"),
            "world_id": world["world_id"],
            "kind": "workflow_gate_completed",
            "effective_at": world["end_at"],
            "recorded_at": world["end_at"],
            "available_at": world["end_at"],
            "actor_ids": [],
            "artifact_ids": [],
            "visibility": "agent_visible",
            "channel": "system",
            "causal_parent_ids": [observable_event_id],
            "visible_roles": [],
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
                "artifact_ids": [],
                "visibility": "oracle_only",
                "channel": "crm",
                "causal_parent_ids": [],
                "visible_roles": [],
                "payload": defect,
            }
        )
    hidden.append(
        {
            "event_id": family_event_id,
            "world_id": world["world_id"],
            "sequence": len(hidden),
            "kind": event_kind,
            "effective_at": family_event_effective_at,
            "recorded_at": family_event_recorded_at,
            "available_at": family_event_available_at,
            "actor_ids": [],
            "artifact_ids": [],
            "visibility": "oracle_only",
            "channel": "system",
            "causal_parent_ids": [observable_event_id],
            "visible_roles": [],
            "payload": {
                "family": world["causal_family"],
                "variant": world["variant"],
                "description": world["family_description"],
                "checkpoint_id": world["intervention_checkpoint_id"],
                "trigger_event_id": observable_event_id,
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
            "artifact_ids": [],
            "visibility": "oracle_only",
            "channel": "system",
            "causal_parent_ids": [family_event_id],
            "visible_roles": [],
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


def _typed_payload_value(
    world: dict[str, Any], checkpoint: dict[str, Any], field: str
) -> Any:
    amount = int(world["amount_minor_units"])
    gate = checkpoint["gate_id"]
    vertical = str(world["vertical"])
    day = date.fromisoformat(checkpoint["available_at"][:10])
    payload_key = (vertical, gate, field)
    scoped_values: dict[tuple[str, str, str], Any] = {
        (
            "commercial_insurance",
            "quotation",
            "exclusions",
        ): ["known_pollution_without_endorsement"],
        ("consulting", "scope", "exclusions"): ["production_staffing"],
        (
            "consulting",
            "commercial_model",
            "pricing_basis",
        ): {
            "model": "fixed_fee",
            "fee_minor_units": amount,
            "blended_hourly_rate_minor_units": amount // 160,
        },
        (
            "corporate_banking",
            "pricing",
            "pricing_basis",
        ): {
            "model": "risk_based_spread",
            "spread_basis_points": 95,
            "base_rate": "SOFR",
        },
    }
    if payload_key in scoped_values:
        return scoped_values[payload_key]
    if field == "currency":
        return world["currency"]
    if field == "rfq_revision":
        return "RFQ-03"
    if field == "quantity":
        return 25000
    if field == "cost_breakdown":
        materials = amount * 58 // 100
        labor = amount * 24 // 100
        overhead = amount - materials - labor
        return {
            "materials_minor_units": materials,
            "labor_minor_units": labor,
            "overhead_minor_units": overhead,
            "total_minor_units": amount,
        }
    if field == "feasibility_result":
        return "within_capability"
    if field == "open_constraints":
        return ["tooling_lead_time"]
    if field == "inspection_result":
        return "accepted_with_sampling"
    if field == "sample_quantity":
        return 240
    if field == "defect_rate":
        return 0.012
    if field == "qualification_status":
        return "qualified"
    if field == "supplier_scope":
        return {"plant": "primary", "commodity": world["buyer_industry"]}
    if field == "ppap_status":
        return "submitted"
    if field == "run_at_rate":
        return 1200
    if field == "capacity_commitment":
        return 25000
    if field == "price_minor_units":
        return amount
    if field == "payment_terms":
        return {"days": 45, "milestones": ["acceptance", "delivery"]}
    if field == "purchase_order_id":
        return _opaque_id("purchase-order", world["world_id"], gate)
    if field == "award_authorization":
        return "pending_order_authorization"
    if field == "revalidation_status":
        return "pending_revalidation"
    if field == "revalidated_at_rate":
        return 1200
    if field == "responsibility_status":
        return "qualified"
    if field == "experience_scope":
        return {"years": 12, "project_types": ["commercial_build"]}
    if field == "bond_capacity":
        return amount * 3
    if field == "safety_status":
        return "cleared_with_conditions"
    if field == "site_conditions":
        return {"access": "confirmed", "utilities": "survey_required", "hazards": []}
    if field == "site_walk_completed":
        return True
    if field == "tender_version":
        return "addendum-02"
    if field == "addenda_acknowledged":
        return True
    if field == "bid_due_at":
        return _format_datetime(
            datetime.combine(day, datetime.min.time(), tzinfo=UTC)
            + timedelta(days=21, hours=17)
        )
    if field == "bid_amount_minor_units":
        return amount
    if field == "contingency_minor_units":
        return amount * 5 // 100
    if field == "responsiveness":
        return "responsive"
    if field == "interview_score":
        return 86.5
    if field == "team_commitment":
        return {
            "named_roles": ["project_executive", "site_superintendent"],
            "availability": "confirmed",
        }
    if field == "ve_options":
        savings = amount * 3 // 100
        return [{"option": "alternate_finish", "savings_minor_units": savings}]
    if field == "scope_delta":
        return -amount * 3 // 100
    if field == "schedule_impact":
        return -7
    if field == "award_notice":
        return "pending_best_value_award_notice"
    if field == "performance_bond_status":
        return "available_on_execution"
    if field == "executed_contract":
        return False
    if field == "submission_version":
        return "submission-04"
    if field == "coverage_request":
        return {
            "lines": ["general_liability", "property"],
            "effective_date": _date_text(day, 30),
        }
    if field == "insured_entity":
        return world["buyer_name"]
    if field == "exposure_schedule":
        return {
            "locations": 3,
            "payroll_minor_units": amount * 4,
            "revenue_minor_units": amount * 10,
        }
    if field == "loss_runs_as_of":
        return _date_text(day, -30)
    if field == "material_exposures":
        return ["fleet_operations"]
    if field == "markets_contacted":
        return ["market-alpha", "market-beta", "market-gamma"]
    if field == "appetite_status":
        return "within_appetite"
    if field == "comparison_basis":
        return {"limits": "matched", "deductibles": "normalized", "service": "scored"}
    if field == "underwriter_questions":
        return ["confirm loss-control remediation date"]
    if field == "subjectivities":
        return ["signed application", "loss-control survey"]
    if field == "underwriting_status":
        return "pending_subjectivity"
    if field == "limits":
        return {
            "each_claim_minor_units": amount * 2,
            "aggregate_minor_units": amount * 4,
        }
    if field == "premiums":
        return {
            "annual_minor_units": amount // 20,
            "total_term_minor_units": amount // 10,
        }
    if field == "commissions":
        broker = amount // 200
        carrier = amount // 500
        return {
            "broker_minor_units": broker,
            "carrier_fee_minor_units": carrier,
            "total_minor_units": broker + carrier,
        }
    if field == "exclusions":
        if vertical == "commercial_insurance":
            return ["known_pollution_without_endorsement"]
        if vertical == "consulting":
            return ["production_staffing"]
        raise ValueError(f"invalid exclusions field: {vertical}:{gate}")
    if field == "selected_terms":
        return {"market": "market-alpha", "annual_premium_minor_units": amount // 20}
    if field == "order_received_at":
        return _format_datetime(
            datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=11)
        )
    if field == "order_status":
        return "pending_client_confirmation"
    if field == "bind_confirmation":
        return "pending_signed_lines"
    if field == "signed_lines":
        return 0.85
    if field == "effective_at":
        return _format_datetime(
            datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(days=30)
        )
    if field in {"contract_data_status", "validation_status"}:
        return "pending_validation"
    if field in {"post_placement_status", "reconciliation_status"}:
        return "pending_reconciliation"
    if field == "tax_allocation":
        return {
            "premium_tax_minor_units": amount // 100,
            "allocation_basis": "state_filing",
        }
    if field == "make_buy_status":
        return "buy_case_supported"
    if field == "problem_statement":
        return {"scope": world["motion"], "priority": "measurable_outcome"}
    if field == "success_owner":
        return "business_owner"
    if field == "diagnosis":
        return {"root_causes": ["process_variance"], "confidence": 0.82}
    if field == "outcome_measures":
        return [
            {"metric": "cycle_time", "baseline": 100, "target": 80, "unit": "index"}
        ]
    if field == "baseline":
        return {"cycle_time_index": 100, "data_as_of": _date_text(day, -14)}
    if field == "deliverables":
        return ["diagnostic report", "implementation roadmap"]
    if field == "scope_boundaries":
        return {"included": ["diagnosis", "roadmap"], "excluded": ["implementation"]}
    if field == "delivery_approach":
        return {"workstreams": ["diagnosis", "roadmap"], "governance": "weekly"}
    if field == "resource_availability":
        return {"capacity": "available", "start_window_days": 30}
    if field == "pricing_basis":
        return {
            "model": "fixed_fee",
            "fee_minor_units": amount,
            "blended_hourly_rate_minor_units": amount // 160,
        }
    if field == "risk_allocation":
        return {"change_control": "shared", "delivery_risk": "seller_bounded"}
    if field == "payment_basis":
        return {"milestones": ["kickoff", "interim", "acceptance"], "days": 30}
    if field == "outcomes":
        return ["reduce cycle time", "document operating model"]
    if field == "transfer_responsibilities":
        return ["client enablement", "operating documentation"]
    if field == "handoff_plan":
        return {"owner": "business_owner", "sessions": 3}
    if field == "exit_plan":
        return {"materials": ["runbook", "decision_log"], "status": "planned"}
    if field == "dependencies":
        return ["client_data_access", "executive_sponsor"]
    if field == "evaluation_status":
        return "evaluation_in_progress"
    if field == "clarifications":
        return ["confirm acceptance scoring"]
    if field == "procurement_owner":
        return "client_procurement_lead"
    if field == "approval_owner":
        return "client_legal_leadership"
    if field == "business_case_status":
        return "pending_procurement_review"
    if field == "approval_date":
        return _date_text(day, 7)
    if field == "conflicts_status":
        return "clearance_pending"
    if field == "affiliate_list":
        return ["buyer-holdco", "buyer-operatingco"]
    if field == "waiver_status":
        return "not_required"
    if field == "rfi_version":
        return "RFI-02"
    if field == "assumptions":
        return ["uniform scope assumptions acknowledged"]
    if field == "panel_status":
        return "shortlist_pending"
    if field == "matter_scope":
        return {"matter_type": "commercial", "jurisdictions": ["California"]}
    if field == "staffing":
        if vertical == "consulting":
            return ["engagement_lead", "workstream_lead"]
        if vertical == "legal_services":
            return ["responsible_partner", "associate_lead"]
        raise ValueError(f"invalid staffing field: {vertical}:{gate}")
    if field == "fee_basis":
        return {
            "model": "blended_hourly",
            "currency": world["currency"],
            "partner_rate_minor_units": amount // 40,
            "associate_rate_minor_units": amount // 80,
        }
    if field == "budget_minor_units":
        return amount
    if field == "realization_target":
        return 0.72
    if field == "consent_status":
        return "not_required"
    if field == "disclosure_basis":
        return "no_disclosure_without_informed_consent_or_applicable_exception"
    if field == "engagement_status":
        return "pending_signature"
    if field == "leadership_status":
        return "pending_leadership_approval"
    if field == "responsible_partner":
        return "designated_partner"
    if field == "engagement_owner":
        return "designated_partner"
    if field == "executed_at":
        return _date_text(day, 7)
    if field == "delay_reason":
        return "client_review_window"
    if field == "revised_due_date":
        return _date_text(day, 21)
    if field == "customer_identity_status":
        return "verified"
    if field == "beneficial_owner_status":
        return "verified"
    if field == "risk_profile":
        return "moderate"
    if field == "monitoring_status":
        return "ongoing_monitoring_ready"
    if field == "financial_statements_as_of":
        return _date_text(day, -30)
    if field == "repayment_sources":
        return [{"source": "operating_cash_flow", "coverage": 1.45}]
    if field == "guarantors":
        return ["parent_guarantor"]
    if field == "risk_rating":
        return "moderate"
    if field == "dscr":
        return 1.45
    if field == "leverage_ratio":
        return 2.8
    if field == "repayment_sensitivity":
        return {
            "base_dscr": 1.45,
            "downside_dscr": 1.18,
            "stress_case": "coverage_above_1x",
            "leverage_ratio": 2.8,
        }
    if field == "collateral":
        return {
            "value_minor_units": amount * 2,
            "coverage_ratio": 2.0,
            "type": "equipment",
        }
    if field == "credit_authority":
        return "within_delegated_limit"
    if field == "exceptions":
        return ["none"]
    if field == "mitigants":
        return ["parent_guarantor", "quarterly_reporting"]
    if field == "approved_return":
        return 0.085
    if field == "fee_waiver":
        return 0
    if field == "origination_separation":
        return {"originator": "relationship_team", "approver": "independent_credit"}
    if field == "approval_record":
        return {"status": "pending", "approval_authority": "credit_officer"}
    if field == "exception_route":
        return {"required": False, "reason": "within_delegated_authority"}
    if field == "document_status":
        return "draft_for_review"
    if field == "conditions_precedent":
        return ["executed_security_documents", "insurance_certificate"]
    if field == "legal_review_status":
        return "pending_legal_review"
    if field == "covenants":
        return ["quarterly_financial_reporting", "minimum_dscr_1_10"]
    if field == "executed_documents":
        return ["loan_agreement", "security_documents"]
    if field in {"addenda_acknowledged", "site_walk_completed", "executed_contract"}:
        return False
    if field.endswith("_minor_units") or field in {
        "quantity",
        "run_at_rate",
        "capacity_commitment",
        "schedule_impact",
        "scope_delta",
    }:
        return amount
    if field.endswith("_score"):
        return 80.0
    if field.endswith(("_at", "_date")):
        return _format_datetime(datetime.combine(day, datetime.min.time(), tzinfo=UTC))
    if field.endswith("_ids"):
        return [f"{gate}-owner"]
    if field in {
        "markets_contacted",
        "exclusions",
        "subjectivities",
        "exceptions",
        "mitigants",
        "dependencies",
    }:
        return [f"{gate}-item"]
    if field.endswith(("_status", "_result")):
        return "pending_review"
    if field in {
        "limits",
        "premiums",
        "commissions",
        "coverage_request",
        "selected_terms",
    }:
        return {"value_minor_units": amount}
    raise ValueError(f"unmapped structured payload field: {gate}:{field}")


def _seller_approval_exception(
    world: dict[str, Any], checkpoint: dict[str, Any]
) -> dict[str, Any]:
    route = checkpoint["decision_route"]["seller_approval_route"]
    gate = checkpoint["gate_id"]
    control_indexes = {
        "manufacturing": {"purchase_order": 1},
        "construction": {"bid": 0, "value_engineering": 0},
        "commercial_insurance": {"quotation": 1},
        "consulting": {"commercial_model": 1},
        "legal_services": {"fee_arrangement": 1},
        "corporate_banking": {},
    }
    index = control_indexes.get(world["vertical"], {}).get(gate)
    if index is None:
        return {
            "available": False,
            "required": False,
            "trigger": "none",
            "policy_owner": None,
            "policy_evidence": None,
            "policy_limit_minor_units": None,
            "basis": None,
            "reason": "no_seller_exception",
        }
    else:
        owner, evidence, threshold, _trigger = POLICY_CONTROLS[world["vertical"]][index]
        seed = _stable_seed(world["seed"], gate, "seller-exception")
        amount = int(world["amount_minor_units"])
        if world["vertical"] == "manufacturing":
            rate = 6 + seed % 5
            basis = {
                "field": "discount_percent",
                "value": rate,
                "amount_minor_units": amount * rate // 100,
            }
            breached = rate > 8
        elif world["vertical"] == "construction":
            rate = 3 + seed % 4
            basis = {
                "field": "contingency_percent",
                "value": rate,
                "amount_minor_units": amount * rate // 100,
            }
            breached = rate > 5
        elif world["vertical"] == "commercial_insurance":
            premium = amount // 20
            basis = {
                "field": "annual_premium_minor_units",
                "value": premium,
                "amount_minor_units": premium,
            }
            breached = premium > threshold
        elif world["vertical"] == "consulting":
            fee = amount + (seed % 5) * amount // 100
            basis = {
                "field": "fixed_fee_minor_units",
                "value": fee,
                "amount_minor_units": fee,
            }
            breached = fee > threshold
        elif world["vertical"] == "legal_services":
            fee = amount + (seed % 4) * amount // 100
            basis = {
                "field": "matter_budget_minor_units",
                "value": fee,
                "amount_minor_units": fee,
            }
            breached = fee > threshold
        else:
            spread = 75 + seed % 51
            basis = {
                "field": "pricing_spread_basis_points",
                "value": spread,
                "amount_minor_units": amount,
            }
            breached = spread > 110
    required = bool(route.get("available", False)) and breached
    return {
        "available": bool(route.get("available", False)),
        "required": required,
        "trigger": route["trigger"],
        "policy_owner": owner,
        "policy_evidence": evidence,
        "policy_limit_minor_units": threshold,
        "basis": basis,
        "reason": "concrete_term_breaches_policy"
        if required
        else "concrete_term_within_policy",
    }


def _decision_state(world: dict[str, Any], checkpoint: dict[str, Any]) -> str:
    sequence = int(checkpoint["sequence"])
    intervention = int(world["intervention_sequence"])
    resolution = int(world["resolution_sequence"])
    if sequence < intervention:
        return "accepted"
    if sequence < resolution:
        return "deferred"
    recoverable = world["variant_index"] == 0
    if sequence == resolution:
        if recoverable:
            return "accepted"
        return "deferred" if _fallback_outcome(world) == "no_decision" else "rejected"
    if recoverable and sequence < int(world["checkpoint_count"]) - 1:
        return "accepted"
    outcome = TERMINAL_OUTCOMES[world["reference_outcome"]]
    if outcome == "no_decision":
        return "deferred"
    if outcome in {"closed_lost", "disqualified"}:
        return "rejected"
    return "accepted"


def _terminal_sequence(world: dict[str, Any]) -> int:
    return (
        int(world["checkpoint_count"]) - 1
        if world["variant_index"] == 0
        else int(world["resolution_sequence"])
    )


def _fallback_outcome(world: dict[str, Any]) -> str:
    reference = TERMINAL_OUTCOMES[world["reference_outcome"]]
    if world["variant_index"] != 0:
        return reference
    if reference == "closed_won":
        return "closed_lost"
    if reference == "no_decision":
        return "closed_lost"
    if reference == "closed_lost":
        return "no_decision"
    return "disqualified"


def _checkpoint_authority_role_ids(
    world: dict[str, Any], checkpoint: dict[str, Any]
) -> tuple[str, ...]:
    recovery = (
        tuple(world["causal_authority_role_ids"])
        if checkpoint.get("sequence") is not None
        and world.get("resolution_sequence") is not None
        and int(checkpoint["sequence"]) == int(world["resolution_sequence"])
        else ()
    )
    vertical = world["vertical"]
    gate = checkpoint["gate_id"]
    base = tuple(checkpoint["authority_role_ids"])
    extras = tuple(
        item[0] for item in ADDITIONAL_AUTHORITIES.get(vertical, {}).get(gate, ())
    )
    if vertical == "commercial_insurance":
        values = {
            "submission": (
                "insurance.exposure_authority",
                "insurance.broker_authority",
            ),
            "additional_information": ("insurance.underwriter_authority",),
            "quotation_request": ("insurance.underwriter_authority",),
            "quotation": (
                "insurance.underwriter_authority",
                "insurance.broker_authority",
            ),
            "client_order": (
                "insurance.client_authority",
                "insurance.broker_authority",
            ),
            "binding": (
                "insurance.binding_authority",
                "insurance.client_authority",
                "insurance.broker_authority",
            ),
            "contract_data_validation": (
                "insurance.placement_authority",
                "insurance.broker_authority",
            ),
            "post_placement": (
                "insurance.placement_authority",
                "insurance.broker_authority",
            ),
        }[gate]
        return tuple(dict.fromkeys((*values, *recovery)))
    if vertical == "legal_services":
        values = {
            "conflicts": (
                "legal.affected_client_authority",
                "legal.matter_authority",
                "legal.conflicts_authority",
            ),
            "fee_arrangement": (
                "legal.matter_authority",
                "legal.fee_authority",
            ),
            "leadership_approval": (
                "legal.matter_authority",
                "legal.fee_authority",
                "buyer.procurement",
                "legal.engagement_authority",
            ),
            "engagement_letter": (
                "legal.matter_authority",
                "legal.fee_authority",
                "buyer.procurement",
                "legal.engagement_authority",
            ),
        }.get(gate, base)
        return tuple(dict.fromkeys((*values, *extras, *recovery)))
    if vertical == "corporate_banking":
        buyer_role = {
            "customer_identification": "buyer.technical_evaluator",
            "beneficial_ownership": "buyer.economic_buyer",
            "customer_due_diligence": "buyer.finance",
            "underwriting": "buyer.finance",
            "pricing": "buyer.finance",
            "credit_approval": "buyer.economic_buyer",
            "documentation": "buyer.procurement",
            "closing": "buyer.economic_buyer",
        }[gate]
        guarantors = ("buyer.executive_sponsor",) if gate == "closing" else ()
        return tuple(
            dict.fromkeys((buyer_role, *guarantors, *extras, *base, *recovery))
        )
    return tuple(dict.fromkeys((*base, *extras, *recovery)))


def _structured_payload(
    world: dict[str, Any],
    checkpoint: dict[str, Any],
    authority_actors: Sequence[dict[str, Any]],
    author_actor: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    artifact_key = str(spec["artifact_key"])
    approval_exception = _seller_approval_exception(world, checkpoint)
    authority_actor = authority_actors[0]
    authority_actor_ids = [authority_actor["actor_id"]]
    authority_role_ids = [authority_actor["authority"]["role_id"]]
    decision_state = (
        _decision_state(world, checkpoint)
        if spec["artifact_role"] == "decision"
        else "pending"
    )
    payload: dict[str, Any] = {
        "checkpoint_id": checkpoint["checkpoint_id"],
        "gate_id": checkpoint["gate_id"],
        "source_fact_ids": list(checkpoint["source_fact_ids"]),
        "authority_role_id": authority_actor["authority"]["role_id"],
        "authority_role_ids": authority_role_ids,
        "authority_actor_ids": authority_actor_ids,
        "decision_state": decision_state,
        "decision_owner_actor_id": authority_actor["actor_id"],
        "decision_owner_actor_ids": authority_actor_ids,
        "authority_decisions": [
            {
                "actor_id": actor["actor_id"],
                "effective_at": _timestamp(checkpoint["date"], 9),
                "resolution": decision_state,
                "rights": actor["authority"]["rights"],
            }
            for actor in (authority_actor,)
        ],
        "author_actor_id": author_actor["actor_id"],
        "required_signer_role_ids": authority_role_ids,
        "required_signer_actor_ids": authority_actor_ids,
        "forecast_cutoff_at": checkpoint["available_at"],
        "allowed_state_diff_targets": [
            "stage",
            "close_date",
            "next_step",
            "forecast_probability",
        ],
        "approval_required": approval_exception["required"],
        "approval_exception": approval_exception,
        "artifact_key": artifact_key,
        "author_role_id": spec["author_role_id"],
        "authoritative_for": list(spec["authoritative_for"]),
        "artifact_subtype": spec["artifact_subtype"],
        "channel": spec["channel"],
        "source_evidence_term": _vertical_facts(world["vertical"])["evidence_by_gate"][
            checkpoint["gate_id"]
        ],
    }
    for field in checkpoint["required_payload_fields"]:
        if field in payload:
            continue
        payload[field] = _typed_payload_value(world, checkpoint, field)
    if (
        world["vertical"] == "manufacturing"
        and checkpoint["gate_id"] == "sample_or_pilot"
    ):
        payload["sample_defect_count"] = round(
            payload["sample_quantity"] * payload["defect_rate"]
        )
    if world["vertical"] == "construction" and checkpoint["gate_id"] == "bid":
        payload["total_bid_minor_units"] = (
            payload["bid_amount_minor_units"] + payload["contingency_minor_units"]
        )
    if (
        world["vertical"] == "construction"
        and checkpoint["gate_id"] == "value_engineering"
    ):
        payload["net_price_delta_minor_units"] = payload["scope_delta"]
    if (
        world["vertical"] == "commercial_insurance"
        and checkpoint["gate_id"] == "quotation"
    ):
        payload["premium_to_each_claim_limit"] = round(
            payload["premiums"]["annual_minor_units"]
            / payload["limits"]["each_claim_minor_units"],
            6,
        )
    if (
        world["vertical"] == "corporate_banking"
        and checkpoint["gate_id"] == "underwriting"
    ):
        payload["annual_debt_service_minor_units"] = int(
            world["amount_minor_units"] / payload["dscr"]
        )
    causal_payload = _structured_causal_payload(world, checkpoint, spec)
    if causal_payload:
        for field in _causal_cure_data(world):
            payload.pop(field, None)
        payload.update(causal_payload)
    return payload


def _structured_chain(
    world: dict[str, Any], vertical: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    actors_by_role = {
        actor["authority"]["role_id"]: actor
        for actor in world["actors"]
        if actor["authority"].get("role_id")
    }
    buyer = {
        _actor_role(actor): actor
        for actor in world["actors"]
        if actor["kind"] == "buyer"
    }
    seller = {
        _actor_role(actor): actor
        for actor in world["actors"]
        if actor["kind"] == "seller"
    }
    actors_by_recipient_role = {
        **buyer,
        **seller,
        **{
            actor["authority"]["role_id"]: actor
            for actor in world["actors"]
            if actor["authority"].get("role_id")
        },
    }
    result: list[dict[str, Any]] = []
    evidence_ids: dict[str, str] = {}
    for checkpoint in world["checkpoints"]:
        blueprint = _blueprint_gate(vertical["id"], checkpoint["gate_id"])
        authority_actors = [
            actors_by_role[role_id]
            for role_id in _checkpoint_authority_role_ids(world, checkpoint)
        ]
        required = blueprint["required_artifacts"]
        decision_keys = [
            str(spec["artifact_key"])
            for spec in required
            if spec["artifact_role"] == "decision"
        ]
        decision_group_id = _opaque_id(
            "decision-group",
            world["world_id"],
            checkpoint["sequence"],
        )
        artifact_ids_by_key: dict[str, str] = {}
        records_by_key: dict[str, dict[str, Any]] = {}
        pending_specs = list(required)
        artifact_index = 0
        while pending_specs:
            spec_index = next(
                (
                    index
                    for index, candidate in enumerate(pending_specs)
                    if all(
                        key in artifact_ids_by_key
                        for key in candidate["derived_from_artifact_keys"]
                    )
                    and (
                        candidate["supersedes_artifact_key"] is None
                        or candidate["supersedes_artifact_key"] in artifact_ids_by_key
                    )
                ),
                None,
            )
            if spec_index is None:
                raise ValueError(
                    f"structured artifact lineage is cyclic: {checkpoint['gate_id']}"
                )
            spec = pending_specs.pop(spec_index)
            artifact_key = str(spec["artifact_key"])
            author_actor = actors_by_role[spec["author_role_id"]]
            decision_index = (
                decision_keys.index(artifact_key)
                if spec["artifact_role"] == "decision"
                else -1
            )
            decision_is_authoritative = 0 <= decision_index < len(authority_actors)
            remaining_authorities: list[dict[str, Any]] = []
            if spec["artifact_role"] == "decision":
                author_actor = authority_actors[
                    min(decision_index, len(authority_actors) - 1)
                ]
                if (
                    decision_is_authoritative
                    and decision_index
                    == min(len(decision_keys), len(authority_actors)) - 1
                ):
                    remaining_authorities = authority_actors[len(decision_keys) :]
            artifact_id = _opaque_id(
                "artifact",
                world["world_id"],
                "structured",
                checkpoint["sequence"],
                artifact_key,
            )
            parent_available_at = tuple(
                records_by_key[key]["available_at"]
                for key in spec["derived_from_artifact_keys"]
            )
            if spec["supersedes_artifact_key"] is not None:
                parent_available_at += (
                    records_by_key[spec["supersedes_artifact_key"]]["available_at"],
                )
            created_at, available_at = _structured_times(
                world,
                checkpoint,
                artifact_index,
                len(required),
                parent_available_at,
            )
            if not _actor_active_at(author_actor, available_at):
                author_actor = _active_buyer(world, available_at)
            is_crm = spec["kind"] == "crm_record"
            is_document = spec["logical_document_key"] is not None
            recipient_roles = tuple(spec["recipient_role_ids"])
            recipient_actors = [
                actors_by_recipient_role[role] for role in recipient_roles
            ]
            recipient_actors = [
                actor
                if _actor_active_at(actor, available_at)
                else _active_buyer(world, available_at)
                for actor in recipient_actors
            ]
            payload = _structured_payload(
                world,
                checkpoint,
                [author_actor]
                if spec["artifact_role"] == "decision"
                else authority_actors,
                author_actor,
                spec,
            )
            payload["author_role_id"] = author_actor["authority"]["role_id"]
            recipient_actor_ids = [actor["actor_id"] for actor in recipient_actors]
            payload["author"] = {
                "display_name": author_actor["display_name"],
                "email": author_actor["email"],
                "kind": author_actor["kind"],
                "organization_id": author_actor["organization_id"],
                "role_id": author_actor["authority"]["role_id"],
            }
            payload["recipients"] = [
                {
                    "display_name": actor["display_name"],
                    "email": actor["email"],
                    "kind": actor["kind"],
                    "organization_id": actor["organization_id"],
                    "role_id": actor["authority"]["role_id"],
                }
                for actor in recipient_actors
            ]
            payload["recipient_role_ids"] = [
                actor["authority"]["role_id"] for actor in recipient_actors
            ]
            derived_keys = tuple(spec["derived_from_artifact_keys"])
            derived = [artifact_ids_by_key[key] for key in derived_keys]
            supersedes_key = spec["supersedes_artifact_key"]
            supersedes_id = (
                artifact_ids_by_key[supersedes_key]
                if supersedes_key is not None
                else None
            )
            if is_crm:
                origin_key = next(
                    str(item["artifact_key"])
                    for item in required
                    if item["artifact_role"] == "evidence"
                )
                origin_record = records_by_key[origin_key]
                payload["projection_sequence"] = checkpoint["sequence"]
                payload["projection_origin"] = {
                    "source_artifact_id": origin_record["artifact_id"],
                    "source_actor_id": origin_record["source_actor_ids"][0],
                    "source_time": origin_record["available_at"],
                    "transformation": "structured_authority_projection",
                    "visible": True,
                }
            body = json.dumps(
                {
                    "artifact_key": artifact_key,
                    "gate_id": checkpoint["gate_id"],
                    "structured_payload": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            record: dict[str, Any] = {
                "artifact_id": artifact_id,
                "world_id": world["world_id"],
                "kind": spec["kind"],
                "title": f"{checkpoint['gate_id']} {artifact_key}",
                "created_at": created_at,
                "available_at": available_at,
                "visibility": "role_scoped" if is_crm else "agent_visible",
                "synthetic": True,
                "source_actor_ids": [author_actor["actor_id"]],
                "recipient_actor_ids": recipient_actor_ids,
                "thread_id": _opaque_id(
                    "thread", world["world_id"], "structured", checkpoint["sequence"]
                ),
                "record_id": world["deal_id"] if is_crm else None,
                "content": {
                    "mime_type": "application/json",
                    "body": body,
                    "language": "en",
                    "source_uri": f"artifacts/structured/{artifact_id}.json",
                },
                "checksum": _checksum(body),
                "provenance": {
                    "synthetic_only": True,
                    "source_type": "derived_projection"
                    if is_crm
                    else "generated_template",
                    "generator": "edlb.generate",
                    "generator_version": DATASET_VERSION,
                    "source_ids": _fact_source_ids(checkpoint["source_fact_ids"]),
                    "fact_ids": list(checkpoint["source_fact_ids"]),
                    "license": "CC-BY-4.0",
                },
                "gate_id": checkpoint["gate_id"],
                "artifact_key": artifact_key,
                "structured_payload": payload,
                "authoritative_for": list(payload["authoritative_for"]),
                "recipient_role_ids": payload["recipient_role_ids"],
                "projection_origin": payload.get("projection_origin"),
                "logical_document_id": _opaque_id(
                    "logical-document", world["world_id"], spec["logical_document_key"]
                )
                if is_document
                else None,
                "version": 1 if is_document else None,
                "supersedes_artifact_id": supersedes_id if is_document else None,
                "derived_from_artifact_ids": derived,
            }
            if not is_crm:
                record.pop("record_id")
            if is_crm:
                record["visible_roles"] = list(ROLES)
            if spec["artifact_role"] == "decision" and decision_is_authoritative:
                record["decision_group_id"] = decision_group_id
                record["authority_decision_actor_id"] = author_actor["actor_id"]
            result.append(record)
            if spec["artifact_role"] == "decision":
                for authority_actor in remaining_authorities:
                    authority_record = json.loads(json.dumps(record))
                    authority_id = _opaque_id(
                        "artifact",
                        world["world_id"],
                        "authority-decision",
                        checkpoint["sequence"],
                        artifact_key,
                        authority_actor["actor_id"],
                    )
                    authority_key = (
                        f"{artifact_key}_authority_"
                        f"{authority_actor['authority']['role_id'].replace('.', '_')}"
                    )
                    authority_payload = _structured_payload(
                        world,
                        checkpoint,
                        [authority_actor],
                        authority_actor,
                        spec,
                    )
                    if int(checkpoint["sequence"]) == int(
                        world["intervention_sequence"]
                    ):
                        for field in _causal_cure_data(world):
                            authority_payload.pop(field, None)
                        authority_payload.pop("evaluation_status", None)
                        authority_payload.pop("criteria_change_status", None)
                    authority_payload["artifact_key"] = authority_key
                    authority_payload["author_role_id"] = authority_actor["authority"][
                        "role_id"
                    ]
                    authority_payload["author"] = {
                        "display_name": authority_actor["display_name"],
                        "email": authority_actor["email"],
                        "kind": authority_actor["kind"],
                        "organization_id": authority_actor["organization_id"],
                        "role_id": authority_actor["authority"]["role_id"],
                    }
                    authority_payload["recipients"] = payload["recipients"]
                    authority_payload["recipient_role_ids"] = payload[
                        "recipient_role_ids"
                    ]
                    authority_body = json.dumps(
                        {
                            "artifact_key": authority_key,
                            "gate_id": checkpoint["gate_id"],
                            "structured_payload": authority_payload,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    )
                    authority_record.update(
                        {
                            "artifact_id": authority_id,
                            "artifact_key": authority_key,
                            "title": (
                                f"{checkpoint['gate_id']} decision by "
                                f"{authority_actor['display_name']}"
                            ),
                            "source_actor_ids": [authority_actor["actor_id"]],
                            "structured_payload": authority_payload,
                            "authority_decision_actor_id": authority_actor["actor_id"],
                            "checksum": _checksum(authority_body),
                            "logical_document_id": (
                                _opaque_id(
                                    "logical-document",
                                    world["world_id"],
                                    authority_key,
                                )
                                if is_document
                                else None
                            ),
                            "supersedes_artifact_id": None,
                        }
                    )
                    authority_record["content"]["body"] = authority_body
                    authority_record["content"]["source_uri"] = (
                        f"artifacts/structured/{authority_id}.json"
                    )
                    result.append(authority_record)
            artifact_ids_by_key[artifact_key] = artifact_id
            records_by_key[artifact_key] = record
            if spec["artifact_role"] == "evidence":
                evidence_ids[checkpoint["gate_id"]] = artifact_id
            artifact_index += 1
        if checkpoint["gate_id"] not in evidence_ids:
            raise ValueError(f"structured evidence is missing: {checkpoint['gate_id']}")
    return result, evidence_ids


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
    result, evidence_ids = _structured_chain(world, vertical)
    resolution_sequence = int(world["resolution_sequence"])
    resolution_checkpoint = world["checkpoints"][resolution_sequence]
    decisions = [
        artifact
        for artifact in result
        if artifact["gate_id"] == resolution_checkpoint["gate_id"]
        and artifact.get("decision_group_id") is not None
        and artifact["structured_payload"].get("decision_state") != "pending"
    ]
    branch_id = _opaque_id("branch", world["world_id"], resolution_sequence)
    recoverable = world["variant_index"] == 0
    fallback_state = (
        "deferred" if _fallback_outcome(world) == "no_decision" else "rejected"
    )
    reference_option = "success" if recoverable else "fallback"
    reference_state = "accepted" if recoverable else fallback_state
    alternate_option = "fallback" if reference_option == "success" else "success"
    alternate_state = fallback_state if recoverable else "accepted"
    for decision in decisions:
        decision["branch_id"] = branch_id
        decision["branch_option"] = reference_option
        decision["structured_payload"]["decision_state"] = reference_state
        for authority_decision in decision["structured_payload"]["authority_decisions"]:
            authority_decision["resolution"] = reference_state
        decision["content"]["body"] = json.dumps(
            {
                "artifact_key": decision["artifact_key"],
                "gate_id": decision["gate_id"],
                "structured_payload": decision["structured_payload"],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        decision["checksum"] = _checksum(decision["content"]["body"])
        alternate = json.loads(json.dumps(decision))
        alternate_id = _opaque_id(
            "artifact",
            world["world_id"],
            "branch-alternative",
            resolution_sequence,
            decision["authority_decision_actor_id"],
        )
        alternate["artifact_id"] = alternate_id
        alternate["artifact_key"] = _opaque_id(
            "artifact-key",
            world["world_id"],
            resolution_sequence,
            decision["authority_decision_actor_id"],
        )
        alternate["title"] = decision["title"]
        alternate["branch_option"] = alternate_option
        alternate["structured_payload"]["artifact_key"] = alternate["artifact_key"]
        alternate["structured_payload"]["decision_state"] = alternate_state
        for authority_decision in alternate["structured_payload"][
            "authority_decisions"
        ]:
            authority_decision["resolution"] = alternate_state
        alternate["content"]["source_uri"] = f"artifacts/structured/{alternate_id}.json"
        alternate["content"]["body"] = json.dumps(
            {
                "artifact_key": alternate["artifact_key"],
                "gate_id": alternate["gate_id"],
                "structured_payload": alternate["structured_payload"],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        alternate["checksum"] = _checksum(alternate["content"]["body"])
        alternate["supersedes_artifact_id"] = None
        alternate["derived_from_artifact_ids"] = list(
            decision.get("derived_from_artifact_ids", ())
        )
        result.append(alternate)
    structured_by_id = {artifact["artifact_id"]: artifact for artifact in result}
    generic_counts = dict(world["artifact_counts"])
    for artifact in result:
        channel = {
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
            "policy_document": "document",
            "web_page": "web_news",
            "news_item": "web_news",
        }[artifact["kind"]]
        generic_counts[channel] -= 1
    if any(count < 0 for count in generic_counts.values()):
        raise ValueError("structured artifacts exceed the authored channel budget")
    index = 1
    actors_by_role = {
        actor["authority"]["role_id"]: actor
        for actor in actors
        if actor["authority"].get("role_id")
    }
    causal_channel = _causal_artifact_channel(world)
    causal_sequence = int(world["intervention_sequence"])
    if generic_counts[causal_channel] < 1:
        raise ValueError("causal source channel has no generic artifact capacity")
    for artifact_type, count in generic_counts.items():
        for channel_index in range(count):
            if artifact_type == causal_channel:
                noncausal_sequences = [
                    sequence
                    for sequence in range(len(world["checkpoints"]))
                    if sequence != causal_sequence
                ]
                checkpoint_sequence = (
                    causal_sequence
                    if channel_index == 0
                    else noncausal_sequences[
                        (channel_index - 1) % len(noncausal_sequences)
                    ]
                )
            elif artifact_type == "crm" and channel_index < len(world["defects"]):
                checkpoint_sequence = int(
                    world["defects"][channel_index]["checkpoint_sequence"]
                )
            else:
                checkpoint_sequence = _stable_seed(
                    world["seed"], artifact_type, channel_index, "checkpoint"
                ) % len(world["checkpoints"])
            checkpoint = world["checkpoints"][checkpoint_sequence]
            source_role, recipient_role = participant_roles[artifact_type][
                _stable_seed(
                    world["seed"],
                    checkpoint["gate_id"],
                    artifact_type,
                    channel_index,
                    "participants",
                )
                % len(participant_roles[artifact_type])
            ]
            source = seller[source_role]
            if (
                recipient_role == "champion"
                and world["causal_family"] == "champion_exit"
                and checkpoint["sequence"] >= world["intervention_sequence"]
            ):
                recipient_role = "economic_buyer"
            recipient = (
                seller[recipient_role]
                if recipient_role in seller
                else buyer[recipient_role]
            )
            causal_source = artifact_type == causal_channel and channel_index == 0
            if causal_source and world["causal_family"] == "external_event":
                profile = VERTICAL_BLUEPRINTS["verticals"][world["vertical"]][
                    "external_observation"
                ]
                source = actors_by_role[profile["actor_role_id"]]
                recipient = seller[world["causal_owner_role"]]
            defect = (
                world["defects"][channel_index]
                if artifact_type == "crm" and channel_index < len(world["defects"])
                else None
            )
            record = _artifact_record(
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
            if causal_source:
                if (
                    world["causal_family"] == "competition"
                    and world["variant"] == "hidden_influence"
                ):
                    record["structured_payload"].update(
                        {
                            "evaluation_status": "ranking_changed",
                            "criteria_change_status": "no_disclosed_change",
                        }
                    )
                else:
                    record["structured_payload"].update(_causal_cure_data(world))
                if world["causal_family"] == "external_event":
                    profile = VERTICAL_BLUEPRINTS["verticals"][world["vertical"]][
                        "external_observation"
                    ]
                    record["visibility"] = profile["visibility"]
                    if profile["visibility"] == "role_scoped":
                        record["visible_roles"] = list(
                            dict.fromkeys(
                                (world["causal_owner_role"], "account_executive")
                            )
                        )
            if not _actor_active_during(
                source, record["created_at"], record["available_at"]
            ):
                source = _active_buyer(world, record["available_at"])
            if not _actor_active_during(
                recipient, record["created_at"], record["available_at"]
            ):
                recipient = _active_buyer(world, record["available_at"])
            if record["source_actor_ids"] != [source["actor_id"]] or record[
                "recipient_actor_ids"
            ] != [recipient["actor_id"]]:
                record = _artifact_record(
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
            if artifact_type == "crm":
                source_checkpoint = checkpoint
                source_artifact_id = evidence_ids[source_checkpoint["gate_id"]]
                source_artifact = structured_by_id[source_artifact_id]
                projection_origin = {
                    "source_artifact_id": source_artifact_id,
                    "source_actor_id": source_artifact["source_actor_ids"][0],
                    "source_time": source_artifact["available_at"],
                    "transformation": "crm_projection_from_stale_state"
                    if defect is not None
                    else "crm_projection_from_gate_evidence",
                    "visible": True,
                }
                record["projection_origin"] = projection_origin
                record["structured_payload"]["projection_origin"] = projection_origin
            result.append(record)
            index += 1
    _assign_crm_authorities(world, result)
    return result


def _artifact_content(record: dict[str, Any], world: dict[str, Any]) -> str:
    return record["content"]["body"]


_PACKAGE_RESOURCES = files("edlb").joinpath("resources")


def _source_registry() -> dict[str, Any]:
    return json.loads(_PACKAGE_RESOURCES.joinpath("source_registry.json").read_text())


def _attributions() -> dict[str, Any]:
    return json.loads(_PACKAGE_RESOURCES.joinpath("attributions.json").read_text())


def _source_evidence_manifest() -> dict[str, Any]:
    return json.loads(
        _PACKAGE_RESOURCES.joinpath("source_evidence", "manifest.json").read_text()
    )


def _validate_source_contract(registry: Mapping[str, Any]) -> list[str]:
    required = {
        "fact_id",
        "bounded_claim",
        "location",
        "allowed_gates",
        "interpretation_limit",
        "transformation_code",
        "source_version",
        "source_date",
        "license_class",
        "attribution",
    }
    known_gates = {
        gate["gate_id"]
        for vertical in VERTICAL_BLUEPRINTS["verticals"].values()
        for gate in vertical["gates"]
    }
    gates_by_vertical = {
        vertical_id: {
            gate["gate_id"]: set(gate["source_fact_ids"]) for gate in blueprint["gates"]
        }
        for vertical_id, blueprint in VERTICAL_BLUEPRINTS["verticals"].items()
    }
    errors: list[str] = []
    for source in registry.get("sources", []):
        source_id = str(source.get("source_id", "missing"))
        vertical = str(source.get("vertical", "missing"))
        vertical_gates = gates_by_vertical.get(vertical, {})
        if not source.get("url") or not source.get("retrieved_at"):
            errors.append(f"source_metadata={source_id}")
        if not source.get("retrieval_method") or source.get("retrieval_method") in {
            "fetched",
            "downloaded",
            "unknown",
        }:
            errors.append(f"source_retrieval_method={source_id}")
        fact_ids = list(source.get("fact_ids", ()))
        claims = list(source.get("claims", ()))
        if len(fact_ids) != len(set(fact_ids)):
            errors.append(f"source_fact_duplicates={source_id}")
        claim_fact_ids = {
            claim.get("fact_id") for claim in claims if isinstance(claim, Mapping)
        }
        if claim_fact_ids != set(fact_ids):
            errors.append(f"source_claim_coverage={source_id}")
        for record in claims:
            if not isinstance(record, Mapping):
                errors.append(f"source_claim_invalid={source_id}")
                continue
            fact_id = str(record.get("fact_id", "missing"))
            missing = required - set(record)
            if missing:
                errors.append(
                    f"source_claim_required={source_id}:{fact_id}:{','.join(sorted(missing))}"
                )
                continue
            location = record["location"]
            if not isinstance(location, Mapping) or not all(
                key in location for key in ("section", "printed_page", "physical_page")
            ):
                errors.append(f"source_claim_location={source_id}:{fact_id}")
            if any(not record.get(key) for key in required):
                errors.append(f"source_claim_empty={source_id}:{fact_id}")
            if (
                not record["allowed_gates"]
                or not set(record["allowed_gates"]) <= known_gates
            ):
                errors.append(f"source_claim_gates={source_id}:{fact_id}")
            if not set(record["allowed_gates"]) <= set(vertical_gates):
                errors.append(f"source_claim_vertical_gates={source_id}:{fact_id}")
            if record["license_class"] != source.get("license_classification"):
                errors.append(f"source_claim_license={source_id}:{fact_id}")
            if record.get("source_id") not in (None, source_id):
                errors.append(f"source_claim_source={source_id}:{fact_id}")
        gate_fact_ids = source.get("gate_fact_ids", {})
        if not isinstance(gate_fact_ids, Mapping):
            errors.append(f"source_gate_mapping={source_id}")
        else:
            for gate_id, mapped_facts in gate_fact_ids.items():
                if gate_id not in vertical_gates:
                    errors.append(f"source_gate_vertical={source_id}:{gate_id}")
                    continue
                if not isinstance(mapped_facts, list) or not mapped_facts:
                    errors.append(f"source_gate_facts={source_id}:{gate_id}")
                    continue
                for fact_id in mapped_facts:
                    if fact_id not in fact_ids:
                        errors.append(
                            f"source_gate_unknown_fact={source_id}:{gate_id}:{fact_id}"
                        )
                    claim = next(
                        (claim for claim in claims if claim.get("fact_id") == fact_id),
                        None,
                    )
                    if isinstance(claim, Mapping) and gate_id not in claim.get(
                        "allowed_gates", ()
                    ):
                        errors.append(
                            f"source_gate_claim={source_id}:{gate_id}:{fact_id}"
                        )
        digest = source.get("retrieval_sha256")
        if digest is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", str(digest)):
            errors.append(f"source_digest={source_id}")
        if digest is None:
            errors.append(f"source_digest_missing={source_id}")
    mapped_by_vertical: dict[str, dict[str, set[str]]] = {}
    for source in registry.get("sources", []):
        vertical = str(source.get("vertical", "missing"))
        gate_fact_ids = source.get("gate_fact_ids", {})
        if not isinstance(gate_fact_ids, Mapping):
            continue
        for gate_id, fact_ids in gate_fact_ids.items():
            mapped_by_vertical.setdefault(vertical, {}).setdefault(
                gate_id, set()
            ).update(fact_ids)
    for vertical, gates in gates_by_vertical.items():
        for gate_id, expected_facts in gates.items():
            if (
                mapped_by_vertical.get(vertical, {}).get(gate_id, set())
                != expected_facts
            ):
                errors.append(f"blueprint_gate_mapping={vertical}:{gate_id}")
    return errors


def _validate_source_evidence(
    registry: Mapping[str, Any], resource_root: Traversable | None = None
) -> list[str]:
    root = resource_root or _PACKAGE_RESOURCES
    errors: list[str] = []
    manifest_path = root.joinpath("source_evidence", "manifest.json")
    if not manifest_path.is_file():
        return ["source_evidence_manifest_missing"]
    manifest = json.loads(manifest_path.read_text())
    manifest_rows = manifest.get("evidence")
    sources = registry.get("sources")
    if not isinstance(manifest_rows, list) or not isinstance(sources, list):
        return ["source_evidence_manifest_invalid"]
    manifest_ids = [
        row.get("source_id") if isinstance(row, Mapping) else None
        for row in manifest_rows
    ]
    source_ids = [
        source.get("source_id") if isinstance(source, Mapping) else None
        for source in sources
    ]
    if len(manifest_ids) != len(set(manifest_ids)):
        errors.append("source_evidence_manifest_duplicates")
    if len(source_ids) != len(set(source_ids)):
        errors.append("source_evidence_registry_duplicates")
    manifest_by_source = {
        row.get("source_id"): row for row in manifest_rows if isinstance(row, Mapping)
    }
    for source_id in sorted(set(manifest_ids) - set(source_ids), key=str):
        errors.append(f"source_evidence_unknown={source_id}")
    for source_id in sorted(set(source_ids) - set(manifest_ids), key=str):
        errors.append(f"source_evidence_manifest_entry={source_id}")
    for source in sources:
        if not isinstance(source, Mapping):
            errors.append("source_evidence_registry_invalid")
            continue
        evidence_path = source.get("evidence_path")
        source_id = source.get("source_id", "missing")
        manifest_row = manifest_by_source.get(source_id)
        if not isinstance(manifest_row, Mapping):
            continue
        expected = str(source.get("retrieval_sha256", "")).removeprefix("sha256:")
        if manifest_row.get("sha256") != expected:
            errors.append(f"source_evidence_manifest_hash={source_id}")
        if source.get("retrieval_bytes") != manifest_row.get("bytes"):
            errors.append(f"source_evidence_manifest_bytes={source_id}")
        if manifest_row.get("source_url") != source.get("url"):
            errors.append(f"source_evidence_manifest_url={source_id}")
        if (
            not isinstance(source.get("retrieval_bytes"), int)
            or source["retrieval_bytes"] < 1
        ):
            errors.append(f"source_evidence_bytes={source_id}")
        hash_only = source.get("retrieval_method") == "verified_official_hash_only"
        if (
            source.get("license_classification") == "copyrighted-facts-only"
            and evidence_path
        ):
            errors.append(f"source_evidence_copyrighted_bytes={source_id}")
        if hash_only:
            if evidence_path:
                errors.append(f"source_evidence_hash_only_path={source_id}")
            if manifest_row.get("path") is not None:
                errors.append(f"source_evidence_manifest_path={source_id}")
            if manifest_row.get("retrieval_status") != "verified_official_hash_only":
                errors.append(f"source_evidence_manifest_status={source_id}")
            continue
        if not evidence_path:
            errors.append(f"source_evidence_path_missing={source_id}")
            continue
        if manifest_row.get("path") != evidence_path:
            errors.append(f"source_evidence_manifest_path={source_id}")
        if manifest_row.get("retrieval_status") != source.get("retrieval_method"):
            errors.append(f"source_evidence_manifest_status={source_id}")
        evidence = root.joinpath(str(evidence_path))
        if not evidence.is_file():
            errors.append(f"source_evidence_missing={source_id}")
            continue
        payload = evidence.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        expected = str(source.get("retrieval_sha256", "")).removeprefix("sha256:")
        if digest != expected:
            errors.append(f"source_evidence_hash={source_id}")
        expected_bytes = source.get("retrieval_bytes")
        if expected_bytes is not None and len(payload) != int(expected_bytes):
            errors.append(f"source_evidence_bytes={source_id}")
    return errors


def _validate_attributions(registry: Mapping[str, Any]) -> list[str]:
    required = {
        "source_id",
        "title",
        "publisher",
        "license_classification",
        "license_url",
        "attribution",
        "source_url",
    }
    value = _attributions()
    records = value.get("attributions") if isinstance(value, Mapping) else None
    if not isinstance(records, list):
        return ["attribution_records_missing"]
    sources = {
        source.get("source_id"): source
        for source in registry.get("sources", ())
        if isinstance(source, Mapping)
    }
    errors: list[str] = []
    for record in records:
        if not isinstance(record, Mapping):
            errors.append("attribution_record_invalid")
            continue
        source_id = str(record.get("source_id", "missing"))
        missing = required - set(record)
        if missing:
            errors.append(
                f"attribution_required={source_id}:{','.join(sorted(missing))}"
            )
            continue
        source = sources.get(source_id)
        if not isinstance(source, Mapping):
            errors.append(f"attribution_source={source_id}")
            continue
        for field in ("title", "publisher", "license_classification", "source_url"):
            expected = source.get("url") if field == "source_url" else source.get(field)
            if record[field] != expected:
                errors.append(f"attribution_{field}={source_id}")
        if record["license_classification"] != "open-government-licence-3.0":
            errors.append(f"attribution_license={source_id}")
        if not str(record["license_url"]).startswith("https://"):
            errors.append(f"attribution_license_url={source_id}")
    required_sources = {
        source["source_id"]
        for source in sources.values()
        if source.get("license_classification") == "open-government-licence-3.0"
    }
    observed_sources = {
        record.get("source_id") for record in records if isinstance(record, Mapping)
    }
    for source_id in sorted(required_sources - observed_sources):
        errors.append(f"attribution_missing={source_id}")
    return errors


def _source_registry_checksum() -> str:
    return _checksum(
        json.dumps(
            _source_registry(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


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
        artifact
        for artifact in artifacts
        if artifact["kind"] == "proposal"
        and _rendering_asset(artifact["artifact_id"], "pdf").is_file()
    )
    quote = next(
        artifact
        for artifact in artifacts
        if artifact["kind"] == "quote"
        and _rendering_asset(artifact["artifact_id"], "xlsx").is_file()
    )
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


def _checkpoint_evidence(
    checkpoint: dict[str, Any], artifacts: list[dict[str, Any]]
) -> dict[str, str]:
    by_key = {
        artifact["artifact_key"]: artifact
        for artifact in artifacts
        if artifact["available_at"] <= checkpoint["available_at"]
    }
    required = tuple(checkpoint["required_artifact_keys"])
    selected: dict[str, str] = {}
    for artifact_key in required:
        artifact = by_key.get(artifact_key)
        if artifact is None or artifact["gate_id"] != checkpoint["gate_id"]:
            raise ValueError(
                f"required checkpoint artifact is missing: {checkpoint['checkpoint_id']}:{artifact_key}"
            )
        selected[artifact_key] = artifact["artifact_id"]
    roles = checkpoint["required_artifact_roles"]
    evidence_key = next(key for key in required if roles[key] == "evidence")
    decision_key = next(key for key in required if roles[key] == "decision")
    crm_key = next(key for key in required if roles[key] == "projection")
    communication_key = next(
        key for key in required if by_key[key]["kind"] in {"email", "call_transcript"}
    )
    document_key = next(
        (
            key
            for key in required
            if by_key[key]["kind"]
            in {
                "proposal",
                "quote",
                "contract",
                "diligence_document",
                "policy_document",
            }
        ),
        communication_key,
    )
    selected.update(
        {
            "communication": selected[communication_key],
            "document": selected[document_key],
            "crm": selected[crm_key],
            "decision": selected[decision_key],
            "evidence": selected[evidence_key],
        }
    )
    return selected


def _semantic_action_codes(
    resolution: str, expected_outcome: str, terminal: bool
) -> dict[str, str]:
    if terminal and expected_outcome == "closed_won":
        return {
            "purpose": "close_won",
            "decision": "confirm_closing_authority",
            "commitment": "handoff_delivery",
            "commitment_owner": "account_executive",
            "next_step_type": "delivery_handoff",
        }
    if terminal:
        return {
            "purpose": f"record_{expected_outcome}",
            "decision": f"confirm_{resolution}_disposition",
            "commitment": (
                "defer_outreach"
                if expected_outcome == "no_decision"
                else "stop_pursuit"
            ),
            "commitment_owner": (
                "sales_manager" if expected_outcome == "no_decision" else "revops"
            ),
            "next_step_type": (
                "monitor_reentry"
                if expected_outcome == "no_decision"
                else "archive_disposition"
            ),
        }
    return {
        "purpose": "advance_gate",
        "decision": "confirm_gate_authority",
        "commitment": "record_before_advancing",
        "commitment_owner": "account_executive",
        "next_step_type": "buyer_gate_decision",
    }


def _milestone_business_blueprint(
    world: dict[str, Any],
    checkpoint: dict[str, Any],
    resolution: str,
    expected_outcome: str,
    terminal: bool,
    evidence_ids: list[str],
    decision_artifact_ids: Sequence[str],
    authority_actor_id: str,
) -> dict[str, Any]:
    sequence = int(checkpoint["sequence"])
    semantic_codes = _semantic_action_codes(resolution, expected_outcome, terminal)
    defect_fields = {
        defect["field"]: defect["truth_value"]
        for defect in world["defects"]
        if defect["checkpoint_sequence"] == sequence
    }
    static_defect_fields = {
        field: value
        for field, value in defect_fields.items()
        if field not in {"close_date", "next_step"}
    }
    exact_fields = {
        "amount_minor_units": world["amount_minor_units"],
        "currency": world["currency"],
        **static_defect_fields,
    }
    nonempty_fields: list[str]
    number_ranges: dict[str, dict[str, float]] = {}
    date_ranges: dict[str, dict[str, str]] = {}
    if terminal and expected_outcome == "closed_won":
        changes = {
            **defect_fields,
            "stage": "closed_won",
            "forecast_probability": 1.0,
            "close_date": world["forecast_close_date"],
            "next_step": f"account executive to hand the accepted {checkpoint['visible_gate']} scope to delivery",
            "next_step_type": "delivery_handoff",
            "next_step_gate_id": checkpoint["gate_id"],
            "disposition_code": resolution,
        }
        exact_fields = {
            **exact_fields,
            "stage": changes["stage"],
            "forecast_probability": changes["forecast_probability"],
            "close_date": changes["close_date"],
        }
        nonempty_fields = ["next_step"]
        kind = "closure_record"
        purpose = f"record accepted {checkpoint['visible_gate']} and closing handoff"
        requested_decisions = [
            f"confirm closing authority for accepted {checkpoint['visible_gate']}"
        ]
        commitments = [
            f"handoff the accepted {checkpoint['visible_gate']} scope to delivery"
        ]
        text_reference_fields = {"next_step": ["next_step_gate_id"]}
        content_terms = [
            checkpoint["visible_gate"],
            "closed_won",
            *evidence_ids,
        ]
        write_fields = [
            "stage",
            "forecast_probability",
            "close_date",
            "next_step",
            *defect_fields,
        ]
    elif terminal:
        loss_reason = f"{checkpoint['visible_gate']} {resolution}"
        next_step = (
            f"sales manager to monitor buyer re-entry for {checkpoint['visible_gate']} after the deferred decision"
            if expected_outcome == "no_decision"
            else f"revops to archive the {checkpoint['visible_gate']} disposition and stop active outreach"
        )
        changes = {
            **defect_fields,
            "stage": expected_outcome,
            "forecast_probability": 0.0,
            "close_date": checkpoint["date"],
            "loss_reason": loss_reason,
            "next_step": next_step,
            "next_step_type": semantic_codes["next_step_type"],
            "next_step_gate_id": checkpoint["gate_id"],
            "disposition_code": resolution,
        }
        exact_fields = {
            **exact_fields,
            "stage": changes["stage"],
            "forecast_probability": changes["forecast_probability"],
            "close_date": changes["close_date"],
            "loss_reason": changes["loss_reason"],
        }
        nonempty_fields = ["next_step", "loss_reason"]
        kind = "disposition_record"
        purpose = f"record the {resolution} {expected_outcome} disposition for {checkpoint['visible_gate']}"
        requested_decisions = [
            f"confirm the {resolution} disposition for {checkpoint['visible_gate']}"
        ]
        commitments = [
            f"defer outreach for {resolution} {checkpoint['visible_gate']} pending buyer re-entry"
            if expected_outcome == "no_decision"
            else f"stop active pursuit of {resolution} {checkpoint['visible_gate']} and preserve the disposition record"
        ]
        text_reference_fields = {
            "loss_reason": ["next_step_gate_id", "disposition_code"],
            "next_step": ["next_step_gate_id"],
        }
        content_terms = [
            checkpoint["visible_gate"],
            expected_outcome,
            resolution,
            *evidence_ids,
        ]
        write_fields = [
            "stage",
            "forecast_probability",
            "close_date",
            "loss_reason",
            "next_step",
            *defect_fields,
        ]
    else:
        next_checkpoint = world["checkpoints"][sequence + 1]
        projected_close_date = (
            world["checkpoints"][int(world["resolution_sequence"])]["date"]
            if world["variant_index"] == 1
            and sequence >= int(world["intervention_sequence"])
            else world["forecast_close_date"]
        )
        changes = {
            "next_step": f"account executive to confirm the {next_checkpoint['visible_gate']} decision with buyer authority",
            "next_step_owner": "account_executive",
            "next_step_decision": f"confirm {next_checkpoint['visible_gate']} evidence and authority",
            "next_step_date": next_checkpoint["date"],
            "next_step_type": semantic_codes["next_step_type"],
            "next_step_gate_id": next_checkpoint["gate_id"],
            "disposition_code": resolution,
            "close_date": projected_close_date,
            "forecast_probability": round(
                0.3
                + 0.5 * checkpoint["sequence"] / max(1, world["checkpoint_count"] - 1),
                3,
            ),
            **defect_fields,
        }
        nonempty_fields = [
            "next_step",
            "next_step_decision",
            "next_step_owner",
        ]
        number_ranges = {"forecast_probability": {"minimum": 0.0, "maximum": 1.0}}
        date_ranges = {
            "close_date": {
                "not_before": checkpoint["date"],
                "not_after": world["end_at"][:10],
            },
            "next_step_date": {
                "not_before": checkpoint["date"],
                "not_after": next_checkpoint["date"],
            },
        }
        kind = "gate_evidence"
        purpose = (
            f"confirm {resolution} {checkpoint['visible_gate']} evidence and authority"
        )
        requested_decisions = [
            f"confirm the accountable owner for {resolution} {checkpoint['visible_gate']}"
        ]
        commitments = [
            f"record {resolution} {checkpoint['visible_gate']} corrections before advancing"
        ]
        text_reference_fields = {
            "next_step": ["next_step_gate_id"],
            "next_step_decision": ["next_step_gate_id"],
        }
        content_terms = [checkpoint["visible_gate"], *evidence_ids]
        write_fields = [
            "next_step",
            "next_step_owner",
            "next_step_decision",
            "next_step_date",
            "close_date",
            "forecast_probability",
            *defect_fields,
        ]
    exact_fields.update(
        {
            "disposition_code": changes["disposition_code"],
            "next_step_gate_id": changes["next_step_gate_id"],
            "next_step_type": changes["next_step_type"],
        }
    )
    write_fields.extend(("disposition_code", "next_step_gate_id", "next_step_type"))
    evidence_claims = [
        {
            "artifact_id": evidence_id,
            "claim_type": (
                "supports_gate_resolution"
                if evidence_id in set(decision_artifact_ids)
                else "supports_gate_basis"
            ),
            "gate_id": checkpoint["gate_id"],
            "resolution": resolution,
        }
        for evidence_id in evidence_ids
    ]
    due_at = (
        world["checkpoints"][sequence + 1]["available_at"]
        if sequence + 1 < len(world["checkpoints"])
        else world["end_at"]
    )
    envelope = {
        "target_actor_id": authority_actor_id,
        "purpose": purpose,
        "purpose_code": semantic_codes["purpose"],
        "gate_id": checkpoint["gate_id"],
        "resolution": resolution,
        "related_records": [world["deal_id"]],
        "requested_decisions": requested_decisions,
        "decision_codes": [semantic_codes["decision"]],
        "commitments": commitments,
        "commitment_codes": [semantic_codes["commitment"]],
        "commitment_owner_role": semantic_codes["commitment_owner"],
        "decision_due_at": due_at,
        "commitment_due_at": due_at,
        "attachments": evidence_ids,
        "evidence_claims": evidence_claims,
    }
    semantic_requirements = {
        "authority_actor_id": authority_actor_id,
        "commitment_code": semantic_codes["commitment"],
        "commitment_owner_role": semantic_codes["commitment_owner"],
        "decision_code": semantic_codes["decision"],
        "evidence_claims": evidence_claims,
        "gate_id": checkpoint["gate_id"],
        "purpose_code": semantic_codes["purpose"],
        "resolution": resolution,
    }
    return {
        "changes": changes,
        "deliverable_kind": kind,
        "deliverable_content_terms": content_terms,
        "envelope": envelope,
        "requirements": {
            "decision_followup": {
                "allowed_channels": ["email", "calendar"],
                "recipient_actor_id": authority_actor_id,
                "related_record_id": world["deal_id"],
                "required_evidence_ids": evidence_ids,
                "required_message_facts": [
                    checkpoint["visible_gate"],
                    resolution,
                ],
                "semantic_requirements": semantic_requirements,
                "sender_role": "account_executive",
            },
            "crm_projection": {
                "date_ranges": date_ranges,
                "exact_fields": exact_fields,
                "nonempty_fields": nonempty_fields,
                "number_ranges": number_ranges,
                "record_id": world["deal_id"],
                "text_reference_fields": text_reference_fields,
                "write_fields": list(dict.fromkeys(write_fields)),
                "writer_role": "revops",
            },
            "deliverable": {
                "author_role": "domain_specialist",
                "kind": kind,
                "minimum_version": 1,
                "related_id": world["deal_id"],
                "related_type": "opportunity",
                "required_content_terms": content_terms,
                "required_evidence_ids": evidence_ids,
                "semantic_requirements": semantic_requirements,
            },
        },
    }


def _verification_facts(
    world: dict[str, Any], artifacts: list[dict[str, Any]]
) -> dict[str, Any]:
    checkpoint_sources = {
        checkpoint["checkpoint_id"]: _checkpoint_evidence(checkpoint, artifacts)
        for checkpoint in world["checkpoints"]
    }
    artifact_by_id = {artifact["artifact_id"]: artifact for artifact in artifacts}
    actor_by_id = {actor["actor_id"]: actor for actor in world["actors"]}
    intervention = world["checkpoints"][world["intervention_sequence"]]
    resolution_checkpoint = world["checkpoints"][world["resolution_sequence"]]
    branch_artifacts = [
        artifact
        for artifact in artifacts
        if artifact.get("branch_id") is not None
        and artifact["gate_id"] == resolution_checkpoint["gate_id"]
    ]
    branch_id = str(branch_artifacts[0]["branch_id"])
    artifacts_by_option = {
        option: [
            artifact["artifact_id"]
            for artifact in branch_artifacts
            if artifact["branch_option"] == option
        ]
        for option in ("success", "fallback")
    }
    objective_ids = [
        _opaque_id("objective", world["world_id"], checkpoint["sequence"])
        for checkpoint in world["checkpoints"]
    ]
    expected_outcome = TERMINAL_OUTCOMES[world["reference_outcome"]]
    terminal_sequence = _terminal_sequence(world)
    recoverable = world["variant_index"] == 0
    fallback_outcome = _fallback_outcome(world)
    milestones: list[dict[str, Any]] = []
    approval_requirements: list[dict[str, Any]] = []
    lane_effects = {
        "accepted": {
            "validation": {"delta": 10, "fact": "authority accepted the gate"},
            "stakeholder_consensus": {
                "delta": 5,
                "fact": "all required authorities accepted the gate",
            },
        },
        "rejected": {
            "stakeholder_consensus": {
                "delta": -30,
                "fact": "a required authority rejected the gate",
            }
        },
        "deferred": {
            "urgency": {
                "delta": -25,
                "fact": "a required authority deferred the gate",
            }
        },
        "remedied": {
            "validation": {
                "delta": 10,
                "fact": "the adverse gate was remedied with current evidence",
            },
            "stakeholder_consensus": {
                "delta": 10,
                "fact": "all required authorities accepted the remedy",
            },
        },
        "inapplicable": {},
    }
    sales_manager_actor_id = next(
        actor["actor_id"]
        for actor in world["actors"]
        if actor["authority"]["role_id"] == "seller.sales_manager"
    )
    for checkpoint in world["checkpoints"]:
        sequence = int(checkpoint["sequence"])
        reference_resolution = _decision_state(world, checkpoint)
        if sequence > terminal_sequence:
            reference_resolution = "inapplicable"
        elif sequence == int(resolution_checkpoint["sequence"]) and recoverable:
            reference_resolution = "remedied"
        sources = checkpoint_sources[checkpoint["checkpoint_id"]]
        is_branch = (
            checkpoint["checkpoint_id"] == resolution_checkpoint["checkpoint_id"]
        )
        decision_group_id = artifact_by_id[sources["decision"]].get("decision_group_id")
        decision_artifact_ids = sorted(
            {
                artifact["artifact_id"]
                for artifact in artifacts
                if artifact.get("decision_group_id") == decision_group_id
                and (is_branch or artifact.get("branch_id") is None)
            }
        )
        role_evidence = {
            "account_executive": [sources["communication"]],
            "domain_specialist": [sources["evidence"]],
            "sales_manager": [],
            "revops": [sources["document"]],
        }
        if is_branch:
            role_evidence = {
                role: [
                    artifact_id
                    for artifact_id in evidence
                    if artifact_id not in decision_artifact_ids
                ]
                for role, evidence in role_evidence.items()
            }
        static_evidence = sorted(
            {artifact_id for values in role_evidence.values() for artifact_id in values}
        )
        evidence_ids = sorted({*static_evidence, *decision_artifact_ids})
        reference_decision_ids = (
            artifacts_by_option["success"]
            if recoverable and is_branch
            else artifacts_by_option["fallback"]
            if is_branch
            else decision_artifact_ids
        )
        authority_actor_ids = [
            artifact_by_id[artifact_id]["authority_decision_actor_id"]
            for artifact_id in reference_decision_ids
        ]
        authority_requirements = []
        for actor_id in authority_actor_ids:
            actor = actor_by_id[actor_id]
            scope = (
                "seller"
                if actor["organization_id"] == world["seller_org_id"]
                else "buyer"
                if actor["organization_id"] == world["buyer_org_id"]
                else "third_party"
            )
            authority_requirements.append(
                {
                    "actor_id": actor_id,
                    "decision_artifact_ids": [
                        artifact_id
                        for artifact_id in decision_artifact_ids
                        if artifact_by_id[artifact_id]["authority_decision_actor_id"]
                        == actor_id
                    ],
                    "organization_scope": scope,
                    "rights": list(actor["authority"]["rights"]),
                }
            )
        external_authority = min(
            (
                requirement
                for requirement in authority_requirements
                if requirement["organization_scope"] != "seller"
            ),
            key=lambda requirement: (
                requirement["organization_scope"] != "buyer",
                actor_by_id[requirement["actor_id"]]["authority"]["role_id"],
            ),
        )
        allowed = {reference_resolution, "inapplicable"}
        terminal_mapping: dict[str, str] = {}
        resolution_decisions: dict[str, list[str]] = {}
        if is_branch:
            fallback_state = artifact_by_id[artifacts_by_option["fallback"][0]][
                "structured_payload"
            ]["decision_state"]
            allowed = {"inapplicable", "remedied", fallback_state}
            resolution_decisions["remedied"] = artifacts_by_option["success"]
            resolution_decisions[fallback_state] = artifacts_by_option["fallback"]
            terminal_mapping[fallback_state] = fallback_outcome
            if recoverable and sequence == terminal_sequence:
                terminal_mapping["remedied"] = expected_outcome
        elif reference_resolution != "inapplicable":
            resolution_decisions[reference_resolution] = reference_decision_ids
            if sequence == terminal_sequence:
                terminal_mapping[reference_resolution] = expected_outcome
        approval_exception = _seller_approval_exception(world, checkpoint)
        approval_requirement = None
        if (
            approval_exception["required"]
            and (not is_branch or recoverable)
            and any(resolution in {"accepted", "remedied"} for resolution in allowed)
        ):
            approval_requirement = {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "gate_id": checkpoint["gate_id"],
                "approver_actor_ids": [sales_manager_actor_id],
                "amount_minor_units": approval_exception["basis"]["amount_minor_units"],
                "basis": approval_exception["basis"],
                "policy_limit_minor_units": approval_exception[
                    "policy_limit_minor_units"
                ],
                "policy_owner": approval_exception["policy_owner"],
                "policy_evidence": approval_exception["policy_evidence"],
                "trigger": approval_exception["trigger"],
            }
            approval_requirements.append(approval_requirement)
        business_effects: dict[str, Any] = {}
        for resolution, resolution_decision_ids in resolution_decisions.items():
            resolution_outcome = terminal_mapping.get(resolution, expected_outcome)
            resolution_evidence = sorted({*static_evidence, *resolution_decision_ids})
            blueprint = _milestone_business_blueprint(
                world,
                checkpoint,
                resolution,
                resolution_outcome,
                resolution in terminal_mapping or sequence == terminal_sequence,
                resolution_evidence,
                resolution_decision_ids,
                external_authority["actor_id"],
            )
            business_effects[resolution] = blueprint["requirements"]
        milestone_id = _opaque_id("milestone", world["world_id"], sequence)
        remedy_of = (
            _opaque_id("milestone", world["world_id"], world["intervention_sequence"])
            if is_branch
            else None
        )
        if remedy_of is not None:
            prerequisite_ids = [remedy_of]
        elif (
            int(world["intervention_sequence"])
            < sequence
            < int(world["resolution_sequence"])
        ):
            prerequisite_ids = (
                [milestones[int(world["intervention_sequence"]) - 1]["milestone_id"]]
                if int(world["intervention_sequence"]) > 0
                else []
            )
        else:
            prerequisite_ids = [milestones[-1]["milestone_id"]] if milestones else []
        milestones.append(
            {
                "milestone_id": milestone_id,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "gate_id": checkpoint["gate_id"],
                "decision_artifact_ids": decision_artifact_ids,
                "evidence_ids": evidence_ids,
                "evidence_requirements_by_role": role_evidence,
                "decision_evidence_role": "sales_manager",
                "authority_requirements": authority_requirements,
                "chronology": {
                    "sequence": sequence,
                    "available_at": checkpoint["available_at"],
                    "decision_times": {
                        artifact_id: {
                            "created_at": artifact_by_id[artifact_id]["created_at"],
                            "available_at": artifact_by_id[artifact_id]["available_at"],
                        }
                        for artifact_id in decision_artifact_ids
                    },
                },
                "prerequisite_milestone_ids": prerequisite_ids,
                "allowed_resolutions": sorted(allowed),
                "approval_requirement": approval_requirement,
                "business_effect_requirements_by_resolution": business_effects,
                "lane_effects_by_resolution": {
                    resolution: lane_effects[resolution] for resolution in allowed
                },
                "terminal_outcome_by_resolution": terminal_mapping,
                "remedy_of": remedy_of,
                "branch_id": branch_id if is_branch else None,
            }
        )
    intervention_sources = checkpoint_sources[intervention["checkpoint_id"]]
    resolution_milestone = milestones[int(resolution_checkpoint["sequence"])]
    recovery_authorities = list(resolution_milestone["authority_requirements"])
    remediation_evidence_ids = [
        intervention_sources[source_key]
        for source_key in _structured_causal_source_keys(world, intervention)
    ]
    remediation_requirements = {
        "action_code": world["causal_action_code"],
        "cure_data": _causal_cure_data(world),
        "due_at": resolution_checkpoint["available_at"],
        "evidence_checksums": {
            artifact_id: artifact_by_id[artifact_id]["checksum"]
            for artifact_id in remediation_evidence_ids
        },
        "evidence_ids": remediation_evidence_ids,
        "owner_role": world["causal_owner_role"],
    }
    fallback_state = artifact_by_id[artifacts_by_option["fallback"][0]][
        "structured_payload"
    ]["decision_state"]
    response_resolution = "accepted" if recoverable else fallback_state
    crm_effect_id = _opaque_id("effect", world["world_id"], "crm")
    action_effect_rules: list[dict[str, Any]] = []
    authority_effect_ids: list[str] = []
    for requirement in recovery_authorities:
        actor_id = requirement["actor_id"]
        effect_id = _opaque_id("effect", world["world_id"], actor_id, "decision")
        authority_effect_ids.append(effect_id)
        action_effect_rules.append(
            {
                "effect_id": effect_id,
                "branch_id": branch_id,
                "checkpoint_id": intervention["checkpoint_id"],
                "fact_type": "authority_decision_observed",
                "role": "account_executive",
                "gate_id": resolution_checkpoint["gate_id"],
                "record_id": world["deal_id"],
                "tool_names": ["communications.send"],
                "required_evidence_ids": remediation_evidence_ids,
                "authority_actor_id": actor_id,
                "authority_rights": requirement["rights"],
                "next_gate_id": None,
                "purpose_code": "recover_gate",
                "decision_code": "request_remediation_decision",
                "commitment_code": "complete_remediation",
                "resolution": "pending",
                "document_kind": "remediation_plan",
                "next_step_type": None,
                "remediation_requirements": remediation_requirements,
                "response_resolution": response_resolution,
            },
        )
    action_effect_rules.append(
        {
            "effect_id": crm_effect_id,
            "branch_id": branch_id,
            "checkpoint_id": intervention["checkpoint_id"],
            "fact_type": "crm_transition",
            "role": "revops",
            "gate_id": resolution_checkpoint["gate_id"],
            "record_id": world["deal_id"],
            "tool_names": ["crm.update"],
            "required_evidence_ids": [intervention_sources["document"]],
            "authority_actor_id": None,
            "authority_rights": [],
            "next_gate_id": resolution_checkpoint["gate_id"],
            "purpose_code": None,
            "decision_code": None,
            "commitment_code": None,
            "resolution": None,
            "document_kind": None,
            "next_step_type": "remediation_decision",
            "remediation_requirements": None,
            "response_resolution": None,
        },
    )
    success_options = [[*authority_effect_ids, crm_effect_id]]
    branches = [
        {
            "branch_id": branch_id,
            "action_checkpoint_id": intervention["checkpoint_id"],
            "resolution_checkpoint_id": resolution_checkpoint["checkpoint_id"],
            "remedy_milestone_id": _opaque_id(
                "milestone", world["world_id"], resolution_checkpoint["sequence"]
            ),
            "recoverable": recoverable,
            "success_if_any": success_options,
            "success_decision_artifact_ids": artifacts_by_option["success"],
            "fallback_decision_artifact_ids": artifacts_by_option["fallback"],
        }
    ]
    evidence_ids = sorted(
        {
            artifact_id
            for source_map in checkpoint_sources.values()
            for artifact_id in source_map.values()
        }
        | {
            artifact_id
            for milestone in milestones
            for artifact_id in milestone["evidence_ids"]
        }
        | {
            artifact_id
            for rule in action_effect_rules
            for artifact_id in rule["required_evidence_ids"]
        }
    )
    recovery_evidence_ids = {
        artifact_id
        for rule in action_effect_rules
        for artifact_id in rule["required_evidence_ids"]
    }
    return {
        "deal_id": world["deal_id"],
        "checkpoint_ids": world["checkpoint_ids"],
        "objective_ids": objective_ids,
        "responsible_roles": list(ROLES),
        "checkpoint_sources": checkpoint_sources,
        "milestones": milestones,
        "action_effect_rules": action_effect_rules,
        "branches": branches,
        "approval_required": bool(approval_requirements),
        "approval_required_checkpoint_ids": [
            requirement["checkpoint_id"] for requirement in approval_requirements
        ],
        "approval_requirements": approval_requirements,
        "evidence_catalog": {
            artifact_id: {
                "available_at": artifact_by_id[artifact_id]["available_at"],
                "visibility": artifact_by_id[artifact_id]["visibility"],
                "gate_id": artifact_by_id[artifact_id].get("gate_id"),
                "branch_id": artifact_by_id[artifact_id].get("branch_id"),
                "branch_option": artifact_by_id[artifact_id].get("branch_option"),
                "supersedes_artifact_id": artifact_by_id[artifact_id].get(
                    "supersedes_artifact_id"
                ),
                "logical_document_id": artifact_by_id[artifact_id].get(
                    "logical_document_id"
                ),
                "version": artifact_by_id[artifact_id].get("version"),
                "checkpoint_ids": [
                    checkpoint_id
                    for checkpoint_id, source_map in checkpoint_sources.items()
                    if artifact_id in source_map.values()
                ],
                "gates": [
                    checkpoint["visible_gate"]
                    for checkpoint in world["checkpoints"]
                    if artifact_id
                    in checkpoint_sources[checkpoint["checkpoint_id"]].values()
                ],
            }
            for artifact_id in evidence_ids
        },
        "intervention_at": intervention["available_at"],
        "post_intervention_evidence_refs": [
            artifact_id
            for artifact_id in evidence_ids
            if artifact_by_id[artifact_id]["available_at"]
            >= intervention["available_at"]
            or artifact_id in recovery_evidence_ids
        ],
        "crm_defects": world["defects"],
        "expected_amount_minor_units": world["amount_minor_units"],
        "expected_close_date": (
            world["forecast_close_date"]
            if expected_outcome == "closed_won"
            else world["checkpoints"][terminal_sequence]["date"]
        ),
        "expected_terminal_outcome": expected_outcome,
        "seller_organization_id": world["seller_org_id"],
        "buyer_organization_id": world["buyer_org_id"],
        "actor_activity": {
            actor["actor_id"]: {
                "kind": actor["kind"],
                "organization_id": actor["organization_id"],
                "email": actor["email"],
                "active_from": actor["active_from"],
                "active_until": actor.get("active_until"),
                "role_tags": actor["role_tags"],
            }
            for actor in world["actors"]
        },
        "allowed_related_ids": sorted(
            {
                world["deal_id"],
                world["buyer_org_id"],
                world["seller_org_id"],
                *(
                    str(artifact["record_id"])
                    for artifact in artifacts
                    if artifact.get("record_id")
                ),
            }
        ),
    }


def _build_rubric(
    world: dict[str, Any], artifacts: list[dict[str, Any]]
) -> dict[str, Any]:
    facts = _verification_facts(world, artifacts)
    sources = facts["checkpoint_sources"]
    by_group = {
        group: list(
            dict.fromkeys(
                source[group] for source in sources.values() if group in source
            )
        )
        for group in ("communication", "crm", "document", "external")
    }
    all_evidence = list(facts["evidence_catalog"])
    final_checkpoint = world["checkpoints"][-1]
    objective_ids = facts["objective_ids"]
    specifications = {
        "evidence_and_understanding": (
            "source_evidence_read",
            "cross_channel_evidence_read",
            "evidence_read_before_write",
            "milestone_evidence_coverage_score",
            "post_intervention_evidence_read",
        ),
        "crm_integrity": (
            "stage_defect_repaired",
            "close_date_defect_repaired",
            "next_step_defect_repaired",
            "crm_history_preserved",
            "crm_terminal_state_consistent",
        ),
        "stakeholder_management": (
            "active_buyer_contacted",
            "decision_request_sent",
            "commitment_request_sent",
            "post_intervention_buyer_contacted",
            "stakeholder_response_received",
        ),
        "workflow_compliance": (
            "required_roles_completed",
            "checkpoint_order_preserved",
            "approval_path_handled",
            "business_effect_coverage_score",
            "terminal_state_supported",
        ),
        "communication_quality": (
            "grounded_attachment_sent",
            "related_record_linked",
            "communication_claim_coverage_score",
            "authority_audience_coverage_score",
            "external_content_brokered",
        ),
        "forecast_discipline": (
            "forecast_recorded",
            "forecast_probability_valid",
            "forecast_cutoff_coverage_score",
            "forecast_chronology_preserved",
            "forecast_updated_after_intervention",
        ),
        "longitudinal_recovery": (
            "branch_recovery_effect_coverage_score",
            "post_intervention_crm_update",
            "post_intervention_stakeholder_action",
            "inactive_stakeholder_avoided",
            "terminal_rationale_supported",
        ),
        "side_effect_discipline": (
            "write_scope_coverage_score",
            "no_unrelated_removals",
            "no_duplicate_external_writes",
            "write_authorization_coverage_score",
            "idempotency_keys_unique",
        ),
    }
    roles = {
        "evidence_and_understanding": ["account_executive", "domain_specialist"],
        "crm_integrity": ["revops"],
        "stakeholder_management": ["account_executive"],
        "workflow_compliance": ["sales_manager", "revops"],
        "communication_quality": ["account_executive"],
        "forecast_discipline": ["sales_manager", "revops"],
        "longitudinal_recovery": ["account_executive", "sales_manager"],
        "side_effect_discipline": ["sales_manager", "revops"],
    }
    evidence = {
        "evidence_and_understanding": by_group["communication"] + by_group["document"],
        "crm_integrity": by_group["crm"],
        "stakeholder_management": by_group["communication"],
        "workflow_compliance": by_group["document"] + by_group["crm"],
        "communication_quality": by_group["communication"] + by_group["document"],
        "forecast_discipline": by_group["crm"],
        "longitudinal_recovery": facts["post_intervention_evidence_refs"],
        "side_effect_discipline": by_group["crm"] + by_group["document"],
    }
    assertions: list[dict[str, Any]] = []
    for category, names in specifications.items():
        refs = list(dict.fromkeys(evidence[category])) or all_evidence[:1]
        for name in names:
            metric = name.endswith("_score")
            target: dict[str, Any] = (
                {
                    "path": f"verifier.{name}",
                    "operator": "gte",
                    "expected": 0,
                    "minimum_score": 1.0,
                }
                if metric
                else {
                    "path": f"verifier.{name}",
                    "operator": "equals",
                    "expected": True,
                }
            )
            assertions.append(
                {
                    "assertion_id": _opaque_id(
                        "assertion", world["world_id"], category, name
                    ),
                    "world_id": world["world_id"],
                    "scope": "world",
                    "category": category,
                    "kind": "metric" if metric else "deterministic",
                    "target": target,
                    "required": True,
                    "critical": False,
                    "controllability": "controllable",
                    "weight": 0.2,
                    "evidence_refs": refs,
                    "semantic_target": name,
                    "responsible_roles": roles[category],
                    "objective_ids": objective_ids,
                    "available_by": final_checkpoint["available_at"],
                    "provenance": {
                        "source": "synthetic_blueprint",
                        "license": "CC-BY-4.0",
                    },
                }
            )
    communication_refs = (
        list(dict.fromkeys(evidence["communication_quality"])) or all_evidence[:1]
    )
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
                "path": "verifier.communication_candidate_available",
                "operator": "equals",
                "expected": True,
            },
            "required": False,
            "critical": False,
            "controllability": "partially_controllable",
            "weight": 0.05,
            "evidence_refs": communication_refs,
            "semantic_target": "communication_grounding_diagnostic",
            "responsible_roles": ["account_executive"],
            "objective_ids": objective_ids,
            "available_by": final_checkpoint["available_at"],
            "judge": {
                "criterion": "Diagnostic only until calibrated: assess the draft call text for grounding, clarity, tone, and unauthorized claims against the cited evidence and brokered result.",
                "judge_version": DATASET_VERSION,
                "prompt_hash": _checksum("edlb communication diagnostic v1"),
            },
            "provenance": {
                "source": "synthetic_blueprint",
                "license": "CC-BY-4.0",
            },
        }
    )
    return {
        "rubric_version": DATASET_VERSION,
        "contract": "trusted-verifier-v1",
        "world_id": world["world_id"],
        "deterministic_weight": 8.0,
        "categories": list(CANONICAL_CATEGORIES),
        "assertions": assertions,
    }


def _build_reference_trace(
    world: dict[str, Any], artifacts: list[dict[str, Any]], scenario_hash: str
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
        if kind == "tool_result" and values.get("ok") is True:
            call = next(
                row
                for row in reversed(trace)
                if row.get("message_id") == values.get("call_id")
            )
            tool_name = str(call.get("tool_name", ""))
            if tool_name in WRITE_TOOLS:
                arguments = call.get("arguments", {})
                envelope = (
                    arguments.get("semantic_envelope")
                    if isinstance(arguments, Mapping)
                    else None
                )
                related = (
                    list(envelope.get("related_records", ()))
                    if isinstance(envelope, Mapping)
                    else []
                )
                classification = None
                if tool_name == "crm.update":
                    related = [arguments["record_id"]]
                elif tool_name == "crm.merge":
                    related = [arguments["source_id"], arguments["target_id"]]
                elif tool_name == "documents.attach":
                    related = [arguments["related_id"]]
                elif tool_name in {"approvals.approve", "approvals.reject"}:
                    approval_id = arguments["approval_id"]
                    request = next(
                        row
                        for row in reversed(trace)
                        if row.get("tool_name") == "approvals.request"
                        and any(
                            result.get("approval_id") == approval_id
                            for result_row in trace
                            if result_row.get("call_id") == row.get("message_id")
                            and isinstance(result := result_row.get("result"), Mapping)
                        )
                    )
                    related = list(
                        request["arguments"]["semantic_envelope"]["related_records"]
                    )
                elif tool_name == "team.send":
                    classification = "checkpoint_coordination"
                elif tool_name == "run.complete_checkpoint":
                    classification = "checkpoint_completion"
                if not related and classification is None:
                    raise ValueError(f"unscoped reference write: {tool_name}")
                values["result"] = {
                    **dict(values.get("result") or {}),
                    "write_scope": {
                        "related_records": sorted(set(related)),
                        "classification": classification,
                    },
                }
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
        payload={
            "world_id": world["world_id"],
            "track": "open_team",
            "scenario_hash": scenario_hash,
            "agent_manifest": REFERENCE_AGENT_MANIFEST,
            "limits": REFERENCE_TRACE_LIMITS,
            "configuration_hash": _reference_configuration_hash(),
        },
    )
    actors = {actor["actor_id"]: actor for actor in world["actors"]}
    artifacts_by_id = {artifact["artifact_id"]: artifact for artifact in artifacts}
    expected_outcome = TERMINAL_OUTCOMES[world["reference_outcome"]]
    verification = _verification_facts(world, artifacts)
    sales_manager_actor_id = next(
        actor["actor_id"]
        for actor in world["actors"]
        if actor["authority"]["role_id"] == "seller.sales_manager"
    )
    milestones_by_checkpoint = {
        milestone["checkpoint_id"]: milestone
        for milestone in verification["milestones"]
    }
    branch = verification["branches"][0]
    action_rules = {
        rule["fact_type"]: rule
        for rule in verification["action_effect_rules"]
        if rule["fact_type"] != "authority_decision_observed"
    }
    authority_rules = [
        rule
        for rule in verification["action_effect_rules"]
        if rule["fact_type"] == "authority_decision_observed"
    ]
    terminal_sequence = _terminal_sequence(world)
    trace_terminal_sequence = (
        terminal_sequence if world["variant_index"] == 0 else terminal_sequence - 1
    )
    for checkpoint in world["checkpoints"][: trace_terminal_sequence + 1]:
        checkpoint_window_end = (
            world["checkpoints"][checkpoint["sequence"] + 1]["available_at"]
            if checkpoint["sequence"] + 1 < world["checkpoint_count"]
            else world["end_at"]
        )
        milestone = milestones_by_checkpoint[checkpoint["checkpoint_id"]]
        resolution = (
            "remedied"
            if checkpoint["sequence"] == world["resolution_sequence"]
            and world["variant_index"] == 0
            else _decision_state(world, checkpoint)
        )
        observation_token = _opaque_id(
            "observation-token", world["world_id"], checkpoint["sequence"]
        )
        selected_decisions = [
            artifact_id
            for artifact_id in milestone["decision_artifact_ids"]
            if artifacts_by_id[artifact_id].get("branch_option")
            in {None, "success" if world["variant_index"] == 0 else "fallback"}
        ]
        external_authority = min(
            (
                requirement
                for requirement in milestone["authority_requirements"]
                if requirement["organization_scope"] != "seller"
            ),
            key=lambda requirement: (
                requirement["organization_scope"] != "buyer",
                actors[requirement["actor_id"]]["authority"]["role_id"],
            ),
        )
        authority_actor_id = external_authority["actor_id"]
        recipient = actors[authority_actor_id]
        gate = checkpoint["visible_gate"]
        role_evidence = {
            role: tuple(
                [*evidence, *selected_decisions]
                if role == milestone["decision_evidence_role"]
                else evidence
            )
            for role, evidence in milestone["evidence_requirements_by_role"].items()
        }
        resolution_evidence = sorted(
            {artifact_id for values in role_evidence.values() for artifact_id in values}
        )
        business_blueprint = _milestone_business_blueprint(
            world,
            checkpoint,
            resolution,
            expected_outcome,
            checkpoint["sequence"] == terminal_sequence,
            resolution_evidence,
            selected_decisions,
            authority_actor_id,
        )
        read_requirements = [
            (role, artifact_id)
            for role, evidence in role_evidence.items()
            for artifact_id in evidence
        ]
        if checkpoint["checkpoint_id"] == branch["action_checkpoint_id"]:
            read_requirements.extend(
                (rule["role"], artifact_id)
                for rule in verification["action_effect_rules"]
                for artifact_id in rule["required_evidence_ids"]
            )
            read_requirements.extend(
                (world["causal_owner_role"], artifact_id)
                for artifact_id in authority_rules[0]["required_evidence_ids"]
            )
        read_calls = []
        for role, artifact_id in dict.fromkeys(read_requirements):
            artifact = artifacts_by_id[artifact_id]
            kind = artifact["kind"]
            if kind in {"email", "transcript", "call_transcript"}:
                tool_name = "communications.read"
                read_arguments = {"message_id": artifact_id}
            elif kind in {
                "document",
                "proposal",
                "quote",
                "contract",
                "diligence_document",
                "policy_document",
            }:
                tool_name = "documents.read"
                read_arguments = {"document_id": artifact_id}
            elif kind in {"web_news", "web_page", "news_item"}:
                tool_name = "web.open"
                read_arguments = {"record_id": artifact_id}
            else:
                raise ValueError(f"milestone evidence is not readable: {kind}")
            read_calls.append((role, tool_name, read_arguments))
        for role, tool_name, read_arguments in read_calls:
            call_id = append(
                "tool_call",
                role,
                checkpoint["available_at"],
                tool_name=tool_name,
                arguments=read_arguments,
            )
            append(
                "tool_result",
                role,
                checkpoint["available_at"],
                call_id=call_id,
                ok=True,
                result={"status": "read"},
            )
        arguments = {
            "channel": "email",
            "recipients": [recipient["email"]],
            "subject": f"{gate} decision follow-up for {checkpoint['date']}",
            "body": "\n".join(
                [
                    business_blueprint["envelope"]["purpose"],
                    f"Resolution: {resolution}",
                    *business_blueprint["envelope"]["requested_decisions"],
                    *business_blueprint["envelope"]["commitments"],
                ]
            ),
            "semantic_envelope": business_blueprint["envelope"],
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
            result={"status": "sent_with_grounding"},
        )
        if recipient["organization_id"] != world["buyer_org_id"]:
            buyer_recipient = next(
                actor
                for actor in world["actors"]
                if actor["kind"] == "buyer"
                and actor["active_from"] <= checkpoint["available_at"]
                and (
                    actor.get("active_until") is None
                    or checkpoint["available_at"] < actor["active_until"]
                )
            )
            buyer_arguments = {
                "channel": "email",
                "recipients": [buyer_recipient["email"]],
                "subject": f"{gate} customer update for {checkpoint['date']}",
                "body": "\n".join(
                    [
                        business_blueprint["envelope"]["purpose"],
                        f"Resolution: {resolution}",
                        *business_blueprint["envelope"]["requested_decisions"],
                        *business_blueprint["envelope"]["commitments"],
                    ]
                ),
                "semantic_envelope": {
                    **business_blueprint["envelope"],
                    "target_actor_id": buyer_recipient["actor_id"],
                },
            }
            call_id = append(
                "tool_call",
                "account_executive",
                checkpoint["available_at"],
                tool_name="communications.send",
                arguments=buyer_arguments,
                idempotency_key=_opaque_id(
                    "reference",
                    world["world_id"],
                    checkpoint["sequence"],
                    "buyer-update",
                ),
            )
            append(
                "tool_result",
                "account_executive",
                checkpoint["available_at"],
                call_id=call_id,
                ok=True,
                result={"status": "sent_with_grounding"},
            )
        call_id = append(
            "tool_call",
            "account_executive",
            checkpoint["available_at"],
            tool_name="communications.search",
            arguments={"query": gate, "limit": 20},
        )
        append(
            "tool_result",
            "account_executive",
            checkpoint["available_at"],
            call_id=call_id,
            ok=True,
            result={"status": "searched"},
        )
        call_id = append(
            "tool_call",
            "revops",
            checkpoint["available_at"],
            tool_name="crm.update",
            arguments={
                "record_id": world["deal_id"],
                "changes": business_blueprint["changes"],
            },
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
                "title": f"{gate} {business_blueprint['deliverable_kind'].replace('_', ' ')}",
                "content": "\n".join(
                    [
                        *(
                            f"Support: {term}"
                            for term in business_blueprint["deliverable_content_terms"]
                        ),
                        "Evidence basis: Each cited source was reviewed against the accountable gate criteria.",
                        f"Purpose: {business_blueprint['envelope']['purpose']}",
                        *(
                            f"Decision request: {value}"
                            for value in business_blueprint["envelope"][
                                "requested_decisions"
                            ]
                        ),
                        *(
                            f"Commitment: {value}"
                            for value in business_blueprint["envelope"]["commitments"]
                        ),
                        "Owner: domain specialist",
                        f"Disposition: {resolution}",
                        "Decision record: The domain specialist documented the current disposition and next required action.",
                    ]
                ),
                "kind": business_blueprint["deliverable_kind"],
                "semantic_envelope": business_blueprint["envelope"],
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
        if checkpoint["checkpoint_id"] == branch["action_checkpoint_id"]:
            crm_rule = action_rules["crm_transition"]
            plan_rule = authority_rules[0]

            def recovery_envelope(
                rule: dict[str, Any], plan_id: str, due_at: str, gate_name: str = gate
            ) -> dict[str, Any]:
                evidence = list(rule["required_evidence_ids"])
                return {
                    "target_actor_id": rule["authority_actor_id"],
                    "purpose": f"request a supported remediation decision for {gate_name}",
                    "purpose_code": rule["purpose_code"],
                    "gate_id": rule["gate_id"],
                    "resolution": rule["resolution"],
                    "related_records": [world["deal_id"]],
                    "requested_decisions": [
                        f"confirm whether the documented {gate_name} remediation is acceptable"
                    ],
                    "decision_codes": [rule["decision_code"]],
                    "commitments": [
                        f"complete the documented {gate_name} remediation before the next decision"
                    ],
                    "commitment_codes": [rule["commitment_code"]],
                    "commitment_owner_role": "account_executive",
                    "decision_due_at": due_at,
                    "commitment_due_at": due_at,
                    "attachments": [*evidence, plan_id],
                    "evidence_claims": [
                        {
                            "artifact_id": artifact_id,
                            "claim_type": "supports_gate_basis",
                            "gate_id": rule["gate_id"],
                            "resolution": rule["resolution"],
                        }
                        for artifact_id in evidence
                    ],
                }

            recovery_document_key = _opaque_id(
                "reference",
                world["world_id"],
                checkpoint["sequence"],
                "recovery-document",
            )
            recovery_document_id = _side_effect_id(
                "document", world["world_id"], recovery_document_key
            )
            remediation = {
                "cure_data": plan_rule["remediation_requirements"]["cure_data"],
                "gate_id": checkpoint["gate_id"],
                "owner_role": plan_rule["remediation_requirements"]["owner_role"],
            }
            plan_evidence = list(plan_rule["required_evidence_ids"])
            plan_envelope = {
                "target_actor_id": plan_rule["authority_actor_id"],
                "purpose": f"document the evidence-backed remediation for {gate}",
                "purpose_code": "share_document",
                "gate_id": checkpoint["gate_id"],
                "resolution": "pending",
                "related_records": [world["deal_id"]],
                "requested_decisions": [],
                "decision_codes": [],
                "commitments": [
                    f"record the documented {gate} remediation plan by {checkpoint_window_end}"
                ],
                "commitment_codes": ["complete_remediation"],
                "commitment_owner_role": remediation["owner_role"],
                "decision_due_at": None,
                "commitment_due_at": checkpoint_window_end,
                "attachments": plan_evidence,
                "evidence_claims": [
                    {
                        "artifact_id": artifact_id,
                        "claim_type": "supports_gate_basis",
                        "gate_id": checkpoint["gate_id"],
                        "resolution": "pending",
                    }
                    for artifact_id in plan_evidence
                ],
            }
            call_id = append(
                "tool_call",
                remediation["owner_role"],
                checkpoint["available_at"],
                tool_name="documents.create",
                arguments={
                    "title": f"{gate} remediation plan",
                    "content": "\n".join(
                        [
                            f"Owner: {remediation['owner_role']}",
                            f"Required motion: {world['observable_cure']}",
                            f"Cure data: {json.dumps(remediation['cure_data'], sort_keys=True)}",
                        ]
                    ),
                    "kind": plan_rule["document_kind"],
                    "semantic_envelope": plan_envelope,
                    "remediation": remediation,
                },
                idempotency_key=recovery_document_key,
            )
            append(
                "tool_result",
                remediation["owner_role"],
                checkpoint["available_at"],
                call_id=call_id,
                ok=True,
                result={"document_id": recovery_document_id, "version": 1},
            )
            recovery_attach_key = _opaque_id(
                "reference",
                world["world_id"],
                checkpoint["sequence"],
                "recovery-attach",
            )
            call_id = append(
                "tool_call",
                remediation["owner_role"],
                checkpoint["available_at"],
                tool_name="documents.attach",
                arguments={
                    "document_id": recovery_document_id,
                    "related_type": "opportunity",
                    "related_id": world["deal_id"],
                },
                idempotency_key=recovery_attach_key,
            )
            append(
                "tool_result",
                remediation["owner_role"],
                checkpoint["available_at"],
                call_id=call_id,
                ok=True,
                result={
                    "document_id": recovery_document_id,
                    "related_type": "opportunity",
                    "related_id": world["deal_id"],
                },
            )
            for authority_rule in authority_rules:
                recovery_recipient = actors[authority_rule["authority_actor_id"]]
                recovery_email = recovery_envelope(
                    authority_rule, recovery_document_id, checkpoint_window_end
                )
                call_id = append(
                    "tool_call",
                    "account_executive",
                    checkpoint["available_at"],
                    tool_name="communications.send",
                    arguments={
                        "channel": "email",
                        "recipients": [recovery_recipient["email"]],
                        "subject": f"{gate} remediation decision",
                        "body": "\n".join(
                            [
                                recovery_email["purpose"],
                                *recovery_email["requested_decisions"],
                                *recovery_email["commitments"],
                            ]
                        ),
                        "semantic_envelope": recovery_email,
                    },
                    idempotency_key=_opaque_id(
                        "reference",
                        world["world_id"],
                        checkpoint["sequence"],
                        authority_rule["authority_actor_id"],
                        "recovery-email",
                    ),
                )
                append(
                    "tool_result",
                    "account_executive",
                    checkpoint["available_at"],
                    call_id=call_id,
                    ok=True,
                    result={"status": "sent_with_grounding"},
                )
            call_id = append(
                "tool_call",
                "revops",
                checkpoint["available_at"],
                tool_name="crm.update",
                arguments={
                    "record_id": world["deal_id"],
                    "changes": {
                        "next_step": f"secure the {gate} remediation decision",
                        "next_step_decision": f"confirm the {gate} remediation",
                        "next_step_owner": "account_executive",
                        "next_step_date": checkpoint["date"],
                        "next_step_gate_id": crm_rule["next_gate_id"],
                        "next_step_type": crm_rule["next_step_type"],
                        **(
                            {
                                "close_date": next(
                                    value["date"]
                                    for value in world["checkpoints"]
                                    if value["checkpoint_id"]
                                    == branch["resolution_checkpoint_id"]
                                ),
                                "forecast_probability": 0.15,
                            }
                            if not branch["recoverable"]
                            else {}
                        ),
                    },
                },
                idempotency_key=_opaque_id(
                    "reference",
                    world["world_id"],
                    checkpoint["sequence"],
                    "recovery-crm",
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
        approval_exception = _seller_approval_exception(world, checkpoint)
        if approval_exception["required"] and resolution in {"accepted", "remedied"}:
            approval_key = f"approval-request:{checkpoint['checkpoint_id']}:{checkpoint['gate_id']}"
            approval_id = _side_effect_id("approval", world["world_id"], approval_key)
            approval_details = {
                "amount_minor_units": approval_exception["basis"]["amount_minor_units"],
                "currency": world["currency"],
                "document_id": document_id,
                "deal_id": world["deal_id"],
                "record_id": world["deal_id"],
                "gate": checkpoint["gate_id"],
                "checkpoint_id": checkpoint["checkpoint_id"],
                "required_for_close": True,
                "basis": approval_exception["basis"],
                "policy_limit_minor_units": approval_exception[
                    "policy_limit_minor_units"
                ],
                "policy_owner": approval_exception["policy_owner"],
                "policy_evidence": approval_exception["policy_evidence"],
                "trigger": approval_exception["trigger"],
            }
            approval_envelope = {
                "target_actor_id": sales_manager_actor_id,
                "purpose": f"approve bounded {gate} commercial exception",
                "purpose_code": "update_account",
                "gate_id": checkpoint["gate_id"],
                "resolution": "pending",
                "related_records": [world["deal_id"]],
                "requested_decisions": [],
                "decision_codes": [],
                "commitments": [],
                "commitment_codes": [],
                "commitment_owner_role": "account_executive",
                "decision_due_at": None,
                "commitment_due_at": None,
                "attachments": [document_id],
                "evidence_claims": [],
            }
            call_id = append(
                "tool_call",
                "account_executive",
                checkpoint["available_at"],
                tool_name="approvals.request",
                arguments={
                    "approver_actor_ids": [sales_manager_actor_id],
                    "purpose": f"approve bounded {gate} commercial exception",
                    "details": approval_details,
                    "semantic_envelope": approval_envelope,
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
                    "approver_actor_ids": [sales_manager_actor_id],
                    "details": approval_details,
                },
            )
            decision_key = f"approval-decision:{checkpoint['checkpoint_id']}:{checkpoint['gate_id']}"
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
            call_id = append(
                "tool_call",
                role,
                checkpoint["available_at"],
                tool_name="run.complete_checkpoint",
                arguments={
                    "checkpoint_id": checkpoint["checkpoint_id"],
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
            if checkpoint["sequence"] == terminal_sequence and role == ROLES[-1]:
                result["outcome"] = TERMINAL_OUTCOMES[world["reference_outcome"]]
            append(
                "tool_result",
                role,
                checkpoint["available_at"],
                call_id=call_id,
                ok=True,
                result=result,
            )
        if checkpoint["sequence"] < trace_terminal_sequence:
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
                    }
                },
                observation_token=next_observation_token,
            )
    append(
        "run_end",
        "system",
        world["checkpoints"][terminal_sequence]["available_at"],
        status="completed",
    )
    return trace


def _checkpoint_records(
    world: dict[str, Any], artifacts: list[dict[str, Any]], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    terminal_sequence = _terminal_sequence(world)
    for index, checkpoint in enumerate(world["checkpoints"]):
        available_at = checkpoint["available_at"]
        post_disposition = index > terminal_sequence
        visible_gate = (
            "post-disposition closeout"
            if post_disposition
            else checkpoint["visible_gate"]
        )
        label = "post_disposition_closeout" if post_disposition else checkpoint["label"]
        business_objective = (
            "Preserve the terminal disposition, stop buyer outreach, and close remaining internal records."
            if post_disposition
            else checkpoint["business_objective"]
        )
        if post_disposition:
            role_deliverables = {
                "account_executive": "Do not contact the buyer. Preserve the final decision and internal handoff.",
                "domain_specialist": "Stop new pursuit work and preserve the completed evidence record.",
                "sales_manager": "Confirm the disposition remains final and no recovery is authorized.",
                "revops": "Preserve CRM history and the dated terminal disposition without reopening the opportunity.",
            }
        else:
            role_deliverables = dict(checkpoint["role_deliverables"])
            if index == world["intervention_sequence"]:
                business_objective += " Resolve the material change before the buyer's next binding decision or document why the deal cannot proceed."
                role_deliverables["account_executive"] += (
                    " Obtain a dated decision from the accountable buyer."
                )
                role_deliverables[world["causal_owner_role"]] += (
                    " Define a practical response from the current buyer facts."
                )
                role_deliverables["revops"] += (
                    " Keep the next step aligned with the buyer decision."
                )
        completion_conditions = (
            [
                "No buyer outreach or new commercial commitment is made.",
                "The dated terminal disposition and record history remain unchanged.",
            ]
            if post_disposition
            else [
                *checkpoint["completion_conditions"],
                *(
                    ["The material change has a buyer-owned disposition."]
                    if index == world["intervention_sequence"]
                    else []
                ),
            ]
        )
        decision_condition = (
            "No recovery is authorized after the supported terminal disposition."
            if post_disposition
            else checkpoint["decision_condition"]
        )
        records.append(
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "world_id": world["world_id"],
                "sequence": index,
                "available_at": available_at,
                "forecast_cutoff_at": available_at,
                "window_start": available_at,
                "window_end": world["checkpoints"][index + 1]["available_at"]
                if index + 1 < len(world["checkpoints"])
                else world["end_at"],
                "status": "active" if index == 0 else "pending",
                "synthetic": True,
                "objective_ids": [_opaque_id("objective", world["world_id"], index)],
                "visible_artifact_ids": [
                    artifact["artifact_id"]
                    for artifact in artifacts
                    if artifact["available_at"] <= available_at
                ],
                "released_event_ids": [
                    event["event_id"]
                    for event in events
                    if (
                        event["available_at"] <= available_at
                        and (
                            index == 0
                            or event["available_at"]
                            > world["checkpoints"][index - 1]["available_at"]
                        )
                    )
                ],
                "required_roles": list(ROLES),
                "terminal": index == len(world["checkpoints"]) - 1,
                "visible_gate": visible_gate,
                "label": label,
                "business_objective": business_objective,
                "decision_condition": decision_condition,
                "role_deliverables": role_deliverables,
                "completion_conditions": completion_conditions,
                "policy_entrypoints": checkpoint["policy_entrypoints"],
                "gate_id": checkpoint["gate_id"],
                "source_fact_ids": list(checkpoint["source_fact_ids"]),
                "required_artifact_keys": list(checkpoint["required_artifact_keys"]),
                "required_artifact_roles": checkpoint["required_artifact_roles"],
                "authority_role_ids": list(checkpoint["authority_role_ids"]),
                "authority_rights": list(checkpoint["authority_rights"]),
                "required_payload_fields": list(checkpoint["required_payload_fields"]),
                "decision_route": checkpoint["decision_route"],
                "recovery_decisions": list(checkpoint["recovery_decisions"]),
                "availability_delay_bounds": checkpoint["availability_delay_bounds"],
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
        "release_visibility": world["release_visibility"],
        "split": world["split"],
        "vertical": world["vertical"],
        "seller_org_id": world["seller_org_id"],
        "buyer_org_id": world["buyer_org_id"],
        "title": world["deal_name"],
        "description": f"Synthetic {world['vertical'].replace('_', ' ')} opportunity from first meeting through a final decision window.",
        "jurisdiction": world.get("jurisdiction"),
        "start_at": world["start_at"],
        "end_at": world["end_at"],
        "duration_days": world["duration_days"],
        "checkpoint_ids": world["checkpoint_ids"],
        "actor_ids": [actor["actor_id"] for actor in world["actors"]],
        "event_ids": [event["event_id"] for event in events],
        "artifact_ids": [artifact["artifact_id"] for artifact in artifacts],
        "required_channels": list(REQUIRED_CHANNELS),
        "synthetic": True,
        "license": {"code": "MIT", "data": "CC-BY-4.0"},
        "provenance": {
            "synthetic_only": True,
            "generator": "edlb.generate",
            "generator_version": DATASET_VERSION,
            "created_at": "2026-08-17T00:00:00Z",
            "source_policy_ids": _source_policy_ids(world),
            "source_registry": "authoring/source_registry.json",
            "source_registry_checksum": _source_registry_checksum(),
            "source_ids": list(_vertical_facts(world["vertical"])["source_ids"]),
            "fact_ids": list(_vertical_facts(world["vertical"])["fact_ids"]),
        },
    }
    if world["vertical"] == "consulting":
        manifest["provenance"]["attribution_resource"] = "authoring/attributions.json"
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
    is_private = world["release_visibility"] == "private"
    include_oracle = True
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
    manifest = _manifest(world, visible_events, artifacts, False)
    _write_json(base / "manifest.json", manifest)
    _write_jsonl(base / "actors.jsonl", world["actors"])
    _write_jsonl(base / "checkpoints.jsonl", checkpoints)
    _write_jsonl(base / "events.jsonl", visible_events)
    _write_jsonl(base / "artifacts.jsonl", artifacts)
    with (base / "artifacts.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            lineterminator="\n",
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
        oracle = {
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
            "verification_facts": _verification_facts(world, artifacts),
            "hidden_events": hidden_events,
        }
        _write_json(base / "oracle.json", oracle)
        scenario_hash = stable_hash(
            {
                "manifest": manifest,
                "events": visible_events,
                "artifacts": artifacts,
                "actors": world["actors"],
                "checkpoints": checkpoints,
                "hidden_events": hidden_events,
                "oracle": oracle,
            }
        )
        _write_jsonl(
            base / "reference_trace.jsonl",
            _build_reference_trace(world, artifacts, scenario_hash),
        )
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
                        "source_type": "independently_authored",
                        "source_ids": [],
                        "fact_ids": [],
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
    _write_json(authoring / "source_registry.json", _source_registry())
    _write_json(authoring / "attributions.json", _attributions())
    _write_json(
        authoring / "source_evidence_manifest.json", _source_evidence_manifest()
    )
    (authoring / "schema_projection_gaps.json").unlink(missing_ok=True)
    (authoring / "validation.json").unlink(missing_ok=True)
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
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
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
            {
                "world_id": world["world_id"],
                "pair_id": world["pair_id"],
                "vertical": world["vertical"],
                "seller_org_id": world["seller_org_id"],
                "buyer_org_id": world["buyer_org_id"],
                "release_visibility": world["release_visibility"],
                "split": world["split"],
                "seed": world["seed"],
                "duration_days": world["duration_days"],
                "checkpoint_count": world["checkpoint_count"],
                "artifact_counts": world["artifact_counts"],
                "intervention_checkpoint_id": world["intervention_checkpoint_id"],
                "intervention_sequence": world["intervention_sequence"],
                "intervention_gate": world["intervention_gate"],
                "resolution_checkpoint_id": world["resolution_checkpoint_id"],
                "resolution_sequence": world["resolution_sequence"],
                "resolution_gate": world["resolution_gate"],
                "causal_action_code": world["causal_action_code"],
                "observable_cure": world["observable_cure"],
                "causal_owner_role": world["causal_owner_role"],
                "causal_authority_role_ids": world["causal_authority_role_ids"],
                "causal_family": world["causal_family"],
                "variant": world["variant"],
                "reference_outcome": world["reference_outcome"],
                "defects": world["defects"],
                "actors": world["actors"],
                "checkpoints": world["checkpoints"],
            }
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
                "All train, dev, and blind blueprints, truth, assertions, reference traces, and hidden events are public in this v1 release. Future unreleased packs use release_visibility=private and remain outside output/public.",
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
    actors_by_id = {actor["actor_id"]: actor for actor in world["actors"]}
    for index, artifact in enumerate(artifacts):
        if artifact.get("branch_option"):
            actor = actors_by_id[artifact["authority_decision_actor_id"]]
            label = f"{actor['kind']}-{actor['authority']['role_id']}"
            artifact_label = f"branch-{artifact['branch_option']}-{label}"
            replacements[artifact["artifact_key"]] = f"artifact-key-{artifact_label}"
        else:
            artifact_label = f"{index:03d}"
        replacements[artifact["artifact_id"]] = f"artifact-{artifact_label}"
        if artifact.get("logical_document_id"):
            replacements[artifact["logical_document_id"]] = (
                f"logical-document-{artifact_label}"
            )
        for value in re.findall(
            r"(?:purchase-order|policy|thread|artifact|logical-document|decision-group)-[0-9a-f]{20}",
            json.dumps(artifact, ensure_ascii=False, sort_keys=True),
        ):
            prefix = value.split("-", 1)[0]
            replacements.setdefault(value, f"{prefix}-local")
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


def _artifact_checkpoint(
    world: dict[str, Any], artifact: dict[str, Any]
) -> dict[str, Any]:
    return next(
        checkpoint
        for checkpoint in world["checkpoints"]
        if checkpoint["gate_id"] == artifact["gate_id"]
    )


def _artifact_timing(
    world: dict[str, Any], artifacts: Sequence[dict[str, Any]]
) -> tuple[list[str], set[tuple[tuple[int, int], ...]]]:
    errors: list[str] = []
    profiles: set[tuple[tuple[int, int], ...]] = set()
    by_id = {artifact["artifact_id"]: artifact for artifact in artifacts}
    for checkpoint in world["checkpoints"]:
        checkpoint_time = datetime.fromisoformat(checkpoint["available_at"])
        gate_artifacts = [
            artifact
            for artifact in artifacts
            if artifact["gate_id"] == checkpoint["gate_id"]
        ]
        for artifact in gate_artifacts:
            if artifact["available_at"] > checkpoint["available_at"]:
                errors.append(
                    f"artifact_after_checkpoint={world['world_id']}:{artifact['artifact_id']}"
                )
            parent_ids = artifact.get("derived_from_artifact_ids", ())
            for parent_id in parent_ids:
                parent = by_id.get(parent_id)
                if parent is None:
                    errors.append(
                        f"artifact_lineage_parent={world['world_id']}:{artifact['artifact_id']}"
                    )
                elif parent["available_at"] > artifact["created_at"]:
                    errors.append(
                        f"artifact_lineage_time={world['world_id']}:{artifact['artifact_id']}"
                    )
            origin = artifact.get("projection_origin")
            if artifact["provenance"][
                "source_type"
            ] == "derived_projection" and not isinstance(origin, dict):
                errors.append(
                    f"artifact_projection_origin={world['world_id']}:{artifact['artifact_id']}"
                )
            if isinstance(origin, dict):
                source_id = origin.get("source_artifact_id")
                source = by_id.get(source_id)
                if source is None or source_id not in parent_ids:
                    errors.append(
                        f"artifact_projection_source={world['world_id']}:{artifact['artifact_id']}"
                    )
                elif origin.get("source_time") != source["available_at"]:
                    errors.append(
                        f"artifact_projection_time={world['world_id']}:{artifact['artifact_id']}"
                    )
        structured = [
            artifact
            for artifact in gate_artifacts
            if artifact["content"]["source_uri"].startswith("artifacts/structured/")
        ]
        profiles.add(
            tuple(
                sorted(
                    (
                        int(
                            (
                                checkpoint_time
                                - datetime.fromisoformat(artifact["available_at"])
                            ).total_seconds()
                            // 60
                        ),
                        int(
                            (
                                datetime.fromisoformat(artifact["available_at"])
                                - datetime.fromisoformat(artifact["created_at"])
                            ).total_seconds()
                            // 60
                        ),
                    )
                    for artifact in structured
                )
            )
        )
    return errors, profiles


def _pair_base_facts(world: dict[str, Any]) -> dict[str, Any]:
    return {
        "vertical": world["vertical"],
        "seller_id": world["seller_id"],
        "seller_org_id": world["seller_org_id"],
        "seller_name": world["seller_name"],
        "buyer_industry": world["buyer_industry"],
        "jurisdiction": world.get("jurisdiction"),
        "motion": world["motion"],
        "delivery_method": world.get("delivery_method"),
        "project_sector": world.get("project_sector"),
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
        "intervention_gate": world["intervention_gate"],
        "resolution_sequence": world["resolution_sequence"],
        "resolution_gate": world["resolution_gate"],
        "causal_action_code": world["causal_action_code"],
        "observable_cure": world["observable_cure"],
        "causal_owner_role": world["causal_owner_role"],
        "causal_authority_role_ids": world["causal_authority_role_ids"],
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
                    "visible_gate",
                    "business_objective",
                    "decision_condition",
                    "role_deliverables",
                    "completion_conditions",
                    "policy_entrypoints",
                    "source_fact_ids",
                    "required_artifact_keys",
                    "required_artifact_roles",
                    "authority_role_ids",
                    "authority_rights",
                    "required_payload_fields",
                    "decision_route",
                    "recovery_decisions",
                    "availability_delay_bounds",
                )
            }
            for checkpoint in world["checkpoints"]
        ],
        "actor_signatures": [
            {
                key: actor.get(key)
                for key in (
                    "display_name",
                    "email",
                    "organization_id",
                    "kind",
                    "role_tags",
                    "authority",
                    "active_from",
                    "active_until",
                    "visible_roles",
                    "synthetic",
                )
            }
            for actor in world["actors"]
        ],
        "defects": [
            {key: value for key, value in defect.items() if key != "defect_id"}
            for defect in world["defects"]
        ],
        "gates": world["gates"],
        "artifact_counts": world["artifact_counts"],
    }


def _event_reaches(
    events: list[dict[str, Any]], event_id: str, ancestor_id: str
) -> bool:
    parents = {event["event_id"]: tuple(event["causal_parent_ids"]) for event in events}
    pending = list(parents.get(event_id, ()))
    visited: set[str] = set()
    while pending:
        parent_id = pending.pop()
        if parent_id == ancestor_id:
            return True
        if parent_id not in visited:
            visited.add(parent_id)
            pending.extend(parents.get(parent_id, ()))
    return False


def _action_contract_shape(
    world: dict[str, Any], artifacts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    facts = _verification_facts(world, artifacts)
    actors = {actor["actor_id"]: actor for actor in world["actors"]}
    artifact_labels = dict(_alpha_replacements(world, artifacts))
    checkpoints = {
        checkpoint["checkpoint_id"]: checkpoint for checkpoint in world["checkpoints"]
    }
    rule_shapes: dict[str, tuple[Any, ...]] = {}
    for rule in facts["action_effect_rules"]:
        actor = actors.get(rule["authority_actor_id"])
        scope = None
        if actor is not None:
            scope = (
                "seller"
                if actor["organization_id"] == world["seller_org_id"]
                else "buyer"
                if actor["organization_id"] == world["buyer_org_id"]
                else "third_party"
            )
        rule_shapes[rule["effect_id"]] = (
            rule["fact_type"],
            rule["role"],
            tuple(rule["tool_names"]),
            tuple(
                sorted(
                    artifact_labels[artifact_id]
                    for artifact_id in rule["required_evidence_ids"]
                )
            ),
            scope,
            tuple(rule["authority_rights"]),
            rule["purpose_code"],
            rule["decision_code"],
            rule["commitment_code"],
            rule["resolution"],
            rule["document_kind"],
            rule["next_step_type"],
            (
                rule["remediation_requirements"]["action_code"]
                if rule["remediation_requirements"] is not None
                else None
            ),
            (
                rule["remediation_requirements"]["owner_role"]
                if rule["remediation_requirements"] is not None
                else None
            ),
            (
                tuple(sorted(rule["remediation_requirements"]["cure_data"]))
                if rule["remediation_requirements"] is not None
                else ()
            ),
        )
    result: list[dict[str, Any]] = []
    for branch in facts["branches"]:
        result.append(
            {
                "action_sequence": checkpoints[branch["action_checkpoint_id"]][
                    "sequence"
                ],
                "resolution_sequence": checkpoints[branch["resolution_checkpoint_id"]][
                    "sequence"
                ],
                "success_candidate_count": len(branch["success_decision_artifact_ids"]),
                "fallback_candidate_count": len(
                    branch["fallback_decision_artifact_ids"]
                ),
                "success_options": sorted(
                    tuple(sorted(rule_shapes[effect_id] for effect_id in option))
                    for option in branch["success_if_any"]
                ),
            }
        )
    return result


def _event_contract_valid(
    world: dict[str, Any], visible: list[dict[str, Any]], hidden: list[dict[str, Any]]
) -> bool:
    observable_id = _opaque_id("event", world["world_id"], "observable-intervention")
    family_id = _opaque_id("event", world["world_id"], "causal-intervention")
    terminal_id = _opaque_id("event", world["world_id"], "terminal-outcome")
    visible_by_id = {event["event_id"]: event for event in visible}
    hidden_by_id = {event["event_id"]: event for event in hidden}
    observable = visible_by_id.get(observable_id)
    family = hidden_by_id.get(family_id)
    terminal = hidden_by_id.get(terminal_id)
    if observable is None or family is None or terminal is None:
        return False
    buyer = {
        _actor_role(actor): actor
        for actor in world["actors"]
        if actor["kind"] == "buyer"
    }
    external_profile = VERTICAL_BLUEPRINTS["verticals"][world["vertical"]][
        "external_observation"
    ]
    actors_by_role = {
        actor["authority"]["role_id"]: actor
        for actor in world["actors"]
        if actor["authority"].get("role_id")
    }
    role_by_family = {
        "champion_exit": "champion",
        "late_stakeholder": "executive_sponsor",
        "budget_shock": "finance",
        "requirements_change": "technical_evaluator",
        "competition": "procurement",
        "external_event": None,
    }
    variant_values: dict[str, dict[str, dict[str, Any]]] = {
        "champion_exit": {
            "strong_handoff": {
                "change": "departed",
                "handoff_actor_ids": [buyer["economic_buyer"]["actor_id"]],
                "source": "buyer_automatic_reply",
            },
            "weak_handoff": {
                "change": "departed",
                "handoff_actor_ids": [],
                "source": "buyer_automatic_reply",
            },
        },
        "late_stakeholder": {
            "supportive": {
                "change": "joined_decision_group",
                "active_from": world["late_activation_at"],
                "stated_position": "requested_approval_path",
                "source": "meeting_record",
            },
            "blocking": {
                "change": "joined_decision_group",
                "active_from": world["late_activation_at"],
                "stated_position": "questioned_current_priority",
                "source": "meeting_record",
            },
        },
        "budget_shock": {
            "reallocation": {
                "budget_status": "reduced_allocation_available",
                "review_window": "current_cycle",
                "source": "finance_reply",
            },
            "freeze": {
                "budget_status": "spending_hold",
                "review_window": "next_planning_cycle",
                "source": "finance_reply",
            },
        },
        "requirements_change": {
            "within_fit": {
                **_vertical_causal_facts(
                    world["vertical"], "requirements_change", "within_fit"
                ),
                "source": "validation_record",
            },
            "out_of_fit": {
                **_vertical_causal_facts(
                    world["vertical"], "requirements_change", "out_of_fit"
                ),
                "source": "validation_record",
            },
        },
        "competition": {
            "transparent": {
                **_vertical_causal_facts(
                    world["vertical"], "competition", "transparent"
                ),
                "disclosure_channel": "buyer_meeting",
                "source": "buyer_record",
            },
            "hidden_influence": {
                "evaluation_status": "ranking_changed",
                "criteria_change_status": "no_disclosed_change",
                "disclosure_channel": "buyer_scorecard",
                "source": "buyer_evaluation_record",
            },
        },
        "external_event": {
            "recoverable": {
                **_vertical_causal_facts(
                    world["vertical"],
                    "external_event",
                    "recoverable",
                    include_source=True,
                ),
            },
            "terminal": {
                **_vertical_causal_facts(
                    world["vertical"],
                    "external_event",
                    "terminal",
                    include_source=True,
                ),
            },
        },
    }
    actor_id = (
        actors_by_role[external_profile["actor_role_id"]]["actor_id"]
        if world["causal_family"] == "external_event"
        else buyer[cast(str, role_by_family[str(world["causal_family"])])]["actor_id"]
    )
    expected_observable = {
        "stakeholder_actor_id": actor_id,
        **variant_values[world["causal_family"]][world["variant"]],
        "checkpoint_id": world["intervention_checkpoint_id"],
    }
    return bool(
        observable["payload"] == expected_observable
        and observable["actor_ids"] == [actor_id]
        and observable["channel"]
        == (
            external_profile["channel"]
            if world["causal_family"] == "external_event"
            else "internal_chat"
            if world["causal_family"] == "competition"
            else "email"
        )
        and observable["visibility"]
        == (
            external_profile["visibility"]
            if world["causal_family"] == "external_event"
            else "agent_visible"
        )
        and family["payload"]
        == {
            "family": world["causal_family"],
            "variant": world["variant"],
            "description": world["family_description"],
            "checkpoint_id": world["intervention_checkpoint_id"],
            "trigger_event_id": observable_id,
        }
        and family["causal_parent_ids"] == [observable_id]
        and terminal["payload"]
        == {
            "outcome": world["reference_outcome"],
            "reason": world["outcome_reason"],
        }
        and terminal["causal_parent_ids"] == [family_id]
    )


def _verification_contract(
    world: dict[str, Any], artifacts: list[dict[str, Any]]
) -> tuple[dict[str, Any], bool, bool]:
    facts = _verification_facts(world, artifacts)
    milestones = sorted(
        facts["milestones"], key=lambda item: int(item["chronology"]["sequence"])
    )
    branch = facts["branches"][0]
    milestone_by_id = {item["milestone_id"]: item for item in milestones}
    remedy = milestone_by_id.get(branch["remedy_milestone_id"])
    artifact_by_id = {artifact["artifact_id"]: artifact for artifact in artifacts}
    fallback_ids = branch["fallback_decision_artifact_ids"]
    fallback_state = artifact_by_id[fallback_ids[0]]["structured_payload"][
        "decision_state"
    ]
    fallback_outcome = _fallback_outcome(world)
    terminal_mappings = [
        (
            int(item["chronology"]["sequence"]),
            dict(item["terminal_outcome_by_resolution"]),
        )
        for item in milestones
        if item["terminal_outcome_by_resolution"]
    ]
    branch_mapping = {fallback_state: fallback_outcome}
    if world["variant_index"] == 0 and _terminal_sequence(world) == int(
        world["resolution_sequence"]
    ):
        branch_mapping["remedied"] = TERMINAL_OUTCOMES[world["reference_outcome"]]
    expected_mappings = [(int(world["resolution_sequence"]), branch_mapping)]
    if world["variant_index"] == 0 and _terminal_sequence(world) != int(
        world["resolution_sequence"]
    ):
        expected_mappings.append(
            (
                _terminal_sequence(world),
                {
                    _decision_state(
                        world, world["checkpoints"][_terminal_sequence(world)]
                    ): TERMINAL_OUTCOMES[world["reference_outcome"]]
                },
            )
        )
    terminal_valid = sorted(terminal_mappings) == sorted(expected_mappings)
    remedy_valid = bool(
        remedy is not None
        and remedy["branch_id"] == branch["branch_id"]
        and remedy["remedy_of"]
        == milestones[world["intervention_sequence"]]["milestone_id"]
        and set(branch["success_decision_artifact_ids"])
        | set(branch["fallback_decision_artifact_ids"])
        == set(remedy["decision_artifact_ids"])
    )
    authority_valid = True
    for milestone in milestones:
        for requirement in milestone["authority_requirements"]:
            actor_id = requirement["actor_id"]
            for artifact_id in requirement["decision_artifact_ids"]:
                artifact = artifact_by_id[artifact_id]
                payload = artifact["structured_payload"]
                authority_valid = authority_valid and bool(
                    artifact["source_actor_ids"] == [actor_id]
                    and payload["author_actor_id"] == actor_id
                    and payload["authority_decisions"]
                    == [
                        {
                            "actor_id": actor_id,
                            "resolution": payload["decision_state"],
                            "rights": requirement["rights"],
                            "effective_at": payload["authority_decisions"][0][
                                "effective_at"
                            ],
                        }
                    ]
                )
    return facts, terminal_valid and remedy_valid and authority_valid, authority_valid


def _milestone_contract_shape(
    world: dict[str, Any], artifacts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    facts = _verification_facts(world, artifacts)
    actors = {actor["actor_id"]: actor for actor in world["actors"]}
    artifact_by_id = {artifact["artifact_id"]: artifact for artifact in artifacts}
    artifact_labels = dict(_alpha_replacements(world, artifacts))
    result = []
    for milestone in sorted(
        facts["milestones"], key=lambda item: int(item["chronology"]["sequence"])
    ):
        authorities = []
        for requirement in milestone["authority_requirements"]:
            actor = actors[requirement["actor_id"]]
            authorities.append(
                (
                    "seller"
                    if actor["organization_id"] == world["seller_org_id"]
                    else "buyer"
                    if actor["organization_id"] == world["buyer_org_id"]
                    else "third_party",
                    tuple(requirement["rights"]),
                    tuple(
                        sorted(
                            str(artifact_by_id[artifact_id].get("branch_option"))
                            for artifact_id in requirement["decision_artifact_ids"]
                        )
                    ),
                )
            )
        result.append(
            {
                "sequence": int(milestone["chronology"]["sequence"]),
                "gate_id": milestone["gate_id"],
                "authority_requirements": sorted(authorities),
                "evidence_roles": {
                    role: sorted(artifact_labels[artifact_id] for artifact_id in ids)
                    for role, ids in milestone["evidence_requirements_by_role"].items()
                },
                "allowed_resolutions": sorted(milestone["allowed_resolutions"]),
                "business_effect_resolutions": sorted(
                    milestone["business_effect_requirements_by_resolution"]
                ),
                "has_approval": milestone["approval_requirement"] is not None,
                "is_remedy": milestone["remedy_of"] is not None,
                "has_branch": milestone["branch_id"] is not None,
            }
        )
    return result


def _reference_trace_contract(
    world: dict[str, Any], artifacts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    trace = _build_reference_trace(world, artifacts, "sha256:pair-contract")
    facts = _verification_facts(world, artifacts)
    branch = facts["branches"][0]
    rules = [
        rule
        for rule in facts["action_effect_rules"]
        if rule["fact_type"] == "authority_decision_observed"
    ]
    action_checkpoint = next(
        checkpoint
        for checkpoint in world["checkpoints"]
        if checkpoint["checkpoint_id"] == branch["action_checkpoint_id"]
    )
    action_calls = [
        row
        for row in trace
        if row["kind"] == "tool_call"
        and row["occurred_at"] == action_checkpoint["available_at"]
    ]
    plans = [
        row
        for row in action_calls
        if row["tool_name"] == "documents.create"
        and row["arguments"].get("kind") == "remediation_plan"
    ]
    if len(plans) != 1:
        return trace, False
    plan = plans[0]
    plan_id = _side_effect_id("document", world["world_id"], plan["idempotency_key"])
    expected_remediation = {
        "cure_data": rules[0]["remediation_requirements"]["cure_data"],
        "gate_id": action_checkpoint["gate_id"],
        "owner_role": rules[0]["remediation_requirements"]["owner_role"],
    }
    sends = [
        row
        for row in action_calls
        if row["tool_name"] == "communications.send"
        and row["arguments"].get("semantic_envelope", {}).get("purpose_code")
        == "recover_gate"
    ]
    send_targets = {
        row["arguments"]["semantic_envelope"]["target_actor_id"]: row for row in sends
    }
    valid = plan["arguments"].get("remediation") == expected_remediation
    valid = valid and set(send_targets) == {
        rule["authority_actor_id"] for rule in rules
    }
    for rule in rules:
        row = send_targets.get(rule["authority_actor_id"])
        envelope = row["arguments"].get("semantic_envelope", {}) if row else {}
        expected_claims = [
            {
                "artifact_id": artifact_id,
                "claim_type": "supports_gate_basis",
                "gate_id": rule["gate_id"],
                "resolution": rule["resolution"],
            }
            for artifact_id in rule["required_evidence_ids"]
        ]
        valid = valid and bool(
            row is not None
            and set(envelope.get("attachments", ()))
            == set(rule["required_evidence_ids"]) | {plan_id}
            and envelope.get("target_actor_id") == rule["authority_actor_id"]
            and envelope.get("purpose_code") == rule["purpose_code"]
            and envelope.get("gate_id") == rule["gate_id"]
            and envelope.get("decision_codes") == [rule["decision_code"]]
            and envelope.get("commitment_codes") == [rule["commitment_code"]]
            and envelope.get("resolution") == rule["resolution"]
            and envelope.get("related_records") == [world["deal_id"]]
            and envelope.get("evidence_claims") == expected_claims
        )
    terminal_at = world["checkpoints"][_terminal_sequence(world)]["available_at"]
    valid = valid and trace[-1]["kind"] == "run_end"
    valid = valid and trace[-1]["occurred_at"] == terminal_at
    valid = valid and not any(
        row["kind"] == "tool_call" and row["occurred_at"] > terminal_at for row in trace
    )
    return trace, valid


def pair_diff(world_a: dict[str, Any], world_b: dict[str, Any]) -> dict[str, Any]:
    artifacts_a = _build_artifacts(world_a)
    artifacts_b = _build_artifacts(world_b)
    visible_a, hidden_a = _build_events(world_a, artifacts_a)
    visible_b, hidden_b = _build_events(world_b, artifacts_b)
    events_a = [*visible_a, *hidden_a]
    events_b = [*visible_b, *hidden_b]
    event_by_artifact_a = {
        event["artifact_ids"][0]: event
        for event in visible_a
        if len(event["artifact_ids"]) == 1
    }
    event_by_artifact_b = {
        event["artifact_ids"][0]: event
        for event in visible_b
        if len(event["artifact_ids"]) == 1
    }
    intervention_event_a = _opaque_id(
        "event", world_a["world_id"], "observable-intervention"
    )
    intervention_event_b = _opaque_id(
        "event", world_b["world_id"], "observable-intervention"
    )
    replacements_a = _alpha_replacements(world_a, artifacts_a)
    replacements_b = _alpha_replacements(world_b, artifacts_b)

    def event_replacements(
        world: dict[str, Any],
        events: list[dict[str, Any]],
        replacements: list[tuple[str, str]],
    ) -> tuple[list[tuple[str, str]], dict[str, dict[str, Any]]]:
        artifact_labels = dict(replacements)
        fixed = {
            _opaque_id(
                "event", world["world_id"], "meeting-booked"
            ): "event-meeting-booked",
            _opaque_id(
                "event", world["world_id"], "observable-intervention"
            ): "event-observable-intervention",
            _opaque_id(
                "event", world["world_id"], "cycle-horizon"
            ): "event-cycle-horizon",
            _opaque_id(
                "event", world["world_id"], "causal-intervention"
            ): "event-causal-intervention",
            _opaque_id(
                "event", world["world_id"], "terminal-outcome"
            ): "event-terminal-outcome",
        }
        labels: dict[str, dict[str, Any]] = {}
        values: list[tuple[str, str]] = []
        for event in events:
            artifact_ids = event.get("artifact_ids", ())
            if len(artifact_ids) == 1:
                label = f"event-{event['kind']}-{artifact_labels[str(artifact_ids[0])]}"
            elif event["event_id"] in fixed:
                label = fixed[event["event_id"]]
            elif (
                event.get("visibility") == "oracle_only"
                and event.get("kind") == "crm_projection_changed"
            ):
                label = f"event-defect-{event['sequence']}"
            else:
                payload = json.dumps(
                    {
                        "kind": event["kind"],
                        "available_at": event["available_at"],
                        "channel": event["channel"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                label = "event-" + hashlib.sha256(payload).hexdigest()[:20]
            if label in labels:
                raise ValueError("pair events do not have unique semantic identities")
            labels[label] = event
            values.append((event["event_id"], label))
        return values, labels

    event_replacements_a, event_map_a = event_replacements(
        world_a, events_a, replacements_a
    )
    event_replacements_b, event_map_b = event_replacements(
        world_b, events_b, replacements_b
    )
    replacements_a.extend(event_replacements_a)
    replacements_b.extend(event_replacements_b)
    replacements_a.sort(key=lambda item: len(item[0]), reverse=True)
    replacements_b.sort(key=lambda item: len(item[0]), reverse=True)
    pre_equal = True
    descendants_only = True
    post_context_equal = True
    post_differences = 0
    post_total = 0
    branch_artifact_ids_a = {
        record["artifact_id"] for record in artifacts_a if record.get("branch_option")
    }
    branch_artifact_ids_b = {
        record["artifact_id"] for record in artifacts_b if record.get("branch_option")
    }

    def artifact_context(
        record: dict[str, Any],
        replacements: list[tuple[str, str]],
        branch_artifact_ids: set[str],
    ) -> dict[str, Any]:
        source_uri = str(record["content"]["source_uri"])
        value = {
            key: record.get(key)
            for key in (
                "kind",
                "created_at",
                "available_at",
                "visibility",
                "visible_roles",
                "source_actor_ids",
                "recipient_actor_ids",
                "thread_id",
                "version",
                "provenance",
                "gate_id",
                "recipient_role_ids",
                "projection_origin",
            )
        }
        value["content"] = {
            "mime_type": record["content"]["mime_type"],
            "language": record["content"]["language"],
            "source_directory": source_uri.rsplit("/", 1)[0],
        }
        value["channel"] = record["structured_payload"].get("channel")
        if (
            isinstance(value["projection_origin"], dict)
            and value["projection_origin"].get("source_artifact_id")
            in branch_artifact_ids
        ):
            value["projection_origin"] = {
                **value["projection_origin"],
                "source_artifact_id": "branch-decision-artifact",
            }
        return _alpha_normalize(value, replacements)

    def invariant_payload(
        world: dict[str, Any],
        record: dict[str, Any],
        replacements: list[tuple[str, str]],
        branch_artifact_ids: set[str],
    ) -> dict[str, Any]:
        payload = json.loads(json.dumps(record["structured_payload"]))
        checkpoint = _artifact_checkpoint(world, record)
        if checkpoint["sequence"] == world["intervention_sequence"]:
            for key in {
                *_causal_cure_data(world),
                "evaluation_status",
                "criteria_change_status",
            }:
                payload.pop(key, None)
        if checkpoint["sequence"] >= world["resolution_sequence"]:
            payload.pop("decision_state", None)
            for decision in payload.get("authority_decisions", ()):
                decision["resolution"] = "branch-resolution"
        if record.get("branch_option"):
            payload["artifact_key"] = "branch-decision-artifact-key"
        origin = payload.get("projection_origin")
        if (
            isinstance(origin, dict)
            and origin.get("source_artifact_id") in branch_artifact_ids
        ):
            origin["source_artifact_id"] = "branch-decision-artifact"
        return _alpha_normalize(payload, replacements)

    channel_by_kind = {
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
        "policy_document": "document",
        "web_page": "web_news",
        "news_item": "web_news",
    }

    def is_generic_causal(world: dict[str, Any], record: dict[str, Any]) -> bool:
        return (
            "/structured/" not in str(record["content"]["source_uri"])
            and record["gate_id"] == world["intervention_gate"]
            and channel_by_kind[record["kind"]] == _causal_artifact_channel(world)
        )

    for record_a, record_b in zip(artifacts_a, artifacts_b, strict=True):
        checkpoint = _artifact_checkpoint(world_a, record_a)
        normalized_a = _normalized_artifact(record_a, replacements_a)
        normalized_b = _normalized_artifact(record_b, replacements_b)
        if checkpoint["sequence"] < world_a["intervention_sequence"]:
            if normalized_a != normalized_b:
                pre_equal = False
        else:
            post_total += 1
            post_context_equal = post_context_equal and artifact_context(
                record_a, replacements_a, branch_artifact_ids_a
            ) == artifact_context(record_b, replacements_b, branch_artifact_ids_b)
            post_context_equal = post_context_equal and invariant_payload(
                world_a, record_a, replacements_a, branch_artifact_ids_a
            ) == invariant_payload(
                world_b, record_b, replacements_b, branch_artifact_ids_b
            )
            if "/structured/" not in str(
                record_a["content"]["source_uri"]
            ) and not is_generic_causal(world_a, record_a):
                post_context_equal = post_context_equal and _alpha_normalize(
                    record_a["content"]["body"], replacements_a
                ) == _alpha_normalize(record_b["content"]["body"], replacements_b)
            if normalized_a != normalized_b:
                post_differences += 1
                event_a = event_by_artifact_a[record_a["artifact_id"]]
                event_b = event_by_artifact_b[record_b["artifact_id"]]
                descendants_only = descendants_only and _event_reaches(
                    events_a, event_a["event_id"], intervention_event_a
                )
                descendants_only = descendants_only and _event_reaches(
                    events_b, event_b["event_id"], intervention_event_b
                )

    def canonical(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, list):
            items = [canonical(item) for item in value]
            return sorted(
                items,
                key=lambda item: json.dumps(
                    item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            )
        return value

    def normalized_event(
        event: dict[str, Any], replacements: list[tuple[str, str]]
    ) -> dict[str, Any]:
        normalized = _alpha_normalize(event, replacements)
        normalized.pop("sequence", None)
        return canonical(normalized)

    intervention_available_a = next(
        event["available_at"]
        for event in visible_a
        if event["event_id"] == intervention_event_a
    )
    intervention_available_b = next(
        event["available_at"]
        for event in visible_b
        if event["event_id"] == intervention_event_b
    )
    event_descendants_only = set(event_map_a) == set(event_map_b)
    post_event_context_equal = set(event_map_a) == set(event_map_b)
    for label in sorted(set(event_map_a) & set(event_map_b)):
        event_a = event_map_a[label]
        event_b = event_map_b[label]
        normalized_a = normalized_event(event_a, replacements_a)
        normalized_b = normalized_event(event_b, replacements_b)
        if event_a["available_at"] >= intervention_available_a:
            context_keys = (
                "kind",
                "effective_at",
                "recorded_at",
                "available_at",
                "visibility",
                "visible_roles",
                "actor_ids",
                "artifact_ids",
                "causal_parent_ids",
                "channel",
            )
            post_event_context_equal = post_event_context_equal and _alpha_normalize(
                {key: event_a.get(key) for key in context_keys}, replacements_a
            ) == _alpha_normalize(
                {key: event_b.get(key) for key in context_keys}, replacements_b
            )
            artifact_ids_a = set(event_a.get("artifact_ids", ()))
            artifact_ids_b = set(event_b.get("artifact_ids", ()))
            allowed_payload_difference = label in {
                "event-observable-intervention",
                "event-causal-intervention",
                "event-terminal-outcome",
            } or bool(
                artifact_ids_a & branch_artifact_ids_a
                and artifact_ids_b & branch_artifact_ids_b
            )
            if not allowed_payload_difference:
                post_event_context_equal = (
                    post_event_context_equal
                    and _alpha_normalize(event_a["payload"], replacements_a)
                    == _alpha_normalize(event_b["payload"], replacements_b)
                )
        if normalized_a == normalized_b:
            continue
        if label == "event-observable-intervention":
            continue
        event_descendants_only = event_descendants_only and _event_reaches(
            events_a, event_a["event_id"], intervention_event_a
        )
        event_descendants_only = event_descendants_only and _event_reaches(
            events_b, event_b["event_id"], intervention_event_b
        )
    descendants_only = descendants_only and event_descendants_only
    graph_valid = all(
        event["event_id"] not in event["causal_parent_ids"]
        and all(
            parent_id in {item["event_id"] for item in events}
            for parent_id in event["causal_parent_ids"]
        )
        for events in (events_a, events_b)
        for event in events
    )

    def normalized_pre_events(
        events: list[dict[str, Any]],
        replacements: list[tuple[str, str]],
        intervention_available: str,
    ) -> list[dict[str, Any]]:
        return sorted(
            [
                normalized_event(event, replacements)
                for event in events
                if event["available_at"] < intervention_available
            ],
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )

    _, verification_a, authority_a = _verification_contract(world_a, artifacts_a)
    _, verification_b, authority_b = _verification_contract(world_b, artifacts_b)
    events_valid_a = _event_contract_valid(world_a, visible_a, hidden_a)
    events_valid_b = _event_contract_valid(world_b, visible_b, hidden_b)
    milestone_shape_a = _milestone_contract_shape(world_a, artifacts_a)
    milestone_shape_b = _milestone_contract_shape(world_b, artifacts_b)
    pre_milestone_a = milestone_shape_a[: world_a["intervention_sequence"] + 1]
    pre_milestone_b = milestone_shape_b[: world_b["intervention_sequence"] + 1]
    remedy_shape_a = milestone_shape_a[world_a["resolution_sequence"]]
    remedy_shape_b = milestone_shape_b[world_b["resolution_sequence"]]
    trace_a, trace_valid_a = _reference_trace_contract(world_a, artifacts_a)
    trace_b, trace_valid_b = _reference_trace_contract(world_b, artifacts_b)

    def pre_trace_material(
        trace: list[dict[str, Any]],
        world: dict[str, Any],
        replacements: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        cutoff = world["checkpoints"][world["intervention_sequence"]]["available_at"]
        generated_targets = {
            "approvals.approve": {"approval_id"},
            "approvals.reject": {"approval_id"},
            "calendar.cancel": {"calendar_id"},
            "calendar.reschedule": {"calendar_id"},
            "documents.attach": {"document_id"},
            "documents.revise": {"document_id"},
        }

        def causal_arguments(row: dict[str, Any]) -> dict[str, Any] | None:
            arguments = row.get("arguments")
            if not isinstance(arguments, dict):
                return None
            ignored = {
                "body",
                "content",
                "description",
                "reason",
                "subject",
                "summary",
                "title",
            } | generated_targets.get(str(row.get("tool_name")), set())
            result = {
                key: value for key, value in arguments.items() if key not in ignored
            }
            if row.get("tool_name") == "approvals.request" and isinstance(
                result.get("details"), dict
            ):
                result["details"] = {
                    key: value
                    for key, value in result["details"].items()
                    if key != "document_id"
                }
                envelope = result.get("semantic_envelope")
                if isinstance(envelope, dict):
                    result["semantic_envelope"] = {
                        key: value
                        for key, value in envelope.items()
                        if key != "attachments"
                    }
            return result

        values = [
            canonical(
                _alpha_normalize(
                    {
                        "kind": row["kind"],
                        "role": row["role"],
                        "tool_name": row.get("tool_name"),
                        "arguments": causal_arguments(row),
                    },
                    replacements,
                )
            )
            for row in trace
            if row["kind"] == "tool_call" and row["occurred_at"] < cutoff
        ]
        return sorted(
            values,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )

    action_shape_a = _action_contract_shape(world_a, artifacts_a)
    action_shape_b = _action_contract_shape(world_b, artifacts_b)
    return {
        "pair_id": world_a["pair_id"],
        "world_ids": [world_a["world_id"], world_b["world_id"]],
        "base_facts_equal": _pair_base_facts(world_a) == _pair_base_facts(world_b),
        "pre_intervention_artifacts_equal": pre_equal,
        "post_intervention_artifact_differences": post_differences,
        "post_intervention_artifact_total": post_total,
        "post_intervention_changes_are_declared_descendants": descendants_only
        and events_valid_a
        and events_valid_b,
        "post_intervention_context_isomorphic": post_context_equal
        and post_event_context_equal,
        "causal_event_graph_valid": graph_valid,
        "pre_intervention_events_equal": normalized_pre_events(
            visible_a, replacements_a, intervention_available_a
        )
        == normalized_pre_events(visible_b, replacements_b, intervention_available_b),
        "pre_intervention_hidden_events_equal": normalized_pre_events(
            hidden_a, replacements_a, intervention_available_a
        )
        == normalized_pre_events(hidden_b, replacements_b, intervention_available_b),
        "terminal_mappings_isomorphic": verification_a and verification_b,
        "milestone_contracts_isomorphic": pre_milestone_a == pre_milestone_b,
        "branch_contracts_isomorphic": action_shape_a == action_shape_b,
        "selected_evidence_contracts_isomorphic": verification_a
        and verification_b
        and authority_a
        and authority_b
        and remedy_shape_a["authority_requirements"]
        == remedy_shape_b["authority_requirements"]
        and remedy_shape_a["evidence_roles"] == remedy_shape_b["evidence_roles"],
        "reference_trace_causal_material_isomorphic": trace_valid_a
        and trace_valid_b
        and pre_trace_material(trace_a, world_a, replacements_a)
        == pre_trace_material(trace_b, world_b, replacements_b),
        "action_contracts_isomorphic": action_shape_a == action_shape_b,
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
    prose_samples: list[tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]] = []
    structured_profiles: dict[str, set[tuple[tuple[int, int], ...]]] = {
        vertical["id"]: set() for vertical in VERTICALS
    }
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
        "policy_document": "document",
        "web_page": "web_news",
        "news_item": "web_news",
    }
    registry = _source_registry()
    errors.extend(_validate_source_contract(registry))
    errors.extend(_validate_source_evidence(registry))
    errors.extend(_validate_attributions(registry))
    registry_sources = {source["source_id"]: source for source in registry["sources"]}
    registry_fact_ids = {
        fact_id for source in registry["sources"] for fact_id in source["fact_ids"]
    }
    expected_world_count = 72 if include_blind else 48
    if len(worlds) != expected_world_count:
        errors.append(f"world_count={len(worlds)}")
    route_counts = {
        count: sum(world["checkpoint_count"] == count for world in worlds)
        for count in (6, 7, 8)
    }
    if include_blind and (
        min(route_counts.values()) < 12
        or sum(route_counts.values()) != len(worlds)
        or max(route_counts.values()) > 36
    ):
        errors.append(f"checkpoint_routes={route_counts}")
    checkpoint_clocks = Counter(
        checkpoint["available_at"][11:16]
        for world in worlds
        for checkpoint in world["checkpoints"]
    )
    if not checkpoint_clocks or max(checkpoint_clocks.values()) * 4 > sum(
        checkpoint_clocks.values()
    ):
        errors.append(f"checkpoint_clocks={dict(checkpoint_clocks)}")
    if len(shared_documents) != 180:
        errors.append(f"shared_document_count={len(shared_documents)}")
    actor_name_owners: dict[str, set[tuple[str, str]]] = {}
    for world in worlds:
        for actor in world["actors"]:
            actor_name_owners.setdefault(actor["display_name"], set()).add(
                (world["pair_id"], actor["actor_id"])
            )
    if any(
        len(owners) > 1 and len({pair_id for pair_id, _ in owners}) > 1
        for owners in actor_name_owners.values()
    ):
        errors.append("duplicate_actor_names_across_pairs")
    buyer_identities: dict[tuple[str, str], set[str]] = {}
    for world in worlds:
        buyer_identities.setdefault(
            (world["buyer_name"], world["buyer_domain"]), set()
        ).add(world["pair_id"])
    if len(buyer_identities) != 36 or any(
        len(pair_ids) != 1 for pair_ids in buyer_identities.values()
    ):
        errors.append("buyer_identity_allocation")
    count_signatures = {tuple(world["artifact_counts"].items()) for world in worlds}
    if len(count_signatures) < 12 or all(
        sum(world["artifact_counts"].values()) == 72 for world in worlds
    ):
        errors.append(f"artifact_count_signatures={len(count_signatures)}")
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
        if not 6 <= world["checkpoint_count"] <= 8:
            errors.append(f"bad_checkpoints={world['world_id']}")
        if not MANDATORY_GATES[world["vertical"]] <= set(world["gates"]):
            errors.append(f"mandatory_gates={world['world_id']}")
        for checkpoint in world["checkpoints"]:
            if date.fromisoformat(checkpoint["date"]).weekday() >= 5:
                errors.append(
                    f"weekend_checkpoint={world['world_id']}:{checkpoint['checkpoint_id']}"
                )
            if not all(
                checkpoint.get(field)
                for field in (
                    "visible_gate",
                    "business_objective",
                    "decision_condition",
                    "role_deliverables",
                    "completion_conditions",
                    "policy_entrypoints",
                )
            ):
                errors.append(
                    f"checkpoint_contract={world['world_id']}:{checkpoint['checkpoint_id']}"
                )
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
        prose_samples.append((world, artifacts))
        timing_errors, timing_profiles = _artifact_timing(world, artifacts)
        errors.extend(timing_errors)
        structured_profiles[world["vertical"]].update(timing_profiles)
        if len(artifacts) != sum(world["artifact_counts"].values()):
            errors.append(f"artifact_count={world['world_id']}")
        counts = {channel: 0 for channel in ARTIFACT_COUNTS}
        artifact_timestamps = {artifact["available_at"] for artifact in artifacts}
        if len(artifact_timestamps) < 12 or all(
            timestamp[11:16] == "09:00" for timestamp in artifact_timestamps
        ):
            errors.append(f"artifact_time_variation={world['world_id']}")
        facts = _vertical_facts(world["vertical"])
        corpus = "\n".join(artifact["content"]["body"] for artifact in artifacts)
        if not all(
            facts["evidence_by_gate"][gate] in corpus for gate in world["gates"]
        ):
            errors.append(f"vertical_terms={world['world_id']}")
        errors.extend(_crm_authority_errors(world, artifacts))
        stale_projections = [
            artifact
            for artifact in artifacts
            if (artifact.get("projection_origin") or {}).get("transformation")
            == "crm_projection_from_stale_state"
        ]
        stale_values = {
            payload["observed_field"]: payload
            for artifact in stale_projections
            if isinstance(payload := json.loads(artifact["content"]["body"]), dict)
        }
        if len(stale_projections) != 3 or set(stale_values) != {
            "stage",
            "close_date",
            "next_step",
        }:
            errors.append(f"stale_crm_projections={world['world_id']}")
        for defect in world["defects"]:
            if (
                stale_values.get(defect["field"], {}).get(defect["field"])
                != defect["observed_value"]
            ):
                errors.append(f"stale_crm_value={world['world_id']}:{defect['field']}")
        actors_by_id = {actor["actor_id"]: actor for actor in world["actors"]}
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
            source_ids = set(artifact["provenance"].get("source_ids", ()))
            fact_ids = set(artifact["provenance"].get("fact_ids", ()))
            if (
                not source_ids
                or not fact_ids
                or not source_ids <= set(facts["source_ids"])
                or not fact_ids <= set(facts["fact_ids"])
                or not source_ids <= set(registry_sources)
                or not fact_ids <= registry_fact_ids
            ):
                errors.append(
                    f"artifact_source_link={world['world_id']}:{artifact['artifact_id']}"
                )
            participants = set(artifact.get("source_actor_ids", ())) | set(
                artifact.get("recipient_actor_ids", ())
            )
            for actor_id in participants:
                actor = actors_by_id.get(actor_id)
                if actor is None or not _actor_active_during(
                    actor, artifact["created_at"], artifact["available_at"]
                ):
                    errors.append(
                        f"artifact_actor_chronology={world['world_id']}:{artifact['artifact_id']}:{actor_id}"
                    )
        if counts != world["artifact_counts"]:
            errors.append(f"artifact_channels={world['world_id']}:{counts}")
        visible_events, hidden_events = _build_events(world, artifacts)
        for event in visible_events + hidden_events:
            if event["event_id"] in {
                _opaque_id("event", world["world_id"], "observable-intervention"),
                _opaque_id("event", world["world_id"], "causal-intervention"),
            } and any(
                event[field][11:19] == "00:00:00"
                for field in ("effective_at", "recorded_at", "available_at")
            ):
                errors.append(f"midnight_event={world['world_id']}:{event['event_id']}")
            if not all(
                timestamp_pattern.fullmatch(event[field])
                for field in ("effective_at", "recorded_at", "available_at")
            ):
                errors.append(
                    f"event_timestamp={world['world_id']}:{event['event_id']}"
                )
            if not (
                event["effective_at"] <= event["recorded_at"] <= event["available_at"]
            ):
                errors.append(
                    f"event_chronology={world['world_id']}:{event['event_id']}"
                )
            for actor_id in event["actor_ids"]:
                actor = actors_by_id.get(actor_id)
                active = actor is not None and (
                    actor["active_from"] <= event["effective_at"]
                    and (
                        event["kind"] == "stakeholder_departed"
                        and actor.get("active_until") == event["effective_at"]
                        or event["kind"] != "stakeholder_departed"
                        and _actor_active_during(
                            actor,
                            event["effective_at"],
                            event["available_at"],
                        )
                    )
                )
                if not active:
                    errors.append(
                        f"event_actor_chronology={world['world_id']}:{event['event_id']}:{actor_id}"
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
            *(
                key
                for vertical in VERTICALS
                for variant in ("recoverable", "terminal")
                for key in _vertical_causal_facts(
                    vertical["id"], "external_event", variant
                )
            ),
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
            if checkpoint is None:
                errors.append(f"material_event_release={world['world_id']}")
        intervention_available = next(
            event["available_at"]
            for event in hidden_events
            if event["event_id"]
            == _opaque_id("event", world["world_id"], "causal-intervention")
        )
        if (
            material_events
            and material_events[0]["available_at"] != intervention_available
        ):
            errors.append(f"material_event_release={world['world_id']}")
        actors = {actor["actor_id"]: actor for actor in world["actors"]}
        causal_evidence_count = 0
        for artifact in artifacts:
            checkpoint = _artifact_checkpoint(world, artifact)
            channel = artifact["content"]["source_uri"].split("/")[1]
            evidence = _causal_evidence(
                world,
                checkpoint,
                channel,
                actors[artifact["source_actor_ids"][0]],
                actors[
                    artifact["recipient_actor_ids"][0]
                    if artifact["recipient_actor_ids"]
                    else artifact["source_actor_ids"][0]
                ],
            )
            if evidence and artifact["available_at"] < intervention_available:
                errors.append(
                    f"temporal_leak={world['world_id']}:{artifact['artifact_id']}"
                )
            causal_evidence_count += bool(evidence)
        if not 1 <= causal_evidence_count <= 4:
            errors.append(
                f"causal_evidence_density={world['world_id']}:{causal_evidence_count}"
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
    for split in SPLITS:
        expected = sum(world["split"] == split for world in worlds)
        actual = (
            len(list((public_root / split).glob("*/manifest.json")))
            if (public_root / split).exists()
            else 0
        )
        if actual != expected:
            errors.append(f"{split}_bundle_count={actual}")
    blind_root = root / "private/blind"
    expected_private = sum(world["release_visibility"] == "private" for world in worlds)
    actual_private = (
        len(list(blind_root.glob("*/manifest.json"))) if blind_root.exists() else 0
    )
    if actual_private != expected_private:
        errors.append(f"private_bundle_count={actual_private}")
    public_worlds = [
        world for world in worlds if world["release_visibility"] == "public"
    ]
    for world in public_worlds:
        bundle = public_root / world["split"] / world["world_id"]
        manifest = json.loads((bundle / "manifest.json").read_text())
        if manifest.get("release_visibility") != "public":
            errors.append(f"public_manifest_visibility={world['world_id']}")
        if bundle.name != world["world_id"] or not id_pattern.fullmatch(bundle.name):
            errors.append(f"public_bundle_id={bundle}")
        provenance = manifest["provenance"]
        facts = _vertical_facts(world["vertical"])
        if (
            provenance.get("source_registry_checksum") != _source_registry_checksum()
            or set(provenance.get("source_ids", ())) != set(facts["source_ids"])
            or set(provenance.get("fact_ids", ())) != set(facts["fact_ids"])
        ):
            errors.append(f"manifest_source_link={world['world_id']}")
        for required_file in (
            "hidden_events.jsonl",
            "oracle.json",
            "reference_trace.jsonl",
        ):
            if not (bundle / required_file).is_file():
                errors.append(f"public_missing_{required_file}={world['world_id']}")
    authoring_rows = [
        json.loads(line)
        for line in (root / "authoring/worlds.jsonl").read_text().splitlines()
    ]
    if len(authoring_rows) != len(worlds):
        errors.append(f"authoring_world_count={len(authoring_rows)}")
    if any(len(profiles) < 6 for profiles in structured_profiles.values()):
        errors.append(
            "structured_timing_profiles="
            + str({key: len(value) for key, value in structured_profiles.items()})
        )
    prose_metrics = _prose_metrics(prose_samples)
    for channel in ("transcript", "email", "internal_chat", "document", "web_news"):
        channel_metrics = prose_metrics.get(channel, {})
        line_metrics = prose_metrics.get(f"lines:{channel}", {})
        if (
            channel_metrics.get("modal_share", 1.0) > 0.02
            or channel_metrics.get("duplicate_share", 1.0) > 0.55
            or line_metrics.get("duplicate_share", 1.0) > 0.89
        ):
            errors.append(
                f"prose_distribution={channel}:{channel_metrics}:{line_metrics}"
            )
    if any(
        metrics["modal_share"] > 0.07
        for key, metrics in prose_metrics.items()
        if key.startswith("vertical:")
    ):
        errors.append("vertical_prose_skeletons")
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
        if not diff["post_intervention_changes_are_declared_descendants"]:
            errors.append(f"pair_undeclared_descendant={world['pair_id']}")
        if not diff["post_intervention_context_isomorphic"]:
            errors.append(f"pair_post_context={world['pair_id']}")
        if not diff["causal_event_graph_valid"]:
            errors.append(f"pair_causal_graph={world['pair_id']}")
        if not diff["action_contracts_isomorphic"]:
            errors.append(f"pair_action_contract={world['pair_id']}")
        for field in (
            "pre_intervention_events_equal",
            "pre_intervention_hidden_events_equal",
            "terminal_mappings_isomorphic",
            "milestone_contracts_isomorphic",
            "branch_contracts_isomorphic",
            "selected_evidence_contracts_isomorphic",
            "reference_trace_causal_material_isomorphic",
        ):
            if not diff[field]:
                errors.append(f"pair_{field}={world['pair_id']}")
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
        "artifact_count_per_world": None,
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
        "artifact_count_min": min(
            sum(world["artifact_counts"].values()) for world in worlds
        ),
        "artifact_count_max": max(
            sum(world["artifact_counts"].values()) for world in worlds
        ),
        "artifact_total": sum(
            sum(world["artifact_counts"].values()) for world in worlds
        ),
        "checkpoint_min": min(world["checkpoint_count"] for world in worlds),
        "checkpoint_max": max(world["checkpoint_count"] for world in worlds),
        "duration_min": min(world["duration_days"] for world in worlds),
        "duration_max": max(world["duration_days"] for world in worlds),
        "pair_count": len(pair_diffs),
        "pair_diffs": pair_diffs,
        "prose_metrics": prose_metrics,
        "blind_included": include_blind,
    }


def generate_dataset(
    root: Path | str | None = None,
    *,
    forbidden_phrases: Iterable[str] = (),
    forbidden_entities: Iterable[str] = (),
    shared_seller_actor_ids: Iterable[str] = (),
    force: bool = False,
) -> dict[str, Any]:
    target = (
        Path(root)
        if root is not None
        else Path(__file__).resolve().parents[2] / "benchmarks/v1"
    )
    target.mkdir(parents=True, exist_ok=True)
    output_path = target / "output"
    if output_path.exists():
        if not output_path.is_dir() or output_path.is_symlink():
            raise ValueError(f"refusing to replace unsafe output path: {output_path}")
        if any(output_path.iterdir()) and not force:
            raise ValueError(
                f"output path already exists and is not empty: {output_path}; "
                "pass force=True to replace generated output"
            )
        shutil.rmtree(output_path)
    worlds: list[dict[str, Any]] = []
    for vertical_index in range(len(VERTICALS)):
        for family_index in range(len(FAMILIES)):
            for variant in range(2):
                worlds.append(
                    _build_world(vertical_index, family_index, variant, DATASET_SEED)
                )
    shared_documents = _write_shared_documents(target)
    _write_authoring(target, worlds, shared_documents)
    for world in worlds:
        _write_world(target, world)
    summary = _validate(
        target,
        worlds,
        shared_documents,
        True,
        forbidden_phrases=forbidden_phrases,
        forbidden_entities=forbidden_entities,
        shared_seller_actor_ids=shared_seller_actor_ids,
    )
    public_summary = summary
    published_validation = public_summary
    _write_json(
        target / "output/manifest.json",
        {
            "dataset_version": DATASET_VERSION,
            "seed": DATASET_SEED,
            "world_count": len(worlds),
            "verticals": [vertical["id"] for vertical in VERTICALS],
            "splits": {
                split: public_summary["split_counts"][split] for split in SPLITS
            },
            "shared_documents": public_summary["shared_document_count"],
            "artifact_count_per_world": public_summary["artifact_count_per_world"],
            "artifact_total": public_summary["artifact_total"],
            "validation": published_validation,
        },
    )
    _write_json(target / "authoring/validation.json", published_validation)
    if not summary["valid"]:
        raise ValueError(_json(summary))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    summary = generate_dataset(args.root)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
