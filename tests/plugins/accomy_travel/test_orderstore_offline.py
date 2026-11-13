"""Order store rules that need no database."""

from argparse import Namespace

import pytest

from plugins.accomy_travel.orderstore import cli, db, migrate


def test_the_cli_says_when_no_database_is_configured(monkeypatch, capsys):
    monkeypatch.delenv(db.DSN_ENV, raising=False)

    assert cli.run(Namespace(action="status")) == 1
    assert db.DSN_ENV in capsys.readouterr().err


@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@db.example.com:5432/chatbot",
    "postgresql://u:p@db.example.com:5432/chatbot?sslmode=require",
    "postgresql://u:p@db.example.com:5432/chatbot?sslmode=prefer",
])
def test_a_remote_database_without_a_verified_certificate_is_refused(monkeypatch, capsys, dsn):
    monkeypatch.setenv(db.DSN_ENV, dsn)

    assert cli.run(Namespace(action="status")) == 1
    err = capsys.readouterr().err
    assert "sslmode=verify-full" in err and "u:p" not in err


def test_an_unreachable_database_is_reported_without_the_password(monkeypatch, capsys):
    monkeypatch.setenv(db.DSN_ENV, "postgresql://someone:hunter2secret@127.0.0.1:1/chatbot")

    assert cli.run(Namespace(action="status")) == 1
    err = capsys.readouterr().err
    assert "cannot connect" in err and "hunter2secret" not in err and "Traceback" not in err


def test_the_shipped_migrations_load():
    assert [m.version for m in migrate.load_migrations()] == list(range(1, len(migrate.load_migrations()) + 1))


def test_a_gap_in_the_migration_numbers_is_refused(tmp_path):
    (tmp_path / "0002_2026-11-13_second.sql").write_text("SELECT 1;")
    with pytest.raises(migrate.MigrationError, match="without gaps"):
        migrate.load_migrations(tmp_path)


@pytest.mark.parametrize("statement", ["COMMIT;", "create table t (a int);\nrollback ;", "BEGIN;\nSELECT 1;", "END;"])
def test_a_migration_that_would_end_the_runners_transaction_is_refused(tmp_path, statement):
    (tmp_path / "0001_2026-11-13_first.sql").write_text(f"SELECT 1;\n{statement}\n")
    with pytest.raises(migrate.MigrationError, match="must not contain"):
        migrate.load_migrations(tmp_path)


def test_begin_and_end_inside_a_function_body_are_not_transaction_control(tmp_path):
    (tmp_path / "0001_2026-11-13_first.sql").write_text(
        "CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $$\nBEGIN\n  RETURN 1; -- COMMIT;\nEND $$;\n"
        "COMMENT ON FUNCTION f() IS 'never calls COMMIT; ever';\n")
    assert len(migrate.load_migrations(tmp_path)) == 1
