"""Public work plans and corrections at provider-native execution boundaries."""
import copy
import json
import threading
import unittest

from miniagent.agent import Agent
from miniagent.model import AnthropicModel, ModelReply, ToolCall
from miniagent.tools import Tool, ToolArgs
from tests.test_agent import ScriptedModel, calls, final


class EmptyArgs(ToolArgs):
    pass


def work_plan(**changes):
    value = {
        'goal': 'Produce a checked sales summary.',
        'success_criteria': ['Totals match the input rows.'],
        'steps': [{'title': 'Read the source rows', 'status': 'in_progress'},
                  {'title': 'Write and verify the summary', 'status': 'pending'}],
        'summary': 'The source file is available; read its rows before calculating.',
        'evidence': ['workspace/sales.csv'],
        'uncertainties': ['Currency is not specified.'],
        'verification': {'status': 'not_checked', 'summary': ''},
    }
    value.update(changes)
    return value


class PromptModel(ScriptedModel):
    def __init__(self, *responses):
        super().__init__(*responses)
        self.prompts = []

    def complete(self, messages, tools, system_prompt):
        self.prompts.append(system_prompt)
        return super().complete(messages, tools, system_prompt)


def batch_reply(provider):
    if provider == 'openai':
        return calls(('record', '{}'), ('record', '{}'))
    return ModelReply('', [ToolCall('0', 'record', '{}'), ToolCall('1', 'record', '{}')],
        {'role': 'assistant', 'content': [
            {'type': 'thinking', 'thinking': 'A public provider summary.', 'signature': 'signed-original'},
            {'type': 'tool_use', 'id': '0', 'name': 'record', 'input': {}},
            {'type': 'tool_use', 'id': '1', 'name': 'record', 'input': {}}]})


def native_results(history, provider):
    if provider == 'openai':
        return [(message['tool_call_id'], json.loads(message['content']))
                for message in history if message.get('role') == 'tool']
    return [(block['tool_use_id'], json.loads(block['content']))
            for message in history if message.get('role') == 'user' and isinstance(message.get('content'), list)
            for block in message['content'] if block.get('type') == 'tool_result']


