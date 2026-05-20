"""
Image-to-section mapper for A1. Pure code — no LLM.

Maps each image from A0's shared state to the section it appears in,
using paragraph index ranges. Visual content is NEVER described or inferred.
"""


def map_images_to_sections(images: list[dict], sections: list[dict]) -> dict[str, list]:
    """
    Map images to sections by paragraph index range.
    Returns dict: {section_id: [image records], "unassigned": [...]}.
    """
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
