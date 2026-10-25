"""Usage, durable native checkpoints, and bounded long-task context behavior."""
import copy
import json
import threading
import unittest

from miniagent.agent import Agent
from miniagent.model import AnthropicModel, ModelReply, OpenAIModel, ToolCall
from miniagent.tools import Tool, ToolArgs
import miniagent.tools as execution
from tests.test_agent import ScriptedModel, calls, final
from tests.test_model_streaming import anthropic, frame, openai, provider_server


class EmptyArgs(ToolArgs):
    pass


class RecordingModel(ScriptedModel):
    def __init__(self, *replies):
        super().__init__(*replies)
        self.prompts = []

    def complete(self, messages, tools, system_prompt):
        self.prompts.append(system_prompt)
        return super().complete(messages, tools, system_prompt)


def with_usage(reply, **usage):
    reply.usage = usage
    return reply


class UsageTests(unittest.TestCase):
    def test_optional_usage_is_backward_compatible(self):
        self.assertIn('usage', ModelReply.__dataclass_fields__)
        self.assertIsNone(final().usage)

    def test_complete_usage_is_normalized_and_invalid_optional_fields_are_ignored(self):
        payload = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': 10, 'completion_tokens': 4, 'total_tokens': 14}}
        self.assertEqual(getattr(OpenAIModel._reply(payload), 'usage', None),
                         {'input_tokens': 10, 'output_tokens': 4, 'total_tokens': 14})
        payload['usage'] = {'prompt_tokens': True, 'completion_tokens': -1, 'total_tokens': '14'}
        self.assertIsNone(OpenAIModel._reply(payload).usage)
        payload['usage'] = {'prompt_tokens': 10, 'completion_tokens': 4}
        self.assertEqual(OpenAIModel._reply(payload).usage['total_tokens'], 14)

    def test_anthropic_counts_cached_input_once(self):
        reply = AnthropicModel._reply({'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'ok'}],
            'usage': {'input_tokens': 5, 'cache_creation_input_tokens': 20,
                      'cache_read_input_tokens': 10, 'output_tokens': 7}})
        self.assertEqual(getattr(reply, 'usage', None),
                         {'input_tokens': 35, 'output_tokens': 7, 'total_tokens': 42})

    def test_streams_keep_usage_without_new_parameters_on_compatible_endpoints(self):
        chunks = [frame(openai({'content': 'ok'})), frame(openai(reason='stop')),
                  frame({'choices': [], 'usage': {'prompt_tokens': 9, 'completion_tokens': 2, 'total_tokens': 11}}), frame('[DONE]')]
        with provider_server(chunks) as (url, requests, _):
            reply = OpenAIModel('test', 'test-key', url).stream([], [], '', on_delta=lambda _: None)
        self.assertEqual(getattr(reply, 'usage', None),
                         {'input_tokens': 9, 'output_tokens': 2, 'total_tokens': 11})
        self.assertNotIn('stream_options', requests[0]['body'])
        official = OpenAIModel('test', 'test-key')._body([], [], '', stream=True)
        self.assertEqual(official.get('stream_options'), {'include_usage': True})

    def test_anthropic_stream_usage_deltas_are_cumulative(self):
        chunks = [anthropic('message_start', message={'role': 'assistant', 'content': [],
                    'usage': {'input_tokens': 5, 'cache_read_input_tokens': 10, 'output_tokens': 1}}),
                  anthropic('content_block_start', index=0, content_block={'type': 'text', 'text': 'ok'}),
                  anthropic('content_block_stop', index=0),
                  anthropic('message_delta', delta={'stop_reason': None}, usage={'output_tokens': 3}),
                  anthropic('message_delta', delta={'stop_reason': 'end_turn'}, usage={'output_tokens': 7}),
                  anthropic('message_stop')]
        with provider_server(chunks) as (url, _, _):
            reply = AnthropicModel('test', 'test-key', url).stream([], [], '', on_delta=lambda _: None)
        self.assertEqual(getattr(reply, 'usage', None),
                         {'input_tokens': 15, 'output_tokens': 7, 'total_tokens': 22})


