"""Read-only CORAL cooperation reports bound to saved formal and controller evidence."""
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sqlite3

from proteus.artifacts.codecs import bytes_hash
from proteus.artifacts.store import ArtifactStore, _read
from proteus.core.contracts import DataRef, OperationStatus, require
from proteus.core.ledger import Amounts, DIMENSIONS
from proteus.core.run_lock import run_lock
from proteus.core.search import ArchiveSnapshot, Objective, SearchDecision, SelectionView
from proteus.core.serialization import content_hash
from proteus.evaluation.records import EvaluationReceipt, sum_usage
from proteus.evidence.store import EvidenceStore
from proteus.events.records import EventDecision, InterventionDispatch, InterventionPlan
from proteus.events.service import intervention_prompt
from proteus.gateway.records import GatewayClosure, GatewayIdentity, GatewayResult, GatewayTicket
from proteus.knowledge.records import KnowledgeIdentity, KnowledgeOutcome, KnowledgeRevision, KnowledgeSnapshot
from proteus.methods.coral.records import CoralMethodOptions, CoralSummary
from proteus.recipes.export import SavedRun, _total
from proteus.workers.records import WorkerCompletion, WorkerIdentity, WorkerLaunch


def database(run, relative, version, tables):
    path = run.root/relative
    require(path.resolve().is_relative_to(run.root) and not path.is_symlink(), 'invalid report database path')
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        require(db.execute('PRAGMA user_version').fetchone()[0] == version, 'unsupported report database schema')
        values = {table: sorted((dict(row) for row in db.execute('SELECT * FROM '+table)),
                                key=lambda row: json.dumps(row, sort_keys=True)) for table in tables}
    # Gateway credential rows are deliberately never selected or hashed.
    run.evidence[relative+'#logical-snapshot'] = content_hash(values)
    return values


def bound_view(run, ref, access=None):
    store = EvidenceStore(run.root/'evidence')
    view = store.verify(ref)
    require((view.run_id, view.config_hash, view.lock_hash) ==
            (run.manifest.run_id, run.manifest.config_hash, run.manifest.lock_hash), 'foreign shared evidence')
    if access is not None:
        require(set(view.access.evaluation_ids) <= set(access.evaluation_ids)
                and set(view.access.splits) <= set(access.splits)
                and set(view.access.kinds) <= set(access.kinds), 'shared evidence exceeds visibility')
    for entry in view.entries:
        store.read(ref, entry.path)
    return view


