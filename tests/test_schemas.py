from wdvi.schemas import TimeMap


def test_timemap_roundtrip():
    tm = TimeMap(segments=[(1.0, 3.0), (5.0, 6.5)])
    assert tm.duration == 3.5
    assert tm.to_output(2.0) == 1.0
    assert tm.to_output(4.0) is None
    assert tm.to_output(5.5) == 2.5
    assert tm.to_source(2.5) == 5.5


def test_settings_fixture(settings, sample_video):
    assert settings.config.paths.work_dir.exists()
    assert sample_video.exists()
