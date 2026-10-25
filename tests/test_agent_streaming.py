"""Core streaming behavior, with provider I/O replaced at its public boundary."""

import copy
import json
import threading
import unittest

from miniagent.agent import Agent
from miniagent.model import ModelReply
from tests.test_agent import AddArgs, ScriptedModel, call, final
from miniagent.tools import Tool


class AgentStreamingTests(unittest.TestCase):
    def test_model_failure_preserves_structured_diagnostics_without_raw_payloads(self):
        events = []

        class DetailedFailure(RuntimeError):
            diagnostic = {
                'code': 'invalid_stream_field', 'phase': 'stream_parse',
                'field': 'choices[0].delta.tool_calls[0].function.arguments',
                'expected': 'string or null', 'actual_type': 'array',
                'chunk_index': 3, 'request_id': 'request-123',
                'raw_response': {'content': 'private upstream content'},
                'headers': {'Authorization': 'private credential'},
            }

        class BrokenModel(ScriptedModel):
            def stream(self, messages, tools, system_prompt, *, on_delta, should_stop=None):
                raise DetailedFailure('Invalid streamed tool arguments')

        result = Agent(BrokenModel(), stream=True, on_event=events.append).run('private task')
        self.assertIsNotNone(getattr(result, 'diagnostic', None),
                             'Agent discarded the adapter diagnostics')
        self.assertEqual(result.diagnostic['field'],
                         'choices[0].delta.tool_calls[0].function.arguments')
        self.assertEqual(result.diagnostic['chunk_index'], 3)
        self.assertEqual(result.diagnostic['type'], 'DetailedFailure')
        self.assertEqual(events[-1]['data']['diagnostic'], result.diagnostic)
        encoded = json.dumps(result.diagnostic)
        for private in ('private upstream content', 'private credential', 'private task',
                        'raw_response', 'Authorization'):
            self.assertNotIn(private, encoded)

    def test_stream_delivers_before_reply_and_executes_only_validated_calls(self):
        events, executed = [], []
        tool = Tool('add', 'Add.', AddArgs,
                    lambda a, b: executed.append((a, b)) or a + b, read_only=True)
        owner = self

        class StreamingModel(ScriptedModel):
            def stream(self, messages, tools, system_prompt, *, on_delta, should_stop=None):
                self.calls.append(copy.deepcopy(messages))
                if len(self.calls) == 1:
                    on_delta({'type': 'reasoning_delta', 'text': 'Check the sum.'})
                    on_delta({'type': 'tool_call_delta', 'index': 0, 'id': '0',
                              'name': 'add', 'arguments_delta': '{"a":2,'})
                    owner.assertEqual(executed, [])
                    owner.assertEqual(events[-1]['event'], 'tool_call_delta')
                    on_delta({'type': 'tool_call_delta', 'index': 0, 'id': '0',
                              'name': 'add', 'arguments_delta': '"b":3}'})
                    return call(a=2, b=3)
                owner.assertEqual(json.loads(messages[-1]['content'])['result'], 5)
                on_delta({'type': 'text_delta', 'text': 'The answer is '})
                owner.assertEqual(events[-1]['data']['text'], 'The answer is ')
                on_delta({'type': 'text_delta', 'text': '5.'})
                return final('The answer is 5.')

        result = Agent(StreamingModel(), [tool], stream=True, on_event=events.append).run('2+3')
        self.assertEqual((result.status, result.answer, result.tool_calls), ('completed', 'The answer is 5.', 1))
        self.assertEqual(executed, [(2, 3)])
        self.assertEqual(''.join(e['data']['text'] for e in events if e['event'] == 'text_delta'),
                         'The answer is 5.')
        complete = [e for e in events if e['event'] == 'model_complete']
        self.assertEqual([e['data']['has_tool_calls'] for e in complete], [True, False])
        self.assertLess(events.index(complete[0]),
                        next(i for i, e in enumerate(events) if e['event'] == 'tool_call'))

    def test_cancelled_partial_stream_never_commits_native_call_or_executes_it(self):
        stop, events, executed = threading.Event(), [], []
        tool = Tool('add', 'Add.', AddArgs,
                    lambda a, b: executed.append((a, b)), read_only=True)

        class StoppingModel(ScriptedModel):
            first = True

            def stream(self, messages, tools, system_prompt, *, on_delta, should_stop=None):
                if not self.first:
                    return final('continued')
                self.first = False
                on_delta({'type': 'text_delta', 'text': 'Partial answer'})
                on_delta({'type': 'tool_call_delta', 'index': 0, 'id': 'pending',
                          'name': 'add', 'arguments_delta': '{"a":'})
                stop.set()
                if should_stop():
                    raise RuntimeError('stream interrupted')

        agent = Agent(StoppingModel(), [tool], stream=True, on_event=events.append)
        result = agent.run('start', should_stop=stop.is_set)
        self.assertEqual((result.status, result.error, result.tool_calls), ('cancelled', None, 0))
        self.assertEqual(executed, [])
        self.assertEqual(agent.history, [{'role': 'user', 'content': 'start'}])
        self.assertFalse(any(e['event'] == 'model_complete' for e in events))
        self.assertTrue(any(e['data'].get('text') == 'Partial answer' for e in events))
        stop.clear()
        self.assertEqual(agent.run('continue', should_stop=stop.is_set).answer, 'continued')

    def test_incomplete_stream_is_an_error_even_after_visible_text(self):
        events = []

        class BrokenModel(ScriptedModel):
            def stream(self, messages, tools, system_prompt, *, on_delta, should_stop=None):
                on_delta({'type': 'text_delta', 'text': 'Not finished'})
                raise RuntimeError('Missing stream terminator')

        agent = Agent(BrokenModel(), stream=True, on_event=events.append)
        result = agent.run('task')
        self.assertEqual((result.status, result.answer, result.tool_calls), ('model_error', '', 0))
        self.assertEqual(len(agent.history), 1)
        self.assertFalse(any(e['event'] == 'model_complete' for e in events))

    def test_nonstream_model_fallback_emits_only_actual_public_content(self):
        events = []
        message = {'role': 'assistant', 'content': [
            {'type': 'thinking', 'thinking': 'A public summary.', 'signature': 'opaque-signature'},
            {'type': 'redacted_thinking', 'data': 'encrypted-private-data'},
            {'type': 'text', 'text': 'Done.'},
        ]}
        agent = Agent(ScriptedModel(ModelReply('Done.', [], message)), stream=True,
                      on_event=events.append)
        self.assertEqual(agent.run('task').answer, 'Done.')
        visible = [e for e in events if e['event'] in ('text_delta', 'reasoning_delta')]
        self.assertEqual([(e['event'], e['data']['text']) for e in visible],
                         [('reasoning_delta', 'A public summary.'), ('text_delta', 'Done.')])
        self.assertNotIn('opaque-signature', json.dumps(visible))
        self.assertNotIn('encrypted-private-data', json.dumps(visible))
        self.assertEqual(agent.history[-1], message)

    def test_compatible_reasoning_is_emitted_once_and_retained(self):
        events = []
        message = {'role': 'assistant', 'content': 'Answer.', 'reasoning_content': 'Provider reasoning.'}
        agent = Agent(ScriptedModel(ModelReply('Answer.', [], message)), stream=True,
                      on_event=events.append)
        agent.run('task')
        self.assertEqual([e['data']['text'] for e in events if e['event'] == 'reasoning_delta'],
                         ['Provider reasoning.'])
        self.assertEqual([e['data']['text'] for e in events if e['event'] == 'text_delta'], ['Answer.'])
        self.assertEqual(agent.history[-1]['reasoning_content'], 'Provider reasoning.')

    def test_unknown_delta_fields_do_not_enter_visible_events(self):
        events = []

        class NoisyModel(ScriptedModel):
            def stream(self, messages, tools, system_prompt, *, on_delta, should_stop=None):
                on_delta({'type': 'signature_delta', 'text': 'opaque'})
                on_delta({'type': 'text_delta', 'text': 'Hello.', 'signature': 'opaque'})
                on_delta({'type': 'reasoning_delta', 'text': {'hidden': 'opaque'}})
                return final('Hello.')

        Agent(NoisyModel(), stream=True, on_event=events.append).run('task')
        deltas = [e for e in events if e['event'].endswith('_delta')]
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]['data'], {'text': 'Hello.'})

    def test_existing_callers_keep_nonstream_behavior(self):
        class BothModel(ScriptedModel):
            def stream(self, *args, **kwargs):
                raise AssertionError('Streaming must be explicitly selected.')

        events = []
        result = Agent(BothModel(final()), on_event=events.append).run('task')
        self.assertEqual(result.status, 'completed')
        self.assertFalse(any(e['event'].endswith('_delta') for e in events))


if __name__ == '__main__':
    unittest.main()
