from importlib.util import find_spec

import level2_service.api as api


def test_backend_api_module_is_available() -> None:
    """A missing public backend module would leave every API consumer unusable."""
    assert find_spec("level2_service.api") is not None


def test_backend_exposes_an_application_factory() -> None:
    """Removing the factory would prevent isolated app configuration in deployments."""
    assert callable(getattr(api, "create_app", None))