def worker_records(run, summary, accounting):
    specs = {item.id: item for item in run.recipe.workers}
    launches, completions, rows = {}, {}, []
    worker_entries = [entry for entry in run.ledger_entries.values()
                      if entry['identity']['metadata'].get('kind') == 'worker']
    for path in sorted((run.root/'private/workers').glob('*/*/request.json')):
        prefix = path.parent.relative_to(run.root).as_posix()
        launch = WorkerLaunch.from_dict(run.read(prefix+'/request.json'))
        completion = WorkerCompletion.from_dict(run.read(prefix+'/completion.json'))
        name = launch.identity.worker_id
        require(name in specs and launch.identity == WorkerIdentity(run_id=run.manifest.run_id,
            config_hash=run.manifest.config_hash, lock_hash=run.manifest.lock_hash,
            worker_id=name, role=specs[name].role), 'worker identity differs')
        require(launch.runtime == specs[name].runtime.plugin and dict(launch.options) == dict(specs[name].runtime.options),
                'worker runtime differs from recipe')
        digest = content_hash(launch)
        require(digest not in launches and completion.request_hash == digest, 'worker completion binding differs')
        require(path.parent.parent.name == content_hash(name).split(':')[1]
                and path.parent.name == content_hash(launch.operation_id).split(':')[1], 'worker storage identity differs')
        matches = [entry for entry in worker_entries if entry['settlement']['result'].get('request_hash') == digest]
        require(len(matches) == 1 and matches[0]['settlement']['result'] == completion.to_dict()
                and content_hash(matches[0]['identity']['request']) == digest, 'worker completion differs from ledger')
        require(matches[0]['id'] == 'worker:'+digest and matches[0]['identity']['metadata'] ==
                {'kind':'worker','worker_id':name,'role':specs[name].role,'operation_id':launch.operation_id},
                'worker accounting attribution differs')
        require(Amounts.from_dict(matches[0]['settlement']['actual']) == Amounts.from_usage(completion.usage),
                'worker usage differs from ledger')
        for stream in ('stdout','stderr'):
            relative = prefix+'/'+stream+'.log'
            raw = _read(run.root/relative)
            require(bytes_hash(raw) == getattr(completion, stream+'_hash'), 'worker stream hash differs')
            run.evidence[relative] = bytes_hash(raw)
        launches[digest], completions[digest] = launch, completion
        rows.append({'worker_id': name, 'operation_id': launch.operation_id, 'request_hash': digest,
            'runtime': launch.runtime.to_dict(), 'continuity': launch.continuity,
            'previous_request_hash': launch.previous.request_hash if launch.previous else None,
            'session_hash': content_hash(completion.session_id) if completion.session_id else None,
            'status': completion.status, 'control': completion.control, 'return_code': completion.return_code,
            'usage': completion.usage.to_dict(),
            'session_usage': completion.session_usage.to_dict() if completion.session_usage else None,
            'ledger_entry_id': matches[0]['id']})
    require(len(rows) == len(worker_entries) and set(summary.workers) == set(specs), 'worker coverage differs')
    for digest, launch in launches.items():
        if launch.previous is not None:
            old = launch.previous.request_hash
            require(old in launches and launches[old].ref() == launch.previous, 'worker predecessor is missing')
            if launch.continuity == 'native':
                require(launch.session_id == completions[old].session_id, 'worker session identity differs')
                require(launch.session_usage == completions[old].session_usage, 'worker cumulative usage baseline differs')
                if completions[digest].status in ('completed', 'paused'):
                    require(completions[digest].resumed_from == launch.session_id, 'native continuation unconfirmed')
        seen = set()
        current = digest
        while current is not None:
            require(current not in seen, 'worker generation cycle')
            seen.add(current)
            previous = launches[current].previous
            current = previous.request_hash if previous else None
    totals = []
    for name, spec in specs.items():
        final = WorkerCompletion.from_dict(run.value(summary.workers[name]))
        require(final.request_hash in completions and final == completions[final.request_hash]
                and launches[final.request_hash].identity.worker_id == name, 'final worker completion differs')
        generations = [row for row in rows if row['worker_id'] == name]
        chain, current = set(), final.request_hash
        while current is not None:
            chain.add(current)
            previous = launches[current].previous
            current = previous.request_hash if previous else None
        require(chain == {row['request_hash'] for row in generations}, 'worker history branches or final generation is stale')
        require(sum(row['continuity'] == 'fresh' for row in generations) == 1, 'worker must have one initial generation')
        restarted = sum(row['operation_id'].startswith('coral:restart:') for row in generations)
        require(summary.restarts.get(name,0) == restarted, 'worker restart count differs')
        entries = [row for row in accounting['entries'] if row['worker_id'] == name]
        totals.append({'worker_id': name, 'runtime': spec.runtime.plugin.to_dict(), 'budget': spec.budget.to_dict(),
            'generations': len(generations), 'restarts': restarted,
            'statuses': dict(Counter(row['status'] for row in generations)),
            'continuity': dict(Counter(row['continuity'] for row in generations)),
            'actual': {key:_total(entries,key) for key in DIMENSIONS},
            'generation_actual': {key:_total([row for row in entries if row['kind']=='worker'],key) for key in DIMENSIONS},
            'generation_usage': sum_usage(completions[row['request_hash']].usage for row in generations).to_dict()})
    return rows, totals, launches