class CheckpointTests(unittest.TestCase):
    def test_each_tool_result_is_committed_before_checkpoint(self):
        checkpoints, ids = [], []
        tool = Tool('record', 'Record.', EmptyArgs,
                    lambda: ids.append(execution.execution_run_id()) or len(ids), read_only=True)
        agent = Agent(ScriptedModel(calls(('record', '{}'), ('record', '{}')), final()), [tool])
        def event(value):
            if value['event'] == 'checkpoint':
                self.assertEqual(set(value['data']), {'reason'})
                checkpoints.append((value['data']['reason'], copy.deepcopy(agent.history)))
        agent.on_event = event
        result = agent.run('do two actions')
        self.assertEqual(result.status, 'completed')
        committed = [history for reason, history in checkpoints if reason == 'tool_result']
        self.assertEqual(len(committed), 2)
        self.assertEqual([m['tool_call_id'] for m in committed[0] if m['role'] == 'tool'], ['0'])
        self.assertEqual([m['tool_call_id'] for m in committed[1] if m['role'] == 'tool'], ['0', '1'])
        self.assertEqual(agent.history_archive, agent.history)
        self.assertTrue(ids[0])
        self.assertEqual(ids[0], ids[1])
        self.assertIsNone(execution.execution_run_id())

    def test_anthropic_partial_results_form_one_native_user_block(self):
        replies = [ModelReply('', [ToolCall('a', 'record', '{}'), ToolCall('b', 'record', '{}')],
                    {'role': 'assistant', 'content': [
                        {'type': 'thinking', 'thinking': 'Check.', 'signature': 'signed'},
                        {'type': 'tool_use', 'id': 'a', 'name': 'record', 'input': {}},
                        {'type': 'tool_use', 'id': 'b', 'name': 'record', 'input': {}}]}), final()]
        model = ScriptedModel(*replies)
        model.tool_messages = AnthropicModel('test', 'key').tool_messages
        tool = Tool('record', 'Record.', EmptyArgs, lambda: 'ok', read_only=True)
        agent = Agent(model, [tool])
        snapshots = []
        agent.on_event = lambda event: snapshots.append(copy.deepcopy(agent.history)) if (
            event['event'] == 'checkpoint' and event['data']['reason'] == 'tool_result') else None
        self.assertEqual(agent.run('run twice').status, 'completed')
        self.assertEqual(len(snapshots), 2)
        self.assertEqual([b['tool_use_id'] for b in snapshots[0][-1]['content']], ['a'])
        self.assertEqual([b['tool_use_id'] for b in snapshots[1][-1]['content']], ['a', 'b'])
        self.assertEqual(len(snapshots[1]), 3)
        self.assertEqual(snapshots[1][1]['content'][0]['signature'], 'signed')

    def test_metrics_sum_only_reported_usage_and_mark_partial_coverage(self):
        tool = Tool('record', 'Record.', EmptyArgs, lambda: 'ok', read_only=True)
        model = ScriptedModel(with_usage(calls(('record', '{}')), input_tokens=10, output_tokens=3, total_tokens=13), final())
        events = []
        result = Agent(model, [tool], on_event=events.append).run('task')
        self.assertIsNotNone(getattr(result, 'metrics', None))
        self.assertEqual(result.metrics['model_calls'], 2)
        self.assertEqual(result.metrics['tool_calls'], 1)
        self.assertEqual(result.metrics['known_usage_calls'], 1)
        self.assertEqual(result.metrics['total_tokens'], 13)
        self.assertGreaterEqual(result.metrics['elapsed_seconds'], 0)
        self.assertTrue(any(event['event'] == 'usage' for event in events))
        unknown = Agent(ScriptedModel(final())).run('task').metrics
        self.assertIsNone(unknown['input_tokens'])
        self.assertIsNone(unknown['output_tokens'])
        self.assertIsNone(unknown['total_tokens'])


