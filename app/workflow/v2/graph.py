"""LangGraph stage orchestration over the existing bounded v2 operations.

Models, locks, providers, budgets and the authoritative executor remain per-run
runtime dependencies. Graph channels carry business results, not live clients.
No checkpointing, node retries, parallel collection or extra model roles.
"""
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from evidence.contracts import Ticket
from runtime.context import Limits, RunContext
from tools.executor import ToolExecutor
from workflow.v2.contracts import Draft
from workflow.v2.policy import collection_reservation, remaining
from workflow.v2.questions import InvestigationQuestions
from workflow.v2.review import ReviewBatch

ORCHESTRATION_VERSION = 'langgraph_stages_v1'


class GraphState(TypedDict, total=False):
    stop: str
    decisions: int
    failures: list[dict]
    repairs: dict
    draft: Draft
    reviews: ReviewBatch
    drafts: list[dict]
    review_history: list[dict]
    first_pass: dict
    reworks: list[dict]
    challenges: list[dict]
    result: dict


class GraphOutput(TypedDict):
    result: dict


@dataclass(frozen=True)
class RunDependencies:
    operations: Any
    model: Any
    executor: ToolExecutor
    context: RunContext
    events: list
    ticket: Ticket
    questions: InvestigationQuestions | None
    enable_rework: bool
    enable_challenge: bool
    needs_challenge: bool
    reserve: int | None


def collect_stage(state: GraphState, runtime: Runtime[RunDependencies]):
    run = runtime.context
    failures, repairs = list(state['failures']), dict(state['repairs'])
    stop, decisions, _ = run.operations.collect(run.model, run.executor, run.context,
        failures, repairs, reserve_models=run.reserve, questions=run.questions)
    return {'stop': stop, 'decisions': decisions, 'failures': failures, 'repairs': repairs}


def diagnose_stage(state: GraphState, runtime: Runtime[RunDependencies]):
    run = runtime.context
    failures, repairs = list(state['failures']), dict(state['repairs'])
    draft = run.operations.make_draft(run.model, run.executor, run.context, failures, repairs,
        state['stop'], questions=run.questions, reserve_after=1 + int(run.needs_challenge))
    return {'draft': draft, 'failures': failures, 'repairs': repairs}


def review_stage(state: GraphState, runtime: Runtime[RunDependencies]):
    run = runtime.context
    repairs = dict(state['repairs'])
    reviews = run.operations.review_draft(run.operations.model_for(run.model, 'reviewer'),
        state['draft'], run.executor, run.context, repairs=repairs,
        collection=run.operations.collection_view(run.executor),
        allow_correction=remaining(run.context).models > 1 + int(run.needs_challenge))
    draft = state['draft']
    return {'reviews': reviews, 'repairs': repairs,
        'drafts': [{'revision': 1, 'draft': draft.model_dump(mode='json')}],
        'review_history': [{'revision': 1, 'review': reviews.model_dump(mode='json')}],
        'first_pass': {'draft': draft.model_dump(mode='json'), 'review': reviews.model_dump(mode='json'),
            'collection': deepcopy(run.operations.collection_view(run.executor)),
            'evidence': [item.model_dump(mode='json') for item in run.executor.evidence.values()],
            'model_calls': run.context.counts['model_calls'], 'tool_calls': run.context.counts['tool_calls']}}


def after_review(state: GraphState, runtime: Runtime[RunDependencies]):
    run = runtime.context
    if not run.enable_rework or run.ticket.purpose == 'status_check':
        return 'publish'
    return 'challenge' if run.enable_challenge else 'rework'


def challenge_stage(state: GraphState, runtime: Runtime[RunDependencies]):
    run = runtime.context
    reviews, audit = run.operations.run_challenge(run.model, state['draft'], state['reviews'],
                                                 run.executor, run.context)
    first_pass = {**state['first_pass'], 'challenged_review': reviews.model_dump(mode='json')}
    return {'reviews': reviews, 'first_pass': first_pass, 'challenges': [*state['challenges'], audit]}


