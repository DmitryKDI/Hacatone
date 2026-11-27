"""Generation constraints supplement, but never replace, server source checks."""
import re
from itertools import zip_longest


def quote_catalog(fragments, preferred_quote=None):
    groups = []
    reviewed = []
    for fragment in fragments:
        source = fragment['text']
        normalized_source = ' '.join(source.split())
        approved = [' '.join(p['text'].split()) for p in fragment.get('meta', {}).get('numeric_passages', [])]
        reviewed.append([(fragment['id'], quote) for quote in approved
                         if 8 <= len(quote) <= 2000 and quote in normalized_source])
        options = []
        options.extend([source] if len(source) <= 2000 else [source[:1800], source[-1800:]])
        options.extend(re.split(r'\n\s*\n', source))
        options.extend(re.split(r'(?<=[.!?])\s+(?=[А-ЯЁ])', normalized_source))
        if preferred_quote is not None:
            options.extend(re.findall(r'\bсовместить\s+метк[^;]{20,240};', normalized_source, re.I))
        options.extend(source.splitlines())
        normalized = (' '.join(q.split()) for q in options)
        groups.append([(fragment['id'], q) for q in dict.fromkeys(normalized) if 8 <= len(q) <= 2000])
    candidates = [candidate for row in zip_longest(*groups) for candidate in row if candidate is not None]
    catalog, selected, characters = {}, set(), 0

    def add_quotes(options, budget):
        nonlocal characters
        for candidate in options:
            fragment_id, quote = candidate
            if candidate in selected or characters + len(quote) > budget:
                continue
            catalog[f'source_quote_{len(catalog):03d}'] = {'fragment_id': fragment_id, 'quote': quote}
            selected.add(candidate)
            characters += len(quote)

    if preferred_quote is not None:
        # Reserve room for actionable excerpts before full pages fill the shared budget.
        add_quotes((candidate for candidate in candidates if preferred_quote(*candidate)), 3600)
    # Long page chunks must not crowd out the measured procedures and conditions.
    add_quotes((candidate for row in zip_longest(*reviewed) for candidate in row if candidate is not None), 8000)
    add_quotes(candidates, 12000)
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
    # Each selector binds one quote to one fragment. The model selects only the
    # quote ID and the provider restores both fields before source validation.
    catalog = quote_catalog(fragments) if catalog is None else catalog
    for name in ('Evidence', 'SourceQuote'):
        definition = schema.get('$defs', {}).get(name)
        if definition and catalog:
            del definition['properties']['fragment_id']
            definition['required'].remove('fragment_id')
            definition['properties']['quote']['enum'] = list(catalog)
    answer = schema.get('$defs', {}).get('Answer')
    if answer and catalog:
        evidence_fields = schema['$defs']['Evidence']['properties']
        selections = {'quote': evidence_fields['quote']}
        answer['properties']['operations']['items'] = {
            'type': 'object', 'properties': selections,
            'required': list(selections), 'additionalProperties': False,
        }
    return schema


def response_contract(model, fragments, preferred_quote=None):
    catalog = quote_catalog(fragments, preferred_quote)
    return {'_response_schema': response_schema(model, fragments, catalog), '_quote_catalog': catalog}