def knowledge_records(run, spec, summary, gateway):
    prefix = 'private/knowledge/'+content_hash(spec.id).split(':')[1]
    data = database(run, prefix+'/knowledge.sqlite3', 1,
                    ('identity','revisions','heads','operations','snapshots','reads'))
    identity = KnowledgeIdentity(run_id=run.manifest.run_id, config_hash=run.manifest.config_hash,
        lock_hash=run.manifest.lock_hash, evidence_access=spec.evidence, space_id=spec.id)
    require(len(data['identity']) == 1 and KnowledgeIdentity.from_json(data['identity'][0]['value']) == identity,
            'knowledge identity differs')
    artifacts = ArtifactStore(run.root/prefix/'artifacts', registry=None)
    revisions, histories, rows, edges = {}, defaultdict(list), [], []
    for raw in data['revisions']:
        revision = KnowledgeRevision.from_json(raw['body'])
        write = revision.write
        require(revision.identity_hash == content_hash(identity) and revision.ref().content_hash == raw['digest']
                and (write.kind,write.key,revision.revision) == (raw['kind'],raw['key'],raw['revision']),
                'knowledge revision differs')
        if write.artifact:
            artifacts.files(write.artifact)
            run.read(prefix+'/artifacts/manifests/'+write.artifact.content_hash.split(':')[1]+'.json')
            for component in write.artifact.components:
                run.evidence[prefix+'/artifacts/blobs/'+component.content_hash.split(':')[1]] = component.content_hash
        revisions[raw['digest']] = revision
        histories[(write.kind,write.key)].append(revision)
        rows.append({'revision_hash':raw['digest'],'kind':write.kind,'key':write.key,'revision':revision.revision,
            'writer_id':write.writer_id,'writer_role':write.writer_role,'operation_id':write.operation_id,
            'deleted':write.artifact is None,'sources':[ref.to_dict() for ref in write.sources]})
    for values in histories.values():
        prior = None
        for index, revision in enumerate(sorted(values,key=lambda row:row.revision),1):
            require(revision.revision == index and revision.write.expected == prior, 'knowledge history has a gap')
            prior = revision.ref()
    require({(row['kind'],row['key']):row['digest'] for row in data['heads']} ==
            {key:max(values,key=lambda row:row.revision).ref().content_hash for key,values in histories.items()},
            'knowledge heads differ from history')
    published = set()
    for raw in data['operations']:
        outcome = KnowledgeOutcome.from_json(raw['outcome'])
        for ref in (outcome.revision,outcome.current):
            if ref is not None:
                require(ref.content_hash in revisions and revisions[ref.content_hash].ref() == ref, 'knowledge outcome is missing')
        if outcome.revision:
            write = revisions[outcome.revision.content_hash].write
            require((write.writer_role or '',write.writer_id,write.operation_id,content_hash(write)) ==
                    (raw['role'],raw['writer'],raw['operation'],raw['request']), 'knowledge operation binding differs')
            require(outcome.revision.content_hash not in published, 'duplicate knowledge publication')
            published.add(outcome.revision.content_hash)
    require(published == set(revisions), 'knowledge publication coverage differs')
    workers = {worker.id for worker in run.recipe.workers}
    for revision in revisions.values():
        for source in revision.write.sources:
            if source.kind == 'proteus.knowledge.revision@1':
                require(source.content_hash in revisions and revisions[source.content_hash].ref() == source,
                        'knowledge source revision is missing')
                writer = revisions[source.content_hash].write.writer_id
                edges.append({'writer_id':revision.write.writer_id,'source_writer_id':writer,
                    'revision_hash':revision.ref().content_hash,'source_hash':source.content_hash,
                    'cross_worker':writer in workers and revision.write.writer_id in workers and writer != revision.write.writer_id})
            else:
                require(source.kind == 'proteus.evidence.view@1', 'unsupported knowledge source')
                bound_view(run, source, spec.evidence)
    snapshots = {}
    for raw in data['snapshots']:
        snapshot = KnowledgeSnapshot.from_json(raw['body'])
        require(content_hash(snapshot) == raw['digest'] and snapshot.identity == identity, 'knowledge snapshot differs')
        keys = []
        for ref in snapshot.revisions:
            require(ref.content_hash in revisions and revisions[ref.content_hash].ref() == ref, 'snapshot revision is missing')
            write = revisions[ref.content_hash].write
            keys.append((write.kind,write.key))
        require(keys == sorted(set(keys)), 'snapshot keys differ')
        snapshots[raw['digest']] = snapshot
    final = KnowledgeSnapshot.from_dict(run.value(summary.knowledge))
    require(final.ref() == summary.knowledge and snapshots.get(summary.knowledge.content_hash) == final,
            'final knowledge snapshot differs')
    readers = {content_hash(['gateway-worker',gateway.id,worker.role,worker.id]):worker.id for worker in run.recipe.workers}
    captures = []
    for raw in data['reads']:
        require(raw['digest'] in snapshots, 'knowledge read snapshot is missing')
        snapshot = snapshots[raw['digest']]
        name = readers.get(raw['reader'])
        captures.append({'worker_id':name,'operation_id':raw['operation'],'snapshot_hash':raw['digest'],
            'revisions':len(snapshot.revisions),'other_worker_revisions_available':sum(
                revisions[ref.content_hash].write.writer_id in workers and
                revisions[ref.content_hash].write.writer_id != name for ref in snapshot.revisions) if name else None})
    return {'revisions':rows,'declared_sources':edges,'snapshot_captures':captures,
        'counts':{'revisions':len(rows),'by_kind':dict(Counter(row['kind'] for row in rows)),
            'conflicts':len(data['operations'])-len(published),
            'cross_worker_source_edges':sum(edge['cross_worker'] for edge in edges),
            'worker_snapshot_captures':sum(row['worker_id'] is not None for row in captures)},
        'measurement':'source edges are declared provenance; captures record available revisions, not file delivery or causal use'}, revisions


