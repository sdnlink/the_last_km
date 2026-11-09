"""Account only explicit native provider usage reports; never estimate costs."""
import json
import math


class UsageMeter:
    def __init__(self, saved=None):
        self.state = saved or {'tokens': 0, 'cost_usd': 0.0, 'reported_turns': 0, 'unreported_turns': 0}
        if isinstance(self.state, dict):
            self.state.setdefault('unknown_token_turns', self.state.get('unreported_turns', 0))
            self.state.setdefault('unknown_cost_turns', self.state.get('unreported_turns', 0))
        if (not isinstance(self.state, dict) or set(self.state) != {'tokens', 'cost_usd', 'reported_turns', 'unreported_turns', 'unknown_token_turns', 'unknown_cost_turns'}
                or any(type(self.state[k]) is not int or self.state[k] < 0 for k in ('tokens', 'reported_turns', 'unreported_turns', 'unknown_token_turns', 'unknown_cost_turns'))
                or type(self.state['cost_usd']) not in (int, float) or not math.isfinite(self.state['cost_usd']) or self.state['cost_usd'] < 0):
            raise ValueError('Invalid saved provider usage')

    def record(self, output):
        tokens, cost, reported = 0, 0.0, False
        self.last_tokens_reported = self.last_cost_reported = False
        # Only terminal usage records. Assistant/tool text cannot report usage
        # to the watcher through the conclusion schema.
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict) or event.get('type') not in ('turn.completed', 'result', 'end'):
                continue
            usage = event.get('usage')
            if isinstance(usage, dict):
                pairs = [('input_tokens', 'output_tokens'), ('inputTokens', 'outputTokens')]
                for left, right in pairs:
                    if all(type(usage.get(k)) is int and usage[k] >= 0 for k in (left, right)):
                        tokens += usage[left] + usage[right]
                        # Cached input is already part of Codex input_tokens.
                        if event['type'] in ('result', 'end'):
                            tokens += sum(usage.get(k, 0) for k in ('cache_read_input_tokens', 'cache_creation_input_tokens')
                                          if type(usage.get(k, 0)) is int and usage.get(k, 0) >= 0)
                        reported = True
                        self.last_tokens_reported = True
                        break
            dollars = event.get('total_cost_usd')
            if type(dollars) in (int, float) and math.isfinite(dollars) and dollars >= 0:
                cost += dollars
                reported = True
                self.last_cost_reported = True
        self.state['tokens'] += tokens
        self.state['cost_usd'] = round(self.state['cost_usd'] + cost, 8)
        self.state['reported_turns' if reported else 'unreported_turns'] += 1
        self.state['unknown_token_turns'] += int(not self.last_tokens_reported)
        self.state['unknown_cost_turns'] += int(not self.last_cost_reported)
        return reported

    def exhausted(self, config):
        return ((config.get('max_tokens', 0) and self.state['tokens'] >= config['max_tokens'])
                or (config.get('max_cost_usd', 0) and self.state['cost_usd'] >= config['max_cost_usd']))
