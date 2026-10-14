"""Portable coordinator tools tested with fake gws and synthetic Sheets data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import coordinator_data as data
import coordinator_setup as setup
import install
import coordinator_prompts as prompts
import serp_urls


class CoordinatorTests(unittest.TestCase):
    def prepare(self,pair,agreement,foreman,scout,input_encoding=None):
        selection={'schema_version':1,'revision':1,'user_command':'owner explicitly selected these test instructions',
                   'instructions':{'foreman':prompts.instruction(foreman),'scout':prompts.instruction(scout)}}
        return data.prepare(pair,agreement,selection,input_encoding=input_encoding)

    def agreement(self):
        return {'schema_version': 1, 'approved': True, 'approval_note': 'owner chose verdict plus JSON',
                'sheet_title': "Оценки O'Brien", 'input_columns': {'id':'A','query':'B','left':'G','right':'H'},
                'outputs': {'P':'/winner','V':''}, 'output_schema': {'type':'object','properties': {
                    'winner': {'type':'string'}, 'left': {'type':'object','properties': {'score': {'type':'integer'}}},
                    'reason': {'type':'string'}, 'optional': {'type':['string','null']}}}}

    def qw_agreement(self):
        a=self.agreement()
        a['output_schema']=install.read_json(Path(__file__).resolve().parents[1]/'schemas/default-final.schema.json')
        a['projection']={'adapter':'standard-qw-v1'}
        a['outputs']={'Q':'/status','R':{'literal':None},'S':'/winner_label','T':'/left_score',
                      'U':'/right_score','V':{'literal':None},'W':'/comment'}
        return a

    def standard_output(self, winner='left', left=5, right=3):
        aspects={name: {'score':3,'conclusion':'fixture evidence'} for name in
                 ('intent_coverage','usefulness','ordering','shortcomings','extra_blocks')}
        return {'left_urls':[],'right_urls':[],'left_serp':aspects,'right_serp':aspects,
                'left_score':left,'right_score':right,'winner':winner,'winner_reason':'=reason text'}

    def pair(self):
        return {'id':'row-stable-1','query':'map','left_top5':self.ranking(),'right_top5':self.ranking()}

    def ranking(self):
        return [{'position':i,'url':'https://example.org/'+str(i)} for i in range(1,6)]

    def html(self, ranking=None, prefix='', suffix=''):
        cards=''.join('<li class="serp-item"><div class="Organic"><a class="OrganicTitle-Link" href="'+v['url']+'">title</a></div></li>' for v in (ranking or self.ranking()))
        return '<html><body>'+prefix+cards+suffix+'</body></html>'

    def test_url_only_wire_strips_source_and_preserves_source_fingerprint(self):
        raw={'id':'raw','query':'q','left_html':self.html(),'right_html':self.html(),
             'left_source_url':'https://source.example/left','right_source_url':'https://source.example/right'}
        prepared=self.prepare(raw,self.agreement(),'f','s')
        wire=prepared['publication']['payload'][0]['input_data']
        self.assertEqual(wire['contract'],'sbs-instructions-v2')
        self.assertEqual(set(wire['pair']),{'id','query','left_top5','right_top5','captcha_grant'})
        self.assertEqual(wire['pair']['left_top5'],self.ranking())
        self.assertEqual(prepared['input_fingerprint'],data.fingerprint(raw))
        changed=dict(raw,left_html=raw['left_html'].replace('title','different title'))
        again=self.prepare(changed,self.agreement(),'f','s')
        self.assertEqual(wire['pair'],again['publication']['payload'][0]['input_data']['pair'])
        self.assertNotEqual(prepared['input_fingerprint'],again['input_fingerprint'])
        self.assertNotEqual(prepared['publication']['item_id'],again['publication']['item_id'])
        self.assertLess(prepared['transport_preflight']['task_bytes'],3000)

    def test_raw_html_absolute_urls_need_no_source_metadata(self):
        pair=dict(self.pair(),left_html=self.html(),right_html=self.html())
        self.prepare(pair,self.agreement(),'f','s')
        with self.assertRaisesRegex(ValueError,'does not match'):
            self.prepare(dict(pair,left_top5=[dict(v,url='https://other.example/') for v in self.ranking()]),self.agreement(),'f','s')

    def test_gzip_is_explicitly_rejected(self):
        for encoding in ('gzip-base64','raw','gzip','',False):
            with self.subTest(encoding=encoding),self.assertRaisesRegex(ValueError,'Legacy HTML/gzip'):
                self.prepare(self.pair(),self.agreement(),'f','s',input_encoding=encoding)

    def test_parser_excludes_ads_navigation_and_known_video_special(self):
        ad='<li class="serp-item serp-item_type_ad"><div class="Organic"><a class="OrganicTitle-Link" href="https://ads.example/">ad</a></div></li>'
        tracked='<li class="serp-item"><div class="Organic"><a class="OrganicTitle-Link" href="https://yabs.yandex.ru/count">ad</a></div></li>'
        video='<div class="VideoSnippet"><a class="OrganicTitle-Link" href="https://video.example/">video</a></div>'
        html=self.html(prefix='<a href="https://navigation.example/">navigation</a>'+ad+tracked+video)
        self.assertEqual(serp_urls.extract_top5(html),self.ranking())

    def test_known_empty_and_fewer_organic_results_are_not_padded(self):
        empty='<html><body><div class="EmptySearchResults"><a href="https://recommendation.example/">recommendation</a></div></body></html>'
        self.assertEqual(serp_urls.extract_top5(empty),[])
        for count in (1,4):
            self.assertEqual(serp_urls.extract_top5(self.html(self.ranking()[:count])),self.ranking()[:count])
        prepared=self.prepare(dict(self.pair(),left_top5=[],right_top5=[]),self.agreement(),'f','s')
        self.assertEqual(prepared['publication']['payload'][0]['input_data']['pair']['left_top5'],[])
        ambiguous=self.html(prefix='<div class="EmptySearchResults"></div>')
        with self.assertRaisesRegex(ValueError,'both empty-search'):
            serp_urls.extract_top5(ambiguous)

    def test_parser_preserves_duplicate_urls_and_rank(self):
        rank=self.ranking();rank[1]['url']=rank[0]['url']
        self.assertEqual(serp_urls.extract_top5(self.html(rank)),rank)
        pair=dict(self.pair(),left_top5=rank,right_top5=rank)
        wire=self.prepare(pair,self.agreement(),'f','s')['publication']['payload'][0]['input_data']['pair']
        self.assertEqual(wire['left_top5'],rank)

    def test_parser_rejects_unknown_malformed_ambiguous_or_invalid(self):
        bad=['<html><a href="https://example.org/">random</a></html>',self.html()[:-7],
             self.html().replace('</a>','',1),self.html().replace('href="https://example.org/1"','href="javascript:alert(1)"',1),
             self.html().replace('</a>','</a><a class="OrganicTitle-Link" href="https://ambiguous.example/">other</a>',1),
             self.html().replace('class="Organic"','class="Unknown"',1)]
        for html in bad:
            with self.subTest(html=html[:50]),self.assertRaises(serp_urls.SerpParseError):
                serp_urls.extract_top5(html)

    def test_relative_urls_require_local_source(self):
        html=self.html().replace('https://example.org/','/result/')
        with self.assertRaisesRegex(ValueError,'source URL'):
            serp_urls.extract_top5(html)
        expected=[{'position':i,'url':'https://source.example/result/'+str(i)} for i in range(1,6)]
        self.assertEqual(serp_urls.extract_top5(html,'https://source.example/saved'),expected)

    def test_supplied_rankings_are_strict(self):
        bad=[self.ranking()+[self.ranking()[0]],['https://example.org/']*5,
             [dict(v,title='forbidden') for v in self.ranking()],
             [dict(v,position=True) for v in self.ranking()],
             [dict(v,url='file:///private') for v in self.ranking()],
             list(reversed(self.ranking()))]
        for rank in bad:
            with self.subTest(rank=rank),self.assertRaises(ValueError):
                self.prepare(dict(self.pair(),left_top5=rank),self.agreement(),'f','s')

    def test_original_saved_serp_regression_rows_three_through_ten(self):
        workspace=Path(__file__).resolve().parents[4]
        htmlroot=workspace/'outputs/yatorrent-sbs-test/html'
        oldroot=workspace/'outputs/sbs-eight-20261013T220825Z-1bb906'
        if not htmlroot.exists():self.skipTest('Real saved SERPs are local regression artifacts')
        for row in range(3,11):
            old=json.loads((oldroot/('row-'+str(row)+'.wire.json')).read_text())['payload'][0]['input_data']
            for side in ('left','right'):
                with self.subTest(row=row,side=side):
                    expected=[{'position':v['position'],'url':v['url']} for v in old['serps'][side]['top5']]
                    self.assertEqual(serp_urls.extract_top5((htmlroot/('row-'+str(row))/(side+'.html')).read_text()),expected)
        raw=json.loads((workspace/'work/r8-feedback/row10-pair.json').read_text())
        self.assertEqual(len(serp_urls.extract_top5(raw['left_html'])),5)
        # Current row10 right is a recognized empty SERP. Never substitute
        # recommendations or historical evidence to pad it to five URLs.
        self.assertIn('EmptySearchResults',raw['right_html'])
        prepared=self.prepare(raw,self.agreement(),'f','s')
        self.assertEqual(prepared['publication']['payload'][0]['input_data']['pair']['right_top5'],[])
        self.assertEqual(prepared['input_fingerprint'],data.fingerprint(raw))

    def test_format_requires_real_agreement(self):
        a=self.agreement();a['approved']=False
        with self.assertRaises(ValueError): self.prepare(self.pair(),a,'foreman','scout')

    def test_one_pair_one_wire_task_reusable_identity(self):
        a=self.prepare(self.pair(),self.agreement(),'foreman','scout')
        b=self.prepare(self.pair(),self.agreement(),'foreman','scout')
        self.assertEqual(a,b)
        self.assertEqual(set(a['publication']),{'item_id','task_type','worker_version','result_pool','payload'})
        self.assertEqual(len(a['publication']['payload']),1)
        self.assertEqual(a['publication']['payload'][0]['id'],a['publication']['item_id'])
        self.assertNotIn('model',a['publication']['payload'][0])


    def test_transport_limits_and_non_bmp_model_units(self):
        pair=self.pair();pair['query']='😀'
        expected=len(data.MODEL_INPUT_PREFIX+json.dumps(pair,ensure_ascii=False,separators=(',',':')))+1
        self.assertEqual(data.transport_preflight(pair,{}, {})['model_text_utf16_units'],expected)
        with self.assertRaisesRegex(ValueError,'native YaTorrent'):
            data.transport_preflight(self.pair(),{'large':'x'*data.MAX_NATIVE_INPUT_BYTES},{})
        with self.assertRaisesRegex(ValueError,'10 MiB'):
            data.transport_preflight(self.pair(),{},{'large':'x'*data.MAX_TASK_BYTES})
        base=data.transport_preflight(self.pair(),{},{})['model_text_utf16_units']
        pair=dict(self.pair(),query=self.pair()['query']+'x'*(data.MAX_MODEL_TEXT_UNITS-base))
        self.assertEqual(data.transport_preflight(pair,{},{})['model_text_utf16_units'],data.MAX_MODEL_TEXT_UNITS)
        with self.assertRaisesRegex(ValueError,'Codex'):
            data.transport_preflight(dict(pair,query=pair['query']+'x'),{},{})



    def test_file_only_result_format_prepares_without_sheets_columns(self):
        a={'schema_version':1,'approved':True,'approval_note':'owner chose local JSON',
           'destination':'local_json','output_schema':{'type':'object'}}
        publication=self.prepare(self.pair(),a,'f','s')['publication']
        self.assertEqual(len(publication['payload']),1)
        with self.assertRaises(ValueError):data.project({},a,1)
        with self.assertRaises(ValueError):self.prepare(self.pair(),dict(a,destination='unknown'),'f','s')

    def test_result_pool_is_bound_to_publication_and_identity(self):
        a=self.agreement()
        default=self.prepare(self.pair(),a,'f','s')['publication']
        alternate=self.prepare(self.pair(),dict(a,result_pool='aim2'),'f','s')['publication']
        self.assertEqual(default['result_pool'],'kron')
        self.assertEqual(alternate['result_pool'],'aim2')
        self.assertNotEqual(default['item_id'],alternate['item_id'])
        with self.assertRaises(ValueError):self.prepare(self.pair(),dict(a,result_pool='invented'),'f','s')

    def test_technical_pool_uses_known_config_and_explicit_agreement(self):
        self.assertEqual(data.select_result_pool(self.agreement(), {'result_pool':'kron'}),'kron')
        self.assertEqual(data.select_result_pool(dict(self.agreement(),result_pool='aim2'), {'result_pool':'kron'}),'aim2')
        self.assertEqual(data.select_result_pool(self.agreement(), {'unknown_vendor_key':'kron'}),'kron')

    def test_changed_input_schema_and_rubric_get_new_ids(self):
        a=self.agreement();base=self.prepare(self.pair(),a,'f','s')['publication']['item_id']
        for pair,agreement,f in [(dict(self.pair(),query='changed'),a,'f'),
                                  (self.pair(),dict(a,output_schema=dict(a['output_schema'],title='new schema')),'f'),
                                  (self.pair(),a,'new-rubric')]:
            self.assertNotEqual(base,self.prepare(pair,agreement,f,'s')['publication']['item_id'])

    def test_decomposed_json_outputs_and_current_row(self):
        a=self.agreement();a['outputs']={'P':'/winner','Q':'/left/score','V':'/reason'}
        r=data.project({'winner':'left','left':{'score':4},'reason':'=untrusted()'},a,17)
        self.assertEqual(r['valueInputOption'],'RAW')
        self.assertEqual(r['data'][0]['range'],"'Оценки O''Brien'!P17")
        self.assertEqual(r['data'][1]['values'],[[4]])
        self.assertEqual(r['data'][2]['values'],[['=untrusted()']])

    def test_full_json_is_not_truncated(self):
        a=self.agreement()
        output={'winner':'tie','reason':'text'}
        self.assertEqual(json.loads(data.project(output,a,3)['data'][1]['values'][0][0]),output)
        with self.assertRaises(ValueError): data.project(dict(output,reason='x'*50000),a,3)

    def test_input_columns_cannot_be_overwritten(self):
        a=self.agreement();a['outputs']['G']=''
        with self.assertRaises(ValueError): data.project({},a,3)

    def test_unrelated_credentials_and_old_verdicts_not_forwarded(self):
        for key in ['credentials','old_verdict','editor_comment']:
            with self.assertRaises(ValueError):
                self.prepare(dict(self.pair(),**{key:'do-not-forward'}),self.agreement(),'f','s')

    def test_json_pointer_arrays_and_escaping(self):
        self.assertEqual(data.pointer({'a/b':[{'~x':9}]},'/a~1b/0/~0x'),9)

    def test_unknown_schema_paths_rejected_before_preparation(self):
        a=self.agreement();a['output_schema']=self.qw_agreement()['output_schema']
        for path in ('/status','/confidence','/translated_winner','/broken','/comment','/winner~2'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.prepare(self.pair(),dict(a,outputs={'Q':path}),'f','s')
        data.schema_pointer(a['output_schema'],'/left_urls/0/aspect_scores/quality')
        data.schema_pointer(a['output_schema'],'/left_serp/ordering/score')
        with self.assertRaises(ValueError): data.schema_pointer(a['output_schema'],'/left_urls/-1/url')

    def test_completed_qw_adapter_all_columns_raw_numbers_and_full_json(self):
        a=self.qw_agreement();output=self.standard_output()
        self.prepare(self.pair(),a,'f','s')
        plan=data.project(output,a,3);cells=[v['values'][0][0] for v in plan['data']]
        self.assertEqual(plan['valueInputOption'],'RAW')
        self.assertEqual(cells[:6],['done','','Yandex Old',5,3,''])
        self.assertIs(type(cells[3]),int);self.assertIs(type(cells[4]),int)
        reason,full_json=cells[6].split('\n',1)
        self.assertEqual(reason,output['winner_reason']);self.assertEqual(json.loads(full_json),output)
        self.assertEqual([v['range'] for v in plan['data']],
                         ["'Оценки O''Brien'!"+c+'3' for c in 'QRSTUVW'])
        self.assertEqual(data.project(self.standard_output('right'),a,3)['data'][2]['values'],[['Yandex New']])
        self.assertEqual(data.project(self.standard_output('tie',3,3),a,3)['data'][2]['values'],[['Ничья']])

    def test_inconclusive_qw_adapter_preserves_evidence_without_invented_scores(self):
        a=self.qw_agreement()
        for output in (self.standard_output(None,None,None),self.standard_output('left',None,3),
                       self.standard_output(None,5,3)):
            with self.subTest(output=output):
                cells=[v['values'][0][0] for v in data.project(output,a,3)['data']]
                self.assertEqual(cells[:6],['inconclusive','','','','',''])
                self.assertEqual(json.loads(cells[6].split('\n',1)[1]),output)

    def test_adapter_schema_is_distinct_from_worker_schema(self):
        a=self.qw_agreement()
        self.assertNotIn('status',a['output_schema']['properties'])
        self.assertIn('status',data.projection_schema(a)['properties'])
        data.validate_agreement(dict(a,projection_schema=data.QW_SCHEMA))
        with self.assertRaises(ValueError): data.validate_agreement(dict(a,projection_schema=a['output_schema']))
        with self.assertRaises(ValueError): data.validate_agreement(dict(a,outputs={'Q':'/confidence'}))
        with self.assertRaises(ValueError): data.validate_agreement(dict(a,projection={'adapter':'unknown'}))

    def test_qw_comment_cell_limit_includes_reason_and_complete_json(self):
        a=self.qw_agreement();output=self.standard_output()
        base=len(data.project_standard(dict(output,winner_reason='x'))['comment'])-2
        output['winner_reason']='x'*((49000-base)//2)
        plan=data.project(output,a,3)
        self.assertLessEqual(len(plan['data'][-1]['values'][0][0]),49000)
        output['winner_reason']+='x'
        with self.assertRaises(ValueError):data.project(output,a,3)

    def test_null_mapping_and_strict_standard_scores(self):
        a=self.agreement();a['outputs']={'P':'/optional','Q':{'literal':None}}
        self.assertEqual([c['values'] for c in data.project({'optional':None},a,3)['data']],[[['']],[['']]])
        for bad in (True,0,6,2.5,'4'):
            with self.subTest(score=bad),self.assertRaises(ValueError):
                data.project(self.standard_output(left=bad),self.qw_agreement(),3)
        with self.assertRaises(ValueError):data.project({'winner':'left'},self.qw_agreement(),3)

    def test_offline_cli_mapping_preflight_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);agreement=root/'agreement.json';receipt=root/'mapping.json'
            agreement.write_text(json.dumps(self.qw_agreement()))
            command=[sys.executable,str(Path(__file__).with_name('coordinator_data.py')),'validate',
                     '--agreement',str(agreement),'--output',str(receipt)]
            success=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(success.returncode,0,success.stderr)
            self.assertTrue(json.loads(receipt.read_text())['agreement_mapping_valid'])
            a=self.qw_agreement();a['outputs']['Q']='/missing';agreement.write_text(json.dumps(a))
            failure=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(failure.returncode,1)
            self.assertTrue(json.loads(receipt.read_text())['agreement_mapping_valid'])

    def test_plan_does_not_install_or_authenticate(self):
        with tempfile.TemporaryDirectory() as t, patch.object(setup,'status') as status, patch.object(install,'run') as run:
            args=type('Args',(),{'root':Path(t).resolve()/'runtime','google_config':Path(t).resolve()/'google','apply':False,'no_google':False})()
            result=setup.configure(args)
            self.assertFalse(result['mutates']);run.assert_not_called();status.assert_not_called()
            self.assertFalse(args.root.exists())

    def test_python39_rejected_before_mutations(self):
        with tempfile.TemporaryDirectory() as t, patch.object(setup.sys,'version_info',(3,9,0)), patch.object(setup.venv.EnvBuilder,'create') as create, patch.object(install,'run') as run:
            args=type('Args',(),{'root':Path(t).resolve()/'runtime','google_config':Path(t).resolve()/'google','apply':True,'no_google':False})()
            with self.assertRaisesRegex(install.SetupError,'Python 3.10'): setup.configure(args)
            create.assert_not_called();run.assert_not_called();self.assertFalse(args.root.exists())

    def test_old_validator_venv_is_preserved_and_recreated(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve();virtual=root/'coordinator-python';virtual.mkdir();(virtual/'old-marker').write_text('preserve')
            args=type('Args',(),{'root':root,'google_config':root/'google','apply':True,'no_google':True})()
            def create(where): where.mkdir()
            def run(cmd,**kw):
                return '[3,9]' if '-c' in [str(x) for x in cmd] else ''
            with patch.object(install,'read_json',return_value={}),patch.object(install,'entry_points'),patch.object(install,'run',side_effect=run),patch.object(setup.venv.EnvBuilder,'create',side_effect=create) as created:
                result=setup.configure(args)
            created.assert_called_once_with(virtual)
            self.assertEqual(len(list((root/'backups').glob('*/old-marker'))),1)
            self.assertFalse((virtual/'old-marker').exists());self.assertEqual(result['sheet_writes'],0)

    def test_setup_auth_uses_neutral_cwd(self):
        with patch.object(install,'run',return_value='{}') as run:
            setup.status('/fake/gws',Path('/private/google'))
            self.assertEqual(run.call_args.kwargs['cwd'],'/')

    def test_auth_readback_handles_old_and_new_cli_without_false_scope_claim(self):
        with patch.object(install,'run',return_value=json.dumps({'auth_method':'oauth2','encryption_valid':True})):
            value=setup.status('/fake/gws',Path('/private/google'))
            self.assertTrue(value['signed_in']);self.assertIsNone(value['sheets_read_write_scope'])
        with patch.object(install,'run',return_value=json.dumps({'has_refresh_token':True,'scopes':[setup.SCOPE]})):
            self.assertTrue(setup.status('/fake/gws',Path('/private/google'))['sheets_read_write_scope'])

    def test_wrapper_preview_nonzero_and_confirmation_preserves_gws_exit(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);gws=root/'gws'
            gws.write_text('#!/usr/bin/env python3\nimport json,os,sys\nprint("Using keyring backend: fixture")\nprint(json.dumps({"args":sys.argv[1:],"profile":os.environ["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"],"ambient_token_present":"GOOGLE_WORKSPACE_CLI_TOKEN" in os.environ}))\nsys.exit(7 if "get" in sys.argv else 0)\n')
            gws.chmod(0o700)
            wrapper=root/'gwsx.py';wrapper.write_bytes((install.ASSETS/'gwsx.py').read_bytes())
            (root/'google-config.json').write_text(json.dumps({'gws':str(gws),'config_dir':str(root/'profile')}))
            command=[sys.executable,str(wrapper),'work']
            preview=subprocess.run([*command,'sheets','spreadsheets','values','update'],capture_output=True,text=True)
            self.assertEqual(preview.returncode,3);self.assertFalse(json.loads(preview.stdout)['executed'])
            read=subprocess.run([*command,'sheets','spreadsheets','values','get'],capture_output=True,text=True,
                                env=dict(os.environ,GOOGLE_WORKSPACE_CLI_TOKEN='synthetic-do-not-forward'))
            self.assertEqual(read.returncode,7);self.assertFalse(json.loads(read.stdout)['ambient_token_present'])
            write=subprocess.run([*command,'--confirm','sheets','spreadsheets','values','update'],capture_output=True,text=True)
            self.assertEqual(write.returncode,0);self.assertNotIn('--confirm',json.loads(write.stdout)['args'])


if __name__=='__main__': unittest.main(verbosity=2)
