"""Golden Production Replay: a measurement system, not a test suite.

The pytest suite answers "does the code still do what it did". This package
answers a different question -- "does the pipeline still give real customers
the right answer" -- and the two are not the same: 4,405 passing tests coexisted
with an inquiry whose answer was in the store and never reached the customer.

Entry point: ``scripts/run_golden_replay.py``.
"""