def rework_stage(state: GraphState, runtime: Runtime[RunDependencies]):
    run = runtime.context
    repairs = dict(state['repairs'])
    # The existing operation owns eligibility, skip audits and the one-round
    # claim. Routing never spends or resets that allowance a second time.
    draft, reviews, audit = run.operations.execute_review_rework(run.model, state['draft'],
        state['reviews'], run.executor, run.context, repairs=repairs,
        enable_challenge=run.enable_challenge, questions=run.questions)
    drafts, history = list(state['drafts']), list(state['review_history'])
    challenges = list(state['challenges'])
    if audit.get('performed'):
        drafts.append({'revision': 2, 'draft': draft.model_dump(mode='json')})
        history.append({'revision': 2, 'review': reviews.model_dump(mode='json')})
        challenges.extend(audit.get('challenges', []))
    return {'draft': draft, 'reviews': reviews, 'repairs': repairs,
        'reworks': [*state['reworks'], audit], 'challenges': challenges,
        'failures': [*state['failures'], *audit.get('failures', [])],
        'decisions': state['decisions'] + audit.get('decisions', 0),
        'drafts': drafts, 'review_history': history}


def publish_stage(state: GraphState, runtime: Runtime[RunDependencies]):
    run = runtime.context
    result = run.operations.publish(state['draft'], state['reviews'], run.executor, run.context,
        state['failures'], state['stop'], run.events, run.ticket, state['decisions'], state['repairs'])
    return {'result': result}


def finalize_stage(state: GraphState, runtime: Runtime[RunDependencies]):
    run = runtime.context
    result = dict(state['result'])
    result.update(drafts=state['drafts'], reviews=state['review_history'], reworks=state['reworks'],
        challenges=state['challenges'], first_pass=state['first_pass'],
        rework_rounds=sum(bool(row.get('performed')) for row in state['reworks']),
        execution_variant='closed_loop' if run.enable_rework else 'single_pass')
    if run.questions is not None:
        result['investigation_questions'] = run.questions.snapshot(run.executor, state['draft'], state['reviews'])
    history = [{'origin': 'execution', **error} for error in state['failures']]
    for record in state['review_history']:
        history.extend({'origin': 'review', 'revision': record['revision'], **error}
                       for error in record['review'].get('errors', []))
    for index, record in enumerate(state['challenges'], 1):
        history.extend({'origin': 'challenge', 'challenge_index': index, **error}
                       for error in record.get('errors', []))
    result['protocol_failure_history'] = history
    result['semantic_review'].update(challenge_enabled=bool(run.enable_rework and run.enable_challenge),
        challenge_policy_version='blind_scope_challenge_v1' if run.enable_rework and run.enable_challenge else None)
    result['final'] = run.operations.render_report(result)
    run.context.emit({'type': 'workflow_completed', 'role': 'workflow', 'engine_id': run.operations.ENGINE_ID,
                      'status': result['status'], 'reason': result['run_summary']['termination_reason']})
    result['events'] = list(run.events)
    return {'result': result}


class RecoveryGraphState(TypedDict, total=False):
    snapshot: str
    sha256: str


def build_graph(checkpointer=False):
    durable = checkpointer is not False
    graph = StateGraph(RecoveryGraphState if durable else GraphState,
        context_schema=RunDependencies, output_schema=RecoveryGraphState if durable else GraphOutput)
    def guarded(name, operation):
        def stage(state: RecoveryGraphState, runtime: Runtime[RunDependencies]):
            journal = runtime.context.context.journal
            return journal.stage(name, operation, journal.state, runtime.context)
        return stage
    for name, operation in (('collect', collect_stage), ('diagnose', diagnose_stage),
            ('review', review_stage), ('challenge', challenge_stage), ('rework', rework_stage),
            ('publish', publish_stage), ('finalize', finalize_stage)):
        graph.add_node(name, guarded(name, operation) if durable else operation)
    graph.add_edge(START, 'collect')
    graph.add_edge('collect', 'diagnose')
    graph.add_edge('diagnose', 'review')
    graph.add_conditional_edges('review', after_review,
        {'challenge': 'challenge', 'rework': 'rework', 'publish': 'publish'})
    graph.add_edge('challenge', 'rework')
    graph.add_edge('rework', 'publish')
    graph.add_edge('publish', 'finalize')
    graph.add_edge('finalize', END)
    return graph.compile(checkpointer=checkpointer, name='incident_v2')


