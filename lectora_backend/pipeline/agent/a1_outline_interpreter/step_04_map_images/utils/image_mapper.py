"""Step 04 — Image-to-section mapper. Pure code — no LLM."""
import logging
from ...shared.models.state import A1State

logger = logging.getLogger(__name__)


def map_images_to_sections(images: list[dict], sections: list[dict]) -> dict[str, list]:
    """Map images to sections by paragraph index. Returns {section_id: [imgs], 'unassigned': [...]}."""
    image_map: dict[str, list] = {s["id"]: [] for s in sections}
    image_map["unassigned"] = []

    for img in images:
        img_para = img["para_idx"]
        matched = False
        for sec in sections:
            if sec["para_start"] <= img_para <= sec["para_end"]:
                image_map[sec["id"]].append({
                    "id": img["id"],
                    "saved_path": img["saved_path"],
                    "media_filename": img["media_filename"],
                    "size_cm": img["size_cm"],
                    "size_bytes": img["size_bytes"],
                    "para_idx": img_para,
                    "caption": img["caption"],
                    "has_caption": img["has_caption"],
                    "alt_text": img["alt_text"],
                })
                matched = True
                break
        if not matched:
            image_map["unassigned"].append(img)

    return image_map


def map_images(state: A1State) -> A1State:
    if state["status"] in ("failed", "stopped"):
        return state

    images: list[dict] = state["a0_data"].get("images", [])
    if not images:
        logger.info("[A1] No images in shared state — skipping image mapping.")
        return {**state, "image_map": {}}

    logger.info("[A1] Mapping %s images to sections by paragraph index...", len(images))
    image_map = map_images_to_sections(images, state["raw_sections"])
    placed = sum(len(v) for k, v in image_map.items() if k != "unassigned")
    logger.info("[A1] Mapped %s/%s images. Unassigned: %s", placed, len(images), len(image_map["unassigned"]))
    return {**state, "image_map": image_map}