class WorkPlanTests(unittest.TestCase):
    def test_plan_tool_publishes_snapshot_and_keeps_latest_state_in_model_context(self):
        first = work_plan()
        second = work_plan(steps=[{'title': 'Summary checked', 'status': 'completed'}],
                           summary='The written total matches the source rows.', uncertainties=[],
                           verification={'status': 'passed', 'summary': 'Compared both totals: 60.'})
        model = PromptModel(calls(('update_work_plan', json.dumps(first))),
                            calls(('update_work_plan', json.dumps(second))), final('Ready.'))
        agent = Agent(model, planning=True)
        snapshots, events = [], []
        def observe(event):
            events.append(copy.deepcopy(event))
            if event['event'] == 'work_plan':
                snapshots.append(copy.deepcopy(event['data']['plan']))
        agent.on_event = observe
        result = agent.run('Summarize the sales and verify the result.')
        self.assertEqual(result.status, 'completed')
        self.assertEqual([snapshot['revision'] for snapshot in snapshots], [1, 2])
        self.assertEqual(snapshots[0]['verification']['status'], 'not_checked')
        self.assertEqual(agent.work_plan['verification']['status'], 'passed')
        self.assertTrue(agent.work_plan['updated_at'])
        self.assertIn('Compared both totals: 60.', model.prompts[-1])
        self.assertEqual([definition['name'] for definition in model.definitions[0]], ['update_work_plan'])
        first_event = next(i for i, event in enumerate(events) if event['event'] == 'work_plan')
        self.assertEqual(events[first_event + 1]['event'], 'checkpoint')
        self.assertEqual(agent.history_archive, agent.history)

    def test_invalid_plan_cannot_replace_existing_public_state(self):
        agent = Agent(ScriptedModel(), planning=True)
        tool = agent.tools['update_work_plan']
        self.assertTrue(tool.execute(work_plan())['ok'])
        existing = copy.deepcopy(agent.work_plan)
        invalid = [work_plan(goal='   '), work_plan(success_criteria=['x'] * 7),
                   work_plan(summary='x' * 1201), work_plan(revision=99),
                   work_plan(steps=[{'title': 'a', 'status': 'in_progress'},
                                    {'title': 'b', 'status': 'in_progress'}]),
                   work_plan(verification={'status': 'verified_by_host', 'summary': ''})]
        for value in invalid:
            with self.subTest(value=value):
                result = tool.execute(value)
                self.assertFalse(result['ok'])
                self.assertEqual(result['error']['code'], 'invalid_arguments')
                self.assertEqual(agent.work_plan, existing)

    def test_plan_survives_context_compaction_and_simple_answer_needs_no_plan_call(self):
        model = PromptModel(final('Earlier work summarized.'), final('The answer.'))
        agent = Agent(model, planning=True)
        agent.tools['update_work_plan'].execute(work_plan())
        agent.history = [{'role': 'user', 'content': 'old task'},
                         {'role': 'assistant', 'content': 'x' * 61000},
                         {'role': 'user', 'content': 'recent task'},
                         {'role': 'assistant', 'content': 'recent answer'}]
        result = agent.run('Continue with the current goal.')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(agent.compaction_count, 1)
        self.assertIn('Produce a checked sales summary.', model.prompts[-1])
        self.assertIn('workspace/sales.csv', model.prompts[-1])

    def test_reset_clears_public_plan_and_applied_input_ids(self):
        agent = Agent(ScriptedModel(), planning=True)
        agent.tools['update_work_plan'].execute(work_plan())
        agent.applied_input_ids = ['prior-correction']
        agent.reset()
        self.assertIsNone(agent.work_plan)
        self.assertEqual(agent.applied_input_ids, [])

    def test_unchanged_plan_updates_cannot_bypass_repetition_guard(self):
        response = calls(('update_work_plan', json.dumps(work_plan())))
        agent = Agent(ScriptedModel(*[copy.deepcopy(response) for _ in range(12)], final('Never reached.')),
                      planning=True, max_steps=None)
        result = agent.run('Keep working on the sales summary.')
        self.assertEqual(result.status, 'stalled')
        self.assertLess(result.steps, 12)


