from quartermaster import export_logs


def test_export_copies_completed_logs_and_recon_files(tmp_path):
    root = tmp_path / "quartermaster"
    logs = root / "logs"
    recon = root / "recon"
    logs.mkdir(parents=True)
    recon.mkdir()
    (logs / "quartermaster_20261124-120000.wigle.csv").write_text("csv\n")
    (logs / "quartermaster_20261124-120000.summary.csv").write_text("summary\n")
    (logs / ".quartermaster_20261124-120100.wigle.csv.part").write_text("partial\n")
    (logs / ".summary.csv.tmp").write_text("temporary\n")
    (recon / "recon_20261124-120000.jsonl").write_text("{}\n")

    destination = tmp_path / "usb" / "quartermaster-export"
    copied = export_logs.export(root, destination)

    assert {path.relative_to(destination).as_posix() for path in copied} == {
        "logs/quartermaster_20261124-120000.wigle.csv",
        "logs/quartermaster_20261124-120000.summary.csv",
        "recon/recon_20261124-120000.jsonl",
    }
    assert (destination / "logs/quartermaster_20261124-120000.wigle.csv").read_text() == "csv\n"
    assert not list(destination.rglob("*.part"))


def test_export_refuses_destination_inside_data_root(tmp_path):
    root = tmp_path / "quartermaster"
    root.mkdir()

    try:
        export_logs.export(root, root / "export")
    except ValueError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("internal destination should be rejected")
