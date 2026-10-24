from calc import add


def test_add_positive_values():
    assert add(2, 3) == 5


def test_add_negative_values():
    assert add(-2, -3) == -5
