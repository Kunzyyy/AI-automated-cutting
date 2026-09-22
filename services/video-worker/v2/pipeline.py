from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import hashlib
from pathlib import Path

from analyzer.pipeline import AnalyzerConfig, analyze_directory
from director.pipeline import DirectorConfig, _build_document, generate_edit_plan
from validator import ValidatorConfig, all_candidates_valid, validate_edit_plan

from .ai import AI, DirectorAdapter, SemanticAdapter
from .common import local_path, now, probe, read, validate, video_artifact, write
from .render import captions_ass, check_supported, finalize, rough_cut, technical_qc
from .review import generate_captions, report, review
from .product import identify_product

STAGES = ("analyze", "plan", "rough_cut", "review", "finalize", "quality_control")


def can_resume_initial_failure(manifest):
    if manifest.get('job_status') != 'failed':
        return False
    stage = manifest.get('error', {}).get('stage')
    return stage == 'analyze' or (stage == 'plan' and
        manifest.get('stage_status', {}).get('rough_cut', {}).get('status') == 'pending')


def review_rank(entry):
    """Prefer accepted edits, then fewer serious defects, then the review score."""
    serious = sum(i['severity'] in ('major', 'blocking') for i in entry.get('issues', []))
    failed_checks = sum(not v for v in entry.get('hard_checks', {}).values())
    return (entry['decision'] == 'pass', -serious, -failed_checks, entry['overall_score'])


def delivery_approved(initial_review, final_review, qc, human_approval):
    return (initial_review['decision'] == 'pass' and final_review['decision'] == 'pass'
            and qc['passed'] and not human_approval)


def emit(stage, progress):
    print(json.dumps(dict(type="stage", stage=stage, progress_percent=progress)), flush=True)


