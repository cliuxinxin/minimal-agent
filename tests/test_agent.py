import copy
import json
import threading
import unittest

from miniagent.agent import Agent
from miniagent.model import ModelReply, ToolCall
from miniagent.tools import Tool, ToolArgs


class ScriptedModel:
    """Deterministic model substitute used only by tests."""
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, messages, tools, system_prompt):
        self.calls.append(copy.deepcopy(messages))
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


if __name__ == '__main__':
    unittest.main()
