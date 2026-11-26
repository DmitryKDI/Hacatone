import re

from .corpus import normalize


SENTENCE_END = re.compile(r'(?<=[.!?]) (?=[А-ЯЁA-Z«])')


class UnknownPassage(ValueError):
    pass


class Passages:
    """Offer stable sentence ranges without putting source text in model output."""

    def __init__(self, fragments):
        self.sources = {}
        self.numeric = {}
        self.numbered = []
        for fragment in fragments:
            parts = SENTENCE_END.split(normalize(fragment['text']))
            self.sources[fragment['id']] = parts
            self.numeric[fragment['id']] = fragment['meta'].get('numeric_passages', [])
            self.numbered.append({
                'id': fragment['id'], 'manual': fragment['manual'], 'page': fragment['page'],
                'passages': dict(enumerate(parts, 1)), 'meta': fragment['meta'],
            })

    def quote(self, fragment_id, passage):
        if (not isinstance(fragment_id, str) or not isinstance(passage, list) or len(passage) != 2
                or any(type(index) is not int for index in passage)):
            raise UnknownPassage('Укажи два целых номера из списка passages.')
        parts = self.sources.get(fragment_id, ())
        first, last = passage
        if not 1 <= first <= last <= len(parts):
            raise UnknownPassage('Диапазон выдержки отсутствует. Проверь номера предложений в указанном фрагменте.')
        return ' '.join(parts[first - 1:last])

    def resolve_answer(self, response, draft_intent):
        if not isinstance(response, dict):
            return response
        answer = response.get('answer')
        if not isinstance(answer, dict):
            return response
        answer = dict(answer)
        if not draft_intent and response.get('tool') != 'create_ticket':
            answer.update(operations=[], spare_parts=[], urgency='medium', priority_basis=None)
        for field in ('claims', 'operations', 'spare_parts'):
            if isinstance(answer.get(field), list):
                answer[field] = [self._evidence(item) for item in answer[field]]
        if 'priority_basis' in answer:
            answer['priority_basis'] = self._evidence(answer['priority_basis'])
        return {**response, 'answer': answer}

    def resolve_conflicts(self, response):
        if not isinstance(response, dict) or not isinstance(response.get('conflicts'), list):
            return response
        conflicts = [
            {**conflict, 'sources': [self._evidence(item) for item in conflict['sources']]}
            if isinstance(conflict, dict) and isinstance(conflict.get('sources'), list) else conflict
            for conflict in response['conflicts']
        ]
        return {**response, 'conflicts': conflicts}

    def _evidence(self, item):
        if not isinstance(item, dict) or not ({'passage', 'numeric_ref'} & item.keys()):
            return item
        evidence = dict(item)
        if sum(field in evidence for field in ('quote', 'passage', 'numeric_ref')) != 1:
            raise UnknownPassage('Выбери один способ указать выдержку.')
        fragment_id = evidence.get('fragment_id')
        if 'numeric_ref' in evidence:
            index = evidence.pop('numeric_ref')
            entries = self.numeric.get(fragment_id, []) if isinstance(fragment_id, str) else []
            if type(index) is not int or not 1 <= index <= len(entries):
                raise UnknownPassage('Номер сверенной числовой выдержки отсутствует в meta.numeric_passages.')
            evidence['quote'] = entries[index - 1]['text']
        else:
            evidence['quote'] = self.quote(fragment_id, evidence.pop('passage'))
        return evidence
