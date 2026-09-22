import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'services'/'video-worker'))
from auto_cut import prepare_job
from tests.test_director import make_footage_index,make_payload,build_plan
from validator import ValidatorConfig,validate_edit_plan,all_candidates_valid
from v2.product import identify_product
from v2.ai import AI, normalize_numbers
from v2.pipeline import can_resume_initial_failure, review_rank, delivery_approved
from v2.common import read


class GenericProductTests(unittest.TestCase):
    def test_full_preview_is_not_approved_with_unresolved_rough_or_qc_failure(self):
        good=dict(decision='pass');bad=dict(decision='human_review')
        self.assertTrue(delivery_approved(good,good,dict(passed=True),False))
        self.assertFalse(delivery_approved(bad,good,dict(passed=True),False))
        self.assertFalse(delivery_approved(good,bad,dict(passed=True),False))
        self.assertFalse(delivery_approved(good,good,dict(passed=False),False))
        self.assertFalse(delivery_approved(good,good,dict(passed=True),True))

    def test_best_preview_prefers_fewer_serious_defects_not_last_revision(self):
        clean=dict(decision='human_review',overall_score=70,issues=[],hard_checks={'no_black_frames':True})
        worse=dict(decision='human_review',overall_score=85,issues=[dict(severity='major')],hard_checks={'no_black_frames':True})
        self.assertGreater(review_rank(clean),review_rank(worse))
        passed={**clean,'decision':'pass','overall_score':75}
        self.assertGreater(review_rank(passed),review_rank(clean))

    @patch('v2.review.sample_frames',return_value=[])
    def test_single_product_rule_does_not_override_real_repetition_failure(self,_):
        from v2.review import review
        class FakeAI:
            def ask(self,prompt,*args):
                self.prompt=prompt
                return dict(overall_score=90,scores={},hard_checks={'no_duplicate_shots':False},
                    summary='An identical shot replays',issues=[dict(category='repetition',severity='major',message='Exact footage replays')],caption_safe_area=True)
        ai=FakeAI()
        result,_=review(ai,dict(candidate_id='candidate-1',candidate_index=1,revision=0),Path('video.mp4'),Path('frames'),2)
        self.assertIn('not product identity',ai.prompt)
        self.assertEqual(result['decision'],'revise')

    def test_product_tone_uses_edit_plan_contract(self):
        class CaptureAI:
            def ask(self, prompt, schema, images):
                self.schema = schema
                return dict(single_product=True,confidence=.9)
        ai=CaptureAI()
        identify_product(ai,dict(sources=[],shots=[]),ROOT)
        tone=read(ROOT/'schemas'/'edit_plan.schema.json')['$defs']['candidate']['properties']['caption_direction']['properties']['tone']
        self.assertEqual(ai.schema['properties']['caption_tone'],tone)
        from jsonschema import Draft202012Validator
        failing_tone='Playful and festive, highlighting the fun interactive 3D illusion for kids, pets, and trick-or-treaters.'
        self.assertFalse(Draft202012Validator(tone).is_valid(failing_tone))
        self.assertTrue(Draft202012Validator(tone).is_valid('Playful and festive'))

    def test_resume_only_before_rendering(self):
        manifest=dict(job_status='failed',error=dict(stage='plan'),stage_status=dict(rough_cut=dict(status='pending')))
        self.assertTrue(can_resume_initial_failure(manifest))
        for state in ['running','completed','failed']:
            manifest['stage_status']['rough_cut']['status']=state
            self.assertFalse(can_resume_initial_failure(manifest))
        manifest['error']['stage']='analyze'
        self.assertTrue(can_resume_initial_failure(manifest))
        manifest['job_status']='completed'
        self.assertFalse(can_resume_initial_failure(manifest))

    def test_invalid_ai_schema_gets_one_correction(self):
        ai=AI.__new__(AI)
        with patch.object(ai,'_ask_once',side_effect=[ValueError('invalid category'),{'ok':True}]) as request:
            self.assertEqual(ai.ask('review',{},()),{'ok':True})
            self.assertEqual(request.call_count,2)
            self.assertIn('invalid category',request.call_args.args[0])

    def test_persistent_invalid_ai_schema_stops(self):
        ai=AI.__new__(AI)
        with patch.object(ai,'_ask_once',side_effect=ValueError('invalid category')) as request:
            with self.assertRaises(ValueError):ai.ask('review',{})
            self.assertEqual(request.call_count,2)

    def test_numeric_format_normalization_does_not_accept_units_or_nan(self):
        self.assertEqual(normalize_numbers('0.1',{'type':'number'}),.1)
        self.assertEqual(normalize_numbers('15s',{'type':'number'}),'15s')
        self.assertEqual(normalize_numbers('NaN',{'type':'number'}),'NaN')
        self.assertEqual(normalize_numbers('0.1',{'type':'string'}),'0.1')
        self.assertEqual(normalize_numbers(['0.0','0.1'],{'type':'array','prefixItems':[{'type':'number'},{'type':'number'}]}),[0,.1])
    def test_decor_without_wearing_or_customization_is_valid(self):
        footage=make_footage_index()
        roles=['product_display','product_detail','usage_result','product_detail','product_display']
        for shot in footage['shots']:
            shot['semantic_tags']=list(set(roles))
            shot['description']='evidence for '+shot['shot_id']+' decorative object on a shelf'
        payload=make_payload(footage)
        payload['candidates']=payload['candidates'][:1]
        for clip,role in zip(payload['candidates'][0]['timeline'],roles):clip['role']=role
        plan=build_plan(footage,payload)
        self.assertTrue(all_candidates_valid(validate_edit_plan(plan,footage,ValidatorConfig(generic_product=True))))
        self.assertFalse(all_candidates_valid(validate_edit_plan(plan,footage)))

    def test_generic_mode_still_rejects_invalid_source_times(self):
        footage=make_footage_index();plan=build_plan(footage,make_payload(footage))
        plan['candidates'][0]['timeline'][0]['source_out_seconds']=100
        self.assertFalse(all_candidates_valid(validate_edit_plan(plan,footage,ValidatorConfig(generic_product=True))))

    def test_folder_entry_creates_one_auto_product_and_unique_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);folder=root/'衣服素材';folder.mkdir()
            (folder/'front.MP4').write_bytes(b'input')
            (folder/'side.mov').write_bytes(b'input')
            (folder/'notes.txt').write_text('ignore')
            end=root/'end.mp4';end.write_bytes(b'end')
            music=root/'music';music.mkdir()
            settings=dict(end_card=str(end),bgm_library=str(music))
            first=prepare_job(folder,settings);second=prepare_job(folder,settings)
            self.assertEqual(first['brief']['candidate_count'],1)
            self.assertEqual(first['brief']['category'],'auto')
            self.assertEqual(len(first['input']['source_videos']),2)
            self.assertNotEqual(first['delivery']['output_uri'],second['delivery']['output_uri'])

    def test_mixed_products_and_uncertain_identification_stop(self):
        class FakeAI:
            def __init__(self,result):self.result=result
            def ask(self,*args):return self.result
        for result in [dict(single_product=False,confidence=.9),dict(single_product=True,confidence=.3)]:
            with self.assertRaises(ValueError):
                identify_product(FakeAI(result),dict(sources=[],shots=[]),ROOT)

if __name__=='__main__':unittest.main()
