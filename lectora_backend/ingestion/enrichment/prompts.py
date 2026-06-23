from __future__ import annotations

ENRICHMENT_SYSTEM = "You are an instructional design expert analyzing educational content."

ENRICHMENT_USER = """Analyze this course content chunk and return ONLY valid JSON.

Title: {title}
Parent section: {parent_title}
Content: {content}

Return this exact JSON structure:
{{
  "learning_concepts": ["list of up to 5 core concepts"],
  "skills": ["observable skills learners gain"],
  "keywords": ["8-12 search keywords"],
  "entities": ["named entities: laws, orgs, products, people"],
  "summary": "2-sentence retrieval-optimized summary",
  "prerequisites": ["topics learner must know first"],
  "difficulty": "introductory or intermediate or advanced or expert",
  "learning_outcomes": ["3-5 'learner will be able to...' statements"]
}}"""
