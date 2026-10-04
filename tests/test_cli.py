"""CLI smoke tests on the sim backend."""

from scopepilot.cli import main

PLAN = "/home/hatch/workspace/astro-capture/examples/night_queue.yaml"
SIM = ["--backend", "sim"]


def test_probe():
    assert main(SIM + ["probe"]) == 0


def test_status():
    assert main(SIM + ["status"]) == 0


def test_goto_name():
    assert main(SIM + ["goto", "M51"]) == 0


def test_goto_radec():
    assert main(SIM + ["goto", "--ra", "10", "--dec", "20"]) == 0


def test_goto_altaz():
    assert main(SIM + ["goto", "--az", "200", "--alt", "50"]) == 0


def test_goto_no_target_is_usage_error():
    assert main(SIM + ["goto"]) == 2


def test_sync():
    assert main(SIM + ["sync", "M51"]) == 0


def test_track():
    assert main(SIM + ["track", "off"]) == 0
    assert main(SIM + ["track", "bogus"]) == 2


def test_jog_timed():
    assert main(SIM + ["jog", "up", "--rate", "9", "--seconds", "0.2"]) == 0


def test_jog_bad_direction():
    assert main(SIM + ["jog", "sideways"]) == 2


def test_stop():
    assert main(SIM + ["stop"]) == 0


def test_park_unpark():
    assert main(SIM + ["park"]) == 0
    assert main(SIM + ["unpark"]) == 0


def test_bus_scan():
    assert main(SIM + ["bus-scan"]) == 0


def test_targets():
    assert main(SIM + ["targets", "M5"]) == 0


def test_queue():
    assert main(SIM + ["queue", "--plan", PLAN]) == 0


def test_queue_missing_plan():
    assert main(SIM + ["queue", "--plan", "/no/such.yaml"]) == 2


def test_set_time_and_location():
    assert main(SIM + ["set-time"]) == 0
    assert main(SIM + ["set-location", "--lat", "43.38", "--lon", "-80.98"]) == 0


def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_align_status_no_model(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    assert main(SIM + ["align", "--status"]) == 0


def test_align_star_then_fit(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    assert main(SIM + ["align", "--star", "Vega"]) == 0
    # second star in a separate process: pending file accumulates, auto-fit
    assert main(SIM + ["align", "--star", "Altair"]) == 0
    assert (tmp_path / ".scopepilot" / "pointing.json").exists()
    assert main(SIM + ["align", "--status"]) == 0


def test_align_unknown_star(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    assert main(SIM + ["align", "--star", "NoSuchStarXYZ"]) == 2


def test_align_fit_without_stars(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    assert main(SIM + ["align", "--fit"]) == 2


def test_align_reuse_and_clear(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    assert main(SIM + ["align", "--star", "Vega"]) == 0
    assert main(SIM + ["align", "--star", "Altair"]) == 0
    assert main(SIM + ["align", "--reuse"]) == 0
    assert main(SIM + ["align", "--clear"]) == 0
    assert main(SIM + ["align", "--reuse"]) == 1


def test_center_without_astrocapture_is_clean_error():
    # astrocapture is not importable here -> clear error, exit 2
    assert main(SIM + ["center", "M51"]) == 2
