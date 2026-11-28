"""Retrieval and deterministic refusal gates. Live LLM semantics remain separate."""
import json
from .config import Settings, ROOT
from .corpus import Corpus


def evaluate(settings, cases_path=ROOT/'tests/data/retrieval_cases.json'):
    cases = json.loads(cases_path.read_text(encoding='utf-8'))['cases']
    corpus = Corpus(settings.corpus, settings.manuals)
    results = []
    for case in cases:
        if case.get('expected') == 'refusal':
            reason = corpus.query_problem(case['engine'], case['question'])
            found = corpus.search(case['engine'], case['question'])
            results.append({'id': case['id'], 'refused': bool(reason) and not found,
                            'reason': reason, 'split': case['split'], 'retrieved_count': len(found)})
            continue
        results_for_query = corpus.search(case['engine'], ' '.join(case.get('history', []) + [case['question']]))
        hit = any(f['doc'] == case['manual'] and f['page'] in case['pages'] for f in results_for_query)
        results.append({'id': case['id'], 'hit': hit, 'split': case['split'], 'norm': case.get('norm', False),
                        'retrieved': [{'manual': f['doc'], 'page': f['page']} for f in results_for_query]})
    positive = [r for r in results if 'hit' in r]
    negative = [r for r in results if 'refused' in r]
    accepted = sum(r['hit'] for r in positive) >= 15 and all(r['hit'] for r in positive if r['norm'] or r['split'] == 'holdout')
    return {'positive_hits': sum(r['hit'] for r in positive), 'positive_total': len(positive),
            'retrieval_threshold_passed': accepted,
            'negative_refusals': sum(r['refused'] for r in negative), 'negative_total': len(negative),
            'negative_refusals_verified': len(negative) == 8 and all(r['refused'] for r in negative),
            'search_acceptance_passed': accepted and len(negative) == 8 and all(r['refused'] for r in negative),
            'live_model_semantics_verified': False,
            'full_acceptance_passed': False, 'results': results}


if __name__ == '__main__':
    settings = Settings.load()
    result = evaluate(settings)
    path = settings.corpus.with_suffix('.evaluation.json')
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
