from __future__ import annotations


def check_rule_pack_sanity(rule_pack: dict) -> list[dict]:
    """
    Validate internal consistency of the active rule pack.

    This is a pre-flight check for *new rule keys* that affect A2 behavior but
    cannot be validated against course content until A2 runs (e.g. KC option counts).
    """
    issues: list[dict] = []
    if not isinstance(rule_pack, dict):
        return issues

    kc_rules = rule_pack.get("kc_placement_rules", {}) if isinstance(rule_pack, dict) else {}
    assessment = rule_pack.get("assessment_rules", {}) if isinstance(rule_pack, dict) else {}

    min_opts = kc_rules.get("min_answer_options")
    max_opts = kc_rules.get("max_answer_options")

    if min_opts is None or max_opts is None:
        issues.append(
            {
                "field": "kc_placement_rules.answer_options_bounds",
                "expected": "min_answer_options and max_answer_options present when KC options are constrained",
                "found": {"min_answer_options": min_opts, "max_answer_options": max_opts},
                "severity": "warning",
                "message": (
                    "The rule pack doesn't define how many answer options quiz questions should have. "
                    "This may cause inconsistent quiz formatting in the generated course."
                ),
                "rule_source": "kc_placement_rules.min_answer_options/max_answer_options",
            }
        )
    else:
        try:
            min_i = int(min_opts)
            max_i = int(max_opts)
            if min_i > max_i:
                issues.append(
                    {
                        "field": "kc_placement_rules.answer_options_bounds",
                        "expected": "min_answer_options <= max_answer_options",
                        "found": {"min_answer_options": min_i, "max_answer_options": max_i},
                        "severity": "blocker",
                        "message": (
                            f"The rule pack has a configuration error: the minimum number of quiz answer options "
                            f"({min_i}) is higher than the maximum ({max_i}). This must be fixed before the course can be generated."
                        ),
                        "rule_source": "kc_placement_rules.min_answer_options/max_answer_options",
                    }
                )
        except (TypeError, ValueError):
            issues.append(
                {
                    "field": "kc_placement_rules.answer_options_bounds",
                    "expected": "min_answer_options/max_answer_options parseable as ints",
                    "found": {"min_answer_options": min_opts, "max_answer_options": max_opts},
                    "severity": "warning",
                    "message": (
                        "The quiz answer option limits in the rule pack are not valid numbers. "
                        "This should be corrected to ensure quiz questions are formatted correctly."
                    ),
                    "rule_source": "kc_placement_rules.min_answer_options/max_answer_options",
                }
            )

    if "require_distractor_rationales" not in assessment:
        issues.append(
            {
                "field": "assessment_rules.require_distractor_rationales",
                "expected": "present (True/False)",
                "found": "missing",
                "severity": "warning",
                "message": (
                    "The rule pack doesn't specify whether quiz answer explanations must address each wrong option. "
                    "This may affect the quality of quiz explanations in the generated course."
                ),
                "rule_source": "assessment_rules.require_distractor_rationales",
            }
        )

    return issues
