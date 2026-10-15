"""Input configuration and preflight accounting for larger revision requests."""
import os
import unittest
from unittest.mock import Mock, patch
from langchain_core.messages import AIMessage, HumanMessage
from runtime.context import Limits, RunContext, activate, invoke_chat_model, ExecutionError


class InputLimitTests(unittest.TestCase):
    def test_default_and_empty_environment_use_48000_characters(self):
        self.assertEqual(Limits().input_chars, 48000)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Limits.from_env().input_chars, 48000)
        with patch.dict(os.environ, {'INCIDENT_MAX_INPUT_CHARS': ''}):
            self.assertEqual(Limits.from_env().input_chars, 48000)

    def test_explicit_environment_override_is_bounded_and_invalid_value_is_rejected(self):
        for value, expected in [('64000', 64000), ('200', 4000), ('999999', 128000)]:
            with self.subTest(value=value), patch.dict(os.environ, {'INCIDENT_MAX_INPUT_CHARS': value}):
                self.assertEqual(Limits.from_env().input_chars, expected)
        with patch.dict(os.environ, {'INCIDENT_MAX_INPUT_CHARS': 'invalid'}):
            with self.assertRaises(ValueError):
                Limits.from_env()

    def test_previous_35844_character_revision_can_reach_model_and_is_accounted_once(self):
        context = RunContext(limits=Limits())
        model = Mock()
        response = AIMessage(content='{}')
        model.invoke.return_value = response
        with activate(context):
            self.assertIs(invoke_chat_model(model, [HumanMessage(content='x' * 35844)], 'diagnosis'), response)
        model.invoke.assert_called_once()
        self.assertEqual(context.counts['model_calls'], 1)

    def test_request_above_new_limit_is_rejected_before_paid_attempt(self):
        events = []
        context = RunContext(limits=Limits(), emit=events.append)
        model = Mock()
        with activate(context), self.assertRaises(ExecutionError) as caught:
            invoke_chat_model(model, [HumanMessage(content='x' * 48001)], 'diagnosis')
        self.assertEqual(caught.exception.code, 'BUDGET_EXCEEDED')
        model.invoke.assert_not_called()
        self.assertEqual(context.counts['model_calls'], 0)
        rejection = next(e for e in events if e['type'] == 'budget_rejected')
        self.assertEqual((rejection['actual'], rejection['limit']), (48001, 48000))


if __name__ == '__main__':
    unittest.main()
