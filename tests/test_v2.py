import copy
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services' / 'video-worker'))
from v2.common import probe, run, video_artifact
from v2.render import captions_ass, check_supported, finalize, rough_cut, technical_qc, sample_frames
from v2.review import review
from v2.ai import AI


class V2Tests(unittest.TestCase):
    def test_streamed_tool_arguments_are_reassembled_and_truncation_rejected(self):
        def chunk(arguments, name=None, finish=None):
            call=SimpleNamespace(index=0,function=SimpleNamespace(name=name,arguments=arguments))
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
                delta=SimpleNamespace(content=None,tool_calls=[call]))])
        chunks=[chunk('{"ok":','submit_result'),chunk('true}',finish='tool_calls')]
        requests=[]
        def create(**kwargs):
            requests.append(kwargs)
            return iter(chunks)
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        client.with_options=lambda **kwargs:client
        ai=AI.__new__(AI)
        ai.provider=SimpleNamespace(_client=client,name='minimax')
        ai.model='MiniMax-M3'
        schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
        self.assertEqual(ai.ask('test',schema),{'ok':True})
        self.assertEqual(requests[0]['extra_body']['thinking']['type'],'disabled')
        chunks[-1].choices[0].finish_reason='length'
        with self.assertRaisesRegex(ValueError,'truncated'):
            ai.ask('test',schema)

    def test_unsupported_edits_are_not_silently_rendered(self):
        for clip in [dict(speed=2, transition={'type':'cut','duration_seconds':0},crop_strategy='fit'),
                     dict(transition={'type':'crossfade','duration_seconds':.2},crop_strategy='fit'),
                     dict(transition={'type':'cut','duration_seconds':0},crop_strategy='track_subject')]:
            with self.assertRaises(ValueError):
                check_supported({'timeline':[clip]})

    def test_real_ffmpeg_cuts_captions_music_and_trailer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'source.mp4'
            end=root/'end.mp4'
            music=root/'music.wav'
            run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=360x640:rate=30',
                 '-t','4','-c:v','libx264','-threads','2',source])
            run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=white:size=360x640:rate=30',
                 '-f','lavfi','-i','sine=frequency=700:sample_rate=48000','-t','1','-c:v','libx264','-c:a','aac','-threads','2',end])
            run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=220:sample_rate=48000','-t','4',music])
            clips=[dict(sequence=i+1,source_asset_id='source',shot_id=f'shot-{i}',source_in_seconds=i*2,
                        duration_seconds=1.5,timeline_in_seconds=i*1.5,crop_strategy='fit',
                        transition=dict(type='cut',duration_seconds=0)) for i in range(2)]
            candidate=dict(timeline=clips,total_body_duration_seconds=3)
            footage=dict(sources=[dict(asset_id='source',uri=source.as_uri())],
                         shots=[dict(shot_id=f'shot-{i}',composition=dict(safe_caption_regions=['top'])) for i in range(2)])
            delivery=dict(width=360,height=640,fps=30)
            rough=rough_cut(candidate,footage,root/'render',delivery)
            captions=rough.parent/'captions.ass'
            captions_ass([dict(sequence=1,text='Keep memories close'),dict(sequence=2,text='Made for you')],candidate,footage,captions,delivery)
            final=finalize(rough,captions,music,end,delivery)
            qc=technical_qc(final,4,delivery)
            self.assertTrue(qc['passed'],qc)
            self.assertAlmostEqual(video_artifact(final)['duration_seconds'],4,delta=.15)
            self.assertGreater(final.stat().st_size,1000)
            frames=sample_frames(final,root/'中文审核抽帧')
            self.assertGreaterEqual(len(frames),8)
            self.assertTrue(all(path.stat().st_size > 0 for _,path in frames))
            with self.assertRaises(ValueError):
                captions_ass([dict(sequence=1,text='{\\pos(0,0)}bad'),dict(sequence=2,text='Good')],candidate,footage,captions,delivery)

    def test_delivery_without_music_or_end_card(self):
        """Zero-configuration run: no BGM library, no brand end card, still a valid MP4."""
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'source.mp4'
            run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=360x640:rate=30',
                 '-t','4','-c:v','libx264','-threads','2',source])
            clips=[dict(sequence=i+1,source_asset_id='source',shot_id=f'shot-{i}',source_in_seconds=i*2,
                        duration_seconds=1.5,timeline_in_seconds=i*1.5,crop_strategy='fit',
                        transition=dict(type='cut',duration_seconds=0)) for i in range(2)]
            candidate=dict(timeline=clips,total_body_duration_seconds=3)
            footage=dict(sources=[dict(asset_id='source',uri=source.as_uri())],
                         shots=[dict(shot_id=f'shot-{i}',composition=dict(safe_caption_regions=['top'])) for i in range(2)])
            delivery=dict(width=360,height=640,fps=30)
            rough=rough_cut(candidate,footage,root/'render',delivery)
            captions=rough.parent/'captions.ass'
            captions_ass([dict(sequence=1,text='Keep memories close'),dict(sequence=2,text='Made for you')],candidate,footage,captions,delivery)
            final=finalize(rough,captions,None,None,delivery)
            qc=technical_qc(final,3,delivery,expect_audio=False,expect_end_card=False)
            self.assertTrue(qc['passed'],qc)
            self.assertFalse(qc['end_card_present'])
            self.assertAlmostEqual(video_artifact(final)['duration_seconds'],3,delta=.15)
            # A silent stereo track is kept so the same body still concatenates with an end card that has audio.
            self.assertTrue(any(stream['codec_type']=='audio' for stream in probe(final)['streams']))
            end=root/'end.mp4'
            run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=white:size=360x640:rate=30','-f','lavfi','-i',
                 'sine=frequency=700:sample_rate=48000','-t','1','-c:v','libx264','-c:a','aac','-threads','2',end])
            with_trailer=finalize(rough,captions,None,end,delivery)
            trailer_qc=technical_qc(with_trailer,4,delivery,expect_audio=False,expect_end_card=True)
            self.assertTrue(trailer_qc['passed'],trailer_qc)
            self.assertAlmostEqual(video_artifact(with_trailer)['duration_seconds'],4,delta=.15)

    @patch('v2.review.sample_frames',return_value=[])
    def test_review_cannot_schedule_a_third_recut(self,_):
        class FakeAI:
            def ask(self,*args):
                return dict(overall_score=50,scores={},hard_checks={'no_broken_actions':False},
                            summary='Action discontinuity',issues=[],change_requests=[],caption_safe_area=True)
        candidate=dict(candidate_id='candidate-1',candidate_index=1,revision=2)
        result,_=review(FakeAI(),candidate,Path('video.mp4'),Path('frames'),2)
        self.assertEqual(result['decision'],'human_review')
        self.assertFalse(result['can_recut'])
        candidate['revision']=1
        result,_=review(FakeAI(),candidate,Path('video.mp4'),Path('frames'),2)
        self.assertEqual(result['decision'],'revise')
        self.assertTrue(result['can_recut'])

if __name__ == '__main__':
    unittest.main()