def gateway_records(run, spec, summary, cohorts, revisions):
    identity = GatewayIdentity(run_id=run.manifest.run_id, config_hash=run.manifest.config_hash,
                              lock_hash=run.manifest.lock_hash, gateway_id=spec.id)
    data = database(run, 'private/gateways/'+content_hash(spec.id).split(':')[1]+'/queue.sqlite3', 2,
                    ('identity','requests','claims','closure'))
    counts = {item.id:len(item.tasks.task_ids) for item in run.recipe.evaluations if item.id in spec.evaluations}
    expected = {'identity':identity.to_dict(),'members':list(spec.members),'task_counts':counts,
                'pending_limit':spec.max_pending_per_worker,'evaluation_limit':spec.max_evaluations_per_worker}
    require(len(data['identity']) == 1 and json.loads(data['identity'][0]['policy']) == expected,
            'gateway policy differs')
    closure = GatewayClosure.from_dict(run.value(summary.closure))
    require(len(data['closure']) == 1 and GatewayClosure.from_json(data['closure'][0]['body']) == closure
            and closure.identity_hash == content_hash(identity) and closure.reason == summary.reason,
            'gateway closure differs')
    recorded = {GatewayResult.from_dict(run.value(ref)).ticket.id():GatewayResult.from_dict(run.value(ref))
                for ref in (*summary.results,*summary.cancelled)}
    require(len(recorded) == len(summary.results)+len(summary.cancelled), 'duplicate summary submissions')
    cancelled = {GatewayResult.from_dict(run.value(ref)).ticket.id() for ref in summary.cancelled}
    require(cancelled == {ticket.id() for ticket in closure.pending}, 'cancelled queue differs from closure')
    claims = [GatewayTicket.from_json(row['ticket']) for row in data['claims'] if row['ticket'] is not None]
    require(len(claims) == len({ticket.id() for ticket in claims}), 'submission claimed repeatedly')
    rows, tickets = [], {}
    for raw in sorted(data['requests'],key=lambda row:row['sequence']):
        ticket, result = GatewayTicket.from_json(raw['ticket']), GatewayResult.from_json(raw['result'])
        require(ticket.id() == raw['id'] and ticket.identity_hash == content_hash(identity)
                and (ticket.worker_id,ticket.operation_id) == (raw['worker'],raw['operation'])
                and ticket.worker_id in spec.members and ticket.evaluation_id in counts
                and ticket.task_count == counts[ticket.evaluation_id], 'gateway ticket differs')
        require(result.ticket == ticket and result.status == raw['status'] and recorded.get(ticket.id()) == result,
                'gateway result differs from summary')
        score = None
        if result.candidate:
            require(run.candidates.get(result.candidate.id) == result.candidate, 'submitted candidate differs')
        if result.status == 'completed':
            require(result.evidence is not None and result.shared_attempt is not None, 'missing shared formal evidence')
            view = bound_view(run,result.evidence)
            stage = next(item for item in run.recipe.stages if item.id == spec.members[ticket.worker_id])
            require(view.stage_id == stage.id and view.role == stage.role and view.access == stage.evidence
                    and view.candidate_ids == (result.candidate.id,), 'gateway evidence visibility differs')
            selected = [row for row in cohorts if row.get('operation_id') == 'gateway:'+ticket.id()+':evaluation']
            require(len(selected) == 1 and selected[0]['evaluation_id'] == ticket.evaluation_id
                    and selected[0]['candidate_id'] == result.candidate.id, 'formal submission cohort differs')
            score = selected[0]['mean_score']
            require(result.shared_attempt.content_hash in revisions
                    and revisions[result.shared_attempt.content_hash].ref() == result.shared_attempt,
                    'formal shared attempt is missing')
            attempt = revisions[result.shared_attempt.content_hash].write
            require(attempt.kind == 'attempts' and attempt.writer_id == ticket.worker_id
                    and attempt.sources == (result.evidence,), 'shared attempt provenance differs')
        tickets[ticket.id()] = ticket
        rows.append({'sequence':raw['sequence'],'ticket_id':ticket.id(),'worker_id':ticket.worker_id,
            'evaluation_id':ticket.evaluation_id,'tasks':ticket.task_count,'status':result.status,
            'cancelled_at_close':ticket.id() in cancelled,'candidate_id':result.candidate.id if result.candidate else None,
            'score':score,'artifact_hash':ticket.artifact.content_hash})
    require(set(tickets) == set(recorded) and {ticket.id() for ticket in claims} == set(tickets)-cancelled,
            'submission/claim coverage differs')
    require(all(tickets.get(ticket.id()) == ticket for ticket in claims), 'claim ticket differs')
    return rows, identity


