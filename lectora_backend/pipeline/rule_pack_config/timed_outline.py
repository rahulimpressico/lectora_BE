"""
JSON skeleton for LLM extraction of Timed Outline (TO) documents.

Document layout (each item lives in its own single-cell table, followed by the
7-column outline table):

  Table 0  →  course_title          (text after "COURSE TITLE:")
  Table 1  →  course_id             (text after "COURSE ID:")
  Table 2  →  description           (course description prose)
  Table 3  →  learning_objectives   (newline-separated objectives)
  Table 4  →  7-column outline grid
               Col 0  Lesson Topic        → title
               Col 1  Subtopic            → topics  (split on newline; strip numbering)
               Col 2  Content Objective   → content (brief objective or "" if blank)
               Col 3  Word Count          → word_count
               Col 4  Minutes             → minutes
               Col 5  Credit Hour         → credit_hour
               Col 6  Interactive Elements→ interactive_elements (split on comma)
               Last row = totals (word_count / minutes / credit_hours)

Used by A0 classification prompts; keep in sync with models.to_outline.TOOutline.
"""

TO_outline_format: dict = {
    "course_title": "",
    "course_id": "",
    "description": "",
    "learning_objectives": [],
    "sections": [
        {
            "title": "",
            "content": "",
            "subtopics": [],
            "word_count": "",
            "minutes": "",
            "credit_hour": "",
            "interactive_elements": [],
        }
    ],
    "totals": {"word_count": "", "minutes": "", "credit_hours": ""},
}
