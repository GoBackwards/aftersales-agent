"""One support turn as explicit steps: understand -> business -> policy -> answer.

SQLite remains the authority for business state. The turn owns that turn only; its structured proposals cannot
confirm or create a return. B7 (first-principles plan §2.3 step 4, K20): the planning call is one strict ``plan_turn``
(understand.py) listing the message's items; a broken shape goes back once, an invalid item is dropped and the others
go on. B5d2 (plan §5): the answer is one strict ``respond`` call over the same items (K17), checked once by guards.py
(B5f: piece by piece) and rendered by the single assembler (assemble.py). B6 (plan K2): no retrieval; every turn binds
the whole card catalogue of its pinned version and the answer context carries its model view. Provider requests use
the existing audited client.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from typing import TypedDict

from . import assemble, decide, guards, respond, understand
from . import copy as text
from .models import ModelError, ToolDefinitions
from .domain import ORDER_TOKEN, order_facts, order_view
from .runtime import RunStopped, RuntimeLimits, SessionRunControl
from .sessions import FAILED_TURN_TEXT, SessionError
from .catalog import ANSWER_PROTOCOL, CATALOG_PATH, catalogue
from .shipping_route import resolve_user_route
from .return_cards import return_card_block


class TurnState(TypedDict, total=False):
    message: str
    history: list[dict]
    snapshot: dict
    plan: dict
    plan_items: list[dict]
    rejected_items: list[dict]
    business_results: list[dict]
    catalogue: list[dict]
    route_reference: dict
    answer: dict
    reply: str
    source_cards: list[dict]
    blocks: list[dict]
    plan_notes: list[str]
    policy_needed: bool


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


# The code sentence for each planning note (understand.settle_fee_subjects): an assumption the code made.
PLAN_NOTE_TEXT = {'pickup_return_assumed': text.ASSUMED_RETURN_PICKUP}
DEFAULT_TOOL_DESCRIPTION = '提交当前阶段的结构化结果，不执行业务写入。'


class ConversationAgent:
    def __init__(self, model, store, customer_id, *, public_policy_config=None,
                 runtime_limits=None, policy_knowledge=None,
                 card_catalog_path=None, **_unused):
        self.model, self.store, self.customer_id = model, store, customer_id
        self.config = public_policy_config
        self.policy_knowledge = policy_knowledge
        # A published knowledge-base version is an immutable file; each turn reads
        # exactly one catalog, so the answer context, the checks and the rendering share its binding.
        self.card_catalog = card_catalog_path if card_catalog_path is not None else CATALOG_PATH
        self.policy = None
        self.limits = runtime_limits or RuntimeLimits()
        self.trace, self.usage = [], {}

    def _complete(self, messages, schemas, stage):
        for attempt in range(1, 3):
            timeout = self.control.before_model()
            self.control.event('model_attempt', {'step': stage, 'attempt': attempt})
            try:
                bounded = getattr(self.model, 'complete_with_timeout', None)
                response = (bounded(messages, schemas, timeout=timeout) if callable(bounded)
                            else self.model.complete(messages, schemas))
            except ModelError as exc:
                self.control.event('model_attempt_error', {'step': stage, 'attempt': attempt,
                    'error_category': 'provider_error', 'status_code': exc.status_code,
                    'retryable': exc.retryable})
                self.control.check()
                if not exc.retryable or attempt == 2 or (exc.retry_after or 0) > 2:
                    raise
                self.control.sleep(max(0.1, exc.retry_after or 0))
                continue
            usage = {k: response.usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                     if type(response.usage.get(k)) is int and response.usage[k] >= 0}
            for key, value in usage.items():
                self.usage[key] = self.usage.get(key, 0) + value
            self.control.event('model_response', {'step': stage, 'attempt': attempt,
                                                'usage': usage, 'usage_complete': len(usage) == 3})
            self.trace.append({'type': 'model', 'node': stage, 'model_id': response.model_id,
                               'content': response.content,
                               'tool_calls': [{'name': c.name, 'arguments': c.arguments} for c in response.tool_calls],
                               'usage': usage})
            self.control.check()
            return response
        raise AssertionError('unreachable retry')

    def _structured(self, name, prompt, context, stage, *, parameters, convert, validate=None,
                    description=DEFAULT_TOOL_DESCRIPTION):
        """One named strict function: call, convert, validate; one correction with the error fed back.

        ``convert(arguments)`` gives the value (understand.parse for the plan, respond.parse for the answer) and
        ``validate(value)`` checks it; either raises ValueError or TypeError. After two invalid proposals a ModelError
        ends the turn. Every result is checked by code, including the limits strict mode cannot send.
        """
        schemas = ToolDefinitions([{'type': 'function', 'function': {
            'name': name, 'description': description, 'parameters': parameters}}],
            tool_choice={'type': 'function', 'function': {'name': name}},
            strict_functions={name})
        messages = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': _json(context)}]
        for correction in range(2):
            response = self._complete(messages, schemas, stage)
            try:
                if len(response.tool_calls) != 1 or response.tool_calls[0].name != name:
                    raise ValueError('必须提交一次指定的结构化工具结果')
                value = convert(response.tool_calls[0].arguments)
                if validate:
                    validate(value)
                return value
            except (ValueError, TypeError) as exc:
                detail = str(exc)[:300]
                self.trace.append({'type': 'structure_correction', 'node': stage, 'detail': detail})
                if correction:
                    raise ModelError('本轮结构化结果未能完成，请保留已核实信息后重试。') from None
                if response.tool_calls:
                    messages.append({'role': 'assistant', 'content': response.content or None, 'tool_calls': [
                        {'id': c.id, 'type': 'function', 'function': {'name': c.name, 'arguments': _json(c.arguments)}}
                        for c in response.tool_calls]})
                    messages.extend({'role': 'tool', 'tool_call_id': c.id,
                                     'content': _json({'error': detail, 'retry': '请修正结构后重新提交'})}
                                    for c in response.tool_calls)
                else:
                    messages.extend([{'role': 'assistant', 'content': response.content},
                                     {'role': 'user', 'content': _json({'error': detail})}])
        raise AssertionError('unreachable correction')

    def _plan(self, state):
        """``plan_turn`` (a broken shape goes back once, then the turn fails), then each item checked on its own (K20):
        an invalid item is dropped with a trace entry and the others go on; the one documented fee rule settles a fee
        object the plan or the wording already decides (understand.settle_fee_subjects)."""
        from .business import BusinessAdapter
        catalog = BusinessAdapter.order_catalog(self.store, self.customer_id)
        own = [row['order_id'] for row in catalog['orders']]
        snapshot = state['snapshot']
        context = understand.context(state['message'], state['history'], catalog['orders'], snapshot)
        plan = self._structured(understand.PLAN_TURN, understand.PROMPT, context, 'understand',
                                parameters=understand.schema(own), convert=lambda value: understand.parse(value, own),
                                description=understand.DESCRIPTION)
        self.trace.append({'type': 'plan', 'plan': deepcopy(plan)})
        kept, rejected = understand.check(plan, state['message'], state['history'], catalog,
                                          [card['order_id'] for card in snapshot.get('open_cards') or []])
        self.trace.extend({'type': 'item_rejected', **row} for row in rejected)
        notes = []
        settled = understand.settle_fee_subjects(plan, kept, state['message'])
        if settled is not None:
            self.trace.append({'type': 'fee_object_resolved', **settled})
            if settled['note']:
                notes.append(settled['note'])
        return {'plan': plan, 'plan_items': kept, 'rejected_items': rejected, 'plan_notes': notes}

    def _business(self, state):
        """Every kept business item executed by its row of the decision table (business, decide.py); a dropped
        return or withdrawal item adds the degraded outcome (K20)."""
        from .business import BusinessAdapter
        self.control.check()
        rejected = [row for row in state.get('rejected_items', []) if row['kind'] in understand.ACTION_KINDS]
        results = BusinessAdapter(self.store, self.customer_id, self.session_id, self.run_id).apply(
            [item for item in state['plan_items'] if item['kind'] in understand.BUSINESS_KINDS], rejected=rejected)
        for result in results:
            # Trace-only notes (order_resolution) never reach the answer context.
            self.trace.extend(result.pop('trace_notes', []))
            self.trace.append({'type': 'business', 'result': deepcopy(result)})
        return {'business_results': results, 'snapshot': self.store.get_session(self.customer_id, self.session_id)}

    def _policy(self, state):
        """B6: the turn's whole card catalogue (its pinned version, every card checked against the release), the
        directed route lookup (D10, only for an explicit route-table question) and whether the turn asks about the rules
        (``policy_needed``, read by the turn items and by policy_status, also when the answer then fails). Every turn,
        whatever the plan says: nothing here waits on a model or on the planner deciding to search."""
        from .policy import PolicyAdapter
        self.policy = PolicyAdapter(self.config, knowledge=self.policy_knowledge, card_catalog=self.card_catalog)
        route = resolve_user_route(state['message'], state['history'])
        reference = {**route, 'source_amount_text': None}
        if route['status'] == 'resolved':
            checked = self.policy.route_reference(route['origin'], route['destination'])
            reference.update(checked)
            self.trace.append({'type': 'directed_route_lookup', 'result': deepcopy(checked)})
        elif route['status'] != 'not_requested':
            self.trace.append({'type': 'directed_route_lookup', 'result': deepcopy(reference)})
        # Whether the turn asks about the rules: a policy question, a fee question (its subject declared or unknown)
        # or a route-table question (B7: the planner's items).
        needed = (any(item['kind'] == understand.POLICY_QUESTION or item['fee_subject'] != 'none'
                      for item in state.get('plan_items', [])) or route['status'] != 'not_requested')
        return {'catalogue': self.policy.catalogue_cards(), 'route_reference': reference, 'policy_needed': needed}

    def _answer(self, state):
        """Turn items, then ``respond`` unless code has handled every item, then the single assembler (§5.3)."""
        from .business import BusinessAdapter
        results = state['business_results']
        route = state.get('route_reference', {'status': 'not_requested'})
        cards = state.get('catalogue', [])
        owned = self.store.owned_order_ids(self.customer_id)
        typed = [token for token in dict.fromkeys(ORDER_TOKEN.findall(state['message'])) if token not in owned]
        business = [block for block in (assemble.business_block(result, foreign_ids=typed) for result in results)
                    if block]
        action_blocks = list(business)
        # B8 (HR2 minimal, §5.3 order 1): a safety item's line comes before everything else, by its subkind; the
        # ticket its handoff, complaint and safety items ask for is opened by the store when it finalizes the turn.
        safety = list(dict.fromkeys(text.SAFETY.get(item['subkind'], text.SAFETY['none'])
                                    for item in state['plan_items'] if item['kind'] == 'safety'))
        if safety:
            action_blocks.insert(0, {'type': 'text', 'author': assemble.CODE, 'safety': True, 'text': '\n'.join(safety)})
        wanted = decide.ticket_request(state['plan_items'])
        if wanted:
            self.trace.append({'type': 'ticket_requested', **wanted})
        not_found = any(block.get('error_code') == 'ORDER_NOT_FOUND' for block in business)
        if typed and not not_found:
            # The message names an order this account does not have and no outcome says so yet: code says it (I4).
            action_blocks.append(assemble.not_in_account_block(typed))
            self.trace.append({'type': 'identity_boundary', 'order_ids': typed})
        # Each outcome with the own orders it is about (a failed one is about none), and so each item by its index
        # (B7r: a kept and a dropped item of one kind never share an entry).
        outcomes = [(result, BusinessAdapter._result_orders(result) if result.get('ok') is True else [])
                    for result in results]
        orders = {index: ids for result, ids in outcomes for index in result.get('item_indexes', [])}
        # A dropped question or read, or a read the code could not do, leaves the user's words to the answer; a dropped
        # return or withdrawal has the code's own sentence (its degraded outcome).
        unanswered = (any(row['kind'] not in understand.ACTION_KINDS for row in state.get('rejected_items', []))
                      or any(result.get('item_kind') == 'own_order_status' and result.get('ok') is not True
                             for result in results))
        items = respond.turn_items(state['plan_items'], state['message'], orders=orders,
                                   policy_needed=state.get('policy_needed', False), unanswered=unanswered)
        self.trace.append({'type': 'turn_items', 'items': deepcopy(items)})
        # The answer model is called when some item is not one code has handled completely (§2.3 step 7).
        answer, answering = None, {item['index'] for item in items if item['kind'] in respond.ANSWER_KINDS}
        # What the code's own blocks state this turn, which the words may repeat (guards G1, B5e).
        said = {'orders_shown': any(block.get('orders') for block in business),
                'order_not_in_account': bool(typed) or not_found}
        stated = [{'type': kind, 'scope': 'turn'} for kind, true in said.items() if true]
        if answering:
            answer = self._respond(state, items, cards, route, results, outcomes, stated)
        else:
            self.trace.append({'type': 'business_render', 'status': [result.get('status') for result in results]})
        listed = {row.get('order_id') for block in business for key in ('orders', 'candidate_orders')
                  for row in block.get(key) or [] if isinstance(row, dict)}
        chosen = set((answer or {}).get('show_order_ids') or [])
        returns = self.store.owned_return_requests(self.customer_id)
        views = [order_view(order, returns.get(order.order_id)) for order in self.store.owned_orders(self.customer_id)
                 if order.order_id in chosen - listed]
        if chosen:
            self.trace.append({'type': 'order_facts_shown', 'order_ids': [view['order_id'] for view in views],
                               'not_shown': sorted(chosen - {view['order_id'] for view in views})})
        facts = assemble.order_facts_block(views, above='\n'.join(block.get('text', '') for block in business))
        rows, route_block = (answer or {}).get('items', []), assemble.route_block(route)
        ask, next_step = (answer or {}).get('ask', {}), (answer or {}).get('next_step', '')
        # K23: the code's route block speaks for a route turn's items (no line beside it).
        shown = assemble.item_blocks(rows, cards, answering=set() if route_block else answering,
                                     ask_shown=bool(ask.get('text')), code_shown=bool(action_blocks or facts))
        answered = [row for row in rows if row['index'] in answering]
        # B7r: the fallback only for a reply that shows nothing at all; an open card's reminder line is content too.
        reply, blocks = assemble.assemble(
            action_blocks=action_blocks, order_facts=facts, items=shown, route=route_block, next_step=next_step,
            ask=ask,
            notes=[PLAN_NOTE_TEXT[note] for note in state.get('plan_notes', []) if note in PLAN_NOTE_TEXT],
            lead=not (action_blocks or route_block) and bool(answered)
            and all(row['disposition'] in {'refused', 'gap'} for row in answered),
            fallback=bool(answering) and not (state.get('snapshot') or {}).get('open_cards'))
        if any(block.get('fallback') for block in blocks):
            self.trace.append({'type': 'fallback_used', 'reason': 'no_visible_content'})
        evidence = list(dict.fromkeys(identifier for block in shown if block['type'] == 'policy_card'
                                      for identifier in block['evidence_ids']))
        sources = self.policy.source_cards(evidence) if evidence and self.policy is not None else []
        if route['status'] == 'evidence_found':
            sources = list({card['citation_id']: card for card in
                            [*sources, *route['condition_source_cards'], route['route_source_card']]}.values())
        selection = answer or {}   # the answer's choices and only the words it shows
        # B6: what the answer could cite is the whole catalogue of the turn's version; nothing when it was not called.
        self.trace.append({'type': 'answer_selection', 'selection': deepcopy(selection),
                           'available_policy_card_ids': [card['id'] for card in cards] if answer is not None else [],
                           'selected_evidence_ids': evidence, 'protocol': ANSWER_PROTOCOL})
        return {'answer': selection, 'reply': reply, 'source_cards': sources, 'blocks': blocks, 'items': items,
                'ticket_request': wanted}

    def _respond(self, state, items, cards, route, results, outcomes, stated):
        """``respond`` (a schema break goes back once, then the turn fails, §5.5), tidied, and each piece of its words
        (``note:<index>``, ``next_step``, ``ask``) guarded on its own: a hit drops that piece only (B5f). A route turn
        shows no notes and no next step (K23); PROSE_SLOT_ENABLED off shows no words (K12). ``stated``: what code's own
        blocks say this turn (guard events). Returns the tidied answer with only the words it shows."""
        orders = self.store.owned_orders(self.customer_id)
        returns = self.store.owned_return_requests(self.customer_id)
        facts = [order_facts(order, returns.get(order.order_id)) for order in orders]
        own = [order.order_id for order in orders]
        open_cards = self.store.get_session(self.customer_id, self.session_id)['open_cards']
        context = respond.context(
            message=state['message'], history=state['history'], items=items, orders=facts,
            open_card_order_ids=[card['order_id'] for card in open_cards], done=respond.actions(outcomes),
            cards=cards, route=route)
        schema = respond.respond_schema(items, cards, own)
        pinned = catalogue(self.card_catalog)
        structure = {'items': [{'index': item['index'], 'fee_subject': item['fee_subject'],
                                'verify_path': text.VERIFY_PATHS.get(item['kind'], text.VERIFY_PATH_GENERAL)}
                               for item in items],
                     'cards': {card['id']: card for card in cards}, 'path_ids': tuple(text.PATHS),
                     'option_sets': text.OPTION_SETS, 'own_order_ids': own,
                     'identifiers': (*(card['id'] for card in pinned['cards']), *text.PATHS, *text.OPTION_SETS)}

        def validate(answer):
            problems = guards.check_structure(answer, structure)[0]
            if problems:
                raise ValueError(', '.join(sorted({problem['code'] for problem in problems})))

        answer = self._structured(respond.RESPOND, respond.RESPOND_PROMPT, context, 'answer', parameters=schema,
                                  convert=lambda value: respond.parse(value, schema), validate=validate,
                                  description=respond.RESPOND_DESCRIPTION)
        _, answer, dropped = guards.check_structure(answer, structure)
        if dropped:
            self.trace.append({'type': 'structure_tidied', 'dropped': dropped})
        rows, ask = answer['items'], answer['ask']
        pieces = {part: words.strip() for part, words in [*((f"note:{row['index']}", row['note']) for row in rows),
                                                          ('next_step', answer['next_step']), ('ask', ask['text'])]}
        if route['status'] != 'not_requested' and any(words for part, words in pieces.items() if part != 'ask'):
            self.trace.append({'type': 'route_prose_withheld', 'reason': 'route_result_owned_by_code'})
            pieces = {part: words if part == 'ask' else '' for part, words in pieces.items()}
        if not respond.PROSE_SLOT_ENABLED:   # K12: the path of a guard dropping every piece
            self.trace.append({'type': 'model_words_withheld', 'reason': 'prose_slot_disabled'})
            pieces = dict.fromkeys(pieces, '')
        cited = {claim['card_id'] for row in rows for claim in row['claims']}
        shown = [{'type': 'orders_shown', 'scope': 'turn'}] if answer['show_order_ids'] else []
        checks = guards.context(own_order_ids=own, order_facts=facts,
                                events=self._guard_events(results, returns) + stated + shown,
                                card_state=self._card_state(results, open_cards),
                                selected_cards=[card for card in cards if card['id'] in cited],
                                catalogue_cards=pinned['cards'], source_publishers=pinned['source_publishers'],
                                paths=text.PATHS, route_text=assemble.route_text(route))
        hits = [{**hit, 'part': part} for part, words in pieces.items() for hit in guards.check(words, checks)]
        self.trace.append({'type': 'guards', 'version': guards.GUARDS_VERSION, 'hits': hits})
        pieces.update((hit['part'], '') for hit in hits)
        for row in rows:
            row['note'] = pieces[f"note:{row['index']}"]
        answer['next_step'] = pieces['next_step']
        answer['ask'] = ask if pieces['ask'] else {'text': '', 'option_set_id': 'none'}
        return answer

    @staticmethod
    def _card_state(results, open_cards):
        if any(result.get('ok') is True and result.get('status') == 'awaiting_confirmation' for result in results):
            return 'shown'
        return 'kept' if open_cards else 'none'

    def _guard_events(self, results, returns):
        """The records an action claim may lean on (G1): this account's requests, this session's tickets and their
        events, and a card this turn voided."""
        events = [{'type': 'return_request_submitted', 'scope': 'session', 'order_id': order_id}
                  for order_id in returns]
        events += [{'type': 'card_voided', 'scope': 'turn', 'order_id': result.get('voided_order_id')}
                   for result in results if result.get('ok') is True and result.get('status') == 'card_voided']
        kinds = {'opened': 'ticket_created', 'appended': 'ticket_event_appended', 'withdrawn': 'ticket_withdrawn'}
        for ticket in self.store.list_tickets(self.customer_id):
            if ticket['session_id'] != self.session_id:
                continue
            events += [{'type': kinds[event['kind']], 'scope': 'session', 'ticket': ticket['short_no']}
                       for event in ticket['events'] if event['kind'] in kinds]
        return events

    def run_turn(self, state: TurnState) -> None:
        """understand -> business -> policy -> answer, every step every turn (B6: the policy step no longer waits
        for the planner to ask for a search).

        Each step reads the state so far and returns the fields it sets, which
        are merged into state before the next step; the steps append their own
        trace entries. An exception leaves state with the earlier steps' fields.
        """
        state.update(self._plan(state))
        state.update(self._business(state))
        state.update(self._policy(state))
        state.update(self._answer(state))

    def _envelope(self, state, *, status='completed'):
        snapshot = self.store.get_session(self.customer_id, self.session_id)
        open_cards = snapshot.get('open_cards') or []
        confirmation = {'protocol': 'observed-action-v3', 'status': 'unavailable'}
        reply = state.get('reply', '')
        blocks = list(state.get('blocks') or [])
        if open_cards:
            # Protocol v3: a card binds action_id, version and order; it carries no reason. B3b: a failed
            # turn declares the open cards like any other turn (nothing holds them); the page folds them.
            # B3c: every open card of the session (one per order) is declared in ``cards`` and by its own
            # return_card block; the top-level fields are those of the newest open card.
            bound = [{key: card[key] for key in ('action_id', 'version', 'order_id', 'expires_at')}
                     for card in open_cards]
            confirmation = {'protocol': 'observed-action-v3', 'status': 'available', **bound[-1], 'cards': bound}
            # B8: after a leading safety line and the business block, never before them.
            at = 1 if blocks and blocks[0].get('safety') else 0
            at += 1 if len(blocks) > at and blocks[at].get('type') == 'business' else 0
            for offset, card in enumerate(open_cards):
                blocks.insert(at + offset, return_card_block(card, self.store.owned_order(self.customer_id,
                                                                                          card['order_id'])))
            # A card this reply prepared or re-showed is already stated by its business block (copy.CARD_READY,
            # copy.CARD_KEPT); every other open card gets the waiting line, the reply's last block like any code
            # sentence (B7r: the page draws the blocks, so a line only in the reply string was never seen).
            stated = set()
            for shown in state.get('business_results') or [] if status == 'completed' else []:
                if shown.get('ok') is True and shown.get('status') == 'awaiting_confirmation':
                    stated |= {(shown.get('card') or {}).get('action_id'),
                               *(row.get('action_id') for row in shown.get('return_outcomes') or [])}
            waiting = [card['order_id'] for card in open_cards if card['action_id'] not in stated]
            if waiting:
                line = text.OPEN_CARDS_WAITING.format(order_ids='、'.join(waiting))
                blocks.append({'type': 'text', 'author': assemble.CODE, 'text': line})
                reply = '\n\n'.join(part for part in (reply, line) if part)
        cards = state.get('source_cards', [])
        selection = state.get('answer', {})
        dispositions = {row['index']: row['disposition'] for row in selection.get('items', [])}
        items = [{'index': item['index'], 'kind': item['kind'], 'disposition': dispositions.get(item['index'])}
                 for item in state.get('items', [])]
        policy_status = ('answered' if cards else 'insufficient_evidence' if state.get('policy_needed')
                         else 'not_requested')
        response = {'schema_version': 'unified-conversation-v1', 'conversation_version': 'graph',
            'status': status, 'run_status': status,
            'policy_status': policy_status, 'reply': reply, 'trace': deepcopy(self.trace), 'usage': dict(self.usage),
            'mode': getattr(self.model, 'mode', 'model'), 'confirmation': confirmation,
            'presentation': {'short_answer': reply, 'conditions': [], 'next_steps': [], 'source_cards': cards,
                             **assemble.presentation_payload(blocks)},
            'answer_validation': {'protocol': ANSWER_PROTOCOL, 'valid': status == 'completed',
                'scope': 'item_structure_guards_and_code_rendering_not_semantic_accuracy', 'items': items,
                'prose_shown': any(block.get('type') == 'text' and block.get('author') == 'model' for block in blocks),
                'fallback': any(row['type'] == 'fallback_used' for row in self.trace)},
            'graph': {'nodes': ['understand', 'business'] + (['policy'] if 'catalogue' in state else []) + ['answer'],
                      'plan': state.get('plan'), 'selection': selection,
                      'knowledge_loader': (self.policy_knowledge.configuration_key[0]
                                           if self.policy_knowledge is not None else None),
                      'critic': 'offline_only'}}
        if status == 'completed' and state.get('ticket_request'):
            response['ticket_request'] = deepcopy(state['ticket_request'])   # the store opens it (sessions, B8)
        if self.config is not None:
            response['knowledge_release'] = self.config.release_metadata()
            try:
                catalog = Path(self.card_catalog)
                response['card_catalog'] = {
                    'sha256': sha256(catalog.read_bytes()).hexdigest(),
                    'kb_version_id': catalog.stem if catalog.parent.name == 'versions' else None}
            except OSError:
                response['card_catalog'] = {'sha256': None, 'kb_version_id': None}
        return response

    def send(self, session_id, message, request_id):
        if type(message) is not str or not message.strip():
            raise ValueError('请输入非空消息。')
        turn = self.store.begin_turn(self.customer_id, session_id, request_id, message,
            run_timeout=self.limits.run_timeout, max_model_attempts=self.limits.max_model_attempts,
            max_tool_calls=self.limits.max_tool_calls, mode=getattr(self.model, 'mode', 'model'))
        if turn['replayed']:
            return {**deepcopy(turn['response']), 'replayed': True}
        self.session_id, self.run_id = session_id, turn['run_id']
        self.control = SessionRunControl(self.store, self.customer_id, session_id, self.run_id, self.limits)
        self.trace, self.usage = [], {}
        # User-visible conversation only. Raw prior model proposals do not gain authority.
        history = [m for m in turn['history'] if m.get('role') in {'user', 'assistant'}
                   and not m.get('tool_calls')][-16:]
        state = {'message': message, 'history': history, 'snapshot': self.store.get_session(self.customer_id, session_id)}
        try:
            context = {'conversation_version': 'graph'}
            if self.config is not None and self.config.release_metadata() is not None:
                context['knowledge_release'] = self.config.release_metadata()
            self.control.event('run_context', context)
            self.policy = None
            self.run_turn(state)
            self.control.check()
            response = self._envelope(state)
            return self.store.finish_turn(self.customer_id, session_id, self.run_id,
                [{'role': 'user', 'content': message}, {'role': 'assistant', 'content': response['reply']}], response)
        except RunStopped as exc:
            status, reply, code = exc.status, exc.public_message, exc.status.upper()
        except ModelError as exc:
            status, reply, code = 'model_error', exc.public_message, 'GRAPH_MODEL_ERROR'
        except SessionError as exc:
            status, reply, code = 'failed', exc.public_message, exc.code
        except Exception as exc:
            self.trace.append({'type': 'error', 'exception_type': type(exc).__name__})
            status, reply, code = 'failed', '本轮处理未完成，请保留已核实的信息后重试。', 'GRAPH_RUNTIME_ERROR'
        # B4b: the reply says only that the turn did not finish (the run's own message stays in error); the envelope
        # adds the open cards' state, and fail_turn the one next step (sessions._failure_next_step).
        state['reply'] = FAILED_TURN_TEXT
        response = self._envelope(state, status=status)
        response['error'] = {'code': code, 'message': reply}
        try:
            self.store.fail_turn(self.customer_id, session_id, self.run_id, response['reply'], response=response)
        except SessionError as exc:
            if exc.code not in {'RUN_CANCELLED', 'RUN_INTERRUPTED'}:
                raise
        saved = self.store.begin_turn(self.customer_id, session_id, request_id, message)
        return {**deepcopy(saved['response']), 'replayed': False}