def event_records(run, spec, summary, gateway_identity, launches, submissions):
    values = [(DataRef.from_dict(item['ref']),item['value']) for item in run.records['values']]
    decisions = [(ref,EventDecision.from_dict(value)) for ref,value in values if ref.kind == 'proteus.event.decision@1']
    decisions.sort(key=lambda pair:pair[1].sequence)
    previous, prior_payload, sources, clocks, rows = None, None, set(), defaultdict(list), []
    formal = {row['ticket_id']:row for row in submissions if not row['cancelled_at_close']}
    observed = set()
    expected_identity = content_hash({'gateway':gateway_identity,'policy':spec})
    for index,(ref,decision) in enumerate(decisions,1):
        require(decision.policy_id == spec.id and decision.identity_hash == expected_identity
                and decision.plugin == spec.operator.plugin and decision.sequence == index
                and decision.previous == previous, 'event decision chain differs')
        if previous:
            require(decision.state_before_hash == content_hash(prior_payload), 'event state chain differs')
        observation = decision.observation
        require(observation.source_key not in sources, 'duplicate event observation')
        sources.add(observation.source_key)
        clocks[observation.clock.invocation].append(observation.clock.monotonic_seconds)
        if observation.result:
            result = GatewayResult.from_dict(run.value(observation.result))
            key = result.ticket.id()
            require(key in formal and key not in observed and observation.worker_id == result.ticket.worker_id,
                    'event formal result differs')
            expected_score = formal[key]['score']
            require(observation.score == expected_score and observation.plateau_sample == (result.status == 'completed'),
                    'event score differs from formal evidence')
            observed.add(key)
        rows.append({'sequence':index,'decision_hash':ref.content_hash,'worker_id':observation.worker_id,
            'kind':observation.kind,'score':observation.score,
            'actions':[{'name':action.name,'worker_id':action.worker_id} for action in decision.actions]})
        previous, prior_payload = ref, decision.payload
    require(observed == set(formal), 'event formal-result coverage differs')
    require(set(summary.decisions) == {ref for ref,decision in decisions if decision.observation.kind == 'evaluation'},
            'summary decision coverage differs')
    interventions, plans = [], set()
    for ref,value in values:
        if ref.kind != 'proteus.event.dispatch@1':
            continue
        dispatch = InterventionDispatch.from_dict(value)
        plan = InterventionPlan.from_dict(run.value(dispatch.plan))
        decision = EventDecision.from_dict(run.value(plan.decision))
        require(plan.decision in {item[0] for item in decisions}
                and dispatch.plan.id not in plans and dispatch.worker.request_hash in launches,
                'intervention generation missing or duplicated')
        plans.add(dispatch.plan.id)
        launch = launches[dispatch.worker.request_hash]
        prompt = intervention_prompt(plan)
        if plan.fresh_orientation is not None:
            prompt = plan.fresh_orientation+'\n\n'+prompt
        require(launch.prompt == prompt and launch.operation_id == 'event:'+dispatch.plan.content_hash
                and launch.continuity == ('native' if plan.resume_policy == 'native' else 'fresh_fallback')
                and plan.fresh_orientation == spec.fresh_orientation, 'intervention prompt or continuity differs')
        require(launch.ref() == dispatch.worker and launch.previous == plan.previous
                and launch.identity.worker_id == plan.worker_id and plan.resume_policy == spec.resume_policy
                and plan.actions == tuple(action for action in decision.actions if action.worker_id == plan.worker_id),
                'intervention dispatch differs')
        interventions.append({'worker_id':plan.worker_id,'decision_hash':plan.decision.content_hash,
            'request_hash':dispatch.worker.request_hash,'previous_request_hash':plan.previous.request_hash,
            'actions':[action.name for action in plan.actions],'resume_policy':plan.resume_policy})
    return {'observations':rows,'interventions':interventions,
        'counts':{'observations':len(rows),'interventions':len(interventions),
            'actions':dict(Counter(name for row in interventions for name in row['actions']))},
        'observed_clock_spans':[{'invocation':key,'seconds':max(values)-min(values),'samples':len(values)}
                                for key,values in sorted(clocks.items())],
        'time_semantics':'clock spans cover recorded observations within one invocation; they exclude unobserved startup/shutdown and are not full experiment wall time'}