GRAPH = build_graph()


def run_graph(model, provider, principal, *, context=None, limits=None, event_log=None,
              enable_rework=False, enable_challenge=True):
    from workflow.v2 import engine
    ticket = provider.ticket()
    provider.authorize(principal, ticket.scope)
    events = event_log if event_log is not None else []
    if context is None:
        def sink(event):
            events.append({**event, 'seq': len(events) + 1})
        context = RunContext(scope=ticket.scope, limits=limits or Limits.from_env(), emit=sink)
    elif context.scope != ticket.scope or event_log is None:
        raise ValueError('shared v2 context requires matching scope and event log')
    # Keep the native evaluation hook at engine.ToolExecutor: instrumentation
    # observes this same executor without changing its arguments or policies.
    executor = engine.ToolExecutor(provider, principal, ticket.scope, context, role='single')
    questions = InvestigationQuestions(executor) if enable_rework else None
    needs_challenge = enable_rework and enable_challenge and ticket.purpose != 'status_check'
    reserve = collection_reservation(context, enable_challenge=needs_challenge) if enable_rework else None
    if enable_rework and ticket.purpose == 'status_check':
        reserve = 1
    run = RunDependencies(engine, model, executor, context, events, ticket, questions,
                          enable_rework, enable_challenge, needs_challenge, reserve)
    initial = {'repairs': {'repairs': 0, 'reviewer_repairs': 0}, 'failures': [],
               'reworks': [], 'challenges': []}
    if context.journal is not None:
        from storage.checkpoints import FencedPostgresSaver
        from storage.recovery import ResultUnknown, RecoveryConflict
        from workflow.v2.snapshot import restore
        journal = context.journal
        journal.state, journal.run = initial, run
        graph = build_graph(FencedPostgresSaver(journal.backend))
        config = {'recursion_limit': 16, 'configurable': {'thread_id': context.run_id}}
        checkpoint = graph.get_state(config)
        try:
            graph.invoke(None if checkpoint.created_at else {'snapshot': 'initial', 'sha256': ''},
                         context=run, config=config, durability='sync')
            completed = journal.backend.snapshot('finalize', 'done')
            if completed is None:
                raise RecoveryConflict('Graph finished without its final business snapshot')
            journal.state = restore(completed, run)
            return journal.state['result']
        except ResultUnknown:
            journal.stage_name = None
            journal.authoritative_counts()
            from workflow.v2.contracts import source_facts
            from workflow.v2.review import ReviewBatch
            state = journal.state
            draft = state.get('draft') or Draft(facts=source_facts(executor), claims=[], next_steps=[], gaps=[])
            draft = draft.model_copy(update={'facts': source_facts(executor),
                'gaps': [*draft.gaps, '中断时存在结果未知的调用，已保留预算扣减；未自动重发。']})
            reviews = state.get('reviews') or ReviewBatch()
            state.update(draft=draft, reviews=reviews, stop='RECOVERY_RESULT_UNKNOWN',
                decisions=state.get('decisions', 0), repairs=state.get('repairs', initial['repairs']),
                failures=[*state.get('failures', []), {'stage': 'recovery', 'category': 'transport', 'code': 'RECOVERY_RESULT_UNKNOWN'}],
                drafts=state.get('drafts', []), review_history=state.get('review_history', []),
                first_pass=state.get('first_pass', {}))
            state.update(publish_stage(state, type('RuntimeView', (), {'context': run})()))
            state['result'].update(status='partial', business_result='needs_information')
            state['result']['run_summary']['termination_reason'] = 'RECOVERY_RESULT_UNKNOWN'
            state.update(finalize_stage(state, type('RuntimeView', (), {'context': run})()))
            return state['result']
    # Graph steps bound orchestration only. Existing RunContext remains the sole
    # authority for model/tool/time limits, including calls inside stage loops.
    return GRAPH.invoke(initial, context=run, config={'recursion_limit': 16})['result']
