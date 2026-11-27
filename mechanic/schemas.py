"""Generation constraints supplement, but never replace, server source checks."""
import re
from itertools import zip_longest


def quote_catalog(fragments):
    groups = []
    for fragment in fragments:
        source = fragment['text']
        normalized_source = ' '.join(source.split())
        options = [p['text'] for p in fragment.get('meta', {}).get('numeric_passages', [])
                   if ' '.join(p['text'].split()) in normalized_source]
        options.extend([source] if len(source) <= 2000 else [source[:1800], source[-1800:]])
        options.extend(re.split(r'\n\s*\n', source))
        options.extend(re.split(r'(?<=[.!?])\s+(?=[А-ЯЁ])', normalized_source))
        options.extend(source.splitlines())
        normalized = (' '.join(q.split()) for q in options)
        groups.append([(fragment['id'], q) for q in dict.fromkeys(normalized) if 8 <= len(q) <= 2000])
    catalog, characters = {}, 0
    for row in zip_longest(*groups):
        for candidate in row:
            if candidate is None:
                continue
            fragment_id, quote = candidate
            if characters + len(quote) > 12000:
                continue
            catalog[f'source_quote_{len(catalog):03d}'] = {'fragment_id': fragment_id, 'quote': quote}
            characters += len(quote)
    return catalog


def response_schema(model, fragments=(), catalog=None):
    schema = model.model_json_schema()

    def strict(value):
        if isinstance(value, dict):
            value.pop('default', None)
            value.pop('title', None)
            if value.get('type') == 'object':
                value['additionalProperties'] = False
                value['required'] = list(value.get('properties', {}))
            for child in value.values():
                strict(child)
        elif isinstance(value, list):
            for child in value:
                strict(child)

    strict(schema)
    if not fragments:
        return schema
    # Short selectors avoid repeating long quotes in several schema references.
    # The provider restores their trusted text before normal source validation.
    catalog = quote_catalog(fragments) if catalog is None else catalog
    ids = list(dict.fromkeys(f['id'] for f in fragments))
    for name in ('Evidence', 'SourceQuote'):
        definition = schema.get('$defs', {}).get(name)
        if definition and catalog:
            definition['properties']['fragment_id']['enum'] = ids
            definition['properties']['quote']['enum'] = list(catalog)
    answer = schema.get('$defs', {}).get('Answer')
    if answer and catalog:
        evidence_fields = schema['$defs']['Evidence']['properties']
        selections = {key: evidence_fields[key] for key in ('fragment_id', 'quote')}
        answer['properties']['operations']['items'] = {
            'type': 'object', 'properties': selections,
            'required': list(selections), 'additionalProperties': False,
        }
    return schema


def response_contract(model, fragments):
    catalog = quote_catalog(fragments)
    return {'_response_schema': response_schema(model, fragments, catalog), '_quote_catalog': catalog}