def report_coral(root, *, include_private=False):
    root = Path(root).resolve()
    require(root.is_dir(), 'run directory is missing')
    with run_lock(root):
        run = SavedRun(root)
        require(run.recipe.method.plugin.name == 'coral.single_island' and run.result.status == OperationStatus.COMPLETED,
                'CORAL report needs a completed single-island recipe')
        require(set(run.result.outputs) == {'summary'}, 'CORAL result outputs differ')
        summary = CoralSummary.from_dict(run.value(run.result.outputs['summary']))
        options = CoralMethodOptions.from_dict(run.recipe.method.options)
        gateway = next(item for item in run.recipe.gateways if item.id == options.gateway)
        knowledge = next(item for item in run.recipe.knowledge if item.id == gateway.knowledge_space)
        policy = next(item for item in run.recipe.event_policies if item.id == options.event_policy)
        accounting = run.accounting()
        require(accounting['actual'] is not None, 'CORAL report needs managed accounting')
        tasks, cohorts, missing = run.evaluations(include_private)
        run.stages()
        require(not missing, 'CORAL report has unreported evaluations')
        generations, workers, launches = worker_records(run,summary,accounting)
        shared, revisions = knowledge_records(run,knowledge,summary,gateway)
        submissions, identity = gateway_records(run,gateway,summary,cohorts,revisions)
        events = event_records(run,policy,summary,identity,launches,submissions)
        selection = SearchDecision.from_dict(run.value(summary.selection))
        require(selection.role == 'final' and selection.policy == run.recipe.search[options.final_selector]
                and selection.operation_id == 'coral:final-selection', 'CORAL final selection differs')
        view = SelectionView.from_dict(run.value(selection.view))
        archive = ArchiveSnapshot.from_dict(run.value(view.archive))
        require(archive.run_id == run.manifest.run_id
                and archive.candidates == tuple(run.candidates.values()), 'CORAL selection archive differs')
        observed = {item['id']:item for item in run.records['evaluations']}
        require({item.batch.id for item in archive.evaluations} == set(observed)
                and all(observed.get(item.batch.id) == item.batch.to_dict() for item in archive.evaluations),
                'CORAL selection evidence differs')
        require(view.evaluation_id == gateway.evaluations[0]
                and view.objectives == (Objective(metric='score',maximize=not options.minimize),),
                'CORAL selection objective differs')
        for vector in view.vectors:
            require(vector.batch_id in observed, 'selection batch missing')
            batch = observed[vector.batch_id]
            require(batch['candidate_id'] == vector.candidate_id and batch['protocol_hash'] == view.protocol_hash
                    and all(row['score'] is not None for row in batch['rows']), 'selection vector evidence differs')
            score = (1 if not options.minimize else -1)*math.fsum(row['score']/len(batch['rows']) for row in batch['rows'])
            require(vector.scores == (score,), 'selection vector score differs')
        require(set(selection.result.selected) <= {item.candidate_id for item in view.vectors},
                'CORAL selected candidate lacks an eligible score')
        selected = set(selection.result.selected)
        require(selected and selected <= set(run.candidates), 'CORAL selection candidates missing')
        finals = [row for row in cohorts if row.get('evaluation_id') == options.final_evaluation]
        require(options.final_evaluation is None or {row['candidate_id'] for row in finals} == selected,
                'CORAL confirmation candidate coverage differs')
        receipts = [EvaluationReceipt.from_dict(run.value(ref)) for ref in summary.final_evaluations]
        require(len(receipts) == len(finals) and {item.id for item in receipts} == {row['operation_id'] for row in finals},
                'CORAL confirmation receipts differ')
        for receipt in receipts:
            prefix = 'evaluations/'+content_hash(receipt.id).split(':')[1]
            require(EvaluationReceipt.from_dict(run.read(prefix+'/receipt.json')) == receipt,
                    'CORAL confirmation receipt differs from saved evaluation')
        for name in workers:
            name['formal_submissions'] = sum(row['worker_id']==name['worker_id'] and not row['cancelled_at_close'] for row in submissions)
            name['knowledge_publications'] = sum(row['writer_id']==name['worker_id'] and row['kind'] in ('notes','skills') for row in shared['revisions'])
        payload = {'schema_version':1,'report_kind':'coral-cooperation','run_id':run.manifest.run_id,
            'manifest_hash':run.manifest.manifest_hash,'config_hash':run.manifest.config_hash,'lock_hash':run.manifest.lock_hash,
            'source_run':str(root),'snapshot':run.snapshot,'reference':run.recipe.reference.to_dict(),
            'seed':run.recipe.seed,'include_private':include_private,'budget':run.recipe.budget.to_dict(),
            'worker_count':len(workers),'stop_reason':summary.reason,'polls':summary.polls,
            'workers':workers,'generations':generations,'submissions':submissions,'events':events,'knowledge':shared,
            'selected_candidates':sorted(selected),'final_cohorts':finals,
            'baseline_cohorts':[row for row in cohorts if row.get('operation_id')=='coral:baseline'],
            'accounting':accounting,
            'evidence':{path:digest for path,digest in run.evidence.items()
                        if include_private or not (path.startswith('private/') or '/private/' in path)},
            'notes':['Only persisted generation usage is summed; cumulative session counters are shown separately.',
                     'Unknown usage remains null, and equal formal-submission caps do not imply equal monetary cost.',
                     'Knowledge sources are declared provenance; snapshots show available revisions, not GET delivery or causal benefit.',
                     'Final-test scores are private by default; circle-packing confirmation is the same optimization problem, not a held-out task.',
                     'Formal metrics are bound to saved evaluations; this report does not execute an independent domain grader.']}
        return {**payload,'report_hash':content_hash(payload)}


