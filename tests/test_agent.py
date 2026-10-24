import copy
import json
import threading
import unittest

import miniagent.tools as tool_runtime
from miniagent.agent import Agent
from miniagent.model import ModelReply, ToolCall
from miniagent.tools import Tool, ToolArgs


class ScriptedModel:
    """Deterministic model substitute used only by tests."""
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.definitions = []

    def complete(self, messages, tools, system_prompt):
        self.calls.append(copy.deepcopy(messages))
        self.definitions.append(copy.deepcopy(tools))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def tool_messages(self, results):
        return [{'role': 'tool', 'tool_call_id': r['id'],
                 'content': json.dumps(r['payload'])} for r in results]


class AddArgs(ToolArgs):
    a: int
    b: int


def calls(*items):
    items = [ToolCall(str(i), name, args) for i, (name, args) in enumerate(items)]
    message = {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': c.id, 'type': 'function', 'function': {'name': c.name, 'arguments': c.arguments}}
        for c in items]}
    return ModelReply('', items, message)


def call(name='add', **args):
    return calls((name, json.dumps(args)))


def final(answer='done'):
    return ModelReply(answer, [], {'role': 'assistant', 'content': answer})


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.executed = []
        def add(a, b):
            self.executed.append((a, b))
            return a + b
        self.tool = Tool('add', 'Add.', AddArgs, add, read_only=True)

    def test_real_tool_result_reaches_next_model_call(self):
        model = ScriptedModel(call(a=2, b=3), final('5'))
        events = []
        result = Agent(model, [self.tool], on_event=events.append).run('2+3')
        self.assertEqual((result.status, result.answer, result.steps, result.tool_calls), ('completed', '5', 2, 1))
        self.assertEqual(self.executed, [(2, 3)])
        self.assertEqual(json.loads(model.calls[1][-1]['content'])['result'], 5)
        self.assertEqual(events[-1]['event'], 'run_end')

    def test_multiple_calls_get_matching_results_before_next_round(self):
        model = ScriptedModel(calls(('add', '{"a":1,"b":2}'), ('add', '{"a":3,"b":4}')), final())
        result = Agent(model, [self.tool]).run('two sums')
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(self.executed, [(1, 2), (3, 4)])
        outputs = model.calls[1][-2:]
        self.assertEqual([m['tool_call_id'] for m in outputs], ['0', '1'])
        self.assertEqual([json.loads(m['content'])['result'] for m in outputs], [3, 7])

    def test_bad_json_never_executes(self):
        for arguments in ['not JSON', '{"a":1,"a":2,"b":3}', '{"a":NaN,"b":2}', '[]']:
            with self.subTest(arguments=arguments):
                model = ScriptedModel(calls(('add', arguments)), final())
                self.assertEqual(Agent(model, [self.tool]).run('task').status, 'completed')
                self.assertEqual(self.executed, [])
                self.assertFalse(json.loads(model.calls[1][-1]['content'])['ok'])

    def test_arguments_are_strict_and_extra_fields_are_rejected(self):
        for args in [{'a': '2', 'b': 3}, {'a': 2, 'b': 3, 'extra': True}]:
            model = ScriptedModel(call(**args), final())
            Agent(model, [self.tool]).run('task')
            self.assertEqual(json.loads(model.calls[1][-1]['content'])['error']['code'], 'invalid_arguments')
            self.assertEqual(self.executed, [])

    def test_unknown_tool_returns_error(self):
        model = ScriptedModel(call('missing'), final())
        Agent(model, []).run('task')
        self.assertEqual(json.loads(model.calls[1][-1]['content'])['error']['code'], 'unknown_tool')

    def test_write_tool_needs_host_approval(self):
        tool = Tool('write', 'Write.', AddArgs, self.tool.handler)
        model = ScriptedModel(call('write', a=1, b=2), final())
        Agent(model, [tool]).run('task')
        self.assertEqual(self.executed, [])
        self.assertEqual(json.loads(model.calls[1][-1]['content'])['error']['code'], 'approval_required')

    def test_approval_follows_validation(self):
        approvals = []
        tool = Tool('write', 'Write.', AddArgs, self.tool.handler)
        model = ScriptedModel(call('write', a='1', b=2), call('write', a=1, b=2), final())
        def approve(tool, args):
            approvals.append(args)
            return True
        Agent(model, [tool], approve=approve).run('task')
        self.assertEqual(approvals, [{'a': 1, 'b': 2}])
        self.assertEqual(self.executed, [(1, 2)])

    def test_denial_never_executes(self):
        tool = Tool('write', 'Write.', AddArgs, self.tool.handler)
        Agent(ScriptedModel(call('write', a=1, b=2), final()), [tool], approve=lambda *_: False).run('task')
        self.assertEqual(self.executed, [])

    def test_tool_exception_is_recoverable(self):
        def broken(a, b):
            raise ValueError('failed')
        tool = Tool('broken', 'Fails.', AddArgs, broken, read_only=True)
        model = ScriptedModel(call('broken', a=1, b=0), call(a=1, b=2), final('3'))
        self.assertEqual(Agent(model, [tool, self.tool]).run('task').status, 'completed')
        self.assertFalse(json.loads(model.calls[1][-1]['content'])['ok'])

    def test_step_limit_is_not_completion(self):
        result = Agent(ScriptedModel(call(a=1, b=2)), [self.tool], max_steps=1).run('task')
        self.assertEqual(result.status, 'step_limit')
        self.assertEqual(result.answer, '')

    def test_model_error_is_not_completion(self):
        result = Agent(ScriptedModel(RuntimeError('network unavailable')), []).run('task')
        self.assertEqual(result.status, 'model_error')
        self.assertIn('network unavailable', result.error)

    def test_history_continues_and_reset_clears_it(self):
        model = ScriptedModel(final('ok'), final('blue'))
        agent = Agent(model, [])
        agent.run('my favorite color is blue')
        agent.run('which color?')
        self.assertEqual(model.calls[1][0]['content'], 'my favorite color is blue')
        agent.reset()
        self.assertEqual(agent.history, [])

    def test_duplicate_tool_name_is_rejected(self):
        with self.assertRaises(ValueError):
            Agent(ScriptedModel(), [self.tool, self.tool])

    def test_non_json_tool_output_is_an_observation(self):
        tool = Tool('bad', 'Bad output.', AddArgs, lambda a, b: {1, 2}, read_only=True)
        model = ScriptedModel(call('bad', a=1, b=2), final())
        Agent(model, [tool]).run('task')
        self.assertEqual(json.loads(model.calls[1][-1]['content'])['error']['code'], 'invalid_tool_result')

    def test_stop_before_request_does_not_call_model(self):
        model = ScriptedModel()
        result = Agent(model, [self.tool]).run('task', should_stop=lambda: True)
        self.assertEqual((result.status, result.steps, result.tool_calls), ('cancelled', 0, 0))
        self.assertEqual(result.answer, '')
        self.assertEqual(model.calls, [])

    def test_stop_after_response_prevents_tools_and_keeps_matching_results(self):
        stop = threading.Event()
        model = ScriptedModel(calls(('add', '{"a":1,"b":2}'), ('add', '{"a":3,"b":4}')))
        def on_event(event):
            if event['event'] == 'model_output':
                stop.set()
        agent = Agent(model, [self.tool], on_event=on_event)
        result = agent.run('task', should_stop=stop.is_set)
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(self.executed, [])
        self.assertEqual([m['tool_call_id'] for m in agent.history[-2:]], ['0', '1'])
        self.assertEqual([json.loads(m['content'])['error']['code'] for m in agent.history[-2:]],
                         ['cancelled', 'cancelled'])

    def test_stop_after_final_response_is_not_completion(self):
        stop = threading.Event()
        def on_event(event):
            if event['event'] == 'model_output':
                stop.set()
        result = Agent(ScriptedModel(final()), on_event=on_event).run('task', should_stop=stop.is_set)
        self.assertEqual((result.status, result.answer), ('cancelled', ''))

    def test_stop_between_tools_preserves_results_and_next_turn_history(self):
        stop = threading.Event()
        def add_and_stop(a, b):
            value = self.tool.handler(a=a, b=b)
            stop.set()
            return value
        tool = Tool('add', 'Add.', AddArgs, add_and_stop, read_only=True)
        model = ScriptedModel(calls(('add', '{"a":1,"b":2}'), ('add', '{"a":3,"b":4}')),
                              final('next turn'))
        agent = Agent(model, [tool])
        result = agent.run('first turn', should_stop=stop.is_set)
        self.assertEqual((result.status, result.steps), ('cancelled', 1))
        self.assertEqual(self.executed, [(1, 2)])
        self.assertEqual(len(model.calls), 1)
        stop.clear()
        self.assertEqual(agent.run('continue', should_stop=stop.is_set).answer, 'next turn')
        outputs = model.calls[1][-3:-1]
        self.assertEqual([m['tool_call_id'] for m in outputs], ['0', '1'])
        self.assertEqual(json.loads(outputs[0]['content'])['result'], 3)
        self.assertEqual(json.loads(outputs[1]['content'])['error']['code'], 'cancelled')

    def test_stop_during_model_error_takes_precedence(self):
        stop = threading.Event()
        class StoppingModel(ScriptedModel):
            def complete(self, *args):
                stop.set()
                raise TimeoutError('request timed out')
        result = Agent(StoppingModel()).run('task', should_stop=stop.is_set)
        self.assertEqual((result.status, result.answer, result.error), ('cancelled', '', None))

    def test_default_allows_progress_beyond_eight_requests(self):
        model = ScriptedModel(*(call(a=i, b=1) for i in range(12)), final())
        agent = Agent(model, [self.tool])
        result = agent.run('keep making progress')
        self.assertEqual(agent.max_steps, 80)
        self.assertEqual((result.status, result.steps), ('completed', 13))
        self.assertEqual(len(self.executed), 12)

    def test_default_limit_stops_before_request_eighty_one(self):
        model = ScriptedModel(*(call(a=i, b=1) for i in range(80)), final())
        result = Agent(model, [self.tool]).run('long task')
        self.assertEqual((result.status, result.steps), ('step_limit', 80))
        self.assertEqual(len(model.calls), 80)
        self.assertEqual(len(self.executed), 80)

    def test_unlimited_count_can_finish_after_eighty_requests(self):
        model = ScriptedModel(*(call(a=i, b=1) for i in range(81)), final())
        result = Agent(model, [self.tool], max_steps=None).run('long task')
        self.assertEqual((result.status, result.steps), ('completed', 82))

    def test_invalid_step_limits_are_rejected(self):
        for limit in (0, -1, True, False, 1.5, '80'):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                Agent(ScriptedModel(), max_steps=limit)

    def test_consecutive_failed_rounds_stall_and_complete_native_results(self):
        model = ScriptedModel(*(call('missing', value=i) for i in range(8)), final())
        agent = Agent(model, [self.tool], max_steps=800)
        result = agent.run('unrecoverable task')
        self.assertEqual((result.status, result.steps, result.answer), ('stalled', 8, ''))
        self.assertIn('8', result.error)
        self.assertEqual(agent.history[-1]['role'], 'tool')
        self.assertEqual(json.loads(agent.history[-1]['content'])['error']['code'], 'unknown_tool')
        self.assertEqual(len(model.calls), 8)

    def test_identical_successful_rounds_stall_without_counting_call_ids(self):
        responses = []
        for i in range(8):
            args = '{"a":1,"b":2}' if i % 2 else '{ "b": 2, "a": 1 }'
            response = calls(('add', args))
            response.tool_calls[0].id = str(i)
            response.message['tool_calls'][0]['id'] = str(i)
            responses.append(response)
        model = ScriptedModel(*responses, final())
        agent = Agent(model, [self.tool], max_steps=800)
        result = agent.run('stuck loop')
        self.assertEqual((result.status, result.steps), ('stalled', 8))
        self.assertEqual(len(self.executed), 8)
        self.assertEqual(agent.history[-1]['tool_call_id'], '7')

    def test_changing_results_do_not_trigger_repeated_round_guard(self):
        counter = iter(range(12))
        tool = Tool('add', 'Read changing data.', AddArgs, lambda a, b: next(counter), read_only=True)
        model = ScriptedModel(*(call(a=1, b=2) for _ in range(12)), final())
        result = Agent(model, [tool], max_steps=80).run('watch changing results')
        self.assertEqual((result.status, result.steps), ('completed', 13))

    def test_successful_round_resets_failure_guard(self):
        responses = ([call('missing', value=i) for i in range(7)] + [call(a=1, b=2)] +
                     [call('missing', value=i) for i in range(7)] + [final()])
        result = Agent(ScriptedModel(*responses), [self.tool]).run('recover then retry')
        self.assertEqual((result.status, result.steps), ('completed', 16))

    def test_stall_counters_reset_for_next_user_turn(self):
        model = ScriptedModel(*(call(a=1, b=2) for _ in range(8)), call(a=1, b=2), final())
        agent = Agent(model, [self.tool])
        self.assertEqual(agent.run('first').status, 'stalled')
        self.assertEqual(agent.run('try once more').status, 'completed')

    def test_dynamic_tool_created_during_run_is_available_next_request(self):
        saved = []
        def create(a, b):
            saved.append(Tool('generated_add', 'Created during run.', AddArgs,
                              lambda a, b: a + b, read_only=True))
            return {'created': 'generated_add'}
        create_tool = Tool('create', 'Create a tool.', AddArgs, create, read_only=True)
        model = ScriptedModel(call('create', a=0, b=0), call('generated_add', a=2, b=5), final('7'))
        agent = Agent(model, [create_tool], dynamic_tools=lambda: list(saved))
        result = agent.run('create and use')
        self.assertEqual((result.status, result.answer), ('completed', '7'))
        self.assertNotIn('generated_add', {t['name'] for t in model.definitions[0]})
        self.assertIn('generated_add', {t['name'] for t in model.definitions[1]})
        self.assertEqual(json.loads(model.calls[2][-1]['content'])['result'], 7)

    def test_dynamic_refresh_replaces_and_removes_old_tools(self):
        version1 = Tool('generated_add', 'Version 1.', AddArgs, lambda a, b: a + b, read_only=True)
        version2 = Tool('generated_add', 'Version 2.', AddArgs, lambda a, b: a * b, read_only=True)
        snapshots = iter([[version1], [version2], [], []])
        model = ScriptedModel(call('generated_add', a=2, b=3), call('generated_add', a=2, b=3),
                              call('generated_add', a=2, b=3), final())
        result = Agent(model, [self.tool], dynamic_tools=lambda: next(snapshots)).run('refresh')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(json.loads(model.calls[1][-1]['content'])['result'], 5)
        self.assertEqual(json.loads(model.calls[2][-1]['content'])['result'], 6)
        self.assertEqual(json.loads(model.calls[3][-1]['content'])['error']['code'], 'unknown_tool')
        self.assertNotIn('generated_add', {t['name'] for t in model.definitions[2]})

    def test_dynamic_tool_cannot_override_static_tool(self):
        conflicting = Tool('add', 'Overrides built-in.', AddArgs, lambda a, b: -1, read_only=True)
        model = ScriptedModel(final())
        agent = Agent(model, [self.tool], dynamic_tools=lambda: [conflicting])
        result = agent.run('protected registry')
        self.assertEqual(result.status, 'model_error')
        self.assertIs(agent.tools['add'], self.tool)
        self.assertEqual(model.calls, [])

    def test_stop_context_is_visible_during_tool_and_cleared_afterward(self):
        self.assertTrue(hasattr(tool_runtime, 'execution_stopped'))
        stop, observed = threading.Event(), []
        def handler(a, b):
            observed.append(tool_runtime.execution_stopped())
            stop.set()
            observed.append(tool_runtime.execution_stopped())
            return a + b
        tool = Tool('add', 'Stop-aware tool.', AddArgs, handler, read_only=True)
        agent = Agent(ScriptedModel(call(a=1, b=2)), [tool])
        self.assertEqual(agent.run('stop inside tool', should_stop=stop.is_set).status, 'cancelled')
        self.assertEqual(observed, [False, True])
        self.assertFalse(tool_runtime.execution_stopped())

    def test_stop_context_restores_nested_state_after_exception(self):
        self.assertTrue(hasattr(tool_runtime, 'tool_execution_context'))
        with tool_runtime.tool_execution_context(lambda: True):
            try:
                with tool_runtime.tool_execution_context(lambda: False):
                    self.assertFalse(tool_runtime.execution_stopped())
                    raise ValueError('leave nested context')
            except ValueError:
                pass
            self.assertTrue(tool_runtime.execution_stopped())
        self.assertFalse(tool_runtime.execution_stopped())


if __name__ == '__main__':
    unittest.main()
