"""One product folder in, one reviewed product advertisement out."""
import argparse
import json
import shutil
import sys
import uuid
import hashlib
import copy
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from dotenv import load_dotenv
load_dotenv(ROOT / '.env', override=False)
sys.path.insert(0, str(ROOT / "services" / "video-worker"))
from v2.common import now, read, validate, write, local_path
from v2.pipeline import main as pipeline_main


def prepare_job(folder, settings, output_root=None):
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("素材文件夹不存在")
    videos = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {'.mp4','.mov','.mkv','.avi','.webm','.m4v'})
    if not videos:
        raise ValueError("文件夹中没有视频，请选择直接存放原视频的文件夹")
    # 片尾和音乐都是可选项：不配置就只出带字幕的正文，配置了就必须真实存在。
    end_card = local_path(settings['end_card'], ROOT) if settings.get('end_card') else None
    music = local_path(settings['bgm_library'], ROOT) if settings.get('bgm_library') else None
    if end_card is not None and not end_card.is_file():
        raise ValueError(f'auto-cut.settings.json 里的品牌片尾不存在：{end_card}')
    if music is not None and not music.exists():
        raise ValueError(f'auto-cut.settings.json 里的音乐库不存在：{music}')
    run_id = datetime.now().strftime('product-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
    output = (Path(output_root).resolve() if output_root else folder.parent / '自动剪辑成片') / run_id
    source_input = dict(source_videos=[dict(asset_id=f'source-{i:03}',uri=p.as_uri()) for i,p in enumerate(videos,1)])
    if end_card is not None:
        source_input['end_card'] = dict(asset_id='end-card', uri=end_card.as_uri())
    if music is not None:
        source_input['bgm_library_uri'] = music.as_uri()
    job = dict(schema_version='1.0.0', job_id=run_id, created_at=now(), status='queued',
        input=source_input,
        brief=dict(product_name='auto',category='auto',objective='Create one coherent advertisement for the single product in this footage batch',
                   language='en',candidate_count=1,target_body_duration_seconds=settings.get('body_seconds',15),
                   max_recut_attempts=2,requirements=['Show the product clearly','Use only visible features and supported claims','Build a coherent story from available footage']),
        delivery=dict(output_uri=output.as_uri(),container='mp4',width=settings.get('width',1080),height=settings.get('height',1920),
                      fps=30,human_approval=False))
    validate(job,'job')
    return job


def prepare_retry(request, output_root=None):
    """New delivery, same verified source files; preserve all previous results."""
    old=read(request)
    directory=local_path(old['delivery']['output_uri'],request.parent)
    if read(directory/'job.json') != old:
        raise ValueError('任务配置与已有 job.json 不一致')
    manifest=read(directory/'output_manifest.json')
    if manifest['job_status'] not in ('failed','awaiting_human_approval','partially_completed'):
        raise ValueError('只可重试失败或待检查任务，不能重试正在运行或已通过的任务')
    index=read(directory/'footage_index.json')
    validate(index,'footage_index')
    if len(index['sources']) != len(old['input']['source_videos']):
        raise ValueError('缓存素材数量与原任务不一致')
    # Resolve all evidence before placing this index beside a new request file.
    def absolute(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if (key=='uri' or key.endswith('_uri')) and isinstance(item,str):
                    value[key]=local_path(item,directory).as_uri()
                elif key.endswith('_uris') and isinstance(item,list):
                    value[key]=[local_path(uri,directory).as_uri() for uri in item]
                else:absolute(item)
        elif isinstance(value,list):
            for item in value:absolute(item)
    absolute(index)
    cached_sources={local_path(s['uri']):s for s in index['sources']}
    for i,source in enumerate(old['input']['source_videos']):
        original=local_path(source['uri'],request.parent)
        staged=directory/'sources'/f'source-{i:03}{original.suffix}'
        cached=cached_sources.get(staged.resolve())
        if cached is None:
            raise ValueError('缓存源文件与原任务不对应')
        with original.open('rb') as a,local_path(cached['uri']).open('rb') as b:
            if hashlib.file_digest(a,'sha256').digest()!=hashlib.file_digest(b,'sha256').digest():
                raise ValueError('原素材已改变，请重新分析')
        cached['uri']=original.as_uri()
    job=copy.deepcopy(old)
    job['job_id']=datetime.now().strftime('product-%Y%m%d-%H%M%S-')+uuid.uuid4().hex[:6]
    job['created_at']=now();job['status']='queued'
    job['delivery']['output_uri']=((Path(output_root).resolve() if output_root else directory.parent)/job['job_id']).as_uri()
    index['job_id']=job['job_id']
    validate(job,'job');validate(index,'footage_index')
    return job,index


def main(argv=None):
    parser=argparse.ArgumentParser(description='一个产品素材文件夹 → 自动识别产品 → 一条广告成片')
    parser.add_argument('input',nargs='?')
    parser.add_argument('--settings',type=Path,help='默认读取项目根目录的 auto-cut.settings.json；该文件不存在时使用内置默认值')
    parser.add_argument('--output-root',type=Path)
    parser.add_argument('--prepare-only',action='store_true',help='仅检查素材并生成任务，不调用 AI')
    parser.add_argument('--resume',type=Path,help='恢复分析或首次方案生成失败的任务，填写之前打印的任务配置路径')
    parser.add_argument('--retry',type=Path,help='在新目录重试待检查任务，校验源文件并复用已完成分析')
    args=parser.parse_args(argv)
    try:
        cached_index=None
        if args.resume and args.retry:
            raise ValueError('--resume 与 --retry 不能同时使用')
        if args.retry:
            job,index=prepare_retry(args.retry.resolve(),args.output_root)
            request=ROOT/'data'/'auto-requests'/(job['job_id']+'.json')
            cached_index=request.with_name(job['job_id']+'-footage.json')
            write(request,job);write(cached_index,index)
        elif args.resume:
            request=args.resume.resolve()
            job=read(request)
            if job['brief']['candidate_count']!=1:
                raise ValueError('文件夹入口只恢复单成片任务')
        else:
            folder=args.input or input('请输入或拖入一个产品的素材文件夹路径：').strip().strip('"')
            settings_path=args.settings or ROOT/'auto-cut.settings.json'
            settings=read(settings_path) if (args.settings or settings_path.is_file()) else {}
            job=prepare_job(folder,settings,args.output_root)
            request=ROOT/'data'/'auto-requests'/(job['job_id']+'.json')
            write(request,job)
        output=local_path(job['delivery']['output_uri'])
        print(f"素材：{len(job['input']['source_videos'])} 段；默认生成一条英文广告；正文约 {job['brief']['target_body_duration_seconds']} 秒。",flush=True)
        print('背景音乐：'+('已配置音乐库' if job['input'].get('bgm_library_uri') else '未配置，正文无声')
              +'；品牌片尾：'+('已配置' if job['input'].get('end_card') else '未配置，只输出正文'),flush=True)
        print(f'任务配置：{request}\n输出目录：{output}',flush=True)
        if args.prepare_only:
            return 0
        result=pipeline_main(['--job',str(request)]+(['--resume'] if args.resume else [])+
                             (['--footage-index',str(cached_index)] if cached_index else []))
        if result:
            return result
        manifest=read(output/'output_manifest.json')
        if manifest['job_status']=='completed':
            final=output/'成品.mp4'
            shutil.copy2(local_path(manifest['candidates'][0]['final_video']['uri']),final)
            print(f'完成：{final}',flush=True)
        else:
            candidate=manifest['candidates'][0]
            artifact=candidate.get('final_video') or candidate.get('rough_cut')
            if artifact:
                preview=output/'待检查预览.mp4'
                shutil.copy2(local_path(artifact['uri']),preview)
                print(f'已保留待检查预览（未通过审核）：{preview}',flush=True)
            print(f'需要人工检查，未标记为成品。请查看：{output / "output_manifest.json"}',flush=True)
        return 0
    except (OSError,ValueError,RuntimeError) as exc:
        print(f'任务未完成：{exc}',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
