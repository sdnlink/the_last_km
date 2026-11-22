"""Read the historical handoff linked to a migrated console task."""

from auto_moniter.directions import Directions


def read(run):
    return Directions(run).read('migration-context.json', 2 * 1024 * 1024)


def planning_context(run, history):
    if not history:
        return ''
    return (
        '\n\nThis task continues earlier work. Plan only outstanding implementation and current acceptance. '
        'Read the historical handoff at ' + str(run / 'migration-context.json') + '. '
        'The saved accepted assignments and modules describe prior progress. Preserve their behavior and '
        'use verification-only work where the current source requires no change. '
        'Review outstanding assignments and issues against the current source. Treat historical review notes '
        'as evidence to investigate, not authorization to expand the user scope. The new task uses its own '
        'audit IDs and must verify its current candidate before delivery.\n'
    )
