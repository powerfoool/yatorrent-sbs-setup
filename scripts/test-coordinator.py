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


class CoordinatorTests(unittest.TestCase):
    def agreement(self):
        return {'schema_version': 1, 'approved': True, 'approval_note': 'owner chose verdict plus JSON',
                'sheet_title': "Оценки O'Brien", 'input_columns': {'id':'A','query':'B','left':'G','right':'H'},
                'outputs': {'P':'/winner','V':''}, 'output_schema': {'type':'object'}}

    def pair(self):
        return {'id':'row-stable-1','query':'map','left_html':'<html>left</html>','right_html':'<html>right</html>'}

    def test_format_requires_real_agreement(self):
        a=self.agreement();a['approved']=False
        with self.assertRaises(ValueError): data.prepare(self.pair(),a,'foreman','scout')

    def test_one_pair_one_wire_task_reusable_identity(self):
        a=data.prepare(self.pair(),self.agreement(),'foreman','scout')
        b=data.prepare(self.pair(),self.agreement(),'foreman','scout')
        self.assertEqual(a,b)
        self.assertEqual(set(a['publication']),{'item_id','task_type','payload'})
        self.assertEqual(len(a['publication']['payload']),1)
        self.assertEqual(a['publication']['payload'][0]['id'],a['publication']['item_id'])
        self.assertNotIn('model',a['publication']['payload'][0])

    def test_changed_input_schema_and_rubric_get_new_ids(self):
        a=self.agreement();base=data.prepare(self.pair(),a,'f','s')['publication']['item_id']
        for pair,agreement,f in [(dict(self.pair(),query='changed'),a,'f'),
                                  (self.pair(),dict(a,output_schema={'type':'string'}),'f'),
                                  (self.pair(),a,'new-rubric')]:
            self.assertNotEqual(base,data.prepare(pair,agreement,f,'s')['publication']['item_id'])

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
                data.prepare(dict(self.pair(),**{key:'do-not-forward'}),self.agreement(),'f','s')

    def test_json_pointer_arrays_and_escaping(self):
        self.assertEqual(data.pointer({'a/b':[{'~x':9}]},'/a~1b/0/~0x'),9)

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
