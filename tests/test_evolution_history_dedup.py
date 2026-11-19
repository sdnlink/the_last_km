"""Keep failed issues deduplicated beyond the recent-attempt summary."""

import unittest
from unittest.mock import patch

from tests import test_evolution as evolution_fixtures


class EvolutionHistoryDedupTests(unittest.TestCase):
    def test_older_failure_on_current_base_is_not_selected_again(self):
        fixture = evolution_fixtures.EvolutionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        engine = fixture.engine
        issue = engine.run(plan_only=True)['issue']
        base = engine._identity()

        for index in range(129):
            directory = engine.directory / f'20261119T120000-{index:08x}'
            directory.mkdir(parents=True)
            engine.path = directory / 'evolution.json'
            engine.record = {
                'source': str(engine.source),
                'base': base,
                'issue': issue if index == 0 else {**issue, 'id': f'other-{index}'},
                'workspace': str(directory / 'workspace'),
            }
            engine._save('failed')

        plan = engine.run(plan_only=True)
        self.assertEqual(plan['phase'], 'planned')
        self.assertEqual(plan['issue']['kind'], 'proactive_review')
        self.assertNotEqual(plan['issue']['id'], issue['id'])
        self.assertEqual(len(plan['issue']['previous_attempts']), 8)
        with patch.object(engine, '_run', return_value={'phase': 'no_change'}) as dispatch:
            engine.run()
        dispatch.assert_called_once_with(plan['issue'], base)
