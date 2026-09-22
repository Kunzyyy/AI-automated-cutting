from __future__ import annotations

import json

from .common import ROOT, now, read, validate
from .render import sample_frames


def review(ai, candidate, video, directory, max_recut, *, final=False, cues=None):
    definitions = read(ROOT / "schemas" / "review_report.schema.json")["$defs"]
    fields = ("overall_score", "scores", "hard_checks", "summary", "issues")
    properties = {key: definitions["candidate_review"]["properties"][key] for key in fields}
    properties["overall_score"] = {"type": "number", "minimum": 0, "maximum": 100}
    properties["scores"] = {"type":"object","additionalProperties":False,
        "required":definitions["candidate_review"]["properties"]["scores"]["required"],
        "properties":{name:{"type":"number","minimum":0,"maximum":100}
                      for name in definitions["candidate_review"]["properties"]["scores"]["required"]}}
    properties["issues"] = {"type":"array","items":{"type":"object","additionalProperties":False,
        "required":["category","severity","message"],"properties":{
            "category":definitions["issue"]["properties"]["category"],
            "severity":definitions["issue"]["properties"]["severity"],
            "message":{"type":"string","minLength":1,"maxLength":1000}}}}
    properties["caption_safe_area"] = {"type": "boolean"}
    generic_checks = ["product_visible", "supported_selling_point", "coherent_presentation",
                      "no_black_frames", "no_duplicate_shots", "no_broken_actions"]
    properties["hard_checks"] = {"type": "object", "additionalProperties": False, "required": generic_checks,
                                 "properties": {name: {"type": "boolean"} for name in generic_checks}}
    schema = dict(type="object", additionalProperties=False, required=[*fields, "caption_safe_area"],
                  properties=properties)
    frames = sample_frames(video, directory)
    answer = ai.ask(
        "Review the entire chronological 0.5-second sampled storyboard of this actual rendered video. "
        "Samples cannot prove sub-frame continuity or audio quality: do not claim you heard the soundtrack. "
        "Judge product visibility, story, action order, repetition, and evidence-backed advertising. "
        "This is a SINGLE-product advertisement: the same design, photo, name, or item across different angles, "
        "people and settings is expected. It is NOT duplicate footage or evidence of a copy-paste defect. "
        "no_duplicate_shots concerns replayed footage or redundant near-identical shots, not product identity. "
        "Do not require different personalized variants or a customization demonstration. A photo ornament can simply be advertised as a photo ornament. "
        "For final=false, judge the visible edit, not unpublished plan claims as if they were burned captions. "
        "A mistaken metadata verb (hanging versus steadying an already hanging object) is a minor wording correction "
        "unless the actual edit shows broken actions. Forward wording corrections for caption generation. "
        "Different people may demonstrate the same product in a montage; this alone is not an identity error. "
        "Flag identity changes only when cuts falsely imply a single continuous person's action. "
        "The plan and its descriptions are fallible metadata. Trust the actual images over scene names: "
        "a label gift_box does not prove a box exists. Flag any caption about a box, screen, or action absent from its frames. "
        "Require a clear product, at least one visually supported feature/detail, and a coherent presentation. "
        "Judge the actual product category: decor may be displayed in a room, clothing may be worn or laid out. "
        "Do NOT require customization, people, unboxing, wearing or functional use when irrelevant or absent. "
        "If final=true check all burned captions are readable, within safe margins, do not overlap the important product/face, "
        "and match the displayed footage. The last fixed brand end card is intentional, not a duplicate. "
        "Flag unsupported handmade/handcrafted manufacturing or made-to-last durability claims as major evidence issues; "
        "a textured surface or an attractive display alone does not prove those claims. "
        "Return honest scores and actionable issues. No major/blocking issues means pass only when all hard checks hold. "
        "Call submit_result. Context: " + json.dumps(dict(plan=candidate, final=final, captions=cues), ensure_ascii=False),
        schema, frames)
    passed = (answer["overall_score"] >= 75 and all(answer["hard_checks"].values())
              and not any(i["severity"] in {"major", "blocking"} for i in answer["issues"])
              and (not final or answer["caption_safe_area"]))
    revision = candidate["revision"]
    can_recut = revision < min(2, max_recut)
    decision = "pass" if passed else "revise" if can_recut and not final else "human_review"
    result = {k: answer[k] for k in fields}
    for i, issue in enumerate(result["issues"], 1):
        issue["issue_id"] = f"issue-{i}"
    result["change_requests"] = [dict(request_id=f"change-{i}",operation="regenerate_plan",
                                      reason=issue["message"],priority=1)
                                  for i,issue in enumerate(result["issues"],1) if issue["severity"] in {"major","blocking"}]
    result.update(review_id=f"{candidate['candidate_id']}-r{revision}-{'final' if final else 'rough'}",
                  candidate_id=candidate["candidate_id"], candidate_index=candidate["candidate_index"],
                  plan_revision=revision, recut_attempts_used=revision, rough_cut_uri=video.resolve().as_uri(),
                  decision=decision, next_action={"pass": "lock_plan", "revise": "revise_plan", "human_review": "request_human_review"}[decision],
                  can_recut=can_recut if decision == "revise" else False)
    return result, answer["caption_safe_area"]


def report(job_id, ai, reviews):
    document = dict(schema_version="1.0.0", job_id=job_id, generated_at=now(),
                    reviewer=dict(name="sampled-render-reviewer", version="2.0.0", model=ai.model,
                                  prompt_version="single-product-review-v2"), candidate_reviews=reviews)
    validate(document, "review_report")
    return document


def generate_captions(ai, candidate, video=None, feedback=None):
    schema = {"type": "object", "additionalProperties": False, "required": ["cues"], "properties": {
        "cues": {"type": "array", "minItems": len(candidate["timeline"]), "maxItems": len(candidate["timeline"]),
                 "items": {"type": "object", "additionalProperties": False, "required": ["sequence", "text"],
                           "properties": {"sequence": {"type": "integer", "minimum": 1},
                                          "text": {"type": "string", "minLength": 1, "maxLength": 70}}}}}}
    images = sample_frames(video, video.parent/'caption-frames') if video else []
    return ai.ask("Write one concise English advertising caption per clip in timeline order, max 7 words per cue. "
                  "Write a coherent product ad with the requested caption_direction tone, not literal narration of camera angles. "
                  "Infer the actual product from images; do not assume memorial gifts or sentimental meaning. "
                  "Actual chronological video images are PRIMARY evidence; plan labels/descriptions may be wrong. "
                  "Never mention gift packaging unless a box is actually visible during that clip. "
                  "The whole sequence must read coherently; every factual claim must match the actual clip's images. "
                  "Never invent material, durability, shipping, price, or readable customization. "
                  "Product appearance cannot prove manufacturing method or lifespan: never say handcrafted, handmade, "
                  "hand-carved, made to last, or durable unless actual production or testing evidence is provided. "
                  "No braces, backslashes or line breaks. Call submit_result. Context: " + json.dumps(dict(plan=candidate,correction_feedback=feedback), ensure_ascii=False), schema, images)["cues"]
