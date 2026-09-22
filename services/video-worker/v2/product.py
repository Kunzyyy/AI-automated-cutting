"""Infer a single-product brief from the whole footage batch."""
from .common import ROOT, read, local_path


def identify_product(ai, footage, base):
    properties = {
        "product_name": {"type": "string", "minLength": 1, "maxLength": 200},
        "category": {"type": "string", "minLength": 1, "maxLength": 100},
        "single_product": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "visible_selling_points": {"type": "array", "items": {"type": "string", "maxLength": 300}, "maxItems": 6},
        "narrative_direction": {"type": "string", "maxLength": 1000},
        "music_mood": {"type": "string", "enum": ["calm", "elegant", "emotional", "upbeat", "warm"]},
        "caption_tone": read(ROOT / 'schemas' / 'edit_plan.schema.json')['$defs']['candidate']['properties']['caption_direction']['properties']['tone'],
    }
    evidence = []
    images = []
    for source in footage["sources"]:
        shots = [s for s in footage["shots"] if s["source_asset_id"] == source["asset_id"]]
        if not shots:
            continue
        for shot in {s["shot_id"]: s for s in (shots[0], shots[len(shots)//2])}.values():
            evidence.append({"shot_id": shot["shot_id"], "description": shot["description"]})
            refs = shot["evidence"].get("keyframe_uris", [])
            if refs and len(images) < 24:
                images.append((shot["start_seconds"], local_path(refs[len(refs)//2], base)))
    import json
    result = ai.ask(
        "Identify the SINGLE product being advertised in this footage batch, using images over fallible annotations. "
        "Examples include decorative objects, clothing, household products, or accessories; never default to memorial pins. "
        "Different angles, settings, models or color variants of the same product may be one product. Unrelated products are not. "
        "Infer only visible features; no invented brand, material, benefits, prices or specifications. If unclear lower confidence. "
        "Choose an achievable narrative using available footage, not obligatory unboxing/customization/wearing. "
        "Select a suitable music mood and caption tone. Return submit_result. Evidence: " + json.dumps(evidence, ensure_ascii=False),
        {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}, images)
    if not result["single_product"]:
        raise ValueError("素材包含不同产品，请将每个产品分开到独立文件夹")
    if result["confidence"] < .6:
        raise ValueError("无法可靠识别产品，请补充清晰的产品整体画面")
    return result
