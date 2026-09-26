"""Run with QC_ROLE2_ROOT pointing to PR #6/#7 (or the merged repository root)."""

import base64
import copy
import hashlib
import importlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Thread

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.adapters.erp_orders import ErpContract, WorkOrderMapper
from app.adapters.postgres import (
    quality_bases,
    quality_context_versions,
    quality_erp_imports,
    quality_external_streams,
    quality_items,
    quality_outbox,
    quality_state_versions,
)
from app.adapters.production import PostgresProductionJournal
from app.application.production import IdentityDirectory, Principal, ProductionService
from app.domain.errors import IdempotencyConflict, InvalidTransition, PermissionDenied
from tests.erp_support import order_profile

pytestmark = pytest.mark.integration


@pytest.fixture
def role2_root(monkeypatch):
    root = Path(os.environ.get("QC_ROLE2_ROOT", Path(__file__).resolve().parents[3]))
    if not (root / "contracts/external-openapi.yaml").is_file():
        if "QC_ROLE2_ROOT" in os.environ:
            pytest.fail("QC_ROLE2_ROOT does not contain contracts/external-openapi.yaml.")
        pytest.skip(
            "Set QC_ROLE2_ROOT to the reviewed role 2 checkout for actual contract/ERP tests."
        )
    monkeypatch.syspath_prepend(str(root))
    return root


@pytest.fixture
def imported(database, protector, role2_root):
    order, profile = order_profile()
    contract = ErpContract(role2_root / "contracts/external-openapi.yaml")
    mapper = WorkOrderMapper(profile, contract)
    journal = PostgresProductionJournal(database[1], protector)
    registry = {
        hashlib.sha256(b"integration-test").hexdigest(): Principal(
            "integration-actor", frozenset({"integration"})
        ),
        hashlib.sha256(b"controller-test").hexdigest(): Principal(
            "controller-actor", frozenset({"controller"})
        ),
    }
    service = ProductionService(journal, protector, IdentityDirectory(registry))
    return order, mapper, journal, service