def compare_coral(spec, *, base='.', include_private=False):
    from proteus.recipes.study import compare_runs, _stats
    comparison = compare_runs(spec,base=base,include_private=include_private)
    reports = []
    for case,row in zip(spec.cases,comparison['rows']):
        report = report_coral(Path(base)/case.run,include_private=include_private)
        require(report['manifest_hash']==row['manifest_hash'] and report['seed']==case.seed,
                'CORAL comparison run identity changed')
        require(set(case.evaluation_ids) <= {item['evaluation_id'] for item in report['final_cohorts']},
                'CORAL study must use declared final confirmations')
        require(report['accounting']['actual']['cost_usd'] == row['cost_usd']
                and report['accounting']['actual']['model_calls'] == row['model_calls'], 'comparison accounting changed')
        reports.append({'condition':case.condition,'seed':case.seed,'manifest_hash':report['manifest_hash'],
            'report_hash':report['report_hash'],'worker_count':report['worker_count'],
            'generations':len(report['generations']),'restarts':sum(item['restarts'] for item in report['workers']),
            'interventions':report['events']['counts']['interventions'],
            'knowledge_publications':sum(item['knowledge_publications'] for item in report['workers']),
            'cross_worker_source_edges':report['knowledge']['counts']['cross_worker_source_edges'],
            'worker_snapshot_captures':report['knowledge']['counts']['worker_snapshot_captures'],
            'stop_reason':report['stop_reason'],'budget':report['budget']})
    summaries = []
    for condition in sorted({row['condition'] for row in reports}):
        rows = [row for row in reports if row['condition']==condition]
        require(len({row['worker_count'] for row in rows})==1, 'worker count changes within a condition')
        summaries.append({'condition':condition,'worker_count':rows[0]['worker_count'],
            **{key:_stats([row[key] for row in rows]) for key in ('generations','restarts','interventions',
                'knowledge_publications','cross_worker_source_edges','worker_snapshot_captures')}})
    payload = {'schema_version':1,'report_kind':'coral-study','comparison':comparison,
               'cooperation_rows':reports,'cooperation_summaries':summaries,
               'notes':['Study configuration/protocol checks and seed coverage use the common comparison contract.',
                        'Shared-source counts are not causal estimates of collaboration quality.']}
    return {**payload,'report_hash':content_hash(payload)}


def main():
    import argparse
    from proteus.recipes.study import StudySpec
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('--study',action='store_true',help='read a common StudySpec JSON instead of one run')
    parser.add_argument('--include-private',action='store_true')
    parser.add_argument('--out',type=Path,required=True)
    args = parser.parse_args()
    if args.study:
        spec = StudySpec.from_json(args.source.read_text())
        roots = [(args.source.parent/case.run).resolve() for case in spec.cases]
        report = compare_coral(spec,base=args.source.parent,include_private=args.include_private)
    else:
        roots = [args.source.resolve()]
        report = report_coral(args.source,include_private=args.include_private)
    destination = args.out.resolve()
    require(all(not destination.is_relative_to(root) for root in roots), 'report destination must be outside runs')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open('x',encoding='utf-8') as stream:
        stream.write(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
    print(destination)


if __name__ == '__main__':
    main()
