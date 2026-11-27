def test_deliberate_failure():
    """What: deliberately fails. Use when: proving CI goes red. Inputs: none. Output: assert. Side effects: none. Role: throwaway."""
    assert 1 == 2
