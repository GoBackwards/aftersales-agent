"""Authored response contracts, using independent synthetic cards.

These checks establish selection/rendering integrity, not policy correctness or
model selection quality. They never load development/holdout customer inputs.
B6: the cards reach the answer whole (the loader checks their provenance, tests/test_graph_policy_cards.py); there
is no per-turn availability filter and no fee-subject filter, so these checks start from the catalogue's cards.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from support_agent.catalog import CATALOG_PATH, model_view


class GraphResponseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='graph-response-')
        self.addCleanup(temporary.cleanup)
        self.catalog_path = Path(temporary.name) / 'cards.json'
        first_text = '本条仅适用测试范围。具备条件甲才能获得结果乙。例外丙另行处理。'
        second_text = '条件丁已确认时才采用补充规则戊。'
        self.bundle = {'corpus_sha256': 'c' * 64, 'evidence': [
            {'id': identifier, 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'text': text}
            for identifier, text in [('parent:first', first_text), ('parent:second', second_text)]]}
        self.card = {'id': 'conditional', 'title': '独立测试规则', 'scope': '仅限测试范围',
            'conditions': ['必须先确认条件甲', '还需满足条件丁'],
            'outcomes': ['符合全部条件才获得结果乙'], 'exceptions': ['遇到丙时另行核实'],
            'evidence_ids': ['parent:first', 'parent:second'],
            'references': [{'evidence_id': item['id'], 'parent_sha256': item['sha256'],
                            'quotes': [item['text']]} for item in self.bundle['evidence']]}
        self.catalog = {'schema_version': 'graph-policy-cards-v1', 'human_reviewed': False,
                        'corpus_sha256': self.bundle['corpus_sha256'], 'cards': [self.card]}
        self.write_catalog(self.catalog)

    def write_catalog(self, catalog):
        self.catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding='utf8')

    def available(self):
        """The cards a turn reads: the whole catalogue, as written (B6)."""
        return deepcopy(json.loads(self.catalog_path.read_text(encoding='utf8'))['cards'])

    def test_a_selected_outcome_opens_with_its_own_premises_and_its_siblings_stay_folded(self):
        from support_agent.assemble import card_block
        branched = deepcopy(self.card)
        branched['scope'] = '共有范围，不能套用到全部结果'
        branched['conditions'] = ['分支甲的条件', '分支乙的条件']
        branched['outcomes'] = ['只适用分支甲的结果。', '只适用分支乙的结果。']
        branched['outcome_premises'] = [['仅分支甲且条件甲满足'], ['仅分支乙且条件乙满足']]
        self.catalog['cards'] = [branched]
        self.write_catalog(self.catalog)
        cards = self.available()
        block = card_block(cards[0], [1])
        self.assertIn('仅分支乙且条件乙满足', block['text'])
        self.assertIn('只适用分支乙的结果', block['text'])
        self.assertNotIn('分支甲', block['text'])
        # D7: the card is shown whole; the other branch is folded beside the selected one, with its own premises.
        self.assertEqual(block['other_outcomes'], [{'outcome_index': 0, 'outcome': '只适用分支甲的结果。',
                                                    'premises': ['仅分支甲且条件甲满足']}])
        self.assertEqual(block['evidence_ids'], ['parent:first', 'parent:second'])
        self.assertNotIn('适用于你', json.dumps(block, ensure_ascii=False))
        self.assertEqual(model_view(cards)[0]['outcome_premises'],
                         branched['outcome_premises'])
        self.assertEqual(model_view(cards)[0]['effect_domain'], 'unclassified')


    def test_effect_domain_is_visible_to_answer_model_without_changing_the_rule(self):
        self.card['effect_domain'] = 'return_shipping_benefit'
        self.write_catalog(self.catalog)
        cards = self.available()
        self.assertEqual(model_view(cards)[0]['effect_domain'],
                         'return_shipping_benefit')
        self.assertEqual(cards[0]['outcomes'], self.card['outcomes'])

    def test_a_fee_card_of_another_service_is_dropped_only_when_the_item_names_its_fee_subject(self):
        # B6 deleted the per-turn fee filter: every card is citable, and G9 (the one place for it) drops a claim of
        # another fee service as untidy when the item's fee subject is known; unknown or none lets it stand.
        from support_agent import guards
        cards = {}
        for identifier, domain in (('return_rule', 'return_shipping_responsibility'),
                                   ('repair_rule', 'repair_shipping_responsibility'),
                                   ('pickup_fee', 'return_pickup_fee'), ('warranty', 'warranty_coverage')):
            card = deepcopy(self.card)
            card.update(id=identifier, effect_domain=domain)
            cards[identifier] = card
        claims = [{'card_id': key, 'outcome_index': 0} for key in cards]

        def kept(subject):
            answer = {'items': [{'index': 1, 'disposition': 'answered', 'claims': claims[:3], 'path_id': 'none',
                                 'note': ''}], 'show_order_ids': [], 'next_step': '',
                      'ask': {'text': '', 'option_set_id': 'none'}}
            problems, tidied, dropped = guards.check_structure(
                answer, {'items': [{'index': 1, 'fee_subject': subject}], 'cards': cards})
            self.assertEqual(problems, [])
            return [claim['card_id'] for claim in tidied['items'][0]['claims']], dropped
        self.assertEqual(kept('repair_shipping'), (['repair_rule'], [
            {'code': 'G9_FEE_SUBJECT_MISMATCH', 'part': 'claim:1'}] * 2))
        self.assertEqual(kept('return_shipping')[0], ['return_rule', 'pickup_fee'])
        for subject in ('unknown', 'none'):
            with self.subTest(subject=subject):
                self.assertEqual(kept(subject), (['return_rule', 'repair_rule', 'pickup_fee'], []))

    def test_unmapped_multi_outcome_card_cannot_silently_mix_conditions(self):
        from support_agent import guards
        from support_agent.respond import respond_schema
        card = deepcopy(self.card)
        card['outcomes'] = ['分支甲结果。', '分支乙结果。']
        self.catalog['cards'] = [card]
        self.write_catalog(self.catalog)
        cards = self.available()
        self.assertEqual(model_view(cards)[0]['outcome_premises'], [None, None])
        # the strict schema offers no claim on it
        self.assertNotIn('claims', respond_schema([{'index': 1}], cards, [])['properties']['items']['items']['properties'])
        answer = {'items': [{'index': 1, 'disposition': 'answered', 'path_id': 'none', 'note': '',
                             'claims': [{'card_id': 'conditional', 'outcome_index': 0}]}],
                  'show_order_ids': [], 'next_step': '', 'ask': {'text': '', 'option_set_id': 'none'}}
        problems = guards.check_structure(answer, {'items': [{'index': 1, 'fee_subject': 'none'}],
                                                   'cards': {card['id']: card for card in cards}})[0]
        self.assertEqual([problem['code'] for problem in problems], ['G9_CLAIM_WITHOUT_PREMISES'])


    def test_the_prose_guards_catch_consequential_claims_and_allow_fact_finding(self):
        from support_agent import guards
        risks = ['配送慢不是拒收退款理由。', '这件商品不支持换货。',
                 '这类商品通常不支持直接换，需要退货重买。',
                 '同订单免费权益次数已用完，所以无法申请退货。',
                 '漏用优惠券通常不能事后单独补差价。',
                 '赠品未退回会按赠品价值扣除。',
                 '发错货七天（含）内可退换且免返回运费。',
                 'PLUS 会员由京东承担换货运费。',
                 '质量问题通常由京东承担。',
                 '维修运费一般由你承担。',
                 '这笔费用由您承担是否可以？',
                 '七天内可以退货。', '平台已退款。',
                 '需要撤销原售后申请才能重新申请。']
        context = guards.context()
        for prose in risks:
            with self.subTest(prose=prose):
                self.assertTrue({'G1', 'G3', 'G4'} & set(guards.guard_ids(guards.check(prose, context))))
        inquiries = ['请查看售后单是否支持换货。', '请核实运费是否由平台承担。',
                     '请确认平台是否已退款。', '这笔上门取件费是否该由您承担？']
        for prose in inquiries:
            with self.subTest(prose=prose):
                self.assertEqual(guards.check(prose, context), [])


    def test_claim_must_be_a_candidate_card_and_a_real_outcome_once(self):
        from support_agent import guards
        cards = {card['id']: card for card in self.available()}
        def problems(claims, part=0):
            answer = {'items': [{'index': 1, 'disposition': 'answered', 'claims': claims, 'path_id': 'none',
                                 'note': ''}], 'show_order_ids': [], 'next_step': '',
                      'ask': {'text': '', 'option_set_id': 'none'}}
            found = guards.check_structure(answer, {'items': [{'index': 1}], 'cards': cards})[part]
            return [row['code'] for row in found]
        for claim in ({'card_id': 'invented', 'outcome_index': 0}, {'card_id': 'conditional', 'outcome_index': 2},
                      {'card_id': 'conditional', 'outcome_index': True}):
            with self.subTest(claim=claim):
                self.assertEqual(problems([claim]), ['G9_CLAIM_NOT_A_CANDIDATE'])
        # B5f: a repeated outcome is only untidy: the repeat is dropped (and recorded), the turn goes on.
        self.assertEqual(problems([{'card_id': 'conditional', 'outcome_index': 0}] * 2), [])
        self.assertEqual(problems([{'card_id': 'conditional', 'outcome_index': 0}] * 2, part=2), ['G9_CLAIM_REPEATED'])
        self.assertEqual(problems([{'card_id': 'conditional', 'outcome_index': 0}]), [])


    def test_returned_catalog_context_and_cards_do_not_share_mutable_state(self):
        available = self.available()
        context = model_view(available)
        context[0]['conditions'][0] = 'changed by another request'
        self.assertEqual(available[0]['conditions'], self.card['conditions'])
        self.assertNotIn('references', context[0])
        self.assertNotIn('evidence_ids', context[0])
        available[0]['conditions'].clear()
        available[0]['references'][0]['quotes'].clear()
        self.assertEqual(self.available(), [self.card])

    def test_repository_shipping_and_gift_cards_keep_scope_conditions_and_exceptions(self):
        # Read the actual authored policy file. This checks field retention, not
        # whether the authored policy is current or has been human-reviewed.
        catalog = json.loads(CATALOG_PATH.read_text(encoding='utf-8-sig'))
        authored = {card['id']: card for card in catalog['cards']}
        groups = [('warranty_18m', 'return_shipping_responsibility', 'pickup_prices'),
                  ('gift_general', 'gift_seven_day_branches', 'gift_loss_damage')]
        for identifiers in groups:
            for key in identifiers:
                with self.subTest(card=key):
                    card = authored[key]
                    self.assertTrue(card['scope'].strip() and card['conditions'] and card['outcomes']
                                    and card['evidence_ids'])
                    for paragraph in [card['scope'], *card['conditions'], *card['outcomes'], *card['exceptions']]:
                        self.assertTrue(type(paragraph) is str and paragraph.strip())
        def fields(identifiers):
            return '\n'.join(paragraph for key in identifiers for card in [authored[key]]
                             for paragraph in [card['scope'], *card['conditions'], *card['outcomes'],
                                               *card['exceptions']])
        shipping = fields(groups[0])
        self.assertIn('仅限带“18个月质保”标识', shipping)
        self.assertIn('PLUS', shipping)
        self.assertIn('不能把所有无免费标识的订单都判为客户付费', shipping)
        gifts = fields(groups[1])
        self.assertIn('仅限京东自营商品七天无理由退货', gifts)
        self.assertIn('“赠品价值”', gifts)
        self.assertIn('“赠品分摊价格”', gifts)

    def test_repository_branch_claims_do_not_import_sibling_premises(self):
        authored = {card['id']: card for card in
                    json.loads(CATALOG_PATH.read_text(encoding='utf-8-sig'))['cards']}
        self.assertTrue(all('outcome_premises' in card for card in authored.values()
                            if len(card['outcomes']) > 1))
        from support_agent.assemble import card_block
        seven_day = authored['seven_day_return']
        general = card_block(seven_day, [0])['text']
        self.assertIn('退货或换货', general)
        self.assertNotIn('详情页带有“7天无理由退货”标识', general)
        marked = card_block(seven_day, [1])['text']
        self.assertIn('详情页带有“7天无理由退货”标识', marked)
        self.assertNotIn('可申请退货或换货', marked)
        gift = authored['gift_loss_damage']
        invoice = card_block(gift, [1])['text']
        self.assertIn('发票遗失', invoice)
        # The title names the card; the sibling outcome and its own premises stay folded.
        self.assertNotIn(gift['outcomes'][0].strip(), invoice)
        self.assertFalse([part for part in gift['outcome_premises'][0] if part not in gift['outcome_premises'][1]
                          and part.rstrip('。； ') in invoice])

if __name__ == '__main__':
    unittest.main()
