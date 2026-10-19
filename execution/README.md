# Execution baseline

The execution baseline is deliberately shared by all candidates.  The first
version contains deterministic arithmetic, workspace-file and HTTP tools plus
one Playwright/Chromium smoke task.  Candidate adapters call these helpers
through their own execution bridge; they do not provide separate tool
implementations for the comparison run.
