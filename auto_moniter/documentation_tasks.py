"""Instructions for document analysis and editing, independent of source functions."""


def instructions(role):
    common = (
        '\nDocumentation task: deliver the document analysis or edits requested by the person. '
        'Source-code functions, function assignments, regression check IDs and failing-then-passing '
        'behavioral tests are not prerequisites for this task. '
        'If the request asks only for analysis, inspection or recommendations, preserve the inspected documents '
        'and source; create a separate analysis report with exact file/section references and proposed corrections. '
        'Do not implement the recommendations or develop a checker unless requested. '
        'For requested document edits, limit edits to the requested document scope. '
        'Map the original scope and acceptance criteria to the actual document content, cite the applicable '
        'clauses and distinguish verified facts, gaps, inferences and unavailable sources. '
        'A finding that a document or product is incomplete/noncompliant is a result of the analysis; '
        'it is not itself a reason to block delivery or demand an implementation fix outside the request. '
        'For a completed report or module, record product defects and verification limits in the document and summarize them in '
        'done or summary. Reserve gaps, blockers and next_actions for work still needed to finish the requested report or module. '
        'Distinguish evidence for an individual requirement from coverage of all applicable requirements. '
        'Do not claim product compliance, runtime verification or certification from document review alone. '
        'Preserve fixed checks: any supplied commands must still pass, but unrelated tests and git diff --check '
        'do not establish the accuracy or completeness of the document analysis. '
        'When no relevant executable check is supplied, independently review the delivered document/report '
        'against the requested scope and cited inputs; do not request operator input merely to add check IDs '
        'or a source function. Genuine missing access, required sources or external decisions remain blockers. '
    )
    if role == 'requirements':
        return common + (
            'This stage is strictly read-only. Define the report/document deliverable and its review criteria, '
            'then return ready so the worker can carry out the analysis. No analysis report has been delivered yet.\n'
        )
    if role in ('worker', 'report_correction'):
        reporting = (
            'When several modules share one report, a report edit may make a previously reviewed prerequisite pending again. '
            'Recheck that prerequisite’s existing sections and cite them. If their evidence still holds, include the prerequisite '
            'and the newly completed module in the same completion report for joint independent review. A pending ledger status '
            'alone does not require another edit or prevent submitting both modules. '
        )
        if role == 'report_correction':
            return ('Report correction keeps the saved report unchanged. Product defects and faithfully recorded verification limits '
                    'belong in summary or done. Reserve gaps, blockers and next_actions for missing analysis. ' + reporting)
        return common + reporting + (
            'Create a substantive report in an allowed project document location and cite its path in evidence. '
            'Use a new Markdown report when the original inputs must remain unchanged. '
            'If the user specified an output location, use it. Include findings, supporting references, '
            'proposed corrections, scope coverage and limitations. '
            'Return done when this requested deliverable is ready for independent review, even if the '
            'analysis identifies unresolved product/document defects outside the requested implementation scope.\n'
        )
    return common + (
        'Remain strictly read-only. Read the delivered report and its cited inputs; check scope coverage, '
        'claim accuracy and the distinction between document evidence and actual product behavior. '
        'Reject missing or unsupported analysis. Do not require source changes, a clean bill of compliance '
        'or unrelated product repairs as a condition for accepting an accurate analysis report.\n'
    )
