import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

import analysis
import config
import main
import notifications
import storage


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.prompt = config.PROMPT_PATH.read_text(encoding='utf-8')
        self.result = dict(notice_id='DEMO-1', reason='Software platform procurement.',
                           eligibility_note='Not stated in supplied material',
                           relevance='HIGH', area='Software and Data',
                           confidence='high', evidence='Supply a software data platform.')

    def client(self, result):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
                content=json.dumps({'tenders': [result]})))])
        return client

    def test_variable_category_counts(self):
        for count in (1, 5, 17):
            prompt = '\n'.join(f'- "Category {i}"' for i in range(count))
            schema = analysis.build_response_format(prompt)
            enum = schema['json_schema']['schema']['properties']['tenders']['items']['properties']['area']['enum']
            self.assertEqual(len(enum), count + 1)
        with self.assertRaises(ValueError):
            analysis.build_response_format('No categories')

    def test_invalid_category_and_none(self):
        schema = analysis.build_response_format(self.prompt)
        with patch.object(analysis.time, 'sleep'):
            with self.assertRaises(ValueError):
                analysis.classify('Example', self.client(dict(self.result, area='Unknown')), self.prompt, schema)
            with self.assertRaises(ValueError):
                analysis.classify('Example', self.client(dict(self.result, relevance='NONE')), self.prompt, schema)
            result, _ = analysis.classify('Example', self.client(dict(self.result, relevance='NONE', area=None)), self.prompt, schema)
            self.assertIsNone(result['area'])

    def test_main_to_excel_and_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            detail, llm = root/'details.jsonl', root/'llm.jsonl'
            row = dict(reference='DEMO-1', detailsUrl='https://example.org/tender',
                       source='nspa', sourceId='NSPA', originator='Example buyer',
                       projectTitle='Data platform', opportunityTypeFull='Tender',
                       estimatedValue='', currency='', plannedSolicitationClosing='')
            df = pd.DataFrame([row])
            detail.write_text(json.dumps(dict(reference='DEMO-1', detailsUrl=row['detailsUrl'],
                text=self.result['evidence'], detail_status='ok'))+'\n', encoding='utf-8')
            client = self.client(self.result)
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            from contextlib import ExitStack
            with ExitStack() as stack:
                for module in (analysis, storage):
                    stack.enter_context(patch.object(module, 'DETAIL_DB', detail))
                    stack.enter_context(patch.object(module, 'LLM_DB', llm))
                stack.enter_context(patch.object(main, 'validate_settings', return_value=self.prompt))
                stack.enter_context(patch.object(main, 'fetch_tender_list', return_value=(df, df)))
                stack.enter_context(patch.object(main, 'prepare_run'))
                stack.enter_context(patch.object(main, 'collect_details'))
                stack.enter_context(patch.object(main, 'close_browsers'))
                stack.enter_context(patch.object(main, 'OpenAI', return_value=client))
                stack.enter_context(patch.object(main, 'RUNS_DIR', root))
                stack.enter_context(patch.object(main, 'S'))
                stack.enter_context(patch.object(analysis.time, 'sleep'))
                stack.enter_context(patch.dict('os.environ', {'LLM_API_KEY': 'test-only'}))
                report = {'run_id': 'test'}
                main.main(report)
                self.assertEqual(report['status'], 'completed')
                records = storage.read_jsonl(llm)
                self.assertEqual(records[0]['evidence_status'], 'matched')
                output = pd.read_excel(report['output_path'])
                self.assertEqual(output.loc[0, 'Önerilen Kategori'], 'Software and Data')
                main.main(report)
                self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_optional_teams(self):
        with patch.dict('os.environ', {'TEAMS_WEBHOOK_URL': ''}), patch.object(notifications.requests, 'post') as post:
            notifications.send_run_notification({}, 0, None)
            post.assert_not_called()

    def test_startup_settings(self):
        with patch.object(config, 'LLM_URL', ''), patch.object(config, 'LLM_MODEL', 'example'):
            with self.assertRaises(ValueError):
                config.validate_settings()
        with patch.object(config, 'LLM_URL', 'https://example.org/v1'), patch.object(config, 'LLM_MODEL', 'example'), patch.dict('os.environ', {'LLM_API_KEY': 'test-only'}):
            self.assertEqual(config.validate_settings(), self.prompt.strip())

    def test_unmatched_evidence(self):
        self.assertEqual(analysis.check_evidence('Invented', 'Source'), 'not_found')
        self.assertEqual(analysis.check_evidence(None, 'Source'), 'not_provided')


if __name__ == '__main__':
    unittest.main()