class ContextTests(unittest.TestCase):
    def long_history(self):
        return [
            {'role': 'user', 'content': 'old question'},
            {'role': 'assistant', 'content': 'old answer ' + 'x' * 61000},
            {'role': 'user', 'content': 'recent question'},
            {'role': 'assistant', 'content': 'recent answer'},
        ]

    def test_compaction_keeps_recent_groups_and_full_archive(self):
        model = RecordingModel(final('Goal: keep working. File: result.csv.'), final('done'))
        agent = Agent(model)
        agent.history = self.long_history()
        original = copy.deepcopy(agent.history)
        events = []
        agent.on_event = events.append
        result = agent.run('new question')
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.answer, 'done')
        self.assertEqual(agent.context_summary, 'Goal: keep working. File: result.csv.')
        self.assertEqual(agent.history[:2], original[-2:])
        self.assertEqual(agent.history_archive[:4], original)
        self.assertIn('Goal: keep working.', model.prompts[-1])
        self.assertEqual(model.definitions[0], [])
        self.assertEqual(result.metrics['compactions'], 1)
        self.assertEqual(agent.compaction_count, 1)
        self.assertEqual([e['data']['status'] for e in events if e['event'] == 'context_compaction'], ['start', 'completed'])

    def test_long_single_task_compacts_only_whole_tool_rounds(self):
        model = RecordingModel(final('Older tool results summarized.'), final('finished'))
        model.tool_messages = AnthropicModel('test', 'key').tool_messages
        agent = Agent(model)
        initial = [{'role': 'user', 'content': 'original long task'}]
        for number in range(12):
            call_id = str(number)
            initial.extend([
                {'role': 'assistant', 'content': [
                    {'type': 'thinking', 'thinking': 'Plan.', 'signature': 'signature-' + call_id},
                    {'type': 'tool_use', 'id': call_id, 'name': 'read_file', 'input': {'path': 'a'}}]},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': call_id, 'content': 'x' * 6000}]}])
        agent.history = copy.deepcopy(initial)
        result = agent.run('continue')
        self.assertEqual(result.answer, 'finished')
        self.assertLess(len(agent.history), len(initial))
        retained = model.calls[-1]
        self.assertEqual(retained[0], initial[0])
        for index, message in enumerate(retained):
            if message['role'] == 'assistant' and isinstance(message['content'], list):
                tool = next(b for b in message['content'] if b['type'] == 'tool_use')
                self.assertEqual(retained[index + 1]['content'][0]['tool_use_id'], tool['id'])
                self.assertEqual(message['content'][0]['signature'], 'signature-' + tool['id'])
        self.assertEqual(agent.history_archive[:len(initial)], initial)

    def test_failed_summary_preserves_history_and_never_executes_tools(self):
        model = RecordingModel(RuntimeError('summary failed'))
        agent = Agent(model)
        agent.history = self.long_history()
        expected = copy.deepcopy(agent.history) + [{'role': 'user', 'content': 'new question'}]
        result = agent.run('new question')
        self.assertEqual(result.status, 'model_error')
        self.assertEqual(agent.history, expected)
        self.assertIn('context', result.error.lower())
        self.assertEqual(result.steps, 1)
        self.assertEqual(result.tool_calls, 0)

    def test_compaction_obeys_budget_and_cancel_before_commit(self):
        agent = Agent(RecordingModel(final('summary')), max_steps=1)
        agent.history = self.long_history()
        result = agent.run('new question')
        self.assertEqual(result.status, 'step_limit')
        self.assertEqual(result.steps, 1)
        self.assertEqual(agent.compaction_count, 1)
        stop = threading.Event()
        class StopModel(RecordingModel):
            def stream(self, messages, tools, system_prompt, **kwargs):
                stop.set()
                return final('summary')
        agent = Agent(StopModel(), stream=True)
        agent.history = self.long_history()
        before = copy.deepcopy(agent.history)
        result = agent.run('new question', should_stop=stop.is_set)
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(agent.history[:-1], before)
        self.assertEqual(agent.compaction_count, 0)

    def test_reset_clears_all_memory_state(self):
        agent = Agent(ScriptedModel(final()))
        agent.run('hello')
        agent.context_summary = 'previous summary'
        agent.compaction_count = 3
        agent.reset()
        self.assertEqual(getattr(agent, 'history_archive', None), [])
        self.assertEqual(agent.context_summary, '')
        self.assertEqual(agent.compaction_count, 0)

    def test_stop_at_compaction_start_does_not_make_a_request(self):
        stop = threading.Event()
        model = RecordingModel(final('must not be requested'))
        agent = Agent(model)
        agent.history = self.long_history()
        agent.on_event = lambda event: stop.set() if (
            event['event'] == 'model_start' and event['data'].get('purpose') == 'context_compaction') else None
        result = agent.run('new question', should_stop=stop.is_set)
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(model.calls, [])

    def test_compaction_skips_unresolved_native_calls(self):
        from miniagent.context import plan_compaction
        history = self.long_history() + [calls(('record', '{}')).message]
        self.assertIsNone(plan_compaction(history))

    def test_summary_input_omits_opaque_provider_blocks(self):
        from miniagent.context import plan_compaction, summary_messages
        history = self.long_history()
        history[1]['content'] = [
            {'type': 'thinking', 'thinking': 'Visible planning ' + 'x' * 61000, 'signature': 'private-signature'},
            {'type': 'redacted_thinking', 'data': 'opaque-block'},
            {'type': 'text', 'text': 'old answer'},
        ]
        history.append({'role': 'user', 'content': 'new question'})
        plan = plan_compaction(history)
        self.assertIsNotNone(plan)
        encoded = json.dumps(summary_messages(plan, 'prior summary'))
        self.assertNotIn('private-signature', encoded)
        self.assertNotIn('opaque-block', encoded)
        self.assertIn('old answer', encoded)


if __name__ == '__main__':
    unittest.main()
