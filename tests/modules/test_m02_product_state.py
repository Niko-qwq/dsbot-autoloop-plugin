import pytest

from core.product_state import ProductError, ProductForbidden, ProductStore, authorize, bind_product, register_version


def sample(number, chief):
    return bind_product({"id": number, "visibility": "private"}, title="工厂", actor=chief,
                        pm=f"pm-{number}", usage=f"usage-{number}", budget=10)


def test_restart_preserves_independent_roles_and_version_roots(tmp_path):
    store = ProductStore(tmp_path)
    state = store.read()
    store.put(state, sample(1, "chief-1"))
    store.put(state, sample(2, "chief-2"))
    first = store.product(ProductStore(tmp_path).read(), 1)
    authorize(first, "pm-1", "pm")
    with pytest.raises(ProductForbidden):
        authorize(first, "pm-2", "pm")
    assert store.workspace(1, "v0.1.0") != store.workspace(2, "v0.1.0")
    with pytest.raises(ProductError):
        store.workspace(1, "../../outside")


def test_next_version_requires_released_baseline_and_accepted_issue():
    product = sample(1, "chief")
    original = register_version(product, "v0.1.0", None, [])
    with pytest.raises(ProductError):
        register_version(product, "v0.2.0", "v0.1.0", [17])
    original["released"] = True
    product["issues"]["17"] = {"triage": "accepted"}
    following = register_version(product, "v0.2.0", "v0.1.0", [17])
    assert following["base"] == "v0.1.0"
    assert register_version(product, "v0.2.0", "v0.1.0", [17]) is following
