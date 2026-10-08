"""Free, deterministic tests of the evaluator, not model-quality measurements."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from evals.metrics import compare, gate, summarize
from evals.report import generate, validate
from evals.run import fixtures, capture
from evals.scoring import SCORER_VERSION, apply_reviews, grade, review_template


def sample():
    cases, _ = fixtures()
    case = deepcopy(cases[0])
    return {'run_id':'r1', 'attempt':1, 'case':case, 'model':'test', 'model_config':{},
            'workflow_version':'test', 'dataset_version':'d', 'fixture_version':'f', 'prompt_version':'p',
            'status':'completed', 'report':'A 支持私有部署。[LOC1_1-1]',
            'verified_findings':[{'claim_id':'c1', 'claim':'A 支持私有部署。', 'source_ids':['LOC1_1-1'],
                                  'support':{'evidence_quotes':[{'source_id':'LOC1_1-1','quote':'A 支持私有部署'}]}}],
            'evidence':[{'source_id':'LOC1_1-1','snippet':'A 支持私有部署，提供 Python SDK。'}],
            'summary':{'model_calls':8,'web_calls':1,'tool_calls':2,'duration_ms':100,
                       'limits':{'model_calls':16,'web_calls':12,'tool_calls':24},'token_usage':{'status':'known'}}}


def artifact():
    row = sample()
    return {'schema_version':2,'scorer_version':SCORER_VERSION,'suite':'fixed_workflow','evaluation_id':'e1',
            'selected_cases':['q01'],'repeats':1,'expected_run_count':1,'complete':True,'scope':'test',
            **{k:row[k] for k in ('model','model_config','workflow_version','dataset_version','fixture_version','prompt_version')},
            'results':[row]}


def reviewed(data):
    review = review_template(data)
    for row in review['reviews']:
        row.update(review_status='complete', reviewer={'name':'test reviewer','kind':'assistant'},
                   factual_correctness='correct', uncertainty_handling='appropriate', forbidden_conclusion_present=False)
        for q in row['requirements']:
            q.update(verdict='met',reason='Final report states the annotated deployment fact.')
        for q in row['claims']:
            q.update(verdict='supported',reason='Exact deployment statement in source A.')
    return review


class EvaluationTests(unittest.TestCase):
    def test_archived_baseline_is_reproducible_and_not_release_approved(self):
        root=Path(__file__).resolve().parents[1]/'evals/baselines/qwen-turbo-v2'
        data=json.loads((root/'workflow.json').read_text(encoding='utf-8'))
        reviews=json.loads((root/'assistant-reviews.json').read_text(encoding='utf-8'))
        result=generate(data,reviews=reviews)
        self.assertEqual(result['metrics']['run_count'],40)
        self.assertEqual(result['metrics']['review_completion_rate'],1)
        self.assertEqual(result['metrics']['reviewer_kinds'],{'assistant':40})
        self.assertEqual(result['gate']['decision'],'failed')
        pending=json.loads((root/'human-reviews.template.json').read_text(encoding='utf-8'))
        self.assertEqual(generate(data,reviews=pending)['gate']['decision'],'needs_review')

    def test_retrieval_ranking_does_not_inflate_duplicate_chunks(self):
        from evals.retrieval import ranking, aggregate
        self.assertEqual(ranking(['A','B'],['A','A','C'])['recall_at_k'],.5)
        self.assertEqual(ranking(['B'],['A','A','B'])['reciprocal_rank'],1/3)
        self.assertEqual(ranking(['B'],[])['reciprocal_rank'],0)
        self.assertIsNone(aggregate([])['recall_at_k'])

    def test_gold_corpus_and_holdout(self):
        cases, docs = fixtures()
        self.assertEqual((len(cases),len(docs)),(20,5))
        self.assertEqual(sum(c['split']=='holdout' for c in cases),4)

    def test_citations_are_not_semantic_accuracy(self):
        row = sample()
        row['report'] = row['report'].replace('支持','不支持')
        self.assertTrue(grade(row)['automated_pass'])
        self.assertIsNone(grade(row)['semantic_pass'])
        data = artifact(); data['results']=[row]
        review = reviewed(data)
        review['reviews'][0]['factual_correctness']='incorrect'
        self.assertFalse(apply_reviews(data,review)[0]['grading']['semantic_pass'])

    def test_invalid_citation_and_quote_fail(self):
        for field, expected in [('citation','unknown_report_citation'),('quote','non_verbatim_support_quote')]:
            row = sample()
            if field=='citation': row['report'] += '[LOC_fake]'
            else: row['verified_findings'][0]['support']['evidence_quotes'][0]['quote']='invented'
            self.assertIn(expected,grade(row)['violations'])
        row=sample(); row['report']='A 支持私有部署。\n\n## 参考资料\n\n[LOC1_1-1] A'
        self.assertIn('findings_without_report_citations',grade(row)['violations'])

    def test_call_budget_missing_or_exceeded(self):
        row=sample(); row['summary']['model_calls']=17
        self.assertIn('call_budget_exceeded',grade(row)['violations'])
        del row['summary']['limits']
        self.assertIn('missing_budget_measurement',grade(row)['violations'])

    def test_evidence_absence_requires_abstention(self):
        row=sample(); row['case']['expected_behavior']='abstain'
        self.assertFalse(grade(row)['automated_pass'])
        row.update(verified_findings=[],evidence=[],status='partial',report='资料不足，无法确认。')
        self.assertTrue(grade(row)['automated_pass'])

    def test_pending_review_gate(self):
        result=generate(artifact(),reviews=review_template(artifact()))
        self.assertEqual(result['gate']['decision'],'needs_review')
        self.assertIsNone(result['metrics']['semantic_pass_rate'])

    def test_complete_review_provenance(self):
        data=artifact(); result=generate(data,reviews=reviewed(data))
        self.assertEqual(result['gate']['decision'],'passed')
        self.assertEqual(result['metrics']['reviewer_kinds'],{'assistant':1})

    def test_review_binding_and_exact_coverage(self):
        for corruption in ('fingerprint','missing_requirement','duplicate_claim','unknown_run','no_reason'):
            data=artifact(); review=reviewed(data); r=review['reviews'][0]
            if corruption=='fingerprint': data['results'][0]['report']+=' changed'
            elif corruption=='missing_requirement': r['requirements']=[]
            elif corruption=='duplicate_claim': r['claims'] *= 2
            elif corruption=='unknown_run': r['run_id']='unknown'
            else: r['claims'][0]['reason']=''
            with self.subTest(corruption=corruption), self.assertRaises(ValueError):
                apply_reviews(data,review)

    def test_partial_gold_cannot_pass(self):
        data=artifact(); review=reviewed(data); review['reviews'][0]['requirements'][0]['verdict']='partial'
        result=generate(data,reviews=review)
        self.assertEqual(result['metrics']['requirement_coverage'],.5)
        self.assertEqual(result['gate']['decision'],'failed')

    def test_manifest_cannot_claim_missing_or_duplicate_samples(self):
        for corruption in ('missing','duplicate','metadata'):
            data=artifact()
            if corruption=='missing': data['results']=[]
            elif corruption=='duplicate': data['results']*=2
            else: data['results'][0]['model']='other'
            with self.subTest(corruption=corruption), self.assertRaises(ValueError): validate(data)

    def test_incomplete_artifact_never_passes(self):
        data=artifact(); data.update(results=[],complete=False)
        self.assertEqual(generate(data)['gate']['decision'],'incomplete')

    def test_baseline_requires_matching_manifest_and_reviews(self):
        data=artifact(); old=artifact(); old['dataset_version']='other'
        with self.assertRaises(ValueError): compare(data,old)
        with self.assertRaises(ValueError): generate(data,baseline=artifact())
        old=artifact(); review=reviewed(old); review['reviews'][0]['reviewer']['kind']='human'
        with self.assertRaises(ValueError): generate(data,reviews=reviewed(data),baseline=old,baseline_reviews=review)

    def test_stable_failure_is_not_repeat_success(self):
        data=artifact(); data.update(repeats=2,expected_run_count=2)
        second=deepcopy(data['results'][0]); second.update(run_id='r2',attempt=2); data['results'].append(second)
        review=reviewed(data)
        for r in review['reviews']: r['factual_correctness']='incorrect'
        metrics=summarize(apply_reviews(data,review))
        self.assertEqual(metrics['repeat_quality_agreement'],1)
        self.assertEqual(metrics['all_repeats_pass_rate'],0)

    def test_gate_policy_validation(self):
        m=summarize(apply_reviews(artifact()))
        for policy in ({'min_semantic_pass_rate':float('nan')},{'typo':1},{'min_automated_pass_rate':-1}):
            with self.assertRaises(ValueError): gate(m,policy=policy)

    def test_capture_runs_actual_graph_with_fixed_tools(self):
        from langgraph.checkpoint.memory import InMemorySaver
        from mult_agents.graph import build_app
        from tests.support import bundle
        cases, docs=fixtures(); agents=bundle({'claim':'A 支持私有部署。'})
        row=capture(cases[0],docs,agents,build_app(agents,InMemorySaver()),1,{})
        self.assertEqual(row['status'],'completed')
        self.assertTrue(row['grading']['automated_pass'])
        self.assertGreater(row['summary']['model_calls'],0)
        self.assertTrue(all(e['doc_id'].startswith('evals/') for e in row['evidence']))


if __name__=='__main__': unittest.main()