class SteeringTests(unittest.TestCase):
    def test_initial_acknowledgement_checkpoint_already_contains_new_task(self):
        agent = Agent(ScriptedModel(final('Done.')))
        snapshots = []
        agent.on_event = lambda event: snapshots.append(copy.deepcopy(agent.history)) if (
            event['event'] == 'checkpoint' and agent.applied_input_ids) else None
        agent.run('The new explicit goal.', pending_input=lambda final=False: [
            {'id': 'old-pending', 'message': 'Earlier correction.', 'before_task': True}])
        self.assertTrue(snapshots)
        self.assertTrue(all({'role': 'user', 'content': 'The new explicit goal.'} in history
                            for history in snapshots),
                        'A crash must not persist older corrections without the already-accepted new task.')

    def test_initial_pending_input_order_and_dedup_across_runs(self):
        entries = [{'id': 'older', 'message': 'Use a USD budget.', 'before_task': True},
                   {'id': 'newer', 'message': 'Keep the summary under 100 words.'}]
        model = ScriptedModel(final('First.'), final('Second.'))
        agent = Agent(model)
        events = []
        def observe(event):
            if event['event'] == 'input_applied':
                events.append((copy.deepcopy(event['data']), copy.deepcopy(agent.history),
                               list(agent.applied_input_ids)))
        agent.on_event = observe
        self.assertEqual(agent.run('Actually use an EUR budget.', pending_input=lambda final=False: entries).status,
                         'completed')
        self.assertEqual([item['content'] for item in model.calls[0]],
                         ['Use a USD budget.', 'Actually use an EUR budget.', 'Keep the summary under 100 words.'])
        self.assertEqual(agent.applied_input_ids, ['older', 'newer'])
        self.assertTrue(all(all(identifier in ids for identifier in data['ids']) for data, _, ids in events))
        agent.run('Another question.', pending_input=lambda final=False: entries)
        self.assertEqual(sum(message.get('content') == 'Use a USD budget.' for message in agent.history), 1)
        self.assertEqual(agent.history, agent.history_archive)

    def test_correction_during_model_skips_old_batch_for_both_providers(self):
        for provider in ('openai', 'anthropic'):
            with self.subTest(provider=provider):
                queue, effects = [], []
                class ChangingModel(ScriptedModel):
                    def complete(self, messages, tools, prompt):
                        result = super().complete(messages, tools, prompt)
                        if len(self.calls) == 1:
                            queue.append({'id': 'change', 'message': 'Do not write; explain the proposal first.'})
                        return result
                model = ChangingModel(batch_reply(provider), final('Here is the proposal.'))
                if provider == 'anthropic':
                    model.tool_messages = AnthropicModel('test', 'test-key').tool_messages
                tool = Tool('record', 'Write a record.', EmptyArgs, lambda: effects.append('written'), read_only=True)
                agent = Agent(model, [tool])
                result = agent.run('Write two records.', pending_input=lambda final=False: queue)
                self.assertEqual(result.answer, 'Here is the proposal.')
                self.assertEqual(effects, [])
                results = native_results(model.calls[1], provider)
                self.assertEqual([identifier for identifier, _ in results], ['0', '1'])
                self.assertEqual([payload['error']['code'] for _, payload in results],
                                 ['superseded_by_user', 'superseded_by_user'])
                self.assertEqual(model.calls[1][-1], {'role': 'user', 'content': 'Do not write; explain the proposal first.'})
                if provider == 'anthropic':
                    self.assertEqual(model.calls[1][1]['content'][0]['signature'], 'signed-original')
                self.assertEqual(agent.history, agent.history_archive)

    def test_correction_between_batch_tools_keeps_completed_effect_and_skips_remainder(self):
        for provider in ('openai', 'anthropic'):
            with self.subTest(provider=provider):
                queue, effects = [], []
                def record():
                    effects.append('written')
                    queue.append({'id': 'change', 'message': 'Keep that file, cancel the second write.'})
                    return {'written': len(effects)}
                model = ScriptedModel(batch_reply(provider), final('Kept the first file.'))
                if provider == 'anthropic':
                    model.tool_messages = AnthropicModel('test', 'test-key').tool_messages
                agent = Agent(model, [Tool('record', 'Write.', EmptyArgs, record, read_only=True)])
                result = agent.run('Write two files.', pending_input=lambda final=False: queue)
                self.assertEqual(result.status, 'completed')
                self.assertEqual(effects, ['written'])
                results = native_results(model.calls[1], provider)
                self.assertEqual(results[0][1], {'ok': True, 'result': {'written': 1}})
                self.assertEqual(results[1][1]['error']['code'], 'superseded_by_user')
                self.assertEqual(model.calls[1][-1]['content'], 'Keep that file, cancel the second write.')

    def test_correction_at_tool_start_boundary_prevents_side_effect(self):
        queue, effects = [], []
        model = ScriptedModel(calls(('record', '{}')), final('Changed.'))
        agent = Agent(model, [Tool('record', 'Write.', EmptyArgs, lambda: effects.append(1), read_only=True)])
        agent.on_event = lambda event: queue.append({'id': 'late', 'message': 'Explain only.'}) if (
            event['event'] == 'tool_call') else None
        self.assertEqual(agent.run('Write.', pending_input=lambda final=False: queue).status, 'completed')
        self.assertEqual(effects, [])
        self.assertEqual(native_results(agent.history, 'openai')[0][1]['error']['code'], 'superseded_by_user')

    def test_final_input_boundary_rejects_stale_answer_and_requests_fresh_one(self):
        queue, closed, final_checks = [], [], []
        def pending(final=False):
            if final:
                final_checks.append(True)
                if len(final_checks) == 1:
                    queue.append({'id': 'last-second', 'message': 'Use the corrected total 80.'})
                elif not [item for item in queue if item['id'] not in agent.applied_input_ids]:
                    closed.append(True)
            return queue
        model = ScriptedModel(final('The old total is 60.'), final('The corrected total is 80.'))
        agent = Agent(model)
        result = agent.run('What is the total?', pending_input=pending)
        self.assertEqual(result.answer, 'The corrected total is 80.')
        self.assertEqual(model.calls[1][-1], {'role': 'user', 'content': 'Use the corrected total 80.'})
        self.assertEqual(closed, [True])
        self.assertEqual(len(final_checks), 2)

    def test_stop_wins_before_pending_input_is_consumed(self):
        read = []
        agent = Agent(ScriptedModel(final('Must not run.')))
        result = agent.run('Task.', should_stop=lambda: True,
                           pending_input=lambda final=False: read.append(True) or [{'id': 'keep', 'message': 'Later.'}])
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(read, [])
        self.assertEqual(agent.applied_input_ids, [])

    def test_stop_during_tool_retains_input_for_explicit_continuation_without_replay(self):
        stop, queue, effects = threading.Event(), [], []
        def record():
            effects.append('written')
            queue.append({'id': 'pending', 'message': 'Inspect the existing file before continuing.'})
            stop.set()
            return {'written': True}
        model = ScriptedModel(batch_reply('openai'), final('Existing file retained.'))
        agent = Agent(model, [Tool('record', 'Write.', EmptyArgs, record, read_only=True)])
        result = agent.run('Write two files.', should_stop=stop.is_set, pending_input=lambda final=False: queue)
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(agent.applied_input_ids, [])
        self.assertEqual(native_results(agent.history, 'openai')[1][1]['error']['code'], 'cancelled')
        queue[0]['before_task'] = True
        stop.clear()
        continued = agent.run('Continue, but do not overwrite files.', should_stop=stop.is_set,
                              pending_input=lambda final=False: queue)
        self.assertEqual(continued.answer, 'Existing file retained.')
        self.assertEqual(effects, ['written'])
        self.assertEqual([message['content'] for message in model.calls[1][-2:]],
                         ['Inspect the existing file before continuing.', 'Continue, but do not overwrite files.'])

    def test_budget_exhaustion_never_accepts_answer_superseded_by_correction(self):
        queue = []
        class ChangingModel(ScriptedModel):
            def complete(self, messages, tools, prompt):
                result = super().complete(messages, tools, prompt)
                queue.append({'id': 'change', 'message': 'Consider the revised target.'})
                return result
        agent = Agent(ChangingModel(final('Stale answer.')), max_steps=1)
        result = agent.run('Old target.', pending_input=lambda final=False: queue)
        self.assertEqual(result.status, 'step_limit')
        self.assertEqual(result.answer, '')
        self.assertEqual(agent.history[-1]['content'], 'Consider the revised target.')
        self.assertEqual(agent.applied_input_ids, ['change'])

    def test_applied_id_retention_is_bounded_without_reapplying_current_input(self):
        model = ScriptedModel(final('Done.'))
        agent = Agent(model)
        agent.applied_input_ids = [f'old-{index}' for index in range(510)]
        entries = [{'id': f'new-{index}', 'message': f'Correction {index}.'} for index in range(4)]
        agent.run('Task.', pending_input=lambda final=False: entries)
        self.assertEqual(len(agent.applied_input_ids), 512)
        self.assertEqual(agent.applied_input_ids[0], 'old-2')
        self.assertEqual(agent.applied_input_ids[-4:], ['new-0', 'new-1', 'new-2', 'new-3'])
        self.assertEqual(sum(message.get('content') == 'Correction 0.' for message in agent.history), 1)

    def test_model_and_external_tool_do_not_hold_shared_state_lock(self):
        observed = []
        def other_thread_can_lock():
            values = []
            def probe():
                acquired = agent.state_lock.acquire(timeout=0.3)
                values.append(acquired)
                if acquired:
                    agent.state_lock.release()
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=1)
            observed.extend(values)
            return values == [True]
        class LockCheckingModel(ScriptedModel):
            def complete(self, messages, tools, prompt):
                other_thread_can_lock()
                return super().complete(messages, tools, prompt)
        agent = Agent(LockCheckingModel(calls(('record', '{}')), final('Done.')),
                      [Tool('record', 'Probe.', EmptyArgs, other_thread_can_lock, read_only=True)])
        self.assertEqual(agent.run('Run the probe.', pending_input=lambda final=False: []).status, 'completed')
        self.assertEqual(observed, [True, True, True])


if __name__ == '__main__':
    unittest.main()