def imported_index(path, job):
    index = read(path)
    validate(index, "footage_index")
    expected = {local_path(s["uri"]) for s in job["input"]["source_videos"]}
    actual = {local_path(s["uri"], path.parent) for s in index["sources"]}
    if expected != actual:
        raise ValueError("Cached Footage Index source paths do not match this job")
    def absolute_uris(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if (key == "uri" or key.endswith("_uri")) and isinstance(item, str):
                    value[key] = local_path(item, path.parent).as_uri()
                elif key.endswith("_uris") and isinstance(item, list):
                    value[key] = [local_path(uri, path.parent).as_uri() for uri in item]
                else:
                    absolute_uris(item)
        elif isinstance(value, list):
            for item in value:
                absolute_uris(item)
    absolute_uris(index)
    for source in index["sources"]:
        actual_info = video_artifact(local_path(source["uri"]))
        if abs(actual_info["duration_seconds"] - source["duration_seconds"]) > .1:
            raise ValueError("Cached source duration changed; rerun Analyzer")
    index["job_id"] = job["job_id"]
    return index


def choose_music(library, candidate):
    """Existing library is curated instrumental audio; selection is deterministic by mood."""
    if library.is_file():
        tracks = [library]
    else:
        tracks = sorted(library.rglob("*.mp3")) + sorted(library.rglob("*.wav"))
    if not tracks:
        raise ValueError("BGM library contains no MP3/WAV files")
    keywords = set((candidate["music_direction"]["mood"] + " " +
                    " ".join(candidate["music_direction"].get("search_keywords", []))).lower().replace("_", " ").split())
    return max(tracks, key=lambda p: sum(3 * (word in p.stem.lower()) + (word in p.parent.name.lower()) for word in keywords))


def execute(args):
    job_path = Path(args.job).resolve()
    job = read(job_path)
    validate(job, "job")
    if job["brief"]["language"] != "en":
        raise ValueError("This V2 release supports English advertising captions; set brief.language=en")
    maximum = min(job["brief"]["max_recut_attempts"], args.max_recut_attempts, 2)
    directory = local_path(job["delivery"]["output_uri"], job_path.parent)
    # Every execution owns a new directory: never mix stale output with a new success.
    resume=getattr(args,'resume',False)
    if resume:
        if not directory.is_dir() or read(directory/'job.json') != job:
            raise ValueError("恢复任务必须与已有 job.json 完全一致")
        previous=read(directory/'output_manifest.json')
        if not can_resume_initial_failure(previous):
            raise ValueError("当前仅支持恢复素材分析或首次方案生成失败的任务；其他任务请使用新的输出目录")
    directory.mkdir(parents=True, exist_ok=resume)
    manifest_path = Path(args.manifest).resolve() if args.manifest else directory / "output_manifest.json"
    copied_job = directory / "job.json"
    write(copied_job, job)
    manifest = dict(schema_version="1.0.0", job_id=job["job_id"], updated_at=now(), job_status="running",
                    stage_status={s: {"status": "pending"} for s in STAGES},
                    candidates=[dict(candidate_id=f"candidate-{i}", candidate_index=i, plan_revision=0, status="pending")
                                for i in range(1, job["brief"]["candidate_count"]+1)],
                    supporting_artifacts=dict(job_uri=copied_job.as_uri(), footage_index_uri=(directory/"footage_index.json").as_uri(),
                                              edit_plan_uri=(directory/"edit_plan.json").as_uri()))
    current_stage = "analyze"
    def stage(name, progress):
        nonlocal current_stage
        current_stage = name
        manifest["stage_status"][name] = {"status": "running"}
        manifest["progress_percent"] = progress
        manifest["updated_at"] = now()
        write(manifest_path, manifest)
        if manifest_path != directory / "output_manifest.json":
            write(directory / "output_manifest.json", manifest)
        emit(name, progress)
    def done(name):
        manifest["stage_status"][name] = {"status": "completed"}
    try:
        ai = AI(args.provider, args.model)
        stage("analyze", 5)
        footage_path = directory / "footage_index.json"
        if args.footage_index:
            footage = imported_index(Path(args.footage_index).resolve(), job)
            write(footage_path, footage)
        else:
            source_dir = directory / "sources"
            source_dir.mkdir(exist_ok=resume)
            for i, source in enumerate(job["input"]["source_videos"]):
                path = local_path(source["uri"], job_path.parent)
                target = source_dir / f"source-{i:03}{path.suffix}"
                if resume and target.exists():
                    with path.open('rb') as source_file,target.open('rb') as cached_file:
                        if hashlib.file_digest(source_file,'sha256').digest()!=hashlib.file_digest(cached_file,'sha256').digest():
                            raise ValueError("素材已经改变，请创建新任务以避免复用旧镜头缓存")
                else:
                    shutil.copy2(path, target)
            footage = analyze_directory(source_dir, footage_path, directory/"analysis-cache",
                                        AnalyzerConfig(semantic_provider=args.provider, semantic_model=args.model,
                                                       source_joint_review=False), job_id=job["job_id"],
                                        semantic_provider=SemanticAdapter(ai))
        done("analyze")
        stage("plan", 25)
        profile = identify_product(ai, footage, footage_path.parent)
        write(directory / "product_profile.json", profile)
        director_brief = {**job["brief"], **profile}
        director = DirectorAdapter(ai, director_brief)
        config = DirectorConfig(candidate_count=job["brief"]["candidate_count"],
                                target_duration_seconds=job["brief"]["target_body_duration_seconds"], generic_product=True)
        plan_path = directory / "edit_plan.json"
        if args.edit_plan:
            plan = read(args.edit_plan)
            plan["job_id"] = job["job_id"]
            plan["footage_index_uri"] = footage_path.as_uri()
            plan = validate_edit_plan(plan, footage, ValidatorConfig(config.target_duration_seconds, 2, generic_product=True))
            if not all_candidates_valid(plan) or len(plan["candidates"]) != config.candidate_count:
                raise ValueError("Supplied EditPlan is invalid or has the wrong candidate count")
            if any(c["revision"] != 0 for c in plan["candidates"]):
                raise ValueError("Imported plans must start at revision zero")
        else:
            plan = generate_edit_plan(footage_path, plan_path, config, provider=director)
        for candidate in plan["candidates"]:
            check_supported(candidate)
        write(plan_path, plan)
        done("plan")
        latest_reviews = []
        locked = {}
        best = {}
        for revision in range(maximum + 1):
            for candidate in plan["candidates"]:
                if candidate["candidate_id"] not in locked:
                    candidate["revision"] = revision
            write(plan_path, plan)
            write(directory / f"edit_plan-r{revision}.json", plan)
            stage("rough_cut", 40)
            roughs = [locked[c["candidate_id"]][1] if c["candidate_id"] in locked else
                      rough_cut(c, footage, directory/f"{c['candidate_id']}-r{revision}", job["delivery"]) for c in plan["candidates"]]
            done("rough_cut")
            stage("review", 55)
            latest_reviews = [locked[c["candidate_id"]][2] if c["candidate_id"] in locked else
                              review(ai, c, video, video.parent/"rough-frames", maximum)[0]
                              for c, video in zip(plan["candidates"], roughs)]
            for c, video, entry in zip(plan["candidates"], roughs, latest_reviews):
                saved = best.get(c['candidate_id'])
                if saved is None or review_rank(entry) > review_rank(saved[2]):
                    best[c['candidate_id']] = (copy.deepcopy(c), video, copy.deepcopy(entry))
                if entry["decision"] == "pass":
                    locked[c["candidate_id"]] = (copy.deepcopy(c), video, entry)
            write(directory/f"rough-review-r{revision}.json", report(job["job_id"], ai, latest_reviews))
            done("review")
            if all(r["decision"] == "pass" for r in latest_reviews) or revision == maximum:
                break
            stage("plan", 30)
            feedback = list(latest_reviews)
            revised_plan = None
            for attempt in range(2):
                try:
                    payload = director.generate_candidates(footage, config.candidate_count, config.target_duration_seconds,
                                                             previous_payload=plan, validation_feedback=feedback)
                    proposed = _build_document(payload, footage, footage_path, plan_path, director, config)
                    proposed["candidates"] = [copy.deepcopy(locked[c["candidate_id"]][0]) if c["candidate_id"] in locked else c
                                              for c in proposed["candidates"]]
                    proposed = validate_edit_plan(proposed, footage, ValidatorConfig(config.target_duration_seconds, 2, generic_product=True))
                    write(directory/f"recut-proposal-r{revision+1}-{attempt+1}.json", proposed)
                    if all_candidates_valid(proposed):
                        for candidate in proposed["candidates"]:
                            check_supported(candidate)
                        revised_plan = proposed
                        break
                    feedback = latest_reviews + [{"candidate_id":c["candidate_id"], "validation_errors":c["validation"]["errors"]}
                                                  for c in proposed["candidates"] if not c["validation"]["valid"]]
                except (ValueError, RuntimeError) as exc:
                    write(directory/f'recut-error-r{revision+1}-{attempt+1}.json', dict(error=str(exc)[:1500]))
                    feedback = latest_reviews + [{"generation_error":str(exc)[:800]}]
            done("plan")
            if revised_plan is None:
                for entry in latest_reviews:
                    if entry["decision"] != "pass":
                        entry.update(decision="human_review", next_action="request_human_review", can_recut=False)
                        entry["summary"] = (entry["summary"] + " No valid replacement plan after two bounded corrections.")[:2000]
                break
            plan = revised_plan
            print(json.dumps(dict(type="review", decision="revise", plan_revision=revision+1)), flush=True)
        selected = [best[c['candidate_id']] for c in plan['candidates']]
        plan['candidates'] = [item[0] for item in selected]
        roughs = [item[1] for item in selected]
        latest_reviews = [item[2] for item in selected]
        write(plan_path, plan)
        final_reviews = []
        stage("finalize", 70)
        for candidate, rough, initial_review in zip(plan["candidates"], roughs, latest_reviews):
            result = dict(candidate_id=candidate["candidate_id"], candidate_index=candidate["candidate_index"],
                          plan_revision=candidate["revision"], status="awaiting_human_approval", rough_cut=video_artifact(rough))
            captions = rough.parent/"captions.ass"
            # Music and end card are optional: a job with neither still delivers a captioned body.
            library_uri = job["input"].get("bgm_library_uri")
            music = choose_music(local_path(library_uri, job_path.parent), candidate) if library_uri else None
            end_card_asset = job["input"].get("end_card")
            end_card = local_path(end_card_asset["uri"], job_path.parent) if end_card_asset else None
            caption_feedback = initial_review['issues']
            for caption_attempt in range(2):
                cues = generate_captions(ai, candidate, rough, feedback=caption_feedback)
                write(rough.parent/"captions.json", cues)
                captions_ass(cues, candidate, footage, captions, job["delivery"])
                final = finalize(rough, captions, music, end_card, job["delivery"])
                final_review, safe_area = review(ai, candidate, final, rough.parent/"final-frames", maximum, final=True, cues=cues)
                write(rough.parent/f"final-review-attempt-{caption_attempt+1}.json", final_review)
                if final_review["decision"] == "pass":
                    break
                caption_feedback = final_review["issues"]
                if caption_attempt == 0:
                    shutil.copy2(final, rough.parent/"rejected-final-attempt-1.mp4")
                    write(rough.parent/"rejected-captions-attempt-1.json", cues)
            final_reviews.append(final_review)
            end_artifact = video_artifact(rough.parent/"end-card.mp4") if end_card else None
            body_duration = video_artifact(rough)["duration_seconds"]
            qc = technical_qc(final, body_duration+(end_artifact["duration_seconds"] if end_artifact else 0),
                              job["delivery"], expect_audio=music is not None, expect_end_card=end_card is not None)
            qc["caption_safe_area"] = safe_area
            if not safe_area:
                qc["errors"].append("Final visual review did not confirm caption safe area")
                qc["passed"] = False
            write(rough.parent/"technical_qc.json", qc)
            result.update(final_video=video_artifact(final), technical_qc=qc,
                          captions=dict(sidecar_uri=captions.as_uri(), format="ass", language="en", cue_count=len(cues),
                                        burned_in=True, safe_area_check_passed=safe_area))
            if music:
                result["bgm"] = dict(source_uri=music.as_uri(), track_name=music.name, instrumental=True, source_in_seconds=0,
                                     duration_seconds=body_duration, gain_db=0, fade_in_seconds=.4, fade_out_seconds=.8, beat_aligned=False)
            if end_artifact:
                result["end_card"] = dict(source_uri=end_card.as_uri(), normalized_uri=end_artifact["uri"],
                                          duration_seconds=end_artifact["duration_seconds"], own_audio_preserved=True, appended=True)
            if initial_review['decision'] != 'pass':
                write(rough.parent/'unresolved-rough-review.json', initial_review)
                final_review.update(decision='human_review',next_action='request_human_review',can_recut=False)
                final_review['summary'] = (final_review['summary'] + ' Preview only: selected rough cut still has unresolved review issues; see unresolved-rough-review.json.')[:2000]
            if delivery_approved(initial_review, final_review, qc, job['delivery']['human_approval']):
                result["status"] = "completed"
            manifest["candidates"][candidate["candidate_index"]-1] = result
        done("finalize")
        stage("quality_control", 95)
        review_path = directory / "review_report.json"
        write(review_path, report(job["job_id"], ai, final_reviews))
        manifest["supporting_artifacts"]["review_report_uri"] = review_path.as_uri()
        for result, entry in zip(manifest["candidates"], final_reviews):
            result["review"] = dict(review_id=entry["review_id"], decision=entry["decision"],
                                    overall_score=entry["overall_score"], report_uri=review_path.as_uri())
        done("quality_control")
        complete = sum(c["status"] == "completed" for c in manifest["candidates"])
        manifest["job_status"] = "completed" if complete == len(manifest["candidates"]) else "partially_completed" if complete else "awaiting_human_approval"
        manifest["progress_percent"] = 100
        manifest["updated_at"] = now()
        validate(manifest, "output_manifest")
        write(manifest_path, manifest)
        if manifest_path != directory/"output_manifest.json":
            write(directory/"output_manifest.json", manifest)
        print(json.dumps(dict(type="result", status=manifest["job_status"], manifest=str(manifest_path))), flush=True)
        return 0
    except Exception as exc:
        manifest["job_status"] = "failed"
        manifest["stage_status"][current_stage] = {"status": "failed"}
        manifest["error"] = dict(code="V2_PIPELINE_FAILED", message=f"{type(exc).__name__}: {str(exc)[:1600]}", retryable=False, stage=current_stage)
        write(manifest_path, manifest)
        print(json.dumps(dict(type="error", stage=current_stage, message=manifest["error"]["message"]), ensure_ascii=False), file=sys.stderr)
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="V2: Analyzer -> Director -> Validator -> Renderer -> Reviewer -> Finalize")
    parser.add_argument("--job", required=True)
    parser.add_argument("--resume", action="store_true", help="恢复分析或首次方案生成失败的任务，校验源文件后复用镜头缓存")
    parser.add_argument("--manifest")
    parser.add_argument("--footage-index", help="Explicitly reuse an audited Footage Index for identical source files")
    parser.add_argument("--edit-plan", help="Use an existing validated V2 plan; no heuristic plan conversion")
    parser.add_argument("--provider", default="auto", choices=["auto", "minimax", "openai"])
    parser.add_argument("--model")
    parser.add_argument("--max-recut-attempts", type=int, default=2, choices=[0, 1, 2])
    return execute(parser.parse_args(argv))
