from importlib import import_module


def test_package_imports() -> None:
    package = import_module("browser_use_mcp")

    assert package.__name__ == "browser_use_mcp"