def test_real_erp_http_cli_import_and_process_restart(database, imported, role2_root, tmp_path):
    order, mapper, journal, _ = imported
    module = importlib.import_module("emulator.server")
    receiver = module.Receiver(tmp_path / "erp.sqlite", [order])
    with database[1].connect() as connection:
        schema, role = connection.execute(text("SELECT current_schema(), current_user")).one()
    url = database[1].url.update_query_dict(
        {"options": f"-csearch_path={schema},public -crole={role}"}
    )
    (tmp_path / "event.key").write_text(base64.b64encode(os.urandom(32)).decode(), encoding="ascii")
    (tmp_path / "identities.json").write_text(
        json.dumps(
            [
                {
                    "actor_id": "cli-integrator",
                    "roles": ["integration"],
                    "token_sha256": hashlib.sha256(b"integration-test").hexdigest(),
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "profile.json").write_text(json.dumps(mapper.profile), encoding="utf-8")
    (tmp_path / "erp.token").write_text("erp-test", encoding="utf-8")
    (tmp_path / "integration.token").write_text("integration-test", encoding="utf-8")
    environment = {
        **os.environ,
        "DATABASE_URL": url.render_as_string(hide_password=False),
        "QC_EVENT_KEY_FILE": str(tmp_path / "event.key"),
        "QC_EVENT_KEY_ID": "import-test",
        "QC_EVENT_KEY_VERSION": "1",
        "QC_IDENTITIES_FILE": str(tmp_path / "identities.json"),
    }
    with module.make_server(receiver, "erp-test") as erp:
        thread = Thread(target=erp.serve_forever, daemon=True)
        thread.start()
        try:
            command = [
                sys.executable,
                "-m",
                "app.import_erp",
                "--url",
                f"http://127.0.0.1:{erp.server_port}/external/v2/work-orders",
                "--contract",
                str(role2_root / "contracts/external-openapi.yaml"),
                "--profile",
                str(tmp_path / "profile.json"),
                "--erp-token-file",
                str(tmp_path / "erp.token"),
                "--integration-token-file",
                str(tmp_path / "integration.token"),
            ]
            result = subprocess.run(command, env=environment, capture_output=True, timeout=30)
            assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()
            first = json.loads(result.stdout.splitlines()[0])
            assert first["status"] == "accepted"
            states = {item: journal.quality.state(item) for item in first["item_ids"]}
            assert len(states) == 3
            for state in states.values():
                assert state["release_allowed_now"] is False
                assert state["observations"] == []
                assert state["context"]["control_plan_version"] == "1"
                assert state["outbox"][-1]["payload"]["producer_id"] == "efqa-demo"
            result = subprocess.run(command, env=environment, capture_output=True, timeout=30)
            assert result.returncode == 0, result.stderr.decode()
            second = json.loads(result.stdout.splitlines()[0])
            assert second == {**first, "status": "duplicate"}
            assert {item: journal.quality.state(item) for item in states} == states
            with database[1].connect() as connection:
                saved = (
                    connection.execute(
                        select(quality_erp_imports).where(
                            quality_erp_imports.c.message_id == order["message_id"]
                        )
                    )
                    .mappings()
                    .one()
                )
                assert saved["document"] == order
                assert saved["profile"] == mapper.profile
                assert saved["receipt"] == first
        finally:
            erp.shutdown()
            thread.join(timeout=5)


def test_all_items_and_outbox_rollback_when_second_context_fails(database, imported, monkeypatch):
    order, mapper, journal, service = imported
    original = journal.import_context
    calls = []

    def fail_second(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(args[0])
        if len(calls) == 2:
            raise RuntimeError("injected after second item")
        return result

    monkeypatch.setattr(journal, "import_context", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        service.import_work_order("integration-test", order, mapper)
    ids = [part["item_id"] for part in mapper.profile["items"].values()]
    with database[1].connect() as connection:
        for table in (
            quality_items,
            quality_context_versions,
            quality_bases,
            quality_outbox,
            quality_state_versions,
            quality_external_streams,
        ):
            assert connection.execute(select(table).where(table.c.item_id.in_(ids))).first() is None
        assert (
            connection.execute(
                select(quality_erp_imports).where(
                    quality_erp_imports.c.message_id == order["message_id"]
                )
            ).first()
            is None
        )
    monkeypatch.setattr(journal, "import_context", original)
    assert service.import_work_order("integration-test", order, mapper)["status"] == "accepted"


def test_revisions_original_receipt_profile_conflicts_and_current_permissions(imported):
    order, mapper, journal, service = imported
    receipt = service.import_work_order("integration-test", order, mapper)
    with pytest.raises(PermissionDenied):
        service.import_work_order("controller-test", order, mapper)
    with pytest.raises(IdempotencyConflict):
        service.import_work_order("integration-test", {**order, "revision": 2}, mapper)
    with pytest.raises(InvalidTransition):
        service.import_work_order(
            "integration-test", {**order, "message_id": "same-revision"}, mapper
        )
    newer = {**order, "message_id": f"{order['message_id']}-v2", "revision": 2}
    service.import_work_order("integration-test", newer, mapper)
    assert service.import_work_order("integration-test", order, mapper) == {
        **receipt,
        "status": "duplicate",
    }
    with pytest.raises(InvalidTransition):
        service.import_work_order(
            "integration-test", {**order, "message_id": "old-revision"}, mapper
        )
    changed = copy.deepcopy(mapper.profile)
    changed["producer_id"] = "another-producer"
    changed_mapper = WorkOrderMapper(changed, mapper.contract)
    with pytest.raises(IdempotencyConflict):
        service.import_work_order("integration-test", order, changed_mapper)
    with pytest.raises(IdempotencyConflict):
        service.import_work_order(
            "integration-test",
            {**newer, "message_id": "new-version", "revision": 3},
            changed_mapper,
        )
    assert journal.quality.state(receipt["item_ids"][0])["context"]["work_order_version"] == "2"


def test_concurrent_duplicate_has_one_effect(imported):
    order, mapper, journal, service = imported
    barrier = Barrier(2)

    def run():
        barrier.wait(timeout=10)
        return service.import_work_order("integration-test", order, mapper)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(value["status"] for value in results) == ["accepted", "duplicate"]
    assert {**results[0], "status": "accepted"} == {**results[1], "status": "accepted"}
    for item_id in results[0]["item_ids"]:
        assert len(journal.quality.state(item_id)["outbox"]) == 1


def test_erp_import_history_cannot_be_mutated(database, imported):
    order, mapper, _, service = imported
    service.import_work_order("integration-test", order, mapper)
    for sql in (
        "UPDATE quality_erp_imports SET revision=99 WHERE message_id=:message",
        "DELETE FROM quality_erp_imports WHERE message_id=:message",
        "TRUNCATE quality_erp_imports",
    ):
        # Even the migration owner hits the immutable-history trigger unless it is disabled.
        with pytest.raises(DBAPIError), database[0].begin() as connection:
            connection.execute(text(sql), {"message": order["message_id"]})
