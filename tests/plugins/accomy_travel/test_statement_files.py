"""Where the PDF of a statement is kept: under a name for the user, in a folder nobody can guess, for an hour."""

import asyncio
import os
import time

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from plugins.accomy_travel import ingress, statement_files

REFERENCE = "ACF274KNM28JHV"


def _age(path, seconds: float) -> None:
    """Make the statement's folder look *seconds* old."""
    moment = time.time() - seconds
    os.utime(path.parent, (moment, moment))


def test_a_statement_is_saved_under_a_name_for_the_user_in_a_folder_of_its_own(accomy_home):
    first = statement_files.save(REFERENCE, b"%PDF-first")
    second = statement_files.save(REFERENCE, b"%PDF-second")

    assert first.name == second.name == f"Accomy-statement-{REFERENCE}.pdf"
    assert first.parent != second.parent  # so the one cannot be named from the other, or from the booking code
    assert len(first.parent.name) >= 20
    assert first.is_relative_to(accomy_home / "cache" / "documents")
    assert (first.read_bytes(), second.read_bytes()) == (b"%PDF-first", b"%PDF-second")


def test_the_folder_of_a_statement_is_closed_to_other_accounts_of_the_host(accomy_home):
    saved = statement_files.save(REFERENCE, b"%PDF")

    assert saved.parent.stat().st_mode & 0o077 == 0


def test_saving_a_statement_removes_those_older_than_an_hour_and_keeps_the_younger(accomy_home):
    old = statement_files.save(REFERENCE, b"%PDF-old")
    recent = statement_files.save(REFERENCE, b"%PDF-recent")
    _age(old, statement_files.KEEP_SECONDS + 60)
    _age(recent, statement_files.KEEP_SECONDS - 60)

    newest = statement_files.save(REFERENCE, b"%PDF-newest")

    assert not old.parent.exists()
    assert recent.is_file() and newest.is_file()


def test_a_sweep_removes_old_statements_when_nobody_asks_for_a_new_one(accomy_home):
    old = statement_files.save(REFERENCE, b"%PDF-old")
    recent = statement_files.save(REFERENCE, b"%PDF-recent")
    _age(old, statement_files.KEEP_SECONDS + 60)

    statement_files.sweep()

    assert not old.parent.exists() and recent.is_file()


def test_a_sweep_in_a_home_that_never_held_a_statement_does_nothing(accomy_home):
    statement_files.sweep()

    assert not (accomy_home / "cache").exists()


def test_a_sweep_goes_on_past_something_it_cannot_remove(accomy_home, monkeypatch):
    stuck = statement_files.save(REFERENCE, b"%PDF-stuck")
    old = statement_files.save(REFERENCE, b"%PDF-old")
    _age(stuck, statement_files.KEEP_SECONDS + 60)
    _age(old, statement_files.KEEP_SECONDS + 60)
    real = statement_files.shutil.rmtree

    def rmtree(folder):
        if folder == stuck.parent:
            raise PermissionError("held open")
        real(folder)

    monkeypatch.setattr(statement_files.shutil, "rmtree", rmtree)

    statement_files.sweep()

    assert stuck.is_file() and not old.parent.exists()


def test_every_message_that_comes_in_clears_the_old_statements(accomy_home):
    old = statement_files.save(REFERENCE, b"%PDF-old")
    recent = statement_files.save(REFERENCE, b"%PDF-recent")
    _age(old, statement_files.KEEP_SECONDS + 60)
    source = SessionSource(platform=Platform.TELEGRAM, user_id="42", chat_id="42", user_name="t", chat_type="dm")

    asyncio.run(ingress.gate_inbound(event=MessageEvent(text="hello", message_id="m1", source=source)))

    assert not old.parent.exists() and recent.is_file()
