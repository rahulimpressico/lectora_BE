"""Reorder A2 flat sections to match the course editor layout."""

from __future__ import annotations

from typing import Callable

# Frontend-only section IDs — not stored in agent_outputs.A2.sections
FE_SPECIAL_SECTION_IDS = frozenset({
    "course-overview",
    "course-learning-objectives",
    "course-conclusion",
})


def is_l1_leader(sec: dict) -> bool:
    """True when *sec* starts a new top-level lesson block in the flat A2 list."""
    level = int(sec.get("level") or 2)
    if level == 1:
        return True
    if sec.get("is_parent_overview"):
        return True
    outline = (sec.get("outline_lesson") or "").strip()
    heading = (sec.get("heading") or "").strip()
    return bool(outline and heading == outline)


def split_sections_into_blocks(
    sections: list[dict],
) -> list[tuple[int, list[dict]]]:
    """Split flat A2 sections into [(start_index, [L1, L2, L2, ...]), ...]."""
    blocks: list[tuple[int, list[dict]]] = []
    start = 0
    current: list[dict] = []

    for i, sec in enumerate(sections):
        if is_l1_leader(sec) and current:
            blocks.append((start, current))
            start = i
            current = [sec]
        else:
            current.append(sec)

    if current:
        blocks.append((start, current))
    return blocks


def apply_section_order(
    sections: list[dict],
    section_order: list[str],
    stable_id_fn: Callable[[dict, int], str],
) -> list[dict]:
    """Return a reordered copy of *sections* matching the editor order.

    The frontend may send either:
    - L1-only order (top-level tree nodes): each lesson block moves with all L2 children.
    - Depth-first flat order (L1 then its children): individual section reorder.
    """
    if not sections or not section_order:
        return list(sections)

    content_order = [sid for sid in section_order if sid not in FE_SPECIAL_SECTION_IDS]
    if not content_order:
        return list(sections)

    stable_ids = [stable_id_fn(sec, i) for i, sec in enumerate(sections)]
    by_stable_id = {sid: sec for sid, sec in zip(stable_ids, sections)}

    blocks = split_sections_into_blocks(sections)
    block_by_leader: dict[str, list[dict]] = {}
    for start_idx, block in blocks:
        leader_id = stable_id_fn(block[0], start_idx)
        block_by_leader[leader_id] = block

    leader_ids = set(block_by_leader.keys())
    flat_mode = any(
        sid in by_stable_id and sid not in leader_ids
        for sid in content_order
    )

    if flat_mode:
        ordered_set = set(content_order)
        reordered = [by_stable_id[sid] for sid in content_order if sid in by_stable_id]
        remaining = [
            sec for sid, sec in zip(stable_ids, sections) if sid not in ordered_set
        ]
        reordered.extend(remaining)
        return reordered

    # L1-only order: move each lesson block (parent + subtopics) together.
    used_leaders: set[str] = set()
    reordered: list[dict] = []
    for sid in content_order:
        block = block_by_leader.get(sid)
        if block and sid not in used_leaders:
            reordered.extend(block)
            used_leaders.add(sid)
        elif sid in by_stable_id and sid not in used_leaders:
            # Standalone section (e.g. orphan L2) referenced directly in order.
            reordered.append(by_stable_id[sid])
            used_leaders.add(sid)

    for leader_id, block in block_by_leader.items():
        if leader_id not in used_leaders:
            reordered.extend(block)

    return reordered
