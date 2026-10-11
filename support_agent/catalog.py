"""The authored policy card catalogue: card helpers, the model view and the protocol limits.

B6 (first-principles plan K2, §8.6): no retrieval. The answer context carries the catalogue's whole compact model
view (``model_view``); the runtime loader (policy) checks every card against the pinned release and refuses a
catalogue whose view is over the publication cap (D15), so every published version fits the answer context whole.
This verifies provenance, not the semantic correctness of AI-authored cards.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parents[1] / 'knowledge' / 'graph-policy-cards-v1.json'
MAX_POLICY_CLAIMS = 6
# B5d2: respond items with bound outcomes, guards once per turn, one assembler (first-principles plan §5).
ANSWER_PROTOCOL = 'respond-items-v1'
FEE_EFFECT_SUBJECT = {
    'return_shipping_responsibility': 'return_shipping',
    'return_shipping_compensation': 'return_shipping',
    'return_shipping_benefit': 'return_shipping',
    'return_pickup_fee': 'return_shipping',
    'repair_shipping_responsibility': 'repair_shipping',
}
# B5d1: what kind of source a card's rule comes from (guards G5). Decision D16: no card is a law source, so
# 'law' is not a kind a catalogue may publish and nothing here calls a rule a statutory right. A card
# without the field is a platform rule (catalogues published before B5d1).
SOURCE_KINDS = {'platform': '平台公开规则', 'merchant': '商家补充规则'}
DEFAULT_SOURCE_KIND = 'platform'
# B5d1: other names a card's rule goes by, written word for word in the card or its quotes; with the
# titles they make the rule-name lexicon of guard G3 (guards.rule_names). Limits match the editor.
MAX_ALIASES, MAX_ALIAS_CHARS = 6, 20
# D15: the publication cap of a catalogue, so its whole model view fits the answer context (B6).
MAX_CATALOGUE_CARDS, MAX_MODEL_VIEW_CHARS = 60, 24000


def publisher_problems(catalog):
    """B5f (D8): the one rule for ``source_publishers`` (who published the sources; G10 keeps them out of the answer's
    words), for the runtime loader and, since B5g, the knowledge-base editor alike: a list of at most MAX_ALIASES
    distinct names, each word for word in a card's reference quotes. Returns [(field, message)]; empty when fine."""
    names = catalog.get('source_publishers', [])
    if type(names) is not list or len(names) > MAX_ALIASES:
        return [('source_publishers', f'须是至多 {MAX_ALIASES} 个名称的列表')]
    quotes = '\n'.join(quote for card in catalog['cards'] for ref in card['references'] for quote in ref['quotes'])
    problems = [('source_publishers', '名称不能重复')] if len(set(map(str, names))) != len(names) else []
    return problems + [(f'source_publishers[{index}]', f'须是 1–{MAX_ALIAS_CHARS} 字，并在卡片引用原文里逐字出现')
                       for index, name in enumerate(names) if type(name) is not str or not name.strip()
                       or len(name) > MAX_ALIAS_CHARS or name not in quotes]


def alias_problems(card):
    """The one alias rule, for the editor and the runtime loader alike: no repeats, and every alias written word
    for word in the card itself (title, scope, conditions, outcomes, premises, exceptions) or in its quotes.
    Returns [(field, message)]; empty when the aliases are fine."""
    parts = [card.get('title'), card.get('scope')]
    for key in ('conditions', 'outcomes', 'exceptions'):
        parts += list(card.get(key) or [])
    for rows in card.get('outcome_premises') or []:
        parts += list(rows) if isinstance(rows, list) else []
    for ref in card.get('references') or []:
        parts += list(ref.get('quotes') or []) if isinstance(ref, dict) else []
    own_text = '\n'.join(part for part in parts if isinstance(part, str))
    aliases = card.get('aliases') or []
    problems = [('aliases', '别名不能重复')] if len(set(aliases)) != len(aliases) else []
    return problems + [(f'aliases[{index}]', '别名须在本卡文字或引用原文里逐字出现')
                       for index, alias in enumerate(aliases) if alias not in own_text]


def catalogue(catalog_path=CATALOG_PATH):
    """The turn's pinned catalogue as the guards read it: its cards (G3's rule names) and source publishers (G10)."""
    catalog = json.loads(Path(catalog_path).read_bytes().decode('utf-8-sig'))
    return {'cards': [card for card in catalog.get('cards', []) if isinstance(card, dict)],
            'source_publishers': [name for name in catalog.get('source_publishers') or [] if isinstance(name, str)]}


def claim_premises(card, outcome_index):
    """Return reviewed-for-rendering premises, or None for unmapped branches.

    A single-outcome card can use its card-wide scope and conditions. Multiple
    outcomes may describe different branches and require an authored map.
    """
    if type(outcome_index) is not int or not 0 <= outcome_index < len(card['outcomes']):
        return None
    if 'outcome_premises' in card:
        return card['outcome_premises'][outcome_index]
    if len(card['outcomes']) == 1:
        return [card['scope'], *card['conditions']]
    return None


def model_view(cards):
    """The compact model view of catalogue cards (B6: the whole catalogue enters the answer context): the words a
    card has, its effect domain and each outcome's premises (None for an unmapped branch); no sources or hashes."""
    result = []
    for card in cards:
        item = {key: deepcopy(card[key]) for key in
                ('id', 'title', 'scope', 'conditions', 'outcomes', 'exceptions')}
        # An authored effect category makes the question/card boundary visible
        # to the answer model. It does not certify the card's applicability.
        item['effect_domain'] = card.get('effect_domain', 'unclassified')
        item['outcome_premises'] = [deepcopy(claim_premises(card, index))
                                    for index in range(len(card['outcomes']))]
        result.append(item)
    return result


def model_view_chars(cards):
    """The model view's size: its characters as the answer context serialises it."""
    return len(json.dumps(model_view(cards), ensure_ascii=False, allow_nan=False))


def cap_problem(cards):
    """D15's publication cap, the one rule (the runtime loader applies it, so publishing, activating and every turn
    refuse a larger catalogue): at most MAX_CATALOGUE_CARDS cards and MAX_MODEL_VIEW_CHARS characters of model view.
    Returns a short description of what is over, or ''."""
    chars = model_view_chars(cards)
    if len(cards) <= MAX_CATALOGUE_CARDS and chars <= MAX_MODEL_VIEW_CHARS:
        return ''
    return (f'{len(cards)} 张卡、模型视图 {chars} 字，上限是 {MAX_CATALOGUE_CARDS} 张、'
            f'{MAX_MODEL_VIEW_CHARS} 字')
