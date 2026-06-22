"""
LLM classification prompt for A0 — Request Synthesizer.
"""

from ...shared.constants.difficulty import _DIFFICULTY_MULTIPLIERS, DEFAULT_TO_DURATION_HOURS

CLASSIFICATION_PROMPT = """You are a course-classification expert for a regulated professional-education platform.
You will receive content signals from one or more source documents.
Your sole task: classify this course into EXACTLY ONE of the three rule families below.

═══════════════════════════════════════════════════════════
RULE FAMILY DEFINITIONS
(read these carefully before classifying — families have overlapping vocabulary)
═══════════════════════════════════════════════════════════

insurance_ce — Insurance Continuing Education
  Governed by: State insurance regulatory departments (NAIC, state DOIs)
  Audience:    State-licensed insurance producers, agents, adjusters, back-office staff

  CHOOSE insurance_ce when the content predominantly covers:
  • Property & Casualty insurance: auto, homeowners, commercial property, liability, flood (NFIP/Wright Flood), workers comp, inland marine
  • Life insurance: term, whole life, universal life, variable life
  • Health insurance: individual/group health plans, HMO/PPO, Medicare supplements, long-term care, disability income
  • Employer-sponsored benefits / employer health plans (ACA, COBRA, HIPAA, ERISA) when framed as insurance product knowledge
  • Annuities when the course focuses on insurance licensing, suitability for insurance agents, or state-regulated annuity products
  • Insurance producer licensing, CE credits, state-specific rules (e.g. Washington LTC, Louisiana flood)
  • Coverage, premiums, underwriting, endorsements, policy exclusions, claims handling
  • NAIC guidelines, state DOI regulations, Gramm-Leach-Bliley from an insurance perspective

  KEY SIGNALS: "licensed agent", "insurance producer", "policy", "coverage", "premium",
    "NFIP", "flood zone", "P&C", "homeowners", "workers compensation", "state DOI",
    "CE credit", "state insurance department", "continuing education hours"

  DISTINGUISH FROM iarce: Insurance CE covers insurance products and state licensing.
    If annuities appear alongside securities regulations or FINRA — lean iarce.
    If employer health benefits appear alongside fiduciary / investment concepts — consider iarce.

iarce — Investment Adviser Representative Continuing Education (Ethics & Professional Responsibility)
  Governed by: NASAA / SEC (state and federal securities law)
  Audience:    Investment Adviser Representatives (IARs), registered investment advisers, dually-registered reps

  CHOOSE iarce when the content predominantly covers:
  • Investment adviser regulations: fiduciary duty, RIA registration, Form ADV, investment advisory agreements
  • Ethics and professional responsibility for IARs and advisers
  • Securities law: Investment Advisers Act 1940, Securities Act 1933/1934
  • Behavioral finance, investor psychology, suitability / best-interest standards in investment context
  • Portfolio management, asset allocation, investment strategies as taught to advisers
  • Variable annuities / variable life from a securities regulation angle (not insurance licensing)
  • NASAA model rules, state securities divisions
  • IAR CE requirements (NASAA Series 65/66 context)

  KEY SIGNALS: "investment adviser", "IAR", "fiduciary", "RIA", "Form ADV",
    "NASAA", "Investment Advisers Act", "securities regulations", "suitability",
    "investment advisory", "portfolio", "asset allocation", "behavioral finance"

  DISTINGUISH FROM firm_element: iarce is for IARs and investment advisers under SEC/NASAA.
    Firm Element is for FINRA-registered broker-dealer personnel under FINRA Rule 1240.
    If content covers broker-dealer supervisory procedures or FINRA registered reps — use firm_element.

firm_element — Firm Element Continuing Education
  Governed by: FINRA Rule 1240 (formerly Rule 1250)
  Audience:    Registered representatives (RRs), broker-dealer principals, supervisors, compliance officers, operations staff at FINRA-member firms

  CHOOSE firm_element when the content predominantly covers:
  • FINRA Firm Element CE requirements for registered broker-dealer personnel
  • Broker-dealer supervisory procedures, branch office supervision
  • FINRA rules and regulations: suitability, best execution, order handling, disclosure
  • Anti-money laundering (AML), Bank Secrecy Act for broker-dealers
  • Senior investor protection (Senior Safe Act)
  • Due diligence for complex / alternative investments, private placements
  • Customer account management, KYC / CIP at broker-dealers
  • Cybersecurity, data privacy in the broker-dealer context
  • Reg BI (Regulation Best Interest), Reg SP, Reg SCI
  • Estate planning or elder financial exploitation when targeting BD reps
  • Internal firm compliance policies, ethics for registered persons

  KEY SIGNALS: "FINRA", "registered representative", "broker-dealer", "Reg BI",
    "supervisory procedures", "FINRA Rule 1240", "branch manager",
    "Series 7", "annual compliance", "registered principal", "member firm",
    "AML", "Bank Secrecy Act", "due diligence"

  DISTINGUISH FROM iarce: Firm Element is FINRA/broker-dealer focused.
    iarce is SEC/NASAA/investment-adviser focused.
    If the course covers BOTH broker-dealer rules AND investment adviser rules,
    identify which regulatory framework dominates the content.

═══════════════════════════════════════════════════════════
CLASSIFICATION PROCESS — follow these steps
═══════════════════════════════════════════════════════════
1. Read ALL provided signals (titles, headings, objectives, content).
2. Identify the governing body / regulatory framework referenced most.
3. Identify the PRIMARY audience the content is written for.
4. Match to the rule family whose definition best fits both.
5. Apply disambiguation rules when vocabulary overlaps (annuities, ethics, health).
6. If genuinely ambiguous, pick the BEST fit and note it in reasoning with confidence < 0.7.

Respond with ONLY a JSON object — no markdown, no explanation:
{
  "rule_family": "<one of: insurance_ce | iarce | firm_element>",
  "confidence": <float 0.0–1.0>,
  "audience": "<specific audience this course targets>",
  "course_type": "<e.g. Insurance CE, IAR CE — Ethics, Firm Element Annual Training>",
  "category": "<specific sub-category, e.g. Property & Casualty — Flood Insurance, IAR — Behavioral Finance>",
  "topic": "<primary topic in 5–10 words>",
  "reasoning": "<2–3 sentences explaining which signals drove the classification and why alternatives were ruled out>"
}
"""


def compute_calculated_word_count(duration_hours: int | float, difficulty: str) -> int:
    """Return target word count from duration + difficulty.

    Formula: (duration_hours × 9,000) / multiplier
    Mirrors the identical calculation performed by the frontend.
    """
    mult = _DIFFICULTY_MULTIPLIERS.get((difficulty or "intermediate").lower(), 1.25)
    return max(1, round((duration_hours * 9000) / mult))
