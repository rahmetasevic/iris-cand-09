from __future__ import annotations

import pytest

from iris_pilot.migrate import MigrationError, checksum, discover


def test_packaged_migrations_are_contiguous_and_ordered() -> None:
    migrations = discover()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "extensions_and_schema"


def test_checksum_ignores_line_endings() -> None:
    assert checksum("SELECT 1;\nSELECT 2;\n") == checksum("SELECT 1;\r\nSELECT 2;\r\n")
    assert checksum("SELECT 1;") != checksum("SELECT 2;")


def test_order_comes_from_versions_not_listing_order() -> None:
    files = [("0002_b.sql", "SELECT 2;"), ("0001_a.sql", "SELECT 1;")]
    assert [m.label for m in discover(files)] == ["0001_a", "0002_b"]


@pytest.mark.parametrize("files,message", [
    ([("0001_a.sql", ""), ("0003_c.sql", "")], "contiguous"),
    ([("0002_a.sql", "")], "contiguous"),
    ([("0001_a.sql", ""), ("0001_b.sql", "")], "duplicate"),
    ([("1_a.sql", "")], "does not match"),
    ([("0001_Add-Table.sql", "")], "does not match"),
])
def test_invalid_migration_sets(files, message) -> None:
    with pytest.raises(MigrationError, match=message):
        discover(files)
